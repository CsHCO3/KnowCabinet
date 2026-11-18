"""远程任务监控 Service — 读取 remote_jobs.json"""

import json
from pathlib import Path
from datetime import datetime
from typing import Optional

from api.schemas.common import TaskInfo


DATA_DIR = Path(__file__).resolve().parent.parent.parent / ".data"
JOBS_FILE = DATA_DIR / "remote_jobs.json"


class TaskService:

    @staticmethod
    def list_tasks() -> list[TaskInfo]:
        if not JOBS_FILE.exists():
            return []

        with open(JOBS_FILE, encoding="utf-8") as f:
            raw = json.load(f)

        tasks = []

        # 兼容 list [{...}] 和 dict {id: {...}} 两种格式
        if isinstance(raw, dict):
            items = raw.items()
        elif isinstance(raw, list):
            items = [(j.get("id", str(i)), j) for i, j in enumerate(raw)]
        else:
            return []

        for job_id, job in items:
            # 从 uploaded_files 或 md_file_names 提取文件名
            uploaded = job.get("uploaded_files")
            md_files = job.get("md_file_names")
            filename = job.get("filename") or job.get("file", "")
            if not filename and isinstance(uploaded, list) and uploaded:
                local_path = uploaded[0].get("local", "")
                filename = Path(local_path).name if local_path else "unknown"
            if not filename and isinstance(md_files, list) and md_files:
                filename = md_files[0]
            if not filename:
                filename = "unknown"

            # 状态映射
            raw_status = job.get("status", "queued")
            status_map = {"collected": "completed", "running": "running",
                          "failed": "failed", "failed_download": "failed",
                          "completed": "completed", "duplicate": "completed",
                          "submitted": "queued", "queued": "queued"}
            status = status_map.get(raw_status, raw_status)

            tasks.append(TaskInfo(
                id=job.get("id", job_id),
                type=job.get("type", "pdf_convert"),
                filename=filename,
                status=status,
                created_at=TaskService._parse_dt(job.get("created_at") or job.get("submitted_at")),
                completed_at=TaskService._parse_dt(job.get("collected_at") if status == "completed" else job.get("completed_at")) if job.get("collected_at") or job.get("completed_at") else None,
                error=job.get("error"),
            ))

        # 按创建时间倒序
        tasks.sort(key=lambda t: t.created_at, reverse=True)
        return tasks

    @staticmethod
    def delete_task(task_id: str) -> bool:
        """从 remote_jobs.json 中删除指定任务"""
        if not JOBS_FILE.exists():
            return False

        with open(JOBS_FILE, encoding="utf-8") as f:
            raw = json.load(f)

        if isinstance(raw, list):
            new_raw = [j for j in raw if j.get("id") != task_id]
            if len(new_raw) == len(raw):
                return False  # 未找到
        elif isinstance(raw, dict):
            if task_id not in raw:
                return False
            new_raw = {k: v for k, v in raw.items() if k != task_id}
        else:
            return False

        with open(JOBS_FILE, "w", encoding="utf-8") as f:
            json.dump(new_raw, f, ensure_ascii=False, indent=2)
        return True

    @staticmethod
    def _parse_dt(value) -> datetime:
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value)
            except (ValueError, TypeError):
                pass
        return datetime.now()
