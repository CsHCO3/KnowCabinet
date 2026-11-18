"""InfoSystem Web 服务入口（唯一入口；Web 与 CLI 并行独立）。

前台调试（Ctrl+C 关闭）:  python run_api.py --reload
后台托管（推荐）:        bash server.sh start    -> http://127.0.0.1:8765
分离启动（内部用）:      python run_api.py --launch
Windows 双击:            start.bat  /  stop.bat

环境变量（命令行参数优先）：
  APP_HOST     默认 127.0.0.1
  APP_PORT     默认 8765
  APP_WORKERS  默认 1（多 worker 会各自持有一份进程内任务状态，见 server.ps1）
  APP_RELOAD   默认 0（托管/生产必须为 0）
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def _serve(args) -> int:
    import uvicorn

    if args.reload and args.workers > 1:
        # uvicorn 约束：reload 与多进程互斥（reload 自身会 spawn 子进程）
        print("提示：reload 与多进程互斥，已把 workers 降为 1")
        args.workers = 1
    if args.reload:
        print("警告：reload=True 是开发模式（监听文件变化、开销大），托管请去掉 --reload")
    print(f"InfoSystem Web -> http://{args.host}:{args.port}"
          f"  workers={args.workers}  reload={args.reload}")
    uvicorn.run(
        "api.main:app",          # 必须字符串：多进程下子进程要能自行 import
        host=args.host,
        port=args.port,
        workers=args.workers,
        reload=args.reload,
    )
    return 0


def _launch(args) -> int:
    """分离启动本服务（父进程立刻返回，服务独立存活）。

    Windows: DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP —— 新进程不带控制台、
    不继承 stdout 管道，因此 Git Bash / 双击窗口关掉都不会带走它。
    """
    data_dir = ROOT / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    log_path = data_dir / ".server.log"
    err_path = data_dir / ".server.err.log"
    pid_path = data_dir / ".server.pid"

    cmd = [sys.executable, "-u", str(ROOT / "run_api.py"),
           "--host", args.host, "--port", str(args.port),
           "--workers", str(args.workers)]
    if args.reload:
        cmd.append("--reload")

    creationflags = 0
    start_new_session = False
    if os.name == "nt":
        creationflags = (subprocess.DETACHED_PROCESS
                         | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        start_new_session = True

    # 强制子进程 UTF-8 输出：否则 Windows 下中文日志会被按 GBK 写出，
    # 父进程（PowerShell，UTF-8）读回来就是乱码。
    child_env = dict(os.environ)
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["PYTHONUTF8"] = "1"

    with open(log_path, "wb") as out, open(err_path, "wb") as err:
        proc = subprocess.Popen(
            cmd, cwd=str(ROOT), stdin=subprocess.DEVNULL,
            stdout=out, stderr=err, close_fds=True, env=child_env,
            creationflags=creationflags, start_new_session=start_new_session,
        )
    pid_path.write_text(str(proc.pid), encoding="ascii")
    # 这里刻意用纯 ASCII：本进程 stdout 可能是管道，Windows 会按 ANSI 写出，
    # 父进程再按 UTF-8 读就是乱码。英文输出在任何编码下都安全。
    print(f"[launch] detached, PID {proc.pid} -> http://{args.host}:{args.port}")
    print(f"[launch] log: {log_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="InfoSystem Web 服务")
    parser.add_argument("--host", default=os.environ.get("APP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=_env_int("APP_PORT", 8765))
    parser.add_argument("--workers", type=int, default=_env_int("APP_WORKERS", 1),
                        help="uvicorn 工作进程数（默认 1；>1 各自持有一份进程内任务状态）")
    parser.add_argument("--reload", action="store_true",
                        default=os.environ.get("APP_RELOAD", "0") not in ("", "0", "false"),
                        help="开发热重载；会 spawn 子进程，托管服务禁用")
    parser.add_argument("--launch", action="store_true",
                        help="分离启动（后台托管用；父进程立刻退出）")
    args = parser.parse_args()

    if args.launch:
        return _launch(args)
    return _serve(args)


if __name__ == "__main__":
    sys.exit(main())
