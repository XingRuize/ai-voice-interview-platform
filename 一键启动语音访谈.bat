@echo off
cd /d "%~dp0"
set "PYTHON_EXE="
for /f "delims=" %%P in ('py -3.11 -c "import sys; print(sys.executable)" 2^>nul') do set "PYTHON_EXE=%%P"
if not defined PYTHON_EXE for /f "delims=" %%P in ('where python.exe 2^>nul') do if not defined PYTHON_EXE set "PYTHON_EXE=%%P"
if not defined PYTHON_EXE (
  echo Python was not found. Please run the installer first.
  pause
  exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_voice_services.ps1"
if errorlevel 1 (
  echo Docker voice services failed to start. Please open Docker Desktop first.
  pause
  exit /b 1
)

echo Waiting for local ASR and TTS services...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$deadline=(Get-Date).AddMinutes(5); do { $a=$false; $t=$false; try { $a=(Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8000/docs -TimeoutSec 2).StatusCode -eq 200 } catch {}; try { $t=(Invoke-WebRequest -UseBasicParsing http://127.0.0.1:50000/docs -TimeoutSec 2).StatusCode -eq 200 } catch {}; if($a -and $t){ exit 0 }; Start-Sleep -Seconds 3 } while((Get-Date) -lt $deadline); exit 1"
if errorlevel 1 (
  echo Voice services did not become ready within five minutes.
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
