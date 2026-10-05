# Literal production definitions; source binding is enforced by the Python test owner.

function Stop-EgSupervisor {
    param(
        [Parameter(Mandatory = $true)][string]$SupportRef,
        [int]$ErrorCode = 0,
        [switch]$ContainmentFailure,
        [switch]$EvidenceFailure
    )

    $script:EgState.support_ref = $SupportRef
    if ($ErrorCode -ne 0) {
        $script:EgState.error_code = [int]$ErrorCode
    }
    $script:EgState.precreate_rejection = -not $script:EgState.creation_attempted
    if ($ContainmentFailure) {
        $script:EgState.containment_failure = $true
    }
    if ($EvidenceFailure) {
        $script:EgState.evidence_integrity_failure = $true
    }
    throw (New-Object System.Exception($SupportRef))
}

function Test-EgUnsafeText {
    param([Parameter(Mandatory = $true)][string]$Value)

    if ($Value.IndexOf([char]0) -ge 0 -or $Value.IndexOf([char]10) -ge 0 -or
        $Value.IndexOf([char]13) -ge 0 -or $Value.IndexOf('"') -ge 0) {
        return $true
    }
    foreach ($character in $Value.ToCharArray()) {
        if ([int][char]$character -lt 32) {
            return $true
        }
    }
    return $false
}

