"""后台任务管理器 — 轻量级单进程异步任务执行。

支持提交、取消、状态查询。使用 ThreadPoolExecutor 单线程串行执行，
避免并发冲突。任务在进程重启后丢失（单进程可接受）。

用法:
    task_id = BiliTaskManager.submit("my_task", my_func, arg1, arg2)
    status = BiliTaskManager.get_status(task_id)
    BiliTaskManager.cancel(task_id)  # 取消任务
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Lock
from typing import Any, Callable


class BiliTaskManager:
    _executor = ThreadPoolExecutor(max_workers=2)
    _tasks: dict[str, dict] = {}
    _cancel_flags: dict[str, bool] = {}
    _lock = Lock()

    @classmethod
    def submit(
        cls,
        task_type: str,
        fn: Callable,
        *args: Any,
        task_id: str | None = None,
        **kwargs: Any,
    ) -> str:
        """提交一个后台任务。

        Args:
            task_type: 任务类型标签（如 "extract", "query"）。
            fn: 要执行的函数。
            task_id: 可选的任务 ID，默认自动生成。
            *args, **kwargs: 传给 fn 的参数。

        Returns:
            任务 ID。
        """
        tid = task_id or uuid.uuid4().hex[:12]
        with cls._lock:
            cls._tasks[tid] = {
                "id": tid,
                "type": task_type,
                "status": "pending",
                "progress": "",
                "created_at": datetime.now().isoformat(),
                "completed_at": None,
                "result": None,
                "error": None,
            }
        cls._executor.submit(cls._run, tid, fn, *args, **kwargs)
        return tid

    @classmethod
    def _run(cls, task_id: str, fn: Callable, *args, **kwargs) -> None:
        with cls._lock:
            if cls._tasks.get(task_id, {}).get("status") == "cancelled":
                return  # 队列中已被取消
            cls._tasks[task_id]["status"] = "running"

        try:
            result = fn(task_id, *args, **kwargs)
            with cls._lock:
                if cls._tasks.get(task_id, {}).get("status") == "cancelled":
                    return  # 运行中被取消
                cls._tasks[task_id]["status"] = "completed"
                cls._tasks[task_id]["result"] = result
                cls._tasks[task_id]["completed_at"] = datetime.now().isoformat()
        except Exception as e:
            with cls._lock:
                cls._tasks[task_id]["status"] = "failed"
                cls._tasks[task_id]["error"] = str(e)
                cls._tasks[task_id]["completed_at"] = datetime.now().isoformat()

    @classmethod
    def cancel(cls, task_id: str) -> bool:
        """取消任务。

        - pending 状态：直接从队列移除，不会执行。
        - running 状态：设置 cancel flag，由工作函数自行检查退出。
        - completed/failed 状态：拒绝取消。

        Returns:
            是否成功取消。
        """
        with cls._lock:
            task = cls._tasks.get(task_id)
            if not task:
                return False
            if task["status"] in ("completed", "failed", "cancelled"):
                return False
            cls._cancel_flags[task_id] = True
            task["status"] = "cancelled"
            task["completed_at"] = datetime.now().isoformat()
        return True

    @classmethod
    def _update_progress(cls, task_id: str, progress: str) -> None:
        """更新任务进度描述。"""
        with cls._lock:
            task = cls._tasks.get(task_id)
            if task:
                task["progress"] = progress

    @classmethod
    def is_cancelled(cls, task_id: str) -> bool:
        """工作函数调用此方法检查是否被取消。"""
        return cls._cancel_flags.get(task_id, False)

    @classmethod
    def get_status(cls, task_id: str) -> dict | None:
        """获取任务状态。"""
        with cls._lock:
            task = cls._tasks.get(task_id)
            if task:
                return dict(task)
            return None

    @classmethod
    def list_tasks(cls, task_type: str | None = None) -> list[dict]:
        """列出所有任务，可选按类型筛选。"""
        with cls._lock:
            tasks = list(cls._tasks.values())
        if task_type:
            tasks = [t for t in tasks if t["type"] == task_type]
        return sorted(tasks, key=lambda t: t["created_at"], reverse=True)[:100]

    @classmethod
    def clean_old(cls, max_age_hours: int = 24) -> int:
        """清理过期任务记录。"""
        from datetime import datetime, timedelta

        cutoff = (datetime.now() - timedelta(hours=max_age_hours)).isoformat()
        with cls._lock:
            to_remove = [
                tid for tid, t in cls._tasks.items()
                if t["created_at"] < cutoff and t["status"] in ("completed", "failed", "cancelled")
            ]
            for tid in to_remove:
                del cls._tasks[tid]
                cls._cancel_flags.pop(tid, None)
        return len(to_remove)
