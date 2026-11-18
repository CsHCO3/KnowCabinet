"""基于 JSON 文件的 JobStore 实现。"""

import json
from pathlib import Path

from .schema import Job
from .store import JobStore

# pydantic < 2.10: model_dump_json 无 ensure_ascii 参数
_USE_DUMP = hasattr(Job, "model_dump_json")


def _json_dumps(job: Job) -> str:
    if _USE_DUMP:
        return job.model_dump_json(indent=2)
    return json.dumps(job.model_dump(), ensure_ascii=False, indent=2)


class JsonJobStore(JobStore):
    def __init__(self, storage_dir: str | Path):
        self._dir = Path(storage_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, job_id: str) -> Path:
        return self._dir / f"batch_{job_id}.json"

    def save(self, job: Job) -> None:
        path = self._path(job.batch_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(_json_dumps(job), encoding="utf-8")
        tmp.replace(path)

    def load(self, job_id: str) -> Job:
        path = self._path(job_id)
        if not path.exists():
            raise FileNotFoundError(f"Job not found: {job_id} ({path})")
        data = json.loads(path.read_text(encoding="utf-8"))
        return Job(**data)

    def list_jobs(self) -> list[str]:
        return sorted(
            p.stem.replace("batch_", "", 1)
            for p in self._dir.glob("batch_*.json")
        )

    def delete(self, job_id: str) -> None:
        self._path(job_id).unlink(missing_ok=True)
