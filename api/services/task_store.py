"""统一任务存储 — 持久化到 .data/tasks.json。

tasks.json 是上层编排/记录层；底层 SSH job 仍在 remote_jobs.json（parser.py 管理），
通过 StageInfo.remote_job_id 引用关联。所有写操作加锁并即时落盘。
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime
from pathlib import Path

from api.schemas.task import StageInfo, UnifiedTask

_TASKS_FILE = Path(__file__).resolve().parent.parent.parent / ".data" / "tasks.json"

_tasks: dict[str, UnifiedTask] = {}
_lock = threading.Lock()


def _load_tasks() -> dict[str, UnifiedTask]:
    if not _TASKS_FILE.exists():
        return {}
    try:
        raw = json.loads(_TASKS_FILE.read_text(encoding="utf-8"))
        return {tid: UnifiedTask.model_validate(t) for tid, t in raw.items()}
    except Exception as e:
        print(f"load tasks failed: {e}")
        return {}


def _save_tasks() -> None:
    try:
        _TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {tid: t.model_dump(mode="json") for tid, t in _tasks.items()}
        _TASKS_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"save tasks failed: {e}")


_tasks = _load_tasks()


def create(task_type: str, title: str, stages: list[dict],
           options: dict | None = None,
           parent_task_id: str | None = None) -> UnifiedTask:
    """创建任务记录并持久化。stages 为 [{key, label}, ...] 列表。"""
    now = datetime.now()
    task = UnifiedTask(
        id=uuid.uuid4().hex[:12],
        type=task_type,
        title=title,
        status="pending",
        created_at=now,
        updated_at=now,
        options=options or {},
        stages=[StageInfo(key=s["key"], label=s.get("label", s["key"])) for s in stages],
        parent_task_id=parent_task_id,
    )
    with _lock:
        _tasks[task.id] = task
        _save_tasks()
    return task


def get(task_id: str) -> UnifiedTask | None:
    with _lock:
        return _tasks.get(task_id)


def list_tasks(task_type: str | None = None, status: str | None = None,
               limit: int = 200) -> list[UnifiedTask]:
    with _lock:
        items = list(_tasks.values())
    if task_type:
        items = [t for t in items if t.type == task_type]
    if status:
        items = [t for t in items if t.status == status]
    return sorted(items, key=lambda t: t.created_at, reverse=True)[:limit]


def update_stage(task_id: str, stage_key: str, **updates) -> None:
    """原子更新某阶段（status/detail/progress/remote_job_id/error 等）。"""
    with _lock:
        task = _tasks.get(task_id)
        if not task:
            return
        for s in task.stages:
            if s.key == stage_key:
                for k, v in updates.items():
                    setattr(s, k, v)
                s.updated_at = datetime.now()
                break
        task.updated_at = datetime.now()
        _save_tasks()


def set_status(task_id: str, status: str, error: str | None = None) -> None:
    with _lock:
        task = _tasks.get(task_id)
        if not task:
            return
        task.status = status
        task.updated_at = datetime.now()
        if status in ("completed", "failed", "cancelled"):
            task.completed_at = task.completed_at or datetime.now()
        if error:
            task.error = error
        _save_tasks()


def set_result(task_id: str, result: dict) -> None:
    with _lock:
        task = _tasks.get(task_id)
        if not task:
            return
        task.result = result
        task.updated_at = datetime.now()
        _save_tasks()


def delete(task_id: str) -> bool:
    with _lock:
        if task_id in _tasks:
            del _tasks[task_id]
            _save_tasks()
            return True
        return False


def get_children(parent_task_id: str) -> list[UnifiedTask]:
    with _lock:
        return [t for t in _tasks.values() if t.parent_task_id == parent_task_id]
