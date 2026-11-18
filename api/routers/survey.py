from fastapi import APIRouter, Request, Header, Form, File, UploadFile
from typing import Optional
from fastapi.responses import HTMLResponse

from api.services.survey_service import SurveyService
from api.schemas.survey import SurveyRequest
from api.schemas.common import APIResponse

router = APIRouter(prefix="/api", tags=["survey"])


@router.get("/surveys")
async def list_surveys(request: Request, hx_request: bool = Header(False)):
    surveys = [SurveyService.to_survey_result(t) for t in SurveyService.list()]
    templates = request.app.state.templates
    if hx_request:
        return templates.TemplateResponse(request, "partials/survey_table.html", {"request": request, "surveys": surveys})
    return surveys


@router.post("/surveys")
async def create_survey(
    request: Request,
    raw_query: str = Form(default=""),
    keywords: str = Form(default=""),
    category: str = Form(default=""),
    date_from: Optional[str] = Form(None),
    date_to: Optional[str] = Form(None),
    max_results: int = Form(50),
    auto_download: bool = Form(False),
    auto_convert: bool = Form(False),
    auto_translate: bool = Form(False),
    top_n: int = Form(15),
    translate_model: str = Form("qwen3:8b"),
    content_type: str = Form("paper"),
    detect_lang: bool = Form(True),
):
    req = SurveyRequest(
        raw_query=raw_query,
        keywords=keywords,
        category=category,
        date_from=date_from,
        date_to=date_to,
        max_results=max_results,
        auto_download=auto_download,
        auto_convert=auto_convert,
        auto_translate=auto_translate,
        top_n=top_n,
        translate_model=translate_model,
        content_type=content_type,
        detect_lang=detect_lang,
    )
    task_id = SurveyService.create(req)
    task = SurveyService.get(task_id)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "partials/survey_pipeline.html", {"request": request, "task": task})


@router.get("/surveys/active")
async def active_surveys(request: Request, hx_request: bool = Header(False)):
    """进行中的调研（刷新页面后恢复进度用）。"""
    active = [t for t in SurveyService.list() if t.status in ("pending", "running")]
    active = list(reversed(active))
    templates = request.app.state.templates
    if hx_request:
        return templates.TemplateResponse(
            request, "partials/survey_active.html", {"request": request, "tasks": active})
    return active


@router.get("/surveys/{task_id}")
async def get_survey(task_id: str, request: Request, hx_request: bool = Header(False)):
    task = SurveyService.get(task_id)
    if not task:
        return APIResponse(success=False, error="调研不存在")
    if hx_request:
        result = SurveyService.to_survey_result(task)
        templates = request.app.state.templates
        return templates.TemplateResponse(request, "partials/survey_table.html", {
            "request": request,
            "surveys": [result]
        })
    return task


@router.get("/surveys/{task_id}/progress")
async def get_survey_progress(task_id: str, request: Request, hx_request: bool = Header(False)):
    task = SurveyService.get(task_id)
    templates = request.app.state.templates
    if hx_request:
        if not task:
            return templates.TemplateResponse(
                request, "partials/survey_active.html", {"request": request, "tasks": []})
        return templates.TemplateResponse(
            request, "partials/survey_pipeline.html", {"request": request, "task": task})
    return SurveyService.get_progress(task_id)


@router.post("/surveys/{task_id}/cancel")
async def cancel_survey(task_id: str):
    SurveyService.cancel(task_id)
    return APIResponse(success=True, message="已取消")


# ── 智能搜索：AI 解析 ─────────────────────────────────

@router.post("/arxiv/parse-query")
async def arxiv_parse_query(
    request: Request,
    text: str = Form(default=""),
    md_file: UploadFile = File(default=None),
):
    md_content = ""
    if md_file and md_file.filename:
        md_content = (await md_file.read()).decode("utf-8", errors="replace")
    result = SurveyService.parse_query(text=text, md_content=md_content)
    return result


# ── 智能搜索：解析 raw_query ─────────────────────────

@router.post("/arxiv/parse-raw")
async def arxiv_parse_raw(raw_query: str = Form(...)):
    result = SurveyService.parse_raw_query(raw_query)
    return result


# ── 快速测试：搜索 ──────────────────────────────────

@router.post("/arxiv/search")
async def arxiv_search(
    request: Request,
    keywords: str = Form(default=""),
    raw_query: str = Form(default=""),
    title: str = Form(default=""),
    author: str = Form(default=""),
    category: str = Form(default=""),
    abstract: str = Form(default=""),
    date_from: Optional[str] = Form(None),
    date_to: Optional[str] = Form(None),
    max_results: int = Form(10),
    sort_by: str = Form("relevance"),
    sort_order: str = Form("descending"),
    page: int = Form(1),
    hx_request: bool = Header(False),
):
    papers = SurveyService.quick_search(
        keywords=keywords,
        raw_query=raw_query,
        title=title,
        author=author,
        category=category,
        abstract=abstract,
        date_from=date_from or "",
        date_to=date_to or "",
        max_results=max_results,
        sort_by=sort_by,
        sort_order=sort_order,
        page=page,
    )
    templates = request.app.state.templates
    if hx_request:
        return templates.TemplateResponse(request, "partials/survey_table.html", {"request": request, "surveys": [{"papers": papers}]})
    return papers


# ── LLM 分析 ──────────────────────────────────────

@router.post("/arxiv/analyze")
async def arxiv_analyze(arxiv_ids: list[str]):
    task_id = SurveyService.analyze(arxiv_ids)
    return APIResponse(success=True, data={"task_id": task_id})
