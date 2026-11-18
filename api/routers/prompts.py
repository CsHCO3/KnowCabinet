from fastapi import APIRouter, Request, Header
from fastapi.responses import HTMLResponse

from api.services.config_service import ConfigService
from api.schemas.common import APIResponse

router = APIRouter(prefix="/api", tags=["prompts"])
service = ConfigService()


@router.get("/prompts")
async def get_prompt_tree(request: Request, hx_request: bool = Header(False)):
    tree = service.get_prompt_tree()
    templates = request.app.state.templates
    if hx_request:
        return templates.TemplateResponse(request, "partials/prompt_tree.html", {"request": request, "tree": tree})
    return tree


@router.get("/prompts/{path:path}")
async def get_prompt_content(path: str, request: Request, hx_request: bool = Header(False)):
    content = service.get_prompt_content(path)
    if not content:
        return APIResponse(success=False, error="文件不存在")
    templates = request.app.state.templates
    if hx_request:
        return templates.TemplateResponse(request, "partials/prompt_content.html", {"request": request, "content": content})
    return content
