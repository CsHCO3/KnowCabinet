"""统一任务执行器 — 线程池 + 取消 + 阶段更新（持久化到 tasks.json）。

worker 函数签名：fn(task_id, *args, **kwargs)，内部通过
api.services.task_store 更新阶段/状态，用 task_executor.is_cancelled 检查取消。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

import api.services.task_store as store

_executor = ThreadPoolExecutor(max_workers=4)
_cancel_flags: dict[str, bool] = {}
_cancel_lock = threading.Lock()


def submit(task_id: str, run_fn: Callable, *args: Any, **kwargs: Any) -> None:
    """提交任务到线程池。run_fn(task_id, *args, **kwargs)。"""
    _executor.submit(_run, task_id, run_fn, *args, **kwargs)


def _run(task_id: str, fn: Callable, *args: Any, **kwargs: Any) -> None:
    try:
        store.set_status(task_id, "running")
    except Exception as e:
        print(f"task {task_id} init failed: {e}")
        return
    try:
        fn(task_id, *args, **kwargs)
    except Exception as e:
        print(f"task {task_id} failed: {e}")
        store.set_status(task_id, "failed", error=str(e))
    finally:
        # 取消优先于完成：任务结束后若已被取消，强制置 cancelled（避免 worker
        # 末尾的 completed 覆盖掉 cancel 设置的 cancelled）。
        if is_cancelled(task_id):
            store.set_status(task_id, "cancelled")
        else:
            task = store.get(task_id)
            if task and task.status == "running":
                store.set_status(task_id, "completed")


def cancel(task_id: str) -> bool:
    with _cancel_lock:
        if store.get(task_id) is None:
            return False
        _cancel_flags[task_id] = True
        store.set_status(task_id, "cancelled")
        return True


def is_cancelled(task_id: str) -> bool:
    return _cancel_flags.get(task_id, False)


def mark_interrupted_on_startup() -> int:
    """启动时把 running/pending 任务标为 failed（服务重启中断）。返回数量。"""
    n = 0
    for t in store.list_tasks(limit=10000):
        if t.status in ("running", "pending"):
            store.set_status(t.id, "failed", error="服务重启，任务中断（可重试）")
            n += 1
    return n
