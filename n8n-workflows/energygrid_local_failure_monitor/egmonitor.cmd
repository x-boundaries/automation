@echo off
rem EnergyGrid run monitor hook (#226): report ONE finished run folder to the local n8n monitor, at most once.
rem Usage: egmonitor.cmd <run folder printed by "egnative.cmd new-run" or by the native daily run>
rem Reads only acquire.json + status.json in that folder; never runs Core commands, never retries.
rem EG_HOME is the folder above this script's own monitor folder, so the same file serves every install
rem (for example the protected C:\Program Files\EnergyGrid) without editing.
setlocal
for %%I in ("%~dp0..") do set "EG_HOME=%%~fI"
set "PYTHONIOENCODING=utf-8"
"%EG_HOME%\runtime\venv\Scripts\python.exe" -I "%EG_HOME%\monitor\energygrid_monitor_report.py" --run-dir "%~1" --runs-root "%EG_HOME%\data\runs" --token-file "%USERPROFILE%\.energygrid-monitor\webhook-token.txt"
echo EXIT=%ERRORLEVEL%
exit /b 0