function ConvertTo-EgNativeCommandLine {
    param([Parameter(Mandatory = $true)][AllowEmptyCollection()][string[]]$Argument)

    $builder = New-Object System.Text.StringBuilder
    for ($argumentIndex = 0; $argumentIndex -lt $Argument.Count; $argumentIndex++) {
        if ($argumentIndex -gt 0) { [void]$builder.Append(' ') }
        $item = [string]$Argument[$argumentIndex]
        if (Test-EgUnsafeText -Value $item) {
            Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_SERIALISED_INPUT_INVALID'
        }
        [void]$builder.Append('"')
        $backslashes = 0
        foreach ($character in $item.ToCharArray()) {
            if ($character -eq '\') {
                $backslashes++
                continue
            }
            if ($character -eq '"') {
                [void]$builder.Append(('\' * (2 * $backslashes + 1)))
                [void]$builder.Append('"')
                $backslashes = 0
                continue
            }
            if ($backslashes -gt 0) {
                [void]$builder.Append(('\' * $backslashes))
                $backslashes = 0
            }
            [void]$builder.Append($character)
        }
        if ($backslashes -gt 0) {
            [void]$builder.Append(('\' * (2 * $backslashes)))
        }
        [void]$builder.Append('"')
    }
    if ($builder.Length -gt 32766) {
        Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_COMMAND_LINE_TOO_LONG'
    }
    return $builder.ToString()
}

function ConvertTo-EgUtf8JsonBytes {
    param([Parameter(Mandatory = $true)]$Object)

    $json = $Object | ConvertTo-Json -Depth 12 -Compress
    $text = [string]$json + "`n"
    $encoding = New-Object System.Text.UTF8Encoding($false)
    $bytes = $encoding.GetBytes($text)
    if ($bytes.Length -gt 65536) {
        Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_EVIDENCE_TOO_LARGE' -EvidenceFailure
    }
    return $bytes
}

function Close-EgHandle {
    param([Parameter(Mandatory = $true)][IntPtr]$Handle)

    if ($Handle -eq [IntPtr]::Zero) { return }
    $result = [EnergyGridOneShotSupervisorNative]::CloseHandleChecked($Handle)
    if (-not $result.Success -and -not $script:EgState.containment_failure) {
        $script:EgState.containment_failure = $true
        $script:EgState.support_ref = 'EG_SUPERVISOR_HANDLE_CLOSE_FAILED'
        if ($result.ErrorCode -ne 0) { $script:EgState.error_code = $result.ErrorCode }
    }
}

function Write-EgReservedIntent {
    param(
        [Parameter(Mandatory = $true)][System.IO.FileStream]$Stream,
        [Parameter(Mandatory = $true)][byte[]]$Bytes,
        [Parameter(Mandatory = $true)][int]$TimeoutMilliseconds
    )

    try {
        $handleResult = [EnergyGridOneShotSupervisorNative]::SetHandleInheritance(
            $Stream.SafeFileHandle.DangerousGetHandle(), $false)
        if (-not $handleResult.Success) {
            Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_INTENT_HANDLE_FAILED' `
                -ErrorCode $handleResult.ErrorCode -EvidenceFailure
        }
        $durability = [EnergyGridOneShotSupervisorNative]::WriteFlushBounded(
            $Stream, $Bytes, $TimeoutMilliseconds)
        if ($durability.TimedOut -or -not $durability.Succeeded) {
            Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_INTENT_FLUSH_FAILED' `
                -ErrorCode $durability.ErrorCode -EvidenceFailure
        }
        $script:EgState.intent_bytes = $Bytes
        $script:EgState.intent_committed = $true
    }
    catch {
        if ($_.Exception.Message -like 'EG_SUPERVISOR_*') { throw }
        Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_INTENT_FLUSH_FAILED' -EvidenceFailure
    }
}

function Get-EgOutcomeObject {
    param([Parameter(Mandatory = $true)][string]$StartVerdict)

    $intentHash = $null
    if ($script:EgState.intent_committed) {
        $sha = New-Object System.Security.Cryptography.SHA256Managed
        try { $intentHash = ([BitConverter]::ToString($sha.ComputeHash($script:EgState.intent_bytes))).Replace('-', '').ToLowerInvariant() }
        finally { $sha.Dispose() }
    }
    return [ordered]@{
        schema = 'energygrid.one_shot_supervisor.outcome.v1'
        run_id = $RunId
        operation = 'run'
        intent_sha256 = $intentHash
        start_verdict = $StartVerdict
        launcher_exit_code = $script:EgState.launcher_exit_code
        creation_attempted = [bool]$script:EgState.creation_attempted
        resume_attempted = [bool]$script:EgState.resume_attempted
        application_child_observed = [bool]$script:EgState.application_child_observed
        application_child_observation_elapsed_ms = $script:EgState.application_child_observation_elapsed_ms
        total_processes = $script:EgState.total_processes
        active_processes = $script:EgState.active_processes
        total_terminated_processes = $script:EgState.total_terminated_processes
        reap_confirmed = [bool]$script:EgState.reap_confirmed
        stdout_bytes = [uint64]$script:EgState.stdout_bytes
        stderr_bytes = [uint64]$script:EgState.stderr_bytes
        stdout_complete = [bool]$script:EgState.stdout_complete
        stderr_complete = [bool]$script:EgState.stderr_complete
        raw_stream_retained_bytes = [uint64]0
        containment = if ($script:EgState.reap_confirmed) { 'REAP_CONFIRMED' } else { 'REAP_UNPROVEN' }
        completed_utc = [DateTime]::UtcNow.ToString('o', [Globalization.CultureInfo]::InvariantCulture)
    }
}

function Write-EgOutcome {
    param([Parameter(Mandatory = $true)][string]$StartVerdict)

    $script:EgState.outcome_write_attempted = $true
    $outcomePath = Join-Path $script:EgEvidenceRootNormal ($RunId + '.outcome.json')
    try {
        $bytes = ConvertTo-EgUtf8JsonBytes -Object (Get-EgOutcomeObject -StartVerdict $StartVerdict)
        $stream = New-Object System.IO.FileStream(
            $outcomePath, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write,
            [System.IO.FileShare]::None, 4096, [System.IO.FileOptions]::WriteThrough)
        try {
            $handleResult = [EnergyGridOneShotSupervisorNative]::SetHandleInheritance(
                $stream.SafeFileHandle.DangerousGetHandle(), $false)
            if (-not $handleResult.Success) {
                Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_OUTCOME_HANDLE_FAILED' `
                    -ErrorCode $handleResult.ErrorCode -EvidenceFailure
            }
            $durability = [EnergyGridOneShotSupervisorNative]::WriteFlushBounded(
                $stream, $bytes, 5000)
            if ($durability.TimedOut -or -not $durability.Succeeded) {
                Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_OUTCOME_FLUSH_FAILED' `
                    -ErrorCode $durability.ErrorCode -EvidenceFailure
            }
            $script:EgState.outcome_committed = $true
        }
        finally {
            if ($null -ne $stream) { $stream.Dispose() }
        }
    }
    catch {
        $script:EgState.evidence_integrity_failure = $true
        if ($_.Exception.PSObject.Properties['ErrorCode']) {
            $script:EgState.error_code = [int]$_.Exception.ErrorCode
        }
        if ($_.Exception.Message -like 'EG_SUPERVISOR_*') {
            $script:EgState.support_ref = $_.Exception.Message
        }
        else {
            $script:EgState.support_ref = 'EG_SUPERVISOR_OUTCOME_FLUSH_FAILED'
        }
    }
}

function Get-EgOutcomeTimedOutFlushResult {
    return [pscustomobject]@{ Succeeded = $false; TimedOut = $true; ErrorCode = 0 }
}

function Write-EgOutcomeWithTimeoutControl {
    param([Parameter(Mandatory = $true)][string]$StartVerdict)

    $script:EgState.outcome_write_attempted = $true
    $outcomePath = Join-Path $script:EgEvidenceRootNormal ($RunId + '.outcome.json')
    try {
        $bytes = ConvertTo-EgUtf8JsonBytes -Object (Get-EgOutcomeObject -StartVerdict $StartVerdict)
        $stream = New-Object System.IO.FileStream(
            $outcomePath, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write,
            [System.IO.FileShare]::None, 4096, [System.IO.FileOptions]::WriteThrough)
        try {
            $handleResult = [EnergyGridOneShotSupervisorNative]::SetHandleInheritance(
                $stream.SafeFileHandle.DangerousGetHandle(), $false)
            if (-not $handleResult.Success) {
                Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_OUTCOME_HANDLE_FAILED' `
                    -ErrorCode $handleResult.ErrorCode -EvidenceFailure
            }
            $durability = Get-EgOutcomeTimedOutFlushResult
            if ($durability.TimedOut -or -not $durability.Succeeded) {
                Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_OUTCOME_FLUSH_FAILED' `
                    -ErrorCode $durability.ErrorCode -EvidenceFailure
            }
            $script:EgState.outcome_committed = $true
        }
        finally {
            if ($null -ne $stream) { $stream.Dispose() }
        }
    }
    catch {
        $script:EgState.evidence_integrity_failure = $true
        if ($_.Exception.PSObject.Properties['ErrorCode']) {
            $script:EgState.error_code = [int]$_.Exception.ErrorCode
        }
        if ($_.Exception.Message -like 'EG_SUPERVISOR_*') {
            $script:EgState.support_ref = $_.Exception.Message
        }
        else {
            $script:EgState.support_ref = 'EG_SUPERVISOR_OUTCOME_FLUSH_FAILED'
        }
    }
}

function Get-EgCanonicalApplicationCommandLine {
    return (ConvertTo-EgNativeCommandLine -Argument @(
        $script:EgPythonExeNormal, '-m', 'energygrid_bill_downloader', 'run', '--config',
        $script:EgConfigPathNormal
    ))
}

function Test-EgApplicationChild {
    param(
        [Parameter(Mandatory = $true)][IntPtr]$JobHandle,
        [Parameter(Mandatory = $true)][IntPtr]$LauncherHandle,
        [Parameter(Mandatory = $true)][uint32]$LauncherPid,
        [Parameter(Mandatory = $true)][long]$StartTicks
    )

    $observerDeadline = [int64]([System.Diagnostics.Stopwatch]::GetTimestamp() +
        [int64]([System.Diagnostics.Stopwatch]::Frequency / 20))
    $supervisorDeadline = Get-EgDeadlineTicks -StartTicks $StartTicks -Seconds $TimeoutSeconds
    $pidResult = [EnergyGridOneShotSupervisorNative]::GetProcessIds($JobHandle)
    if (-not $pidResult.Succeeded) {
        $script:EgState.observer_failed = $true
        return $false
    }
    $expectedImage = [System.IO.Path]::GetFullPath($script:EgPythonExeNormal)
    $expectedCommandLine = Get-EgCanonicalApplicationCommandLine
    foreach ($candidatePid in $pidResult.ProcessIds) {
        if ([System.Diagnostics.Stopwatch]::GetTimestamp() -ge $observerDeadline) { break }
        if ([uint32]$candidatePid -eq $LauncherPid) { continue }
        $candidate = [EnergyGridOneShotSupervisorNative]::OpenQueryProcess([uint32]$candidatePid)
        if (-not $candidate.Succeeded) {
            # A listed descendant that has already exited and been released is absent, not
            # unprovable. Every other open failure remains an observer failure.
            if ($candidate.ErrorCode -ne $script:EgObserverProcessGoneErrorCode) {
                $script:EgState.observer_failed = $true
            }
            continue
        }
        try {
            $live = [EnergyGridOneShotSupervisorNative]::GetProcessLive($candidate.Handle)
            if (-not $live.Succeeded) {
                $script:EgState.observer_failed = $true
                continue
            }
            if (-not $live.Live) { continue }
            $membership = [EnergyGridOneShotSupervisorNative]::CheckMembership(
                $candidate.Handle, $JobHandle)
            if (-not $membership.Succeeded) {
                $script:EgState.observer_failed = $true
                continue
            }
            if (-not $membership.IsMember) { continue }
            $launcherLive = [EnergyGridOneShotSupervisorNative]::GetProcessLive($LauncherHandle)
            if (-not $launcherLive.Succeeded) {
                $script:EgState.observer_failed = $true
                continue
            }
            if (-not $launcherLive.Live) { continue }
            $image = [EnergyGridOneShotSupervisorNative]::GetImage($candidate.Handle)
            if (-not $image.Succeeded) {
                # Only a candidate proven exited through the held handle is absent.
                $exited = [EnergyGridOneShotSupervisorNative]::GetProcessLive($candidate.Handle)
                if (-not ($exited.Succeeded -and -not $exited.Live)) {
                    $script:EgState.observer_failed = $true
                }
                continue
            }
            if ([System.StringComparer]::OrdinalIgnoreCase.Equals(
                [System.IO.Path]::GetFullPath($image.ImagePath), $expectedImage) -eq $false) { continue }
            # A provider query is started only when its whole bounded budget fits before the
            # supervisor deadline, so observation can never outlive the one-shot deadline.
            $remainingMilliseconds = [Math]::Floor((($supervisorDeadline -
                [System.Diagnostics.Stopwatch]::GetTimestamp()) * 1000.0) /
                [System.Diagnostics.Stopwatch]::Frequency)
            if ($remainingMilliseconds -lt $script:EgObserverMetadataTimeoutMilliseconds) { break }
            $metadata = [EnergyGridOneShotSupervisorNative]::QueryProcessMetadata(
                [uint32]$candidatePid, $script:EgObserverMetadataTimeoutMilliseconds)
            if ($metadata.TimedOut -or $metadata.ProviderFailed) {
                # Genuine provider uncertainty: a late or busy provider is never evidence.
                $script:EgState.observer_failed = $true
                continue
            }
            if (-not $metadata.Succeeded) {
                # No provider row: absent only when the held handle proves the candidate exited.
                $exited = [EnergyGridOneShotSupervisorNative]::GetProcessLive($candidate.Handle)
                if (-not ($exited.Succeeded -and -not $exited.Live)) {
                    $script:EgState.observer_failed = $true
                }
                continue
            }
            if ([uint32]$metadata.ParentProcessId -ne $LauncherPid) { continue }
            if ($metadata.CommandLine -cne $expectedCommandLine) { continue }
            $launcherLiveAgain = [EnergyGridOneShotSupervisorNative]::GetProcessLive($LauncherHandle)
            $candidateLiveAgain = [EnergyGridOneShotSupervisorNative]::GetProcessLive($candidate.Handle)
            $membershipAgain = [EnergyGridOneShotSupervisorNative]::CheckMembership(
                $candidate.Handle, $JobHandle)
            if (-not $launcherLiveAgain.Succeeded -or
                -not $candidateLiveAgain.Succeeded -or
                -not $membershipAgain.Succeeded) {
                $script:EgState.observer_failed = $true
                continue
            }
            if (-not $launcherLiveAgain.Live -or -not $candidateLiveAgain.Live -or
                -not $membershipAgain.IsMember) { continue }
            $elapsed = [Math]::Floor((([System.Diagnostics.Stopwatch]::GetTimestamp() -
                $StartTicks) * 1000.0) / [System.Diagnostics.Stopwatch]::Frequency)
            $script:EgState.application_child_observation_elapsed_ms = [int64]$elapsed
            return $true
        }
        finally {
            Close-EgHandle -Handle $candidate.Handle
        }
    }
    return $false
}
function Get-EgN13ProcessMetadata {
    param([uint32]$ProcessId, [int]$TimeoutMilliseconds)

    if ($ProcessId -ne $script:EgN13ProcessId -or $TimeoutMilliseconds -ne 1000) {
        throw 'EG_N13_PROCESS_IDENTITY_MISMATCH'
    }
    [System.IO.File]::WriteAllText($script:EgN13ReleasePath, 'release')
    $wait = [EnergyGridOneShotSupervisorNative]::WaitProcess(
        $script:EgN13ChildHandle, 5000)
    if ($wait.Value -ne [EnergyGridOneShotSupervisorNative]::WAIT_OBJECT_0) {
        throw 'EG_N13_OWNED_CHILD_EXIT_TIMEOUT'
    }
    $script:EgN13Metadata = [EnergyGridOneShotSupervisorNative]::QueryProcessMetadata(
        $ProcessId, $TimeoutMilliseconds)
    return $script:EgN13Metadata
}

function Test-EgN13ApplicationChild {
    param(
        [Parameter(Mandatory = $true)][IntPtr]$JobHandle,
        [Parameter(Mandatory = $true)][IntPtr]$LauncherHandle,
        [Parameter(Mandatory = $true)][uint32]$LauncherPid,
        [Parameter(Mandatory = $true)][long]$StartTicks
    )

    $observerDeadline = [int64]([System.Diagnostics.Stopwatch]::GetTimestamp() +
        [int64]([System.Diagnostics.Stopwatch]::Frequency / 20))
    $supervisorDeadline = Get-EgDeadlineTicks -StartTicks $StartTicks -Seconds $TimeoutSeconds
    $pidResult = [EnergyGridOneShotSupervisorNative]::GetProcessIds($JobHandle)
    if (-not $pidResult.Succeeded) {
        $script:EgState.observer_failed = $true
        return $false
    }
    $expectedImage = [System.IO.Path]::GetFullPath($script:EgPythonExeNormal)
    $expectedCommandLine = Get-EgCanonicalApplicationCommandLine
    foreach ($candidatePid in $pidResult.ProcessIds) {
        if ([System.Diagnostics.Stopwatch]::GetTimestamp() -ge $observerDeadline) { break }
        if ([uint32]$candidatePid -eq $LauncherPid) { continue }
        $candidate = [EnergyGridOneShotSupervisorNative]::OpenQueryProcess([uint32]$candidatePid)
        if (-not $candidate.Succeeded) {
            # A listed descendant that has already exited and been released is absent, not
            # unprovable. Every other open failure remains an observer failure.
            if ($candidate.ErrorCode -ne $script:EgObserverProcessGoneErrorCode) {
                $script:EgState.observer_failed = $true
            }
            continue
        }
        try {
            $live = [EnergyGridOneShotSupervisorNative]::GetProcessLive($candidate.Handle)
            if (-not $live.Succeeded) {
                $script:EgState.observer_failed = $true
                continue
            }
            if (-not $live.Live) { continue }
            $membership = [EnergyGridOneShotSupervisorNative]::CheckMembership(
                $candidate.Handle, $JobHandle)
            if (-not $membership.Succeeded) {
                $script:EgState.observer_failed = $true
                continue
            }
            if (-not $membership.IsMember) { continue }
            $launcherLive = [EnergyGridOneShotSupervisorNative]::GetProcessLive($LauncherHandle)
            if (-not $launcherLive.Succeeded) {
                $script:EgState.observer_failed = $true
                continue
            }
            if (-not $launcherLive.Live) { continue }
            $image = [EnergyGridOneShotSupervisorNative]::GetImage($candidate.Handle)
            if (-not $image.Succeeded) {
                # Only a candidate proven exited through the held handle is absent.
                $exited = [EnergyGridOneShotSupervisorNative]::GetProcessLive($candidate.Handle)
                if (-not ($exited.Succeeded -and -not $exited.Live)) {
                    $script:EgState.observer_failed = $true
                }
                continue
            }
            if ([System.StringComparer]::OrdinalIgnoreCase.Equals(
                [System.IO.Path]::GetFullPath($image.ImagePath), $expectedImage) -eq $false) { continue }
            # A provider query is started only when its whole bounded budget fits before the
            # supervisor deadline, so observation can never outlive the one-shot deadline.
            $remainingMilliseconds = [Math]::Floor((($supervisorDeadline -
                [System.Diagnostics.Stopwatch]::GetTimestamp()) * 1000.0) /
                [System.Diagnostics.Stopwatch]::Frequency)
            if ($remainingMilliseconds -lt $script:EgObserverMetadataTimeoutMilliseconds) { break }
            $metadata = Get-EgN13ProcessMetadata -ProcessId ([uint32]$candidatePid) -TimeoutMilliseconds $script:EgObserverMetadataTimeoutMilliseconds
            if ($metadata.TimedOut -or $metadata.ProviderFailed) {
                # Genuine provider uncertainty: a late or busy provider is never evidence.
                $script:EgState.observer_failed = $true
                continue
            }
            if (-not $metadata.Succeeded) {
                # No provider row: absent only when the held handle proves the candidate exited.
                $exited = [EnergyGridOneShotSupervisorNative]::GetProcessLive($candidate.Handle)
                if (-not ($exited.Succeeded -and -not $exited.Live)) {
                    $script:EgState.observer_failed = $true
                }
                continue
            }
            if ([uint32]$metadata.ParentProcessId -ne $LauncherPid) { continue }
            if ($metadata.CommandLine -cne $expectedCommandLine) { continue }
            $launcherLiveAgain = [EnergyGridOneShotSupervisorNative]::GetProcessLive($LauncherHandle)
            $candidateLiveAgain = [EnergyGridOneShotSupervisorNative]::GetProcessLive($candidate.Handle)
            $membershipAgain = [EnergyGridOneShotSupervisorNative]::CheckMembership(
                $candidate.Handle, $JobHandle)
            if (-not $launcherLiveAgain.Succeeded -or
                -not $candidateLiveAgain.Succeeded -or
                -not $membershipAgain.Succeeded) {
                $script:EgState.observer_failed = $true
                continue
            }
            if (-not $launcherLiveAgain.Live -or -not $candidateLiveAgain.Live -or
                -not $membershipAgain.IsMember) { continue }
            $elapsed = [Math]::Floor((([System.Diagnostics.Stopwatch]::GetTimestamp() -
                $StartTicks) * 1000.0) / [System.Diagnostics.Stopwatch]::Frequency)
            $script:EgState.application_child_observation_elapsed_ms = [int64]$elapsed
            return $true
        }
        finally {
            Close-EgHandle -Handle $candidate.Handle
        }
    }
    return $false
}

function Get-EgAccounting {
    param([Parameter(Mandatory = $true)][IntPtr]$JobHandle)

    $accounting = [EnergyGridOneShotSupervisorNative]::GetAccounting($JobHandle)
    if (-not $accounting.Succeeded) {
        Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_ACCOUNTING_FAILED' `
            -ErrorCode $accounting.ErrorCode -ContainmentFailure
    }
    $script:EgState.total_processes = [uint64]$accounting.TotalProcesses
    $script:EgState.active_processes = [uint64]$accounting.ActiveProcesses
    $script:EgState.total_terminated_processes = [uint64]$accounting.TotalTerminatedProcesses
    return $accounting
}

function Invoke-EgTerminateJob {
    param(
        [Parameter(Mandatory = $true)][IntPtr]$JobHandle,
        [Parameter(Mandatory = $true)][string]$Reason
    )

    if ($script:EgState.termination_started) { return }
    $script:EgState.termination_started = $true
    if ($Reason -eq 'TIMEOUT') { $script:EgState.timed_out = $true }
    if ($Reason -eq 'INTERRUPTION') { $script:EgState.interrupted = $true }
    [EnergyGridOneShotSupervisorNative]::RequestFailure()
    $termination = [EnergyGridOneShotSupervisorNative]::TerminateJob(
        $JobHandle, [EnergyGridOneShotSupervisorNative]::SUPERVISOR_TERMINATION_EXIT_CODE)
    if ($termination.Success) {
        $script:EgState.termination_succeeded = $true
    }
    else {
        $script:EgState.termination_failure = $true
        $script:EgState.containment_failure = $true
        $script:EgState.support_ref = 'EG_SUPERVISOR_TERMINATION_FAILED'
        $script:EgState.error_code = $termination.ErrorCode
    }
}

function Get-EgDeadlineTicks {
    param([Parameter(Mandatory = $true)][long]$StartTicks,[Parameter(Mandatory = $true)][int]$Seconds)
    return [int64]($StartTicks + ([int64]$Seconds * [int64][System.Diagnostics.Stopwatch]::Frequency))
}

function Get-EgDurabilityMilliseconds {
    param([Parameter(Mandatory = $true)][long]$StartTicks,[Parameter(Mandatory = $true)][int]$Seconds)
    $maximum = 5000
    if ($StartTicks -eq 0) { return $maximum }
    $deadline = Get-EgDeadlineTicks -StartTicks $StartTicks -Seconds $Seconds
    $remainingTicks = $deadline - [System.Diagnostics.Stopwatch]::GetTimestamp()
    if ($remainingTicks -le 0) { return 0 }
    $remaining = [int][Math]::Floor(($remainingTicks * 1000.0) /
        [System.Diagnostics.Stopwatch]::Frequency)
    return [Math]::Min($maximum, [Math]::Max(0, $remaining))
}

function Test-EgDeadlineReached {
    param([Parameter(Mandatory = $true)][long]$DeadlineTicks)
    return ([System.Diagnostics.Stopwatch]::GetTimestamp() -ge $DeadlineTicks)
}

function Wait-EgReap {
    param(
        [Parameter(Mandatory = $true)][IntPtr]$JobHandle,
        [Parameter(Mandatory = $true)][long]$DeadlineTicks,
        [Parameter(Mandatory = $true)][int]$WindowSeconds
    )

    $windowDeadline = [int64]([System.Diagnostics.Stopwatch]::GetTimestamp() +
        ([int64]$WindowSeconds * [int64][System.Diagnostics.Stopwatch]::Frequency))
    while ([System.Diagnostics.Stopwatch]::GetTimestamp() -lt $windowDeadline) {
        try {
            $accounting = Get-EgAccounting -JobHandle $JobHandle
        }
        catch {
            $script:EgState.containment_failure = $true
            break
        }
        if ($accounting.ActiveProcesses -eq 0) {
            $script:EgState.reap_confirmed = $true
            return
        }
        Start-Sleep -Milliseconds 100
    }
    try {
        $final = Get-EgAccounting -JobHandle $JobHandle
        if ($final.ActiveProcesses -eq 0) {
            $script:EgState.reap_confirmed = $true
        }
        else {
            $script:EgState.reap_confirmed = $false
        }
    }
    catch {
        $script:EgState.reap_confirmed = $false
        $script:EgState.containment_failure = $true
    }
}

function Wait-EgDescendantGrace {
    param(
        [Parameter(Mandatory = $true)][IntPtr]$JobHandle,
        [Parameter(Mandatory = $true)][IntPtr]$LauncherHandle,
        [Parameter(Mandatory = $true)][uint32]$LauncherPid,
        [Parameter(Mandatory = $true)][long]$StartTicks,
        [Parameter(Mandatory = $true)][long]$DeadlineTicks
    )

    $graceDeadline = [int64]([System.Diagnostics.Stopwatch]::GetTimestamp() +
        ([int64]5 * [int64][System.Diagnostics.Stopwatch]::Frequency))
    while ($true) {
        $accounting = Get-EgAccounting -JobHandle $JobHandle
        if ($accounting.ActiveProcesses -eq 0) { return }

        if ([EnergyGridOneShotSupervisorNative]::IsTerminationRequested) {
            Invoke-EgTerminateJob -JobHandle $JobHandle -Reason 'INTERRUPTION'
            return
        }

        if (Test-EgDeadlineReached -DeadlineTicks $DeadlineTicks) {
            Invoke-EgTerminateJob -JobHandle $JobHandle -Reason 'TIMEOUT'
            return
        }

        if ([System.Diagnostics.Stopwatch]::GetTimestamp() -ge $graceDeadline) {
            $script:EgState.descendant_grace_expired = $true
            Invoke-EgTerminateJob -JobHandle $JobHandle -Reason 'DESCENDANT_GRACE_EXPIRED'
            return
        }

        Start-Sleep -Milliseconds 100
    }
}

function Get-EgStartVerdict {
    if ($script:EgState.evidence_integrity_failure -or $script:EgState.containment_failure -or
        $script:EgState.observer_failed) {
        return 'AMBIGUOUS'
    }
    if ((-not $script:EgState.creation_attempted) -and $script:EgState.precreate_rejection) {
        return 'NOT_STARTED_PROVEN'
    }
    if ($script:EgState.creation_succeeded -and
        $script:EgState.baseline_total_processes -eq 1 -and
        $script:EgState.baseline_active_processes -eq 1 -and
        $script:EgState.total_processes -eq 1 -and
        $script:EgState.active_processes -eq 0 -and
        -not $script:EgState.application_child_observed -and
        $script:EgState.reap_confirmed) {
        return 'NOT_STARTED_PROVEN'
    }
    if ($script:EgState.application_child_observed -and
        $script:EgState.total_processes -ge 2 -and
        $script:EgState.reap_confirmed -and
        $script:EgState.intent_committed) {
        return 'STARTED_PROVEN'
    }
    return 'AMBIGUOUS'
}

function Get-EgExitCode {
    if (-not $script:EgState.outcome_committed) { return 3 }
    if ($script:EgState.containment_failure -or $script:EgState.evidence_integrity_failure -or
        $script:EgState.drain_failure -or $script:EgState.termination_failure -or
        $script:EgState.observer_failed) { return 3 }
    if ($script:EgState.creation_succeeded -and -not $script:EgState.reap_confirmed) {
        return 3
    }
    if ($script:EgState.creation_succeeded -and
        (-not $script:EgState.stdout_complete -or -not $script:EgState.stderr_complete)) {
        return 3
    }
    if ($script:EgState.timed_out -or $script:EgState.interrupted) { return 2 }
    if ($script:EgState.start_verdict -eq 'AMBIGUOUS') { return 4 }
    if ($script:EgState.start_verdict -eq 'STARTED_PROVEN' -and
        $script:EgState.creation_succeeded -and
        $script:EgState.launcher_exit_code -eq 0 -and
        $script:EgState.reap_confirmed -and
        $script:EgState.stdout_complete -and $script:EgState.stderr_complete) { return 0 }
    return 1
}

$script:EgState = [pscustomobject]@{
    support_ref = 'EG_SUPERVISOR'
    error_code = $null
    creation_attempted = $false
    creation_succeeded = $false
    resume_attempted = $false
    resume_succeeded = $false
    intent_committed = $false
    outcome_committed = $false
    intent_bytes = $null
    launcher_exit_code = $null
    application_child_observed = $false
    application_child_observation_elapsed_ms = $null
    observer_failed = $false
    total_processes = $null
    active_processes = $null
    total_terminated_processes = $null
    baseline_total_processes = $null
    baseline_active_processes = $null
    reap_confirmed = $false
    stdout_bytes = [uint64]0
    stderr_bytes = [uint64]0
    stdout_complete = $false
    stderr_complete = $false
    timed_out = $false
    interrupted = $false
    termination_started = $false
    termination_succeeded = $false
    termination_failure = $false
    containment_failure = $false
    evidence_integrity_failure = $false
    drain_failure = $false
    descendant_grace_expired = $false
    start_verdict = 'AMBIGUOUS'
    outcome_write_attempted = $false
    duplicate_run_id = $false
    precreate_rejection = $false
}

$script:EgObserverMetadataTimeoutMilliseconds = 1000

$script:EgObserverProcessGoneErrorCode = 87

function Test-EgN4bApplicationChild {
    param(
        [Parameter(Mandatory = $true)][IntPtr]$JobHandle,
        [Parameter(Mandatory = $true)][IntPtr]$LauncherHandle,
        [Parameter(Mandatory = $true)][uint32]$LauncherPid,
        [Parameter(Mandatory = $true)][long]$StartTicks
    )

    $observerDeadline = [int64]([System.Diagnostics.Stopwatch]::GetTimestamp() +
        [int64]([System.Diagnostics.Stopwatch]::Frequency / 20))
    $supervisorDeadline = Get-EgDeadlineTicks -StartTicks $StartTicks -Seconds $TimeoutSeconds
    $pidResult = Get-EgN4bProcessIds -JobHandle $JobHandle
    if (-not $pidResult.Succeeded) {
        $script:EgState.observer_failed = $true
        return $false
    }
    $expectedImage = [System.IO.Path]::GetFullPath($script:EgPythonExeNormal)
    $expectedCommandLine = Get-EgCanonicalApplicationCommandLine
    foreach ($candidatePid in $pidResult.ProcessIds) {
        if ([System.Diagnostics.Stopwatch]::GetTimestamp() -ge $observerDeadline) { break }
        if ([uint32]$candidatePid -eq $LauncherPid) { continue }
        $candidate = [EnergyGridOneShotSupervisorNative]::OpenQueryProcess([uint32]$candidatePid)
        if (-not $candidate.Succeeded) {
            # A listed descendant that has already exited and been released is absent, not
            # unprovable. Every other open failure remains an observer failure.
            if ($candidate.ErrorCode -ne $script:EgObserverProcessGoneErrorCode) {
                $script:EgState.observer_failed = $true
            }
            continue
        }
        try {
            $live = [EnergyGridOneShotSupervisorNative]::GetProcessLive($candidate.Handle)
            if (-not $live.Succeeded) {
                $script:EgState.observer_failed = $true
                continue
            }
            if (-not $live.Live) { continue }
            $membership = [EnergyGridOneShotSupervisorNative]::CheckMembership(
                $candidate.Handle, $JobHandle)
            if (-not $membership.Succeeded) {
                $script:EgState.observer_failed = $true
                continue
            }
            if (-not $membership.IsMember) { continue }
            $launcherLive = [EnergyGridOneShotSupervisorNative]::GetProcessLive($LauncherHandle)
            if (-not $launcherLive.Succeeded) {
                $script:EgState.observer_failed = $true
                continue
            }
            if (-not $launcherLive.Live) { continue }
            $image = [EnergyGridOneShotSupervisorNative]::GetImage($candidate.Handle)
            if (-not $image.Succeeded) {
                # Only a candidate proven exited through the held handle is absent.
                $exited = [EnergyGridOneShotSupervisorNative]::GetProcessLive($candidate.Handle)
                if (-not ($exited.Succeeded -and -not $exited.Live)) {
                    $script:EgState.observer_failed = $true
                }
                continue
            }
            if ([System.StringComparer]::OrdinalIgnoreCase.Equals(
                [System.IO.Path]::GetFullPath($image.ImagePath), $expectedImage) -eq $false) { continue }
            # A provider query is started only when its whole bounded budget fits before the
            # supervisor deadline, so observation can never outlive the one-shot deadline.
            $remainingMilliseconds = [Math]::Floor((($supervisorDeadline -
                [System.Diagnostics.Stopwatch]::GetTimestamp()) * 1000.0) /
                [System.Diagnostics.Stopwatch]::Frequency)
            if ($remainingMilliseconds -lt $script:EgObserverMetadataTimeoutMilliseconds) { break }
            $metadata = [EnergyGridOneShotSupervisorNative]::QueryProcessMetadata(
                [uint32]$candidatePid, $script:EgObserverMetadataTimeoutMilliseconds)
            if ($metadata.TimedOut -or $metadata.ProviderFailed) {
                # Genuine provider uncertainty: a late or busy provider is never evidence.
                $script:EgState.observer_failed = $true
                continue
            }
            if (-not $metadata.Succeeded) {
                # No provider row: absent only when the held handle proves the candidate exited.
                $exited = [EnergyGridOneShotSupervisorNative]::GetProcessLive($candidate.Handle)
                if (-not ($exited.Succeeded -and -not $exited.Live)) {
                    $script:EgState.observer_failed = $true
                }
                continue
            }
            if ([uint32]$metadata.ParentProcessId -ne $LauncherPid) { continue }
            if ($metadata.CommandLine -cne $expectedCommandLine) { continue }
            $launcherLiveAgain = [EnergyGridOneShotSupervisorNative]::GetProcessLive($LauncherHandle)
            $candidateLiveAgain = [EnergyGridOneShotSupervisorNative]::GetProcessLive($candidate.Handle)
            $membershipAgain = [EnergyGridOneShotSupervisorNative]::CheckMembership(
                $candidate.Handle, $JobHandle)
            if (-not $launcherLiveAgain.Succeeded -or
                -not $candidateLiveAgain.Succeeded -or
                -not $membershipAgain.Succeeded) {
                $script:EgState.observer_failed = $true
                continue
            }
            if (-not $launcherLiveAgain.Live -or -not $candidateLiveAgain.Live -or
                -not $membershipAgain.IsMember) { continue }
            $elapsed = [Math]::Floor((([System.Diagnostics.Stopwatch]::GetTimestamp() -
                $StartTicks) * 1000.0) / [System.Diagnostics.Stopwatch]::Frequency)
            $script:EgState.application_child_observation_elapsed_ms = [int64]$elapsed
            return $true
        }
        finally {
            Close-EgHandle -Handle $candidate.Handle
        }
    }
    return $false
}

function Test-EgN5ApplicationChild {
    param(
        [Parameter(Mandatory = $true)][IntPtr]$JobHandle,
        [Parameter(Mandatory = $true)][IntPtr]$LauncherHandle,
        [Parameter(Mandatory = $true)][uint32]$LauncherPid,
        [Parameter(Mandatory = $true)][long]$StartTicks
    )

    $observerDeadline = [int64]([System.Diagnostics.Stopwatch]::GetTimestamp() +
        [int64]([System.Diagnostics.Stopwatch]::Frequency / 20))
    $supervisorDeadline = Get-EgDeadlineTicks -StartTicks $StartTicks -Seconds $TimeoutSeconds
    $pidResult = Get-EgN5ProcessIds -JobHandle $JobHandle
    if (-not $pidResult.Succeeded) {
        $script:EgState.observer_failed = $true
        return $false
    }
    $expectedImage = [System.IO.Path]::GetFullPath($script:EgPythonExeNormal)
    $expectedCommandLine = Get-EgCanonicalApplicationCommandLine
    foreach ($candidatePid in $pidResult.ProcessIds) {
        if ([System.Diagnostics.Stopwatch]::GetTimestamp() -ge $observerDeadline) { break }
        if ([uint32]$candidatePid -eq $LauncherPid) { continue }
        $candidate = [EnergyGridOneShotSupervisorNative]::OpenQueryProcess([uint32]$candidatePid)
        if (-not $candidate.Succeeded) {
            # A listed descendant that has already exited and been released is absent, not
            # unprovable. Every other open failure remains an observer failure.
            if ($candidate.ErrorCode -ne $script:EgObserverProcessGoneErrorCode) {
                $script:EgState.observer_failed = $true
            }
            continue
        }
        try {
            $live = [EnergyGridOneShotSupervisorNative]::GetProcessLive($candidate.Handle)
            if (-not $live.Succeeded) {
                $script:EgState.observer_failed = $true
                continue
            }
            if (-not $live.Live) { continue }
            $membership = [EnergyGridOneShotSupervisorNative]::CheckMembership(
                $candidate.Handle, $JobHandle)
            if (-not $membership.Succeeded) {
                $script:EgState.observer_failed = $true
                continue
            }
            if (-not $membership.IsMember) { continue }
            $launcherLive = [EnergyGridOneShotSupervisorNative]::GetProcessLive($LauncherHandle)
            if (-not $launcherLive.Succeeded) {
                $script:EgState.observer_failed = $true
                continue
            }
            if (-not $launcherLive.Live) { continue }
            $image = [EnergyGridOneShotSupervisorNative]::GetImage($candidate.Handle)
            if (-not $image.Succeeded) {
                # Only a candidate proven exited through the held handle is absent.
                $exited = [EnergyGridOneShotSupervisorNative]::GetProcessLive($candidate.Handle)
                if (-not ($exited.Succeeded -and -not $exited.Live)) {
                    $script:EgState.observer_failed = $true
                }
                continue
            }
            if ([System.StringComparer]::OrdinalIgnoreCase.Equals(
                [System.IO.Path]::GetFullPath($image.ImagePath), $expectedImage) -eq $false) { continue }
            # A provider query is started only when its whole bounded budget fits before the
            # supervisor deadline, so observation can never outlive the one-shot deadline.
            $remainingMilliseconds = [Math]::Floor((($supervisorDeadline -
                [System.Diagnostics.Stopwatch]::GetTimestamp()) * 1000.0) /
                [System.Diagnostics.Stopwatch]::Frequency)
            if ($remainingMilliseconds -lt $script:EgObserverMetadataTimeoutMilliseconds) { break }
            $metadata = [EnergyGridOneShotSupervisorNative]::QueryProcessMetadata(
                [uint32]$candidatePid, $script:EgObserverMetadataTimeoutMilliseconds)
            if ($metadata.TimedOut -or $metadata.ProviderFailed) {
                # Genuine provider uncertainty: a late or busy provider is never evidence.
                $script:EgState.observer_failed = $true
                continue
            }
            if (-not $metadata.Succeeded) {
                # No provider row: absent only when the held handle proves the candidate exited.
                $exited = [EnergyGridOneShotSupervisorNative]::GetProcessLive($candidate.Handle)
                if (-not ($exited.Succeeded -and -not $exited.Live)) {
                    $script:EgState.observer_failed = $true
                }
                continue
            }
            if ([uint32]$metadata.ParentProcessId -ne $LauncherPid) { continue }
            if ($metadata.CommandLine -cne $expectedCommandLine) { continue }
            $launcherLiveAgain = [EnergyGridOneShotSupervisorNative]::GetProcessLive($LauncherHandle)
            $candidateLiveAgain = [EnergyGridOneShotSupervisorNative]::GetProcessLive($candidate.Handle)
            $membershipAgain = [EnergyGridOneShotSupervisorNative]::CheckMembership(
                $candidate.Handle, $JobHandle)
            if (-not $launcherLiveAgain.Succeeded -or
                -not $candidateLiveAgain.Succeeded -or
                -not $membershipAgain.Succeeded) {
                $script:EgState.observer_failed = $true
                continue
            }
            if (-not $launcherLiveAgain.Live -or -not $candidateLiveAgain.Live -or
                -not $membershipAgain.IsMember) { continue }
            $elapsed = [Math]::Floor((([System.Diagnostics.Stopwatch]::GetTimestamp() -
                $StartTicks) * 1000.0) / [System.Diagnostics.Stopwatch]::Frequency)
            $script:EgState.application_child_observation_elapsed_ms = [int64]$elapsed
            return $true
        }
        finally {
            Close-EgHandle -Handle $candidate.Handle
        }
    }
    return $false
}

function Test-EgN10ApplicationChild {
    param(
        [Parameter(Mandatory = $true)][IntPtr]$JobHandle,
        [Parameter(Mandatory = $true)][IntPtr]$LauncherHandle,
        [Parameter(Mandatory = $true)][uint32]$LauncherPid,
        [Parameter(Mandatory = $true)][long]$StartTicks
    )

    $observerDeadline = [int64]([System.Diagnostics.Stopwatch]::GetTimestamp() +
        [int64]([System.Diagnostics.Stopwatch]::Frequency / 20))
    $supervisorDeadline = Get-EgDeadlineTicks -StartTicks $StartTicks -Seconds $TimeoutSeconds
    $pidResult = Get-EgN10ProcessIds -JobHandle $JobHandle
    if (-not $pidResult.Succeeded) {
        $script:EgState.observer_failed = $true
        return $false
    }
    $expectedImage = [System.IO.Path]::GetFullPath($script:EgPythonExeNormal)
    $expectedCommandLine = Get-EgCanonicalApplicationCommandLine
    foreach ($candidatePid in $pidResult.ProcessIds) {
        if ([System.Diagnostics.Stopwatch]::GetTimestamp() -ge $observerDeadline) { break }
        if ([uint32]$candidatePid -eq $LauncherPid) { continue }
        $candidate = [EnergyGridOneShotSupervisorNative]::OpenQueryProcess([uint32]$candidatePid)
        if (-not $candidate.Succeeded) {
            # A listed descendant that has already exited and been released is absent, not
            # unprovable. Every other open failure remains an observer failure.
            if ($candidate.ErrorCode -ne $script:EgObserverProcessGoneErrorCode) {
                $script:EgState.observer_failed = $true
            }
            continue
        }
        try {
            $live = [EnergyGridOneShotSupervisorNative]::GetProcessLive($candidate.Handle)
            if (-not $live.Succeeded) {
                $script:EgState.observer_failed = $true
                continue
            }
            if (-not $live.Live) { continue }
            $membership = [EnergyGridOneShotSupervisorNative]::CheckMembership(
                $candidate.Handle, $JobHandle)
            if (-not $membership.Succeeded) {
                $script:EgState.observer_failed = $true
                continue
            }
            if (-not $membership.IsMember) { continue }
            $launcherLive = [EnergyGridOneShotSupervisorNative]::GetProcessLive($LauncherHandle)
            if (-not $launcherLive.Succeeded) {
                $script:EgState.observer_failed = $true
                continue
            }
            if (-not $launcherLive.Live) { continue }
            $image = [EnergyGridOneShotSupervisorNative]::GetImage($candidate.Handle)
            if (-not $image.Succeeded) {
                # Only a candidate proven exited through the held handle is absent.
                $exited = [EnergyGridOneShotSupervisorNative]::GetProcessLive($candidate.Handle)
                if (-not ($exited.Succeeded -and -not $exited.Live)) {
                    $script:EgState.observer_failed = $true
                }
                continue
            }
            if ([System.StringComparer]::OrdinalIgnoreCase.Equals(
                [System.IO.Path]::GetFullPath($image.ImagePath), $expectedImage) -eq $false) { continue }
            # A provider query is started only when its whole bounded budget fits before the
            # supervisor deadline, so observation can never outlive the one-shot deadline.
            $remainingMilliseconds = [Math]::Floor((($supervisorDeadline -
                [System.Diagnostics.Stopwatch]::GetTimestamp()) * 1000.0) /
                [System.Diagnostics.Stopwatch]::Frequency)
            if ($remainingMilliseconds -lt $script:EgObserverMetadataTimeoutMilliseconds) { break }
            $metadata = [EnergyGridOneShotSupervisorNative]::QueryProcessMetadata(
                [uint32]$candidatePid, $script:EgObserverMetadataTimeoutMilliseconds)
            if ($metadata.TimedOut -or $metadata.ProviderFailed) {
                # Genuine provider uncertainty: a late or busy provider is never evidence.
                $script:EgState.observer_failed = $true
                continue
            }
            if (-not $metadata.Succeeded) {
                # No provider row: absent only when the held handle proves the candidate exited.
                $exited = [EnergyGridOneShotSupervisorNative]::GetProcessLive($candidate.Handle)
                if (-not ($exited.Succeeded -and -not $exited.Live)) {
                    $script:EgState.observer_failed = $true
                }
                continue
            }
            if ([uint32]$metadata.ParentProcessId -ne $LauncherPid) { continue }
            if ($metadata.CommandLine -cne $expectedCommandLine) { continue }
            $launcherLiveAgain = [EnergyGridOneShotSupervisorNative]::GetProcessLive($LauncherHandle)
            $candidateLiveAgain = [EnergyGridOneShotSupervisorNative]::GetProcessLive($candidate.Handle)
            $membershipAgain = [EnergyGridOneShotSupervisorNative]::CheckMembership(
                $candidate.Handle, $JobHandle)
            if (-not $launcherLiveAgain.Succeeded -or
                -not $candidateLiveAgain.Succeeded -or
                -not $membershipAgain.Succeeded) {
                $script:EgState.observer_failed = $true
                continue
            }
            if (-not $launcherLiveAgain.Live -or -not $candidateLiveAgain.Live -or
                -not $membershipAgain.IsMember) { continue }
            $elapsed = [Math]::Floor((([System.Diagnostics.Stopwatch]::GetTimestamp() -
                $StartTicks) * 1000.0) / [System.Diagnostics.Stopwatch]::Frequency)
            $script:EgState.application_child_observation_elapsed_ms = [int64]$elapsed
            return $true
        }
        finally {
            Close-EgHandle -Handle $candidate.Handle
        }
    }
    return $false
}

function Test-EgN11ApplicationChild {
    param(
        [Parameter(Mandatory = $true)][IntPtr]$JobHandle,
        [Parameter(Mandatory = $true)][IntPtr]$LauncherHandle,
        [Parameter(Mandatory = $true)][uint32]$LauncherPid,
        [Parameter(Mandatory = $true)][long]$StartTicks
    )

    $observerDeadline = [int64]([System.Diagnostics.Stopwatch]::GetTimestamp() +
        [int64]([System.Diagnostics.Stopwatch]::Frequency / 20))
    $supervisorDeadline = Get-EgDeadlineTicks -StartTicks $StartTicks -Seconds $TimeoutSeconds
    $pidResult = [EnergyGridOneShotSupervisorNative]::GetProcessIds($JobHandle)
    if (-not $pidResult.Succeeded) {
        $script:EgState.observer_failed = $true
        return $false
    }
    $expectedImage = [System.IO.Path]::GetFullPath($script:EgPythonExeNormal)
    $expectedCommandLine = Get-EgCanonicalApplicationCommandLine
    foreach ($candidatePid in $pidResult.ProcessIds) {
        if ([System.Diagnostics.Stopwatch]::GetTimestamp() -ge $observerDeadline) { break }
        if ([uint32]$candidatePid -eq $LauncherPid) { continue }
        $candidate = [EnergyGridOneShotSupervisorNative]::OpenQueryProcess([uint32]$candidatePid)
        if (-not $candidate.Succeeded) {
            # A listed descendant that has already exited and been released is absent, not
            # unprovable. Every other open failure remains an observer failure.
            if ($candidate.ErrorCode -ne $script:EgObserverProcessGoneErrorCode) {
                $script:EgState.observer_failed = $true
            }
            continue
        }
        try {
            $live = [EnergyGridOneShotSupervisorNative]::GetProcessLive($candidate.Handle)
            if (-not $live.Succeeded) {
                $script:EgState.observer_failed = $true
                continue
            }
            if (-not $live.Live) { continue }
            $membership = [EnergyGridOneShotSupervisorNative]::CheckMembership(
                $candidate.Handle, $JobHandle)
            if (-not $membership.Succeeded) {
                $script:EgState.observer_failed = $true
                continue
            }
            if (-not $membership.IsMember) { continue }
            $launcherLive = [EnergyGridOneShotSupervisorNative]::GetProcessLive($LauncherHandle)
            if (-not $launcherLive.Succeeded) {
                $script:EgState.observer_failed = $true
                continue
            }
            if (-not $launcherLive.Live) { continue }
            $image = [EnergyGridOneShotSupervisorNative]::GetImage($candidate.Handle)
            if (-not $image.Succeeded) {
                # Only a candidate proven exited through the held handle is absent.
                $exited = [EnergyGridOneShotSupervisorNative]::GetProcessLive($candidate.Handle)
                if (-not ($exited.Succeeded -and -not $exited.Live)) {
                    $script:EgState.observer_failed = $true
                }
                continue
            }
            if ([System.StringComparer]::OrdinalIgnoreCase.Equals(
                [System.IO.Path]::GetFullPath($image.ImagePath), $expectedImage) -eq $false) { continue }
            # A provider query is started only when its whole bounded budget fits before the
            # supervisor deadline, so observation can never outlive the one-shot deadline.
            $remainingMilliseconds = [Math]::Floor((($supervisorDeadline -
                [System.Diagnostics.Stopwatch]::GetTimestamp()) * 1000.0) /
                [System.Diagnostics.Stopwatch]::Frequency)
            if ($remainingMilliseconds -lt $script:EgObserverMetadataTimeoutMilliseconds) { break }
            $metadata = Get-EgN11ProcessMetadata -ProcessId ([uint32]$candidatePid) -TimeoutMilliseconds $script:EgObserverMetadataTimeoutMilliseconds
            if ($metadata.TimedOut -or $metadata.ProviderFailed) {
                # Genuine provider uncertainty: a late or busy provider is never evidence.
                $script:EgState.observer_failed = $true
                continue
            }
            if (-not $metadata.Succeeded) {
                # No provider row: absent only when the held handle proves the candidate exited.
                $exited = [EnergyGridOneShotSupervisorNative]::GetProcessLive($candidate.Handle)
                if (-not ($exited.Succeeded -and -not $exited.Live)) {
                    $script:EgState.observer_failed = $true
                }
                continue
            }
            if ([uint32]$metadata.ParentProcessId -ne $LauncherPid) { continue }
            if ($metadata.CommandLine -cne $expectedCommandLine) { continue }
            $launcherLiveAgain = [EnergyGridOneShotSupervisorNative]::GetProcessLive($LauncherHandle)
            $candidateLiveAgain = [EnergyGridOneShotSupervisorNative]::GetProcessLive($candidate.Handle)
            $membershipAgain = [EnergyGridOneShotSupervisorNative]::CheckMembership(
                $candidate.Handle, $JobHandle)
            if (-not $launcherLiveAgain.Succeeded -or
                -not $candidateLiveAgain.Succeeded -or
                -not $membershipAgain.Succeeded) {
                $script:EgState.observer_failed = $true
                continue
            }
            if (-not $launcherLiveAgain.Live -or -not $candidateLiveAgain.Live -or
                -not $membershipAgain.IsMember) { continue }
            $elapsed = [Math]::Floor((([System.Diagnostics.Stopwatch]::GetTimestamp() -
                $StartTicks) * 1000.0) / [System.Diagnostics.Stopwatch]::Frequency)
            $script:EgState.application_child_observation_elapsed_ms = [int64]$elapsed
            return $true
        }
        finally {
            Close-EgHandle -Handle $candidate.Handle
        }
    }
    return $false
}

function Test-EgN7ApplicationChild {
    param(
        [Parameter(Mandatory = $true)][IntPtr]$JobHandle,
        [Parameter(Mandatory = $true)][IntPtr]$LauncherHandle,
        [Parameter(Mandatory = $true)][uint32]$LauncherPid,
        [Parameter(Mandatory = $true)][long]$StartTicks
    )

    $observerDeadline = [int64]([System.Diagnostics.Stopwatch]::GetTimestamp() +
        [int64]([System.Diagnostics.Stopwatch]::Frequency / 20))
    $supervisorDeadline = Get-EgDeadlineTicks -StartTicks $StartTicks -Seconds $TimeoutSeconds
    $pidResult = [EnergyGridOneShotSupervisorNative]::GetProcessIds($JobHandle)
    if (-not $pidResult.Succeeded) {
        $script:EgState.observer_failed = $true
        return $false
    }
    $expectedImage = [System.IO.Path]::GetFullPath($script:EgPythonExeNormal)
    $expectedCommandLine = Get-EgCanonicalApplicationCommandLine
    foreach ($candidatePid in $pidResult.ProcessIds) {
        if ([System.Diagnostics.Stopwatch]::GetTimestamp() -ge $observerDeadline) { break }
        if ([uint32]$candidatePid -eq $LauncherPid) { continue }
        $candidate = [EnergyGridOneShotSupervisorNative]::OpenQueryProcess([uint32]$candidatePid)
        if (-not $candidate.Succeeded) {
            # A listed descendant that has already exited and been released is absent, not
            # unprovable. Every other open failure remains an observer failure.
            if ($candidate.ErrorCode -ne $script:EgObserverProcessGoneErrorCode) {
                $script:EgState.observer_failed = $true
            }
            continue
        }
        try {
            $live = [EnergyGridOneShotSupervisorNative]::GetProcessLive($candidate.Handle)
            if (-not $live.Succeeded) {
                $script:EgState.observer_failed = $true
                continue
            }
            if (-not $live.Live) { continue }
            $membership = [EnergyGridOneShotSupervisorNative]::CheckMembership(
                $candidate.Handle, $JobHandle)
            if (-not $membership.Succeeded) {
                $script:EgState.observer_failed = $true
                continue
            }
            if (-not $membership.IsMember) { continue }
            $launcherLive = [EnergyGridOneShotSupervisorNative]::GetProcessLive($LauncherHandle)
            if (-not $launcherLive.Succeeded) {
                $script:EgState.observer_failed = $true
                continue
            }
            if (-not $launcherLive.Live) { continue }
            $image = [EnergyGridOneShotSupervisorNative]::GetImage($candidate.Handle)
            if (-not $image.Succeeded) {
                # Only a candidate proven exited through the held handle is absent.
                $exited = [EnergyGridOneShotSupervisorNative]::GetProcessLive($candidate.Handle)
                if (-not ($exited.Succeeded -and -not $exited.Live)) {
                    $script:EgState.observer_failed = $true
                }
                continue
            }
            if ([System.StringComparer]::OrdinalIgnoreCase.Equals(
                [System.IO.Path]::GetFullPath($image.ImagePath), $expectedImage) -eq $false) { continue }
            # A provider query is started only when its whole bounded budget fits before the
            # supervisor deadline, so observation can never outlive the one-shot deadline.
            $remainingMilliseconds = [Math]::Floor((($supervisorDeadline -
                [System.Diagnostics.Stopwatch]::GetTimestamp()) * 1000.0) /
                [System.Diagnostics.Stopwatch]::Frequency)
            if ($remainingMilliseconds -lt $script:EgObserverMetadataTimeoutMilliseconds) { break }
            $metadata = Get-EgN7ProcessMetadata -ProcessId ([uint32]$candidatePid) -TimeoutMilliseconds $script:EgObserverMetadataTimeoutMilliseconds
            if ($metadata.TimedOut -or $metadata.ProviderFailed) {
                # Genuine provider uncertainty: a late or busy provider is never evidence.
                $script:EgState.observer_failed = $true
                continue
            }
            if (-not $metadata.Succeeded) {
                # No provider row: absent only when the held handle proves the candidate exited.
                $exited = [EnergyGridOneShotSupervisorNative]::GetProcessLive($candidate.Handle)
                if (-not ($exited.Succeeded -and -not $exited.Live)) {
                    $script:EgState.observer_failed = $true
                }
                continue
            }
            if ([uint32]$metadata.ParentProcessId -ne $LauncherPid) { continue }
            if ($metadata.CommandLine -cne $expectedCommandLine) { continue }
            $launcherLiveAgain = [EnergyGridOneShotSupervisorNative]::GetProcessLive($LauncherHandle)
            $candidateLiveAgain = [EnergyGridOneShotSupervisorNative]::GetProcessLive($candidate.Handle)
            $membershipAgain = [EnergyGridOneShotSupervisorNative]::CheckMembership(
                $candidate.Handle, $JobHandle)
            if (-not $launcherLiveAgain.Succeeded -or
                -not $candidateLiveAgain.Succeeded -or
                -not $membershipAgain.Succeeded) {
                $script:EgState.observer_failed = $true
                continue
            }
            if (-not $launcherLiveAgain.Live -or -not $candidateLiveAgain.Live -or
                -not $membershipAgain.IsMember) { continue }
            $elapsed = [Math]::Floor((([System.Diagnostics.Stopwatch]::GetTimestamp() -
                $StartTicks) * 1000.0) / [System.Diagnostics.Stopwatch]::Frequency)
            $script:EgState.application_child_observation_elapsed_ms = [int64]$elapsed
            return $true
        }
        finally {
            Close-EgHandle -Handle $candidate.Handle
        }
    }
    return $false
}
