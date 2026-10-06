@echo off
rem EnergyGrid deterministic core shim (#226 G3). Installed alone in runtime\bin, which the
rem supervisor puts first on the Claude process PATH. It forwards at most four argument
rem slots plus one overflow sentinel to the supervisor's -CoreDispatch mode, which accepts
rem only the eleven exact reviewed command forms and refuses everything else with exit 64.
rem It reads no file, names no path except its own sibling supervisor, and holds no secret.
rem The authority boundary is the exact Claude permission allowlist plus the supervisor's
rem transcript audit and the core's planned-action check; this shim is defence in depth.
setlocal DisableDelayedExpansion
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0..\claude_supervisor.ps1" -SettingsPath "%EGCORE_SUPERVISOR_SETTINGS%" -CoreDispatch -CoreArg1 "%~1" -CoreArg2 "%~2" -CoreArg3 "%~3" -CoreArg4 "%~4" -CoreArgOverflow "%~5"
exit /b %ERRORLEVEL%
