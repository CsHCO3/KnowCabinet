"""贴吧 API：抓取、列表与知识点读取/提炼。"""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException

from api.schemas.common import APIResponse
from backend.config.settings import Settings
from backend.services.community.tieba.service import TiebaService

router = APIRouter(prefix="/api/community/tieba", tags=["community-tieba"])


@router.get("/threads/{tid}")
def read_thread(tid: str, page: int | None = None):
    try:
        return APIResponse(success=True, data=TiebaService().thread(tid, page=page))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/forums/{forum}/threads")
def read_forum(forum: str, page: int = 1):
    try:
        return APIResponse(success=True, data=TiebaService().forum(forum, page))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/forums/{forum}/featured")
def read_featured(forum: str, page: int = 1):
    try:
        return APIResponse(success=True, data=TiebaService().featured(forum, page))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/forums/{forum}/search")
def search(forum: str, keyword: str, page: int = 1, max_pages: int = 1):
    try:
        return APIResponse(success=True, data=TiebaService().search(forum, keyword, page, max_pages))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/favorites")
def favorites(max_pages: int = 0):
    try:
        return APIResponse(success=True, data=TiebaService().favorites(max_pages=max_pages))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/knowledge/{tid}")
def knowledge(tid: str):
    result = TiebaService().load_knowledge(tid)
    if result is None:
        raise HTTPException(status_code=404, detail="knowledge not found")
    return APIResponse(success=True, data=result)


@router.post("/knowledge/{tid}/extract")
def extract_knowledge(tid: str, model: str = Form("deepseek-chat"), rebuild: bool = Form(False)):
    try:
        llm = Settings().get_llm_client(model)
        result = TiebaService().extract(tid, llm=llm, update=not rebuild)
        return APIResponse(success=True, data=result)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
