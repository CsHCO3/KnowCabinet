"""归档管理 API — 论文/视频处理状态 + 一键补做缺失阶段（OCR/浓缩）。"""

from __future__ import annotations

from fastapi import APIRouter

from api.schemas.common import APIResponse
from backend.services.archive.service import (
    run_archive, list_papers, list_videos, _FULL_PAPERS, _BIN_PAPERS,
)
import api.services.task_store as store
import api.services.task_executor as executor

router = APIRouter(prefix="/api", tags=["archive"])


@router.get("/archive/overview")
def overview():
    return {"papers": list_papers(), "videos": list_videos()}


@router.post("/archive/run")
def archive_run():
    """重跑归档：拾取新文件、解压 tar、刷新 frontmatter/metadata/status。"""
    report = run_archive()
    return APIResponse(success=True, data={
        "report": report,
        "papers": list_papers(),
        "videos": list_videos(),
    })


def _paper_md(paper_id: str):
    md = _FULL_PAPERS / paper_id / "paper.md"
    if not md.exists():
        return None
    return md


@router.post("/archive/papers/{paper_id}/convert")
def paper_convert(paper_id: str):
    """转换归档 paper.pdf → md（后台任务）。复制一份提交，不动归档原件。"""
    pdf = _BIN_PAPERS / paper_id / "paper.pdf"
    if not pdf.exists():
        return APIResponse(success=False, error="没有 paper.pdf（先上传）")
    import shutil
    working = _BIN_PAPERS.parent / "pdfs" / f"{paper_id}_paper.pdf"
    working.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(pdf), str(working))

    def _run(tid: str) -> None:
        from backend.services.pdf_remote.service import RemotePDFService
        from api.services.task_handlers import _wait_remote_job
        svc = RemotePDFService()
        res = svc.submit_batch(file_list=[str(working)], translate_requested=False)
        if not res.get("success"):
            err = next((s.get("error") for s in res.get("steps", [])
                        if s.get("status") == "failed"), res.get("error", "提交失败"))
            store.set_status(tid, "failed", error=f"转换提交失败: {err}")
            return
        jid = res.get("job_id")
        store.update_stage(tid, "collect", status="running", remote_job_id=jid,
                           progress=50, detail=f"job {jid}")
        st = _wait_remote_job(tid, jid)
        if st == "collected":
            store.update_stage(tid, "collect", status="completed", progress=100, detail="已收集")
            store.set_status(tid, "completed")
        else:
            store.set_status(tid, "failed", error=f"转换未完成({st})")

    task = store.create("convert", f"转换 {paper_id}",
                        [{"key": "submit", "label": "提交"}, {"key": "collect", "label": "收集"}],
                        options={})
    executor.submit(task.id, _run)
    return APIResponse(success=True, data={"task_id": task.id},
                       message="转换任务已提交，完成后请归档刷新")


@router.post("/archive/papers/{paper_id}/ocr")
def paper_ocr(paper_id: str):
    """对 paper.md 做图片 OCR → paper_ocr.md（后台任务）。"""
    md = _paper_md(paper_id)
    if md is None:
        return APIResponse(success=False, error="没有 paper.md（先转换）")
    from api.services.ocr_service import OCRTaskService
    tid = OCRTaskService.submit([str(md)], "qwen-vl-max_ocr")
    return APIResponse(success=True, data={"task_id": tid}, message="OCR 任务已提交，完成后请归档刷新")


@router.post("/archive/papers/{paper_id}/condense")
def paper_condense(paper_id: str):
    """浓缩 paper.md → condensation.md（后台任务）。"""
    md = _paper_md(paper_id)
    if md is None:
        return APIResponse(success=False, error="没有 paper.md（先转换）")

    def _run(tid: str) -> None:
        from backend.services.condense.service import condense_paper
        r = condense_paper(str(md), arxiv_id=paper_id)
        store.set_result(tid, r)
        if r.get("ok"):
            store.set_status(tid, "completed")
        else:
            store.set_status(tid, "failed", error=r.get("error", "浓缩失败"))

    task = store.create("condense", f"浓缩 {paper_id}",
                        [{"key": "condense", "label": "浓缩"}], options={})
    executor.submit(task.id, _run)
    return APIResponse(success=True, data={"task_id": task.id}, message="浓缩任务已提交，完成后请归档刷新")
