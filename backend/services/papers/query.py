"""结构化论文查询模型 + 跨平台查询构建（arXiv/Crossref/OpenAlex 用高级语法）。

现状：paper-search 把同一关键词喂给所有平台。这里让结构化条件
（keyword/title/author/year/category）能映射到各平台的原生语法，
避免只靠关键词、错失平台高级检索能力。

用途：`run_task.search_all()` / `pipeline.search()` 接收 PaperQuery，
每源用 `build_*_params()` 生成平台查询。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PaperQuery:
    """统一结构化论文查询。字段任选，空字段不参与过滤。"""

    keyword: str = ""
    title: str = ""
    author: str = ""
    year_from: int = 0       # 包含
    year_to: int = 0         # 包含
    category: str = ""       # arXiv 分类（如 cs.CR）；OpenAlex 用 topics.search 兜底
    db_types: list[str] = field(default_factory=list)  # 万方文献类型（journal/thesis/conference/patent/standard）


# ── arXiv ───────────────────────────────────────────

def build_arxiv_query(q: PaperQuery) -> str:
    """arXiv 查询串：all:/ti:/au:/cat:/submittedDate，AND 连接。"""
    parts = []
    if q.keyword:
        parts.append(f'all:"{q.keyword}"')
    if q.title:
        parts.append(f'ti:"{q.title}"')
    if q.author:
        parts.append(f'au:"{q.author}"')
    if q.category:
        parts.append(f"cat:{q.category}")
    if q.year_from or q.year_to:
        df = f"{q.year_from}0101" if q.year_from else "00000101"
        dt = f"{q.year_to}1231" if q.year_to else "99991231"
        parts.append(f"submittedDate:[{df} TO {dt}]")
    return " AND ".join(parts) if parts else 'all:" "'


# ── Crossref ────────────────────────────────────────

def build_crossref_params(q: PaperQuery, *, rows: int = 20, type_filter: str = "",
                         sort_by_citations: bool = False) -> list[str]:
    """Crossref URL 参数段：query.bibliographic/title/author + filter(日期/类型) + 排序。"""
    params = [f"rows={rows}", "select=DOI,title,author,issued,container-title,type,is-referenced-by-count"]
    if q.keyword:
        params.append(f"query.bibliographic={quote(q.keyword)}")
    if q.title:
        params.append(f"query.title={quote(q.title)}")
    if q.author:
        params.append(f"query.author={quote(q.author)}")
    filters = []
    if q.year_from:
        filters.append(f"from-pub-date:{q.year_from}-01-01")
    if q.year_to:
        filters.append(f"until-pub-date:{q.year_to}-12-31")
    if type_filter:
        filters.append(f"type:{type_filter}")
    if filters:
        params.append(f"filter={','.join(filters)}")
    if sort_by_citations:
        params.append("sort=is-referenced-by-count&order=desc")
    return params


# ── OpenAlex ────────────────────────────────────────

def build_openalex_params(q: PaperQuery, *, per_page: int = 20) -> tuple[str, str]:
    """OpenAlex：(search 关键词, filter 串)。filter 含 title.search/author.search/日期/topics.search。"""
    search = q.keyword or q.title or ""
    flt: list[str] = []
    if q.title:
        flt.append(f"title.search:{q.title}")
    if q.author:
        flt.append(f"author.search:{q.author}")
    if q.year_from:
        flt.append(f"from_publication_date:{q.year_from}-01-01")
    if q.year_to:
        flt.append(f"to_publication_date:{q.year_to}-12-31")
    if q.category:
        flt.append(f"topics.search:{q.category}")
    return search, ",".join(flt)


def quote(s: str) -> str:
    import urllib.parse
    return urllib.parse.quote(s, safe="")
