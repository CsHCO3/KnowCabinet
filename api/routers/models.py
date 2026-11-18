from fastapi import APIRouter, Request, Header
from fastapi.responses import HTMLResponse

from api.services.config_service import ConfigService
from api.schemas.common import APIResponse, ModelInfo

router = APIRouter(prefix="/api", tags=["models"])
service = ConfigService()


@router.get("/models")
async def list_models(request: Request, hx_request: bool = Header(False)):
    models = service.list_models()
    templates = request.app.state.templates
    if hx_request:
        return templates.TemplateResponse(request, "partials/model_card.html", {"request": request, "models": models})
    return models


@router.get("/models/{key}")
async def get_model(key: str, request: Request, hx_request: bool = Header(False)):
    model = service.get_model(key)
    if not model:
        return APIResponse(success=False, error="模型不存在")
    templates = request.app.state.templates
    if hx_request:
        return templates.TemplateResponse(request, "partials/model_detail.html", {"request": request, "model": model})
    return model
