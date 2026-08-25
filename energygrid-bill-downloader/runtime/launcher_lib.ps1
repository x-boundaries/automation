# Energy@Grid runtime launcher library.
#
# Design lock: DL-XB-141-RUNTIME-005-SOURCE-DURABILITY.
# Controlling specification: energygrid-bill-downloader/docs/runtime_source_durability_design.md
# Controlling plan: energygrid-bill-downloader/docs/runtime_source_durability_implementation_plan.md
#
# This file is a PURE, DOT-SOURCEABLE LIBRARY (EGRT-I01). It performs no filesystem
# mutation at import time, no network access, no Git invocation at load, and no live
# action. Every function is deterministic given its inputs and its explicitly supplied
# paths, which is what makes the offline test suite possible. It mirrors the separation
# already established by scripts/member_create_uat_runner_lib.ps1.
#
# The dependency direction is one-way: entry scripts dot-source this library, and this
# library never dot-sources an entry script and never reads a caller's $PSScriptRoot.
#
# Compatibility boundary: every construct here must execute correctly under Windows
# PowerShell 5.1 on .NET Framework 4.x (PSEdition Desktop), which is the production
# runtime. The prohibited PowerShell 7 only constructs are guarded statically by the
# project test suite.
#
# Nothing private is committed here. No credential value, account identity, private
# absolute path, UNC path, host identity, or principal identity appears in this file.

Set-StrictMode -Version Latest

function Get-EgLauncherLibraryContract {
    # The library's own bounded self-description. Pure; no side effect.
    [CmdletBinding()]
    param()

    [pscustomobject]@{
        SchemaVersion       = 'eg_launcher_lib/v1'
        DeployedMemberNames = @('launcher.ps1', 'launcher_lib.ps1', 'installation_manifest.json')
    }
}
