# Test-only in-memory AutoCount double for the member-write v2 primitive.
# Synthetic data only. Never part of the installable package.
#
# New-XbFakeAutoCountBook builds a Member table from a case object and returns
# the SessionFactory / MemberCommandFactory seams the adapter already accepts.

Set-StrictMode -Version Latest

$script:XbFakeColumns = @(
    "MemberNo", "MemberType", "Name", "MobilePhone", "EmailAddress", "DOB", "RegisterDate", "ExpiryDate",
    "OpeningPoints", "IsActive", "Individual", "Guid", "CreatedUserID", "CreatedTime"
)

function Get-XbFakeValue {
    param($Object, [string]$Name, $Default = $null)
    if ($null -eq $Object) { return $Default }
    if ($Object -is [System.Collections.IDictionary]) { if ($Object.Contains($Name)) { return $Object[$Name] } ; return $Default }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $Default }
    return $property.Value
}

function New-XbFakeTable {
    param([string[]]$Columns = $script:XbFakeColumns)
    $table = New-Object System.Data.DataTable
    foreach ($column in $Columns) { [void]$table.Columns.Add($column, [object]) }
    return ,$table
}

function ConvertTo-XbFakeDate {
    param([string]$Text)
    return [datetime]::ParseExact($Text, "yyyy-MM-dd", [Globalization.CultureInfo]::InvariantCulture)
}

function Add-XbFakeRow {
    param([Parameter(Mandatory)][System.Data.DataTable]$Table, [Parameter(Mandatory)][hashtable]$Values)
    $row = $Table.NewRow()
    foreach ($column in $script:XbFakeColumns) {
        if (-not $Table.Columns.Contains($column)) { continue }
        if ($Values.ContainsKey($column) -and $null -ne $Values[$column]) { $row[$column] = $Values[$column] } else { $row[$column] = [DBNull]::Value }
    }
    [void]$Table.Rows.Add($row)
    return $row
}

