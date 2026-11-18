"""InfoSystem Web 前端 — FastAPI 应用入口"""

from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware

from api.routers import models, prompts, survey, bilibili, tasks, social, pdf_tasks, ocr, task_center, archive, papers, research_projects, community_tieba, community_qq

# 启动时：把上次运行中/待处理的任务标为中断（服务重启），可在任务中心手动重试
from api.services import task_executor
task_executor.mark_interrupted_on_startup()
from api.services import bili_db

app = FastAPI(title="InfoSystem", version="0.1.0")


@app.on_event("startup")
async def startup():
    bili_db.init_db()
    _start_remote_collect_sync()


def _start_remote_collect_sync() -> None:
    """后台线程：有 pending 远程任务时周期性 check_and_collect，让 /tasks 状态自动流转。"""
    import threading
    import time as _time
    from pathlib import Path as _Path

    def _loop() -> None:
        import json
        jobs_file = _Path(__file__).resolve().parent.parent / ".data" / "remote_jobs.json"
        sleep = 60
        while True:
            _time.sleep(sleep)
            try:
                if not jobs_file.exists():
                    continue
                jobs = json.loads(jobs_file.read_text(encoding="utf-8"))
                if not any(j.get("status") in ("submitted", "running", "failed") for j in jobs):
                    continue
                from backend.services.pdf_remote.service import RemotePDFService
                RemotePDFService().check_and_collect()
            except Exception:
                pass

    t = threading.Thread(target=_loop, daemon=True, name="remote-collect-sync")
    t.start()

# CORS（允许未来 SPA 跨域）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 静态文件
STATIC_DIR = Path(__file__).resolve().parent.parent / "frontend" / "static"
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "frontend" / "templates"

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# 注入 templates 到各路由
app.state.templates = templates


# ─── 杂项路由 ───

@app.get('/favicon.ico')
async def favicon():
    return HTMLResponse(content='')


# ─── 页面路由 ───

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse(request, "dashboard.html", {"request": request})


# ── arXiv 调研 ──

@app.get("/survey", response_class=HTMLResponse)
async def survey_redirect(request: Request):
    return templates.TemplateResponse(request, "survey/start.html", {"request": request})


@app.get("/survey/smart", response_class=HTMLResponse)
async def survey_smart(request: Request):
    return templates.TemplateResponse(request, "survey/smart.html", {"request": request})


@app.get("/survey/quick-test", response_class=HTMLResponse)
async def survey_quick_test(request: Request):
    return templates.TemplateResponse(request, "survey/quick.html", {"request": request})


@app.get("/survey/start", response_class=HTMLResponse)
async def survey_start(request: Request):
    return templates.TemplateResponse(request, "survey/start.html", {"request": request})


@app.get("/bilibili/collect", response_class=HTMLResponse)
async def bili_collect_page(request: Request):
    return templates.TemplateResponse(request, "bilibili/collect.html", {"request": request})


@app.get("/bilibili/extract", response_class=HTMLResponse)
async def bili_extract_page(request: Request):
    return templates.TemplateResponse(request, "bilibili/extract.html", {"request": request})


@app.get("/bilibili/query", response_class=HTMLResponse)
async def bili_query_page(request: Request):
    return templates.TemplateResponse(request, "bilibili/query.html", {"request": request})


@app.get("/tasks", response_class=HTMLResponse)
async def tasks_page(request: Request):
    return templates.TemplateResponse(request, "tasks.html", {"request": request})


@app.get("/tasks/submit", response_class=HTMLResponse)
async def tasks_submit_page(request: Request):
    return templates.TemplateResponse(request, "tasks_submit.html", {"request": request})


@app.get("/ocr", response_class=HTMLResponse)
async def ocr_page(request: Request):
    return templates.TemplateResponse(request, "ocr.html", {"request": request})


@app.get("/task-center", response_class=HTMLResponse)
async def task_center_page(request: Request):
    return templates.TemplateResponse(request, "task_center.html", {"request": request})


@app.get("/archive", response_class=HTMLResponse)
async def archive_page(request: Request):
    return templates.TemplateResponse(request, "archive.html", {"request": request})


@app.get("/videos", response_class=HTMLResponse)
async def videos_page(request: Request):
    return templates.TemplateResponse(request, "videos.html", {"request": request})


@app.get("/papers", response_class=HTMLResponse)
async def papers_page(request: Request):
    return templates.TemplateResponse(request, "papers.html", {"request": request})


@app.get("/models", response_class=HTMLResponse)
async def models_page(request: Request):
    return templates.TemplateResponse(request, "models.html", {"request": request})


@app.get("/prompts", response_class=HTMLResponse)
async def prompts_page(request: Request):
    return templates.TemplateResponse(request, "prompts.html", {"request": request})


@app.get("/settings/social", response_class=HTMLResponse)
async def social_settings_page(request: Request):
    return templates.TemplateResponse(request, "settings/social.html", {"request": request})


@app.get("/about", response_class=HTMLResponse)
async def about_page(request: Request):
    return templates.TemplateResponse(request, "about.html", {"request": request})


# 注册 API 路由
app.include_router(models.router)
app.include_router(prompts.router)
app.include_router(survey.router)
app.include_router(bilibili.router)
app.include_router(tasks.router)
app.include_router(social.router)
app.include_router(pdf_tasks.router)
app.include_router(ocr.router)
app.include_router(task_center.router)
app.include_router(archive.router)
app.include_router(papers.router)
app.include_router(research_projects.router)
app.include_router(community_tieba.router)
app.include_router(community_qq.router)
