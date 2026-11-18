from pydantic import BaseModel
from typing import Any, Optional
from datetime import datetime


class APIResponse(BaseModel):
    success: bool = True
    message: str = ""
    data: Any = None
    error: Optional[str] = None


class ModelInfo(BaseModel):
    key: str
    display_name: str
    provider: str
    endpoint: str
    status: str = "available"  # "available" | "unavailable"


class PromptFile(BaseModel):
    path: str
    filename: str
    usage: str = ""


class PromptDir(BaseModel):
    path: str
    dirname: str
    files: list[PromptFile] = []
    subdirs: list["PromptDir"] = []


class PromptContent(BaseModel):
    path: str
    filename: str
    content: str


class TaskInfo(BaseModel):
    id: str
    type: str  # "pdf_convert" | "translate"
    filename: str
    status: str  # "queued" | "running" | "completed" | "failed"
    created_at: datetime
    completed_at: Optional[datetime] = None
    error: Optional[str] = None
