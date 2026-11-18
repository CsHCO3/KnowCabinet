"""统一任务数据模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel


class StageInfo(BaseModel):
    key: str
    label: str = ""
    status: Literal["pending", "running", "completed", "failed", "skipped"] = "pending"
    detail: str = ""
    progress: int = 0
    remote_job_id: Optional[str] = None
    updated_at: Optional[datetime] = None
    error: Optional[str] = None
    retry_count: int = 0


class UnifiedTask(BaseModel):
    id: str
    type: Literal["survey", "convert", "mineru", "translate", "ocr", "condense", "archive", "pipeline"] = "pipeline"
    title: str = ""
    status: Literal["pending", "running", "completed", "failed", "cancelled"] = "pending"
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime] = None
    options: dict[str, Any] = {}
    stages: list[StageInfo] = []
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    parent_task_id: Optional[str] = None
