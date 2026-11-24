@echo off
REM Stops the running AI Trader cleanly (graceful API shutdown, then terminate, then kill).
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist ".venv\Scripts\python.exe" (
  echo No .venv found, so the app was never set up here. Nothing to stop.
  pause
  exit /b 0
)
.venv\Scripts\python.exe scripts\trader.py stop
pause
