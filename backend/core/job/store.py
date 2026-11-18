"""Job 存储接口 — 解耦存储格式与业务逻辑。"""

from abc import ABC, abstractmethod
from .schema import Job


class JobStore(ABC):
    @abstractmethod
    def save(self, job: Job) -> None: ...

    @abstractmethod
    def load(self, job_id: str) -> Job: ...

    @abstractmethod
    def list_jobs(self) -> list[str]: ...

    @abstractmethod
    def delete(self, job_id: str) -> None: ...
