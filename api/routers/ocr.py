"""OCR 任务 API — 对 .md 文档内的图片做文字识别（后台任务）。

复用 backend.services.ocr.OCRService（CLI `ocr` 命令同一套），跑在 BiliTaskManager。
注意：OCR 会原地修改 .md 文件。
"""

from pathlib import Path

from fastapi import APIRouter, Request, Header, Form

from api.schemas.common import APIResponse
from api.services.ocr_service import OCRTaskService

router = APIRouter(prefix="/api", tags=["ocr"])

OCR_MODELS = ["qwen-vl-max_ocr", "glm-4.6v_ocr", "paddleocr-vl1.6_ocr"]
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_MD_DIR = _PROJECT_ROOT / "data" / "ocr_docs"


@router.post("/ocr/submit")
async def ocr_submit(
    request: Request,
    files: str = Form(""),         # 逗号分隔的 .md 路径
    dir: str = Form(""),           # 目录（扫描其下 *.md，与 files 叠加）
    model: str = Form("qwen-vl-max_ocr"),
    parallel: bool = Form(True),
    retry: bool = Form(False),
    min_size_kb: int = Form(7),
    strip_refs: bool = Form(True),
):
    """提交 OCR 后台任务。"""
    md_files = [f.strip() for f in files.split(",") if f.strip()]
    if dir:
        d = Path(dir).resolve()
        if d.is_dir():
            md_files += [str(p) for p in sorted(d.glob("*.md"))
                         if not p.stem.lower().endswith("_ocr")]
    md_files = list(dict.fromkeys(md_files))

    if not md_files:
        return APIResponse(success=False, error="没有可处理的 .md 文件（请选文件或目录；*_ocr.md 已自动跳过）")
    if model not in OCR_MODELS:
        return APIResponse(success=False, error=f"不支持的模型: {model}")

    # 去重：相同文件+模型已有排队/运行中任务时直接复用，不重复建任务
    key = (tuple(md_files), model)
    for t in OCRTaskService.list_tasks():
        if t["status"] in ("pending", "running"):
            opts = t.get("options") or {}
            if (tuple(opts.get("md_files") or []), opts.get("model")) == key:
                return APIResponse(success=True, data={"task_id": t["id"]},
                                   message="相同 OCR 任务已在队列/运行中，直接复用")

    task_id = OCRTaskService.submit(md_files, model, parallel, retry,
                                    min_size_kb=max(0, min_size_kb), strip_refs=strip_refs)
    return APIResponse(success=True, data={"task_id": task_id}, message="OCR 任务已提交")


@router.get("/ocr/tasks")
async def ocr_tasks(request: Request, hx_request: bool = Header(False)):
    """OCR 任务列表（active 在前 + 最近完成）。"""
    all_tasks = OCRTaskService.list_tasks()
    active = [t for t in all_tasks if t["status"] in ("pending", "running")]
    done = [t for t in all_tasks if t["status"] in ("completed", "failed", "cancelled")]
    tasks = active + done[:10]
    if hx_request:
        templates = request.app.state.templates
        return templates.TemplateResponse(
            request, "partials/ocr_tasks.html", {"request": request, "tasks": tasks})
    return {"tasks": tasks}


@router.get("/ocr/task/{task_id}/status")
async def ocr_task_status(task_id: str):
    """查询 OCR 任务状态（含逐文件结果）。"""
    status = OCRTaskService.get_status(task_id)
    if not status:
        return APIResponse(success=False, error="任务不存在")
    return APIResponse(success=True, data=status)


@router.post("/ocr/task/{task_id}/cancel")
async def ocr_task_cancel(task_id: str):
    ok = OCRTaskService.cancel(task_id)
    if ok:
        return APIResponse(success=True, message="任务已取消")
    return APIResponse(success=False, error="无法取消（任务不存在或已完成）")


@router.get("/ocr/local-mds")
def ocr_local_mds(dir: str = ""):
    """列可选 .md 文件（默认 data/ocr_docs；跳过本工具生成的 *_ocr.md）。"""
    base = Path(dir).resolve() if dir else _DEFAULT_MD_DIR
    if not base.is_dir():
        return {"files": [], "dir": str(base), "error": "目录不存在"}
    files = [{"path": str(p), "name": p.name} for p in sorted(base.glob("*.md"))
             if not p.stem.lower().endswith("_ocr")]
    return {"files": files, "dir": str(base)}
