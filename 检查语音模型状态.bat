@echo off
cd /d "%~dp0"
docker compose ps
echo.
echo ===== CosyVoice 最近日志 =====
docker logs --tail 30 clinical-cosyvoice
echo.
echo ===== FunASR 最近日志 =====
docker logs --tail 30 clinical-funasr
pause