function New-XbFakeAutoCountBook {
    param(
        [Parameter(Mandatory)]$Case,
        [Parameter(Mandatory)]$Request,
        [Parameter(Mandatory)][DateTimeOffset]$ServerNow,
        [Parameter(Mandatory)][string]$IntegrationUserId,
        [Parameter(Mandatory)][string]$DatabaseName
    )
    $firstClaimed = [DateTimeOffset]::Parse([string]$Request.first_claimed_at, [Globalization.CultureInfo]::InvariantCulture)
    $state = @{
        table = (New-XbFakeTable)
        saves = 0
        getmember = 0
        probe_calls = 0
        new_member = 0
        session_opened = 0
        save_mode = [string](Get-XbFakeValue $Case "save_mode" "normal")
        readback_mode = [string](Get-XbFakeValue $Case "readback_mode" "normal")
        readback_created_time_mode = [string](Get-XbFakeValue $Case "readback_created_time_mode" "normal")
        readback_created_user_mode = [string](Get-XbFakeValue $Case "readback_created_user_mode" "normal")
        probe_mode = [string](Get-XbFakeValue $Case "probe_mode" "normal")
        prior_attempt_window_start_local = $firstClaimed.AddMinutes(-10).ToLocalTime().DateTime
        new_member_mode = [string](Get-XbFakeValue $Case "new_member_mode" "normal")
        iu = $IntegrationUserId
        created_guid = [string](Get-XbFakeValue $Case "created_guid" "0f1e2d3c-4b5a-4968-8778-a1b2c3d4e5f6")
        now_local = $ServerNow.ToLocalTime().DateTime
        request = $Request
    }
    $index = 0
    foreach ($spec in @(Get-XbFakeValue $Case "rows" @())) {
        $index++
        $values = @{}
        if ([bool](Get-XbFakeValue $spec "from_request" $false)) {
            $values.MemberType = "Default"
            $values.Name = [string]$Request.name
            $values.MobilePhone = [string]$Request.base_member_no
            $values.EmailAddress = [string]$Request.email
            $values.DOB = ConvertTo-XbFakeDate $Request.DOB
            $values.RegisterDate = ConvertTo-XbFakeDate $Request.RegisterDate
            $values.ExpiryDate = ConvertTo-XbFakeDate $Request.ExpiryDate
            $values.OpeningPoints = [decimal]0
            $values.IsActive = "T"
            $values.Individual = "T"
        }
        foreach ($field in @("MemberNo", "Name", "MobilePhone", "EmailAddress", "IsActive", "CreatedUserID")) {
            $value = Get-XbFakeValue $spec $field $null
            if ($null -ne $value) { $values[$field] = [string]$value }
        }
        if (-not $values.ContainsKey("IsActive")) { $values.IsActive = "T" }
        if (-not $values.ContainsKey("CreatedUserID")) { $values.CreatedUserID = "STAFF_PLACEHOLDER" }
        $guid = Get-XbFakeValue $spec "Guid" $null
        $values.Guid = $(if ($null -eq $guid) { "00000000-0000-4000-8000-{0:d12}" -f $index } elseif ([string]$guid -eq "") { $null } else { [string]$guid })
        $timeMode = [string](Get-XbFakeValue $spec "created_time_mode" "recent")
        switch ($timeMode) {
            "null" { $values.CreatedTime = $null }
            "dbnull" { $values.CreatedTime = [DBNull]::Value }
            "malformed" { $values.CreatedTime = "not-a-date" }
            "non_datetime" { $values.CreatedTime = [long]123 }
            default {
                if ($timeMode -cne "recent") { throw "fake_created_time_mode_invalid" }
                $minutes = Get-XbFakeValue $spec "created_minutes" -100000
                $values.CreatedTime = $firstClaimed.AddMinutes([double]$minutes).ToLocalTime().DateTime
            }
        }
        [void](Add-XbFakeRow -Table $state.table -Values $values)
    }

    $command = [pscustomobject]@{ State = $state }
    [void]($command | Add-Member -MemberType ScriptMethod -Name LoadBrowseTable -Value {
        $this.State.probe_calls = [int]$this.State.probe_calls + 1
        if ($this.State.probe_mode -ceq "fail") { throw "fake_probe_failure" }
        if ($this.State.probe_mode -ceq "missing_column") {
            $copy = $this.State.table.Copy()
            $copy.Columns.Remove("CreatedUserID")
            return ,$copy
        }
        if ($this.State.probe_mode -ceq "missing_created_time") {
            $copy = $this.State.table.Copy()
            $copy.Columns.Remove("CreatedTime")
            return ,$copy
        }
        return ,($this.State.table.Copy())
    })
    [void]($command | Add-Member -MemberType ScriptMethod -Name NewMember -Value {
        param([bool]$Unused)
        $this.State.new_member = [int]$this.State.new_member + 1
        if ($this.State.new_member_mode -ceq "fail") { throw "fake_new_member_failure" }
        $template = New-XbFakeTable -Columns @("MemberNo", "MemberType", "Name", "MobilePhone", "EmailAddress", "DOB", "RegisterDate", "ExpiryDate", "OpeningPoints", "IsActive", "Individual")
        $row = $template.NewRow()
        [void]$template.Rows.Add($row)
        return [pscustomobject]@{ Row = $row }
    })
    [void]($command | Add-Member -MemberType ScriptMethod -Name SaveMember -Value {
        param($Entity)
        $s = $this.State
        $s.saves = [int]$s.saves + 1
        $source = $Entity.Row
        $values = @{}
        foreach ($column in $source.Table.Columns) { $values[$column.ColumnName] = $source[$column.ColumnName] }
        $values.Guid = $s.created_guid
        $values.CreatedUserID = $s.iu
        $values.CreatedTime = $s.now_local
        $mode = $s.save_mode
        if ($mode -ceq "commit_mutated" -or $mode -ceq "commit_mutated_throw") { $values.Name = "Mutated By Save" }
        if ($mode -ceq "commit_foreign_user") { $values.CreatedUserID = "OTHER_USER_PLACEHOLDER" }
        if ($mode -ceq "commit_no_guid") { $values.Guid = $null }
        if ($mode -ceq "throw_foreign") {
            $values.CreatedUserID = "OTHER_USER_PLACEHOLDER"
            $values.Guid = "11111111-2222-4333-8444-555555555555"
            [void](Add-XbFakeRow -Table $s.table -Values $values)
            throw "fake_duplicate_member_no"
        }
        if ($mode -ceq "throw_no_commit") { throw "fake_save_failure" }
        if ($mode -ceq "vanish") { return }
        [void](Add-XbFakeRow -Table $s.table -Values $values)
        if ($mode -ceq "commit_plus_staff_dup") {
            [void](Add-XbFakeRow -Table $s.table -Values @{
                MemberNo = "STAFFDUP1"; Name = [string]$s.request.name; MobilePhone = [string]$s.request.base_member_no
                EmailAddress = "staff.dup@example.test"; IsActive = "T"; CreatedUserID = "STAFF_PLACEHOLDER"
                Guid = "22222222-3333-4444-8555-666666666666"; CreatedTime = $s.now_local
            })
        }
        if ($mode -ceq "throw_after_commit" -or $mode -ceq "commit_mutated_throw") { throw "fake_error_after_commit" }
    })
    [void]($command | Add-Member -MemberType ScriptMethod -Name GetMember -Value {
        param([string]$MemberNo)
        $this.State.getmember = [int]$this.State.getmember + 1
        if ($this.State.readback_mode -ceq "fail" -and [int]$this.State.saves -gt 0) { throw "fake_readback_failure" }
        foreach ($row in $this.State.table.Rows) {
            if ([string]$row["MemberNo"] -eq $MemberNo) {
                if ($this.State.saves -eq 0 -and $this.State.readback_created_user_mode -cne "normal") {
                    switch ($this.State.readback_created_user_mode) {
                        "wrong" { $row["CreatedUserID"] = "OTHER_USER_PLACEHOLDER" }
                        default { throw "fake_readback_created_user_mode_invalid" }
                    }
                }
                if ($this.State.saves -eq 0 -and $this.State.readback_created_time_mode -cne "normal") {
                    switch ($this.State.readback_created_time_mode) {
                        "null" { $row["CreatedTime"] = [DBNull]::Value }
                        "malformed" { $row["CreatedTime"] = "not-a-date" }
                        "old" { $row["CreatedTime"] = $this.State.prior_attempt_window_start_local.AddMinutes(-1) }
                        "missing" { [void]$row.Table.Columns.Remove("CreatedTime") }
                        default { throw "fake_readback_created_time_mode_invalid" }
                    }
                }
                return [pscustomobject]@{ Row = $row }
            }
        }
        return $null
    })

    $memberFactory = { param($Session) return $command }.GetNewClosure()
    $sessionMode = [string](Get-XbFakeValue $Case "session_mode" "normal")
    $sessionDatabase = [string](Get-XbFakeValue $Case "session_database" $DatabaseName)
    $sessionUser = [string](Get-XbFakeValue $Case "session_user" $IntegrationUserId)
    $sessionFactory = {
        $state.session_opened = [int]$state.session_opened + 1
        if ($sessionMode -ceq "fail") { throw "fake_session_failure" }
        [pscustomobject]@{
            UserSession = [pscustomobject]@{ Kind = "fake-user-session" }
            DBSetting = [pscustomobject]@{ Kind = "fake-db-setting" }
            MemberCommandFactory = $memberFactory
            DatabaseName = $sessionDatabase
            LoginUserId = $sessionUser
        }
    }.GetNewClosure()
    [pscustomobject]@{ State = $state; SessionFactory = $sessionFactory; MemberCommandFactory = $memberFactory }
}
