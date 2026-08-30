# Official local AutoCount boundary for the production member worker.
# This file is intentionally inert unless the worker supplies the explicit
# production adapter switch and a separately configured runtime session.

Set-StrictMode -Version Latest

function Assert-XbAutoCountAdapterEnabled {
    param([switch]$EnableProductionAdapter)
    if (-not $EnableProductionAdapter) {
        throw "autocount_adapter_disabled"
    }
    if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable("XB_AC2_ASSEMBLY_PATH", "Process"))) {
        throw "autocount_assembly_path_missing"
    }
}

function New-XbAutoCountSession {
    param([Parameter(Mandatory)][switch]$EnableProductionAdapter)
    Assert-XbAutoCountAdapterEnabled -EnableProductionAdapter:$EnableProductionAdapter
    $factoryName = [Environment]::GetEnvironmentVariable("XB_AC2_SESSION_FACTORY", "Process")
    if ([string]::IsNullOrWhiteSpace($factoryName)) {
        throw "autocount_session_factory_not_configured"
    }
    # The deployment binds a reviewed session factory outside Git. Runtime
    # credentials are never command-line arguments or repository values.
    throw "autocount_session_factory_binding_required"
}

function Get-XbAutoCountMember {
    param(
        [Parameter(Mandatory)]$Session,
        [Parameter(Mandatory)][string]$MemberNo
    )
    if ([string]::IsNullOrWhiteSpace($MemberNo)) { throw "member_no_required" }
    # The installed official surface is MemberCommand.Create(...).GetMember(...).
    $command = [AutoCount.BonusPoint.Member.MemberCommand]::Create($Session.UserSession, $Session.DBSetting)
    return $command.GetMember($MemberNo)
}

function Convert-XbAutoCountMemberRecord {
    param([Parameter(Mandatory)]$Entity)
    # Only the assigned/read-back contract is returned to the worker. The
    # worker must compare every field and must not persist arbitrary entity data.
    [ordered]@{
        MemberNo = [string]$Entity.MemberNo
        MemberType = [string]$Entity.MemberType
        Name = [string]$Entity.Name
        MobilePhone = [string]$Entity.MobilePhone
        EmailAddress = [string]$Entity.EmailAddress
        DOB = [string]$Entity.DOB
        RegisterDate = [string]$Entity.RegisterDate
        ExpiryDate = [string]$Entity.ExpiryDate
        OpeningPoints = [int]$Entity.OpeningPoints
        IsActive = [bool]$Entity.IsActive
        Individual = [bool]$Entity.Individual
    }
}

function New-XbAutoCountMember {
    param(
        [Parameter(Mandatory)]$Session,
        [Parameter(Mandatory)][hashtable]$Member,
        [Parameter(Mandatory)][switch]$EnableProductionAdapter
    )
    Assert-XbAutoCountAdapterEnabled -EnableProductionAdapter:$EnableProductionAdapter
    if ($Member.ContainsKey("IsActive") -or $Member.ContainsKey("Individual")) {
        throw "adapter_managed_defaults_are_not_caller_inputs"
    }
    $command = [AutoCount.BonusPoint.Member.MemberCommand]::Create($Session.UserSession, $Session.DBSetting)
    $entity = $command.NewMember($false)
    $entity.MemberNo = [string]$Member.MemberNo
    $entity.MemberType = "Default"
    $entity.Name = [string]$Member.Name
    $entity.MobilePhone = [string]$Member.MobilePhone
    $entity.EmailAddress = [string]$Member.EmailAddress
    $entity.DOB = [datetime]$Member.DOB
    $entity.RegisterDate = [datetime]$Member.RegisterDate
    $entity.ExpiryDate = [datetime]$Member.ExpiryDate
    $entity.OpeningPoints = 0
    # These values belong to the adapter's reviewed defaults, not the source.
    $entity.IsActive = $true
    $entity.Individual = $true

    $script:XbSaveMemberInvocationCount = 0
    $script:XbSaveMemberInvocationCount++
    if ($script:XbSaveMemberInvocationCount -ne 1) {
        throw "save_member_invocation_count_invalid"
    }
    # This is the sole irreversible member write call in the repository.
    $command.SaveMember($entity)
    return [pscustomobject]@{
        SaveInvocationCount = $script:XbSaveMemberInvocationCount
        ReadBack = (Convert-XbAutoCountMemberRecord -Entity ($command.GetMember([string]$Member.MemberNo)))
    }
}

function Compare-XbAutoCountMemberReadBack {
    param(
        [Parameter(Mandatory)][hashtable]$Expected,
        [Parameter(Mandatory)]$Actual
    )
    $actualValues = Convert-XbAutoCountMemberRecord -Entity $Actual
    $mismatches = @()
    foreach ($field in @("MemberNo", "MemberType", "Name", "MobilePhone", "EmailAddress", "DOB", "RegisterDate", "ExpiryDate", "OpeningPoints", "IsActive", "Individual")) {
        if ([string]$Expected[$field] -ne [string]$actualValues[$field]) { $mismatches += $field }
    }
    [pscustomobject]@{ Match = ($mismatches.Count -eq 0); Mismatches = $mismatches }
}
