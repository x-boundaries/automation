Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Set-EgFixturePythonEnvironment {
    param(
        [Parameter(Mandatory = $true)][string]$ModulePath,
        [Parameter(Mandatory = $true)][string]$ExpectedSha256
    )

    $module = [System.IO.Path]::GetFullPath($ModulePath)
    if (-not (Test-Path -LiteralPath $module -PathType Leaf)) {
        throw 'EG_FIXTURE_MODULE_MISSING'
    }
    $actualSha256 = (Get-FileHash -LiteralPath $module -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualSha256 -cne $ExpectedSha256.ToLowerInvariant()) {
        throw 'EG_FIXTURE_MODULE_HASH_MISMATCH'
    }

    $fixtureRoot = [System.IO.Path]::GetDirectoryName($module)
    $env:PYTHONPATH = $fixtureRoot
    $env:PYTHONDONTWRITEBYTECODE = '1'
    $env:PYTHONNOUSERSITE = '1'
    [System.Environment]::SetEnvironmentVariable('PYTHONHOME', $null, 'Process')
    [System.Environment]::SetEnvironmentVariable('PYTHONUSERBASE', $null, 'Process')
    [System.Environment]::SetEnvironmentVariable('PYTHONSTARTUP', $null, 'Process')
    [System.Environment]::SetEnvironmentVariable('PYTHONINSPECT', $null, 'Process')
    $env:EG_TEST_MODULE_PATH = $module
    $env:EG_TEST_MODULE_SHA256 = $actualSha256
}

