# Energy@Grid scheduler handoff shape only.
#
# This file is intentionally inert. It does not register, start, alter, or remove
# a Scheduled Task, provision credentials, choose an account, or install a browser.
# A later owner-approved handoff must replace these placeholders and separately
# confirm the Windows account, external config path, and runtime directories.

# Intended daily action shape (documentation only):
#   Working directory: <CHECKOUT_ROOT>\energygrid-bill-downloader
#   Executable:        <PYTHON_3_14_EXE>
#   Arguments:         -m energygrid_bill_downloader run --config <EXTERNAL_CONFIG_JSON>
#   Output capture:    <EXTERNAL_LOG_ROOT>\scheduler.stdout.log / scheduler.stderr.log
#   Overlap policy:    do not start a second instance while one is running
#   Missed run policy: run once after restart if the owner approves that policy
#
# Eventual scheduled executable shape (documentation only): once the runtime layer is
# installed and separately approved, the scheduled action invokes the installed
# runtime/launcher.ps1 rather than the interpreter directly. The launcher is the only thing
# the Scheduled Task will ever invoke, and it supplies the private paths, the expected
# branch, and the authorised launcher-root write trustees as parameters. A run principal
# holding SeTakeOwnershipPrivilege or SeRestorePrivilege fails the launcher's run-principal
# check by construction, so LocalSystem is not a candidate run principal.
#
# Credential values must be injected by a later approved host mechanism. They are
# never placed in this example or passed as command-line arguments.

# DL-XB-199 G3-101: the reviewed direct-HTTP MVP task shape is the inert XML template
# task-scheduler/energygrid_daily.task.example.xml (Enabled=false, placeholders only):
# absolute powershell.exe path, the installed launcher.ps1 through -File so its exit code
# is preserved, a non-elevated LeastPrivilege run principal, MultipleInstances IgnoreNew,
# a hard ExecutionTimeLimit and StartWhenAvailable. The "Intended daily action shape"
# above predates the launcher and is superseded: Python is never scheduled directly.

# Template only; no scheduler action is defined or performed here.
