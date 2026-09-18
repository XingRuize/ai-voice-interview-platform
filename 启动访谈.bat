@echo off
cd /d "%~dp0"
where python.exe >nul 2>nul
if errorlevel 1 (
  echo Python was not found.
  pause
  exit /b 1
)
powershell -NoProfile -Command "try { if((Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8765/ -TimeoutSec 2).StatusCode -eq 200){ exit 0 } } catch {}; exit 1"
if not errorlevel 1 (
  start "" http://127.0.0.1:8765/
  exit /b 0
)
start "AI Interview Web" python.exe "%~dp0web_app.py"
exit /b 0
