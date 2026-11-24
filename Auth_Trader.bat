@echo off
REM Webull login. Run once, and again whenever the token expires (about every 15 days).
REM When it says so, approve the request in the Webull mobile app.
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist ".venv\Scripts\python.exe" (
  echo No .venv found. Run Start_Trader.bat first to set up.
  pause
  exit /b 1
)
.venv\Scripts\python.exe scripts\trader.py auth
pause