function ConvertTo-EgFixtureArguments {
    param([Parameter(Mandatory = $true)][string[]]$Items)
    $quoted = New-Object 'System.Collections.Generic.List[string]'
    foreach ($item in $Items) {
        if ($item.IndexOfAny([char[]]@(0, 10, 13)) -ge 0) {
            throw 'EG_FIXTURE_ARGUMENT_INVALID'
        }
        $slashes = 0
        $builder = New-Object System.Text.StringBuilder
        [void]$builder.Append('"')
        foreach ($character in $item.ToCharArray()) {
            if ($character -eq '\') {
                $slashes++
                continue
            }
            if ($character -eq '"') {
                [void]$builder.Append(('\' * (2 * $slashes + 1)))
                [void]$builder.Append('"')
                $slashes = 0
                continue
            }
            if ($slashes -gt 0) {
                [void]$builder.Append(('\' * $slashes))
                $slashes = 0
            }
            [void]$builder.Append($character)
        }
        if ($slashes -gt 0) {
            [void]$builder.Append(('\' * (2 * $slashes)))
        }
        [void]$builder.Append('"')
        $quoted.Add($builder.ToString())
    }
    return [string]::Join(' ', $quoted)
}

function Start-EgFixturePython {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet('run', 'list', 'noise-child', 'tree-child', 'saturate', 'crash-child',
            'wrong-parent-child', 'handle-canary')]
        [string]$Operation,
        [Parameter(Mandatory = $true)][string]$PythonExe,
        [string]$ConfigPath,
        [long]$CanaryHandle = 0,
        [uint64]$CanaryVolume = 0,
        [uint64]$CanaryFileIndex = 0
    )

    if ($Operation -ceq 'handle-canary') {
        if ($CanaryHandle -le 0) { throw 'EG_FIXTURE_HANDLE_INVALID' }
        $items = @('-m', 'energygrid_bill_downloader', 'handle-canary',
            '--handle', [string]$CanaryHandle, '--volume', [string]$CanaryVolume,
            '--file-index', [string]$CanaryFileIndex)
    }
    else {
        if ([string]::IsNullOrWhiteSpace($ConfigPath) -or
            -not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
            throw 'EG_FIXTURE_CONFIG_MISSING'
        }
        $items = @('-m', 'energygrid_bill_downloader', $Operation, '--config', $ConfigPath)
    }
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = [System.IO.Path]::GetFullPath($PythonExe)
    $startInfo.Arguments = ConvertTo-EgFixtureArguments -Items $items
    $startInfo.WorkingDirectory = [System.IO.Path]::GetFullPath((Get-Location).Path)
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    if (-not $process.Start()) {
        $process.Dispose()
        throw 'EG_FIXTURE_CHILD_START_FAILED'
    }
    return ,$process
}

function Invoke-EgFixtureApplication {
    param(
        [Parameter(Mandatory = $true)][string]$PythonExe,
        [Parameter(Mandatory = $true)][string]$ConfigPath,
        [Parameter(Mandatory = $true)][ValidateSet('positive', 'noise', 'wrongcmd', 'wrong-image', 'early',
            'wrongparent', 'launcher-exit', 'quick-exit', 'deadchild', 'idle')][string]$Mode
    )

    if ($Mode -ceq 'early') { return 70 }
    if ($Mode -ceq 'idle') {
        Start-Sleep -Seconds 60
        return 0
    }
    if ($Mode -ceq 'launcher-exit') {
        $application = Start-EgFixturePython -Operation 'list' -PythonExe $PythonExe -ConfigPath $ConfigPath
        $application.Dispose()
        return 70
    }

    $owned = New-Object 'System.Collections.Generic.List[System.Diagnostics.Process]'
    try {
        if ($Mode -ceq 'noise') {
            for ($index = 0; $index -lt 10; $index++) {
                $noise = Start-EgFixturePython -Operation 'noise-child' -PythonExe $PythonExe -ConfigPath $ConfigPath
                $owned.Add($noise)
            }
            $noiseConfig = ConvertFrom-Json -InputObject ([System.IO.File]::ReadAllText($ConfigPath))
            $noiseStartedPathProperty = $noiseConfig.PSObject.Properties['noise_started_path']
            if ($null -ne $noiseStartedPathProperty -and
                -not [string]::IsNullOrWhiteSpace([string]$noiseStartedPathProperty.Value)) {
                [System.IO.File]::WriteAllText(
                    [string]$noiseStartedPathProperty.Value, '{"status":"started"}')
            }
            foreach ($noise in $owned) {
                if (-not $noise.WaitForExit(30000)) { throw 'EG_FIXTURE_NOISE_CHILD_TIMEOUT' }
                if ($noise.ExitCode -ne 0) { throw 'EG_FIXTURE_NOISE_CHILD_FAILED' }
            }
            foreach ($noise in $owned) { $noise.Dispose() }
            $owned.Clear()
        }

        $operation = 'run'
        if ($Mode -ceq 'wrongcmd') { $operation = 'list' }
        if ($Mode -ceq 'deadchild') { $operation = 'list' }
        if ($Mode -ceq 'wrongparent') { $operation = 'wrong-parent-child' }
        if ($Mode -ceq 'wrong-image') {
            $wrongImageConfig = ConvertFrom-Json -InputObject ([System.IO.File]::ReadAllText($ConfigPath))
            $canonicalExecutable = [string]$wrongImageConfig.canonical_command_line_executable
            if ([string]::IsNullOrWhiteSpace($canonicalExecutable)) {
                throw 'EG_FIXTURE_WRONG_IMAGE_CANONICAL_EXECUTABLE_MISSING'
            }
            $canonicalCommandLine = ConvertTo-EgFixtureArguments -Items @(
                [System.IO.Path]::GetFullPath($canonicalExecutable), '-m',
                'energygrid_bill_downloader', 'run', '--config', $ConfigPath)
            $processId = [EnergyGridWrongImageProcessControl]::Start(
                [System.IO.Path]::GetFullPath($PythonExe),
                $canonicalCommandLine,
                [System.IO.Path]::GetFullPath((Get-Location).Path))
            $application = [System.Diagnostics.Process]::GetProcessById([int]$processId)
        }
        else {
            $application = Start-EgFixturePython -Operation $operation -PythonExe $PythonExe -ConfigPath $ConfigPath
        }
        $owned.Add($application)
        if ($Mode -ceq 'deadchild') {
            if (-not $application.WaitForExit(15000)) { throw 'EG_FIXTURE_DEAD_CHILD_TIMEOUT' }
            Start-Sleep -Seconds 60
            return [int]$application.ExitCode
        }
        if ($Mode -ceq 'quick-exit') {
            if (-not $application.WaitForExit(15000)) { throw 'EG_FIXTURE_QUICK_CHILD_TIMEOUT' }
        }
        else {
            if (-not $application.WaitForExit(65000)) { throw 'EG_FIXTURE_APPLICATION_TIMEOUT' }
        }
        return [int]$application.ExitCode
    }
    finally {
        foreach ($process in $owned) {
            try {
                if (-not $process.HasExited) {
                    $process.Kill()
                    [void]$process.WaitForExit(5000)
                }
            }
            finally {
                $process.Dispose()
            }
        }
    }
}
