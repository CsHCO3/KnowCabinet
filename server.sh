#!/usr/bin/env bash
# infosystem Web 服务管理入口（Git Bash / WSL 通用）
# 真正的实现是 server.ps1 + run_api.py --launch；本脚本只做路径换算与转发。
# 用法: bash server.sh start|stop|restart|status|log
# 环境变量: APP_PORT / APP_WORKERS / APP_PYTHON 会传给 server.ps1
#
# 不再需要 server_start.cmd：分离启动由 run_api.py --launch（DETACHED_PROCESS）
# 完成，父进程立刻返回，服务不继承 bash 的 stdout 管道，关窗口也不会被杀。
cd "$(dirname "$0")" || exit 1

PORT="${APP_PORT:-${PORT:-8765}}"
export APP_PORT="$PORT"

if command -v powershell.exe >/dev/null 2>&1; then
  PS="powershell.exe"
else
  PS="/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
fi

if command -v wslpath >/dev/null 2>&1; then
  WIN_ROOT="$(wslpath -w "$(pwd)")"
else
  WIN_ROOT="$(cygpath -w "$(pwd)")"
fi
PS1_WIN="$WIN_ROOT\\server.ps1"

_port_pids() {
  "$PS" -NoProfile -Command \
    "Get-NetTCPConnection -LocalPort $PORT -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique" \
    2>/dev/null | tr -d '\r'
}

run_ps() {
  "$PS" -NoProfile -ExecutionPolicy Bypass -File "$PS1_WIN" "$1"
}

if [ ! -f "./server.ps1" ]; then
  echo "[错误] 找不到 server.ps1（应与本脚本同目录）"
  exit 1
fi
if [ ! -f "./run_api.py" ]; then
  echo "[错误] 找不到 run_api.py（应与本脚本同目录）"
  exit 1
fi

case "${1:-status}" in
  start)
    if [ -n "$(_port_pids)" ]; then
      echo "端口 $PORT 已被占用 (PID: $(_port_pids | tr '\n' ' ')) — 先 stop 再 start"
      exit 1
    fi
    run_ps start
    ;;
  restart)
    run_ps stop
    sleep 1
    run_ps start
    ;;
  stop|status|log)
    run_ps "$1"
    ;;
  *)
    echo "用法: bash server.sh start|stop|restart|status|log"
    exit 1
    ;;
esac
