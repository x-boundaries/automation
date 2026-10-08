# Energy@Grid scheduler handoff shape only.
#
# This file is intentionally inert. It does not register, start, alter, or remove
# a Scheduled Task, provision credentials, choose an account, or install a browser.
# A later owner-approved handoff must replace these placeholders and separately
# confirm the Windows account, external config path, and runtime directories.
#
# #226 G3 scheduled action (documentation only): the reviewed inert XML template
# task-scheduler/energygrid_daily.task.example.xml (Enabled=false, placeholders only)
# runs the absolute Windows PowerShell 5.1 path with -File on the installed
#   REPLACE_WITH_RUNTIME_ROOT\claude_supervisor.ps1 -SettingsPath <private settings>
# The supervisor checks its pinned Claude executable, reviewed prompt, Claude settings,
# empty MCP config, egcore.cmd and launcher manifest hashes, decrypts the DPAPI
# CurrentUser setup token, runs Claude in a kill-on-close Job Object, audits the
# transcript, and maps its own final deterministic core `status` to the exit code.
# The task never calls launcher.ps1, Python or Claude directly: the supervisor reaches
# the installed runtime/launcher.ps1 only through the eleven-command core allowlist, and
# the launcher still runs the accepted <PYTHON_3_14_EXE> interpreter.
#
# Policy: daily 08:00 +08:00, IgnoreNew, StartWhenAvailable, ExecutionTimeLimit PT30M,
# LeastPrivilege, LogonType Password (needed for DPAPI), never LocalSystem. A run
# principal holding SeTakeOwnershipPrivilege or SeRestorePrivilege fails the launcher's
# run-principal check by construction. The task stays disabled until the controlled
# end-to-end run and Owner UAT pass and a separate enable authority is given.
#
# Credential and token values are never placed in this example or passed as
# command-line arguments.
#
# Template only; no scheduler action is defined or performed here.
