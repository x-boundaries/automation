@echo off
rem EnergyGrid run monitor hook (#226): report ONE finished run folder to the local n8n monitor, at most once.
rem Usage: egmonitor.cmd <run folder printed by "egnative.cmd new-run">
rem Reads only acquire.json + status.json in that folder; never runs Core commands, never retries.
setlocal
set "EG_HOME=C:\XB\_ScheduledTask\EnergyGrid"
set "PYTHONIOENCODING=utf-8"
"%EG_HOME%\runtime\venv\Scripts\python.exe" -I "%EG_HOME%\monitor\energygrid_monitor_report.py" --run-dir "%~1" --runs-root "%EG_HOME%\data\runs" --token-file "%USERPROFILE%\.energygrid-monitor\webhook-token.txt"
echo EXIT=%ERRORLEVEL%
exit /b 0
