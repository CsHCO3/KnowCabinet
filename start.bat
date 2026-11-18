@echo off
chcp 936 >nul
cd /d "%~dp0"
set "PY=D:\Anaconda\envs\infosystem\python.exe"

if not exist "%PY%" (
    echo [错误] 找不到 conda 环境 infosystem 的 python: %PY%
    echo        请确认 D:\Anaconda\envs\infosystem\ 存在，或用 APP_PYTHON 覆盖。
    pause
    exit /b 1
)

echo ========================================
echo  infosystem Web 服务后台启动器
echo ========================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0server.ps1" start

echo.
echo 服务已在后台运行（本窗口可关闭，服务不受影响）。
echo.
echo 常用管理:
echo     bash server.sh status   查看状态
echo     bash server.sh log      查看日志  data/.server.log
echo     bash server.sh stop     停止服务
echo  或直接访问 http://127.0.0.1:8765
echo.
pause
