@echo off
cd /d "%~dp0"
set "PYTHON_EXE="
for /f "delims=" %%P in ('py -3.11 -c "import sys; print(sys.executable)" 2^>nul') do set "PYTHON_EXE=%%P"
if not defined PYTHON_EXE for /f "delims=" %%P in ('where python.exe 2^>nul') do if not defined PYTHON_EXE set "PYTHON_EXE=%%P"
if not defined PYTHON_EXE (
  echo Python was not found.
  pause
  exit /b 1
)
powershell -NoProfile -Command "try { if((Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8765/ -TimeoutSec 2).StatusCode -eq 200){ exit 0 } } catch {}; exit 1"
if not errorlevel 1 (
  start "" http://127.0.0.1:8765/
  exit /b 0
)
start "AI Interview Web" "%PYTHON_EXE%" "%~dp0web_app.py"
exit /b 0
