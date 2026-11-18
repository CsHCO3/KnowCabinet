"""任务状态数据模型 — 替代旧 ir_handler.py 的裸 dict。"""

from datetime import datetime
from enum import Enum
from pydantic import BaseModel, Field


class ItemStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    SUCCESS = "success"
    FAILED = "failed"


class JobItem(BaseModel):
    """批处理中的单个条目。"""

    id: int
    status: ItemStatus = ItemStatus.PENDING
    input: dict = Field(default_factory=dict)       # {type, data, metadata}
    output: str | None = None
    error: str | None = None
    processed_at: str | None = None


class Job(BaseModel):
    """一个批处理任务批次。"""

    batch_id: str
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    source_file: str = ""
    total_items: int = 0
    items: list[JobItem] = Field(default_factory=list)

    def summary(self) -> dict:
        counts: dict[str, int] = {
            "total": self.total_items,
            "pending": 0,
            "processing": 0,
            "success": 0,
            "failed": 0,
        }
        for item in self.items:
            s = item.status.value
            if s in counts:
                counts[s] += 1
        return counts

    def is_all_done(self) -> bool:
        return all(
            i.status in (ItemStatus.SUCCESS, ItemStatus.FAILED)
            for i in self.items
        )


def build_job(items: list[dict], batch_id: str, source_file: str = "") -> Job:
    """从原始输入列表构建 Job。"""
    job_items = []
    for idx, item in enumerate(items):
        job_items.append(JobItem(
            id=idx,
            input={
                "type": item.get("type", "text"),
                "data": item.get("data", ""),
                "metadata": item.get("metadata", {}),
            },
        ))
    return Job(
        batch_id=batch_id,
        source_file=source_file,
        total_items=len(job_items),
        items=job_items,
    )
