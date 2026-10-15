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
"%PYTHON_EXE%" -m pip install -r requirements-client.txt
pause
