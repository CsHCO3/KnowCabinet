"""统一任务中心 API — 所有类型任务（survey/convert/mineru/translate/ocr/pipeline）统一展示与管理。"""

from __future__ import annotations

from fastapi import APIRouter, Request, Header

from api.schemas.common import APIResponse
from api.schemas.survey import SurveyRequest
import api.services.task_store as store
import api.services.task_executor as executor
from api.services.task_handlers import _run_ocr, _run_collect_poll
from api.services.survey_service import _run_survey_pipeline

router = APIRouter(prefix="/api", tags=["task-center"])


def _retry_task(task_id: str) -> bool:
    """按类型重跑失败任务。返回是否成功派发。"""
    task = store.get(task_id)
    if not task:
        return False
    opts = task.options or {}

    if task.type == "survey":
        # 阶段级断点续跑：保留已完成/跳过阶段，只重置失败/中断/未完成的，
        # 从断点续跑（_run_survey_pipeline 会跳过 completed 阶段）。
        for s in task.stages:
            if s.status in ("failed", "cancelled", "pending", "running"):
                s.status = "pending"
                s.error = None
                s.detail = ""
        store.set_status(task_id, "pending")
        executor.submit(task_id, _run_survey_pipeline, SurveyRequest(**opts))
        return True

    for s in task.stages:
        s.status = "pending"
        s.error = None
        s.detail = ""
        s.remote_job_id = None
    store.set_status(task_id, "pending")

    if task.type == "ocr":
        executor.submit(task_id, _run_ocr,
                        opts.get("md_files", []),
                        opts.get("model", "qwen-vl-max_ocr"),
                        opts.get("parallel", True),
                        opts.get("retry", False),
                        opts.get("min_size_kb", 7),
                        opts.get("strip_refs", True))
        return True

    if task.type in ("convert", "mineru"):
        def _resubmit(tid: str) -> None:
            from backend.services.pdf_remote.service import RemotePDFService
            svc = RemotePDFService()
            file_list = opts.get("file_list", [])
            if not file_list:
                store.set_status(tid, "failed", error="缺少重试文件列表")
                return
            if task.type == "mineru":
                res = svc.submit_mineru(
                    file_list=file_list, method=opts.get("method") or None,
                    lang=opts.get("lang") or None,
                    backend=opts.get("backend", "pipeline"),
                    formula=opts.get("formula") or None, table=opts.get("table") or None)
            else:
                res = svc.submit_batch(file_list=file_list,
                                       translate_requested=opts.get("translate", False))
            if res.get("success"):
                jid = res.get("job_id")
                store.update_stage(tid, "submit", status="completed", progress=40,
                                   detail=f"已上传 {res.get('uploaded_count', 0)} 个")
                store.update_stage(tid, "collect", status="running",
                                   remote_job_id=jid, progress=50, detail=f"job {jid}")
                _run_collect_poll(tid, jid)
            else:
                store.set_status(tid, "failed", error="重试提交失败")
        executor.submit(task_id, _resubmit)
        return True

    if task.type == "translate":
        def _retranslate(tid: str) -> None:
            from backend.services.pdf_remote.service import RemotePDFService
            svc = RemotePDFService()
            res = svc.submit_translate(source_job_id=opts.get("source_job_id"),
                                       model=opts.get("model", "qwen3:8b"),
                                       detect=opts.get("detect", True),
                                       content_type=opts.get("content_type", "paper"))
            if res.get("success"):
                jid = res.get("job_id")
                store.update_stage(tid, "collect", status="running",
                                   remote_job_id=jid, progress=50, detail=f"job {jid}")
                _run_collect_poll(tid, jid)
            else:
                store.set_status(tid, "failed", error="重试翻译提交失败")
        executor.submit(task_id, _retranslate)
        return True

    return False


@router.get("/tasks/center")
async def list_center(request: Request, type: str = "", status: str = "",
                      limit: int = 100, hx_request: bool = Header(False)):
    tasks = store.list_tasks(task_type=type or None, status=status or None, limit=limit)
    data = [t.model_dump(mode="json") for t in tasks]
    if hx_request:
        templates = request.app.state.templates
        return templates.TemplateResponse(
            request, "partials/task_center_list.html", {"request": request, "tasks": data})
    return {"tasks": data}


@router.get("/tasks/center/{task_id}")
def get_center(task_id: str):
    t = store.get(task_id)
    if not t:
        return APIResponse(success=False, error="任务不存在")
    data = t.model_dump(mode="json")
    data["children"] = [c.model_dump(mode="json") for c in store.get_children(task_id)]
    return APIResponse(success=True, data=data)


@router.post("/tasks/center/{task_id}/retry")
def retry_center(task_id: str):
    ok = _retry_task(task_id)
    if not ok:
        return APIResponse(success=False, error="无法重试（类型不支持或任务不存在）")
    return APIResponse(success=True, message="已重新派发")


@router.post("/tasks/center/{task_id}/cancel")
def cancel_center(task_id: str):
    ok = executor.cancel(task_id)
    if not ok:
        return APIResponse(success=False, error="无法取消（任务不存在或已完成）")
    return APIResponse(success=True, message="已取消")


@router.delete("/tasks/center/{task_id}")
def delete_center(task_id: str):
    ok = store.delete(task_id)
    return APIResponse(success=ok, message="已删除" if ok else "任务不存在")
