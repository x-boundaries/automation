# Energy@Grid scheduler handoff shape only.
#
# This file is intentionally inert. It does not register, start, alter, or remove
# a Scheduled Task, provision credentials, choose an account, or install a browser.
# A later owner-approved handoff must replace these placeholders and separately
# confirm the Windows account, external config path, and runtime directories.

# Intended daily action shape (documentation only):
#   Working directory: <CHECKOUT_ROOT>\energygrid-bill-downloader
#   Executable:        <PYTHON_3_12_EXE>
#   Arguments:         -m energygrid_bill_downloader run --config <EXTERNAL_CONFIG_JSON>
#   Output capture:    <EXTERNAL_LOG_ROOT>\scheduler.stdout.log / scheduler.stderr.log
#   Overlap policy:    do not start a second instance while one is running
#   Missed run policy: run once after restart if the owner approves that policy
#
# Credential values must be injected by a later approved host mechanism. They are
# never placed in this example or passed as command-line arguments.

# Template only; no scheduler action is defined or performed here.
