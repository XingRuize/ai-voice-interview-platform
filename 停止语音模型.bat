@echo off
cd /d "%~dp0"
docker compose stop
where ollama.exe >nul 2>nul
if not errorlevel 1 ollama.exe stop qwen3:4b >nul 2>nul
pause
