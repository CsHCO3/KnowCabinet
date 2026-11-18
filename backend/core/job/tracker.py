"""断点续传追踪器 — 基于 JobStore 接口，不耦合存储格式。"""

from datetime import datetime
from .schema import Job, JobItem, ItemStatus
from .store import JobStore


class JobTracker:
    """读写 Job 状态的便捷封装，每次修改均持久化。"""

    def __init__(self, store: JobStore, job: Job):
        self.store = store
        self.job = job
        self._reset_orphaned_processing()

    def _reset_orphaned_processing(self) -> None:
        """将上次异常中断时遗留的 processing 条目重置为 pending。"""
        changed = False
        for item in self.job.items:
            if item.status == ItemStatus.PROCESSING:
                item.status = ItemStatus.PENDING
                changed = True
        if changed:
            self.store.save(self.job)

    def get_pending(self) -> list[JobItem]:
        return [i for i in self.job.items if i.status == ItemStatus.PENDING]

    def mark_start(self, item_id: int) -> None:
        self.job.items[item_id].status = ItemStatus.PROCESSING
        self.store.save(self.job)

    def commit_success(self, item_id: int, output: str) -> None:
        item = self.job.items[item_id]
        item.status = ItemStatus.SUCCESS
        item.output = output
        item.processed_at = datetime.now().isoformat(timespec="seconds")
        self.store.save(self.job)

    def commit_failed(self, item_id: int, error: str) -> None:
        item = self.job.items[item_id]
        item.status = ItemStatus.FAILED
        item.error = error
        item.processed_at = datetime.now().isoformat(timespec="seconds")
        self.store.save(self.job)

    def summary(self) -> dict:
        return self.job.summary()

    def is_all_done(self) -> bool:
        return self.job.is_all_done()
