from pydantic import BaseModel
from typing import Optional, Literal
from datetime import date


class SurveyRequest(BaseModel):
    raw_query: str = ""
    keywords: str = ""          # 兼容旧版，优先用 raw_query
    category: str = ""
    date_from: Optional[str] = None   # YYYYMMDD
    date_to: Optional[str] = None     # YYYYMMDD
    max_results: int = 50
    auto_download: bool = False
    auto_convert: bool = False
    auto_translate: bool = False     # 转换完成后自动翻译
    top_n: int = 15                  # 下载 top-N 篇 PDF
    translate_model: str = "qwen3:8b"
    content_type: str = "paper"
    detect_lang: bool = True


class QuickSearchRequest(BaseModel):
    keywords: str = ""
    raw_query: str = ""
    title: str = ""
    author: str = ""
    category: str = ""
    abstract: str = ""
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    max_results: int = 10
    sort_by: str = "relevance"
    sort_order: str = "descending"
    page: int = 1               # 前端传页码，后端转 start


class ParseQueryRequest(BaseModel):
    text: str = ""
    md_content: str = ""        # .md 文件内容


class ParseQueryResponse(BaseModel):
    raw_query: str
    explanation: str = ""
    parsed: dict = {}           # 拆解出的字段 {query, title, author, category, ...}


class PaperSummary(BaseModel):
    title: str
    authors: list[str] = []
    year: int = 0
    arxiv_id: str = ""
    pdf_url: str = ""
    entry_url: str = ""
    citations: int = 0
    ccf_rank: str | None = None
    score: float = 0.0
    abstract: str = ""
    primary_category: str = ""
    is_accepted: bool = False
    has_code_or_data: bool = False
    page_count: int | None = None


class ProgressInfo(BaseModel):
    percent: int = 0
    current_step: str = ""
    detail: str = ""


class SurveyResult(BaseModel):
    task_id: str
    status: str = "pending"  # pending | running | completed | failed
    papers: list[PaperSummary] = []
    progress: ProgressInfo | None = None
    error: str | None = None
