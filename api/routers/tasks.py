from fastapi import APIRouter, Request, Header
from fastapi.responses import HTMLResponse

from api.services.task_service import TaskService
from api.schemas.common import APIResponse

router = APIRouter(prefix="/api", tags=["tasks"])


@router.get("/tasks")
async def list_tasks(request: Request, hx_request: bool = Header(False)):
    tasks = TaskService.list_tasks()
    templates = request.app.state.templates
    if hx_request:
        return templates.TemplateResponse(request, "partials/task_list.html", {"request": request, "tasks": tasks})
    return tasks


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: str, request: Request, hx_request: bool = Header(False)):
    TaskService.delete_task(task_id)
    # 返回刷新后的列表
    tasks = TaskService.list_tasks()
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "partials/task_list.html", {"request": request, "tasks": tasks})
