@echo off
cd /d "%~dp0"
if not exist "services\CosyVoice\runtime\python\fastapi\server.py" (
  echo CosyVoice source is missing. Initializing the Git submodule...
  git submodule update --init --recursive
  if errorlevel 1 (
    echo Failed to download CosyVoice source. Check Git and network access.
    pause
    exit /b 1
  )
)
echo 正在构建并启动 CosyVoice 与 FunASR，第一次需要联网下载，可能需要较长时间。
docker compose up -d --build
echo.
echo 完成后请运行“检查语音模型状态.bat”。
pause
