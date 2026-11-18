"""OCR 后台任务 Service — 复用 backend.services.ocr.OCRService，跑在统一 TaskExecutor。"""

from __future__ import annotations

import api.services.task_store as store
import api.services.task_executor as executor
from api.services.task_handlers import _run_ocr


class OCRTaskService:
    """前端 OCR 后台任务入口（统一任务体系）。"""

    @staticmethod
    def submit(md_files: list[str], model: str,
               parallel: bool = True, retry: bool = False,
               min_size_kb: int = 7, strip_refs: bool = True) -> str:
        task = store.create(
            "ocr", f"OCR: {len(md_files)} 个文件",
            [{"key": "processing", "label": "批量 OCR"}],
            options={"md_files": md_files, "model": model, "parallel": parallel,
                     "retry": retry, "min_size_kb": min_size_kb, "strip_refs": strip_refs},
        )
        executor.submit(task.id, _run_ocr, md_files, model, parallel, retry,
                        min_size_kb, strip_refs)
        return task.id

    @staticmethod
    def get_status(task_id: str) -> dict | None:
        task = store.get(task_id)
        return task.model_dump(mode="json") if task else None

    @staticmethod
    def cancel(task_id: str) -> bool:
        return executor.cancel(task_id)

    @staticmethod
    def list_tasks() -> list[dict]:
        return [t.model_dump(mode="json") for t in store.list_tasks(task_type="ocr")]
