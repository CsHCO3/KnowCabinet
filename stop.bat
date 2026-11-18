@echo off
chcp 936 >nul
cd /d "%~dp0"
echo ========================================
echo  infosystem Í£Ö¹Æ÷
echo ========================================
echo.
echo  Í£Ö¹ Web ·þÎñ: http://127.0.0.1:8765
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0server.ps1" stop

echo.
pause
