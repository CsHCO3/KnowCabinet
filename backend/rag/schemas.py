"""RAG 骨架 — Document 模型和存储接口（待后续填充）"""

from datetime import datetime
from pydantic import BaseModel, Field


class Document(BaseModel):
    """知识库文档单元。"""

    id: str
    source: str  # "arxiv" | "bilibili" | "pdf" | "web" | ...
    title: str
    content: str
    metadata: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.now)


class Chunk(BaseModel):
    """分块后的文档片段。"""

    id: str
    document_id: str
    content: str
    metadata: dict = Field(default_factory=dict)
    embedding: list[float] | None = None
