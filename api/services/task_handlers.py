"""各任务类型的 worker 函数（跑在 TaskExecutor 线程池）。"""

from __future__ import annotations

import time as _time

import api.services.task_store as store
import api.services.task_executor as executor


def _wait_remote_job(task_id: str, remote_job_id: str, timeout: int = 7200) -> str:
    """轮询 check_and_collect 直到远程 job 完成/失败。

    返回 'collected' / 'failed' / ''(取消) / 'timeout'。
    """
    from backend.services.pdf_remote.service import RemotePDFService
    from backend.services.pdf_remote.parser import _load_jobs

    svc = RemotePDFService()
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        if executor.is_cancelled(task_id):
            return ""
        try:
            svc.check_and_collect()
        except Exception:
            pass
        for j in _load_jobs():
            if j.get("id") == remote_job_id:
                st = j.get("status", "")
                if st in ("collected", "completed"):
                    return "collected"
                if st == "failed":
                    return "failed"
                break
        _time.sleep(15)
    return "timeout"


def _run_collect_poll(task_id: str, remote_job_id: str) -> None:
    """独立 convert/mineru/translate 提交后的 collect 阶段：轮询到远程 job 完成。"""
    st = _wait_remote_job(task_id, remote_job_id)
    if executor.is_cancelled(task_id):
        return
    if st != "collected":
        store.update_stage(task_id, "collect", status="failed", error=f"远程任务未完成({st})")
        store.set_status(task_id, "failed", error=f"远程任务未完成({st})")
        return
    store.update_stage(task_id, "collect", status="completed", progress=100, detail="已收集")
    store.set_status(task_id, "completed")


def _run_ocr(task_id: str, md_files: list[str], model: str,
             parallel: bool, retry: bool, min_size_kb: int, strip_refs: bool) -> None:
    """OCR 任务：逐文件处理，更新 processing 阶段进度。"""
    import sys as _sys
    try:
        _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    from backend.services.ocr.service import OCRService

    svc = OCRService(model_key=model)
    results = []
    total = success = failed = skipped = 0
    n = len(md_files) or 1
    for idx, md in enumerate(md_files, 1):
        if executor.is_cancelled(task_id):
            store.set_status(task_id, "cancelled")
            return
        name = md.split("\\")[-1].split("/")[-1]
        store.update_stage(task_id, "processing", status="running",
                           progress=int((idx - 1) / n * 100),
                           detail=f"[{idx}/{len(md_files)}] {name}")
        try:
            r = svc.process_file(md, parallel=parallel, retry=retry, inplace=False,
                                 min_size_kb=min_size_kb, strip_refs=strip_refs)
            results.append(r)
            total += r.get("total", 0)
            success += r.get("success", 0)
            failed += r.get("failed", 0)
            skipped += r.get("skipped", 0)
        except Exception as e:
            results.append({"file": md, "error": str(e),
                            "total": 0, "success": 0, "failed": 0, "skipped": 0})
            failed += 1

    store.update_stage(task_id, "processing", status="completed", progress=100,
                       detail=f"完成 {success} 成功 / {failed} 失败 / {skipped} 跳过")
    store.set_result(task_id, {"results": results, "total": total,
                               "success": success, "failed": failed, "skipped": skipped})
    store.set_status(task_id, "completed")
