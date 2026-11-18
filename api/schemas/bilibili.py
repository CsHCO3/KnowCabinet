"""B站模块 Pydantic 数据模型。"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel


class APIResponse(BaseModel):
    success: bool = True
    message: str = ""
    data: Any = None
    error: Optional[str] = None


# ── 采集 ──

class CheckUrlResult(BaseModel):
    ok: bool
    bvid: str = ""
    video: Optional[dict] = None
    collection: Optional[dict] = None
    already_in_db: bool = False
    error: str = ""


class AuthorSearchResult(BaseModel):
    ok: bool
    author: dict = {}
    stat: dict = {}
    videos: list[dict] = []
    error: str = ""


class FavoritesPreviewResult(BaseModel):
    ok: bool
    videos: list[dict] = []
    total: int = 0
    error: str = ""


class CommitRequest(BaseModel):
    videos: list[dict]


class CommitResult(BaseModel):
    inserted: int
    skipped: int
    ids: list[int]


# ── 提取 ──

class ExtractSubmitRequest(BaseModel):
    bvids: list[str]


class ExtractStatusResult(BaseModel):
    id: str
    type: str
    status: str  # pending | running | completed | failed | cancelled
    progress: str = ""
    result: Any = None
    error: Optional[str] = None


# ── 问答 ──

class QueryRunRequest(BaseModel):
    video_ids: list[int]
    prompt: str
    prompt_file_path: str = ""
    filter_mode: str = "keyword"  # keyword | keyword_llm
    keyword: str = ""


class QueryTaskInfo(BaseModel):
    id: int
    title: str
    user_prompt: str
    filter_mode: str
    keyword: str
    video_ids: list[int]
    result_text: str = ""
    status: str
    created_at: str
    completed_at: str = ""


# ── 管理 ──

class DeleteResult(BaseModel):
    deleted: bool
    bvid: str = ""
    cleanup: Optional[dict] = None


class BatchDeleteRequest(BaseModel):
    video_ids: list[int]
    cleanup_files: bool = False
