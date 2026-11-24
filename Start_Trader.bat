@echo off
setlocal EnableDelayedExpansion
REM Double-click on Windows. First run installs everything; later runs just start.
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist ".venv\Scripts\python.exe" goto setup
if not exist "web\out" goto setup
if not exist "web\node_modules" goto setup
goto run
:setup
echo == First-time setup: Python venv, dependencies, dashboard build ==
REM The Webull MCP package supports Python 3.10-3.12 only; never fall back to a newer Python.
if not exist ".venv\Scripts\python.exe" (
  set "PYV="
  for %%V in (3.12 3.11 3.10) do if not defined PYV py -%%V -c "import sys" >nul 2>nul && set "PYV=%%V"
  if not defined PYV (
    echo Python 3.10, 3.11 or 3.12 is required. Install Python 3.12 from python.org and run again.
    pause
    exit /b 1
  )
  py -!PYV! -m venv .venv
)
.venv\Scripts\python.exe -c "import sys;sys.exit(0 if (3,10)<=sys.version_info[:2]<=(3,12) else 1)" || (
  echo The .venv uses a Python version the Webull package does not support. Delete the .venv folder and run again with Python 3.12.
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m pip install --upgrade pip && .venv\Scripts\python.exe -m pip install -r requirements.txt && .venv\Scripts\python.exe scripts\trader.py setup
if errorlevel 1 (
  echo Setup failed.
  pause
  exit /b 1
)
:run
REM Open the browser only once the server answers (poll /api/status, ~1s interval, 60s max).
start "" /min powershell -NoProfile -WindowStyle Hidden -Command "for($i=0;$i -lt 60;$i++){try{Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 http://127.0.0.1:8000/api/status | Out-Null; Start-Process http://127.0.0.1:8000; break}catch{Start-Sleep -Seconds 1}}"
.venv\Scripts\python.exe scripts\trader.py run
pause