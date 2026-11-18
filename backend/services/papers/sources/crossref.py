"""Crossref 检索源：bibliographic 搜索 + DOI 单查。

实测结论：Crossref 免费免认证、稳定直连，是 DOI/venue/引用数的权威来源。
"""

from __future__ import annotations

import re
from typing import Any

from backend.services.papers.http_client import CROSSREF_MAILTO, ROUTE_DIRECT, http_get_json, throttle

API = "https://api.crossref.org/works"
_SELECT = "DOI,title,author,issued,container-title,is-referenced-by-count,type,abstract,URL,page,volume,issue"


def _clean_abstract(raw: str | None) -> str:
    """Crossref abstract 是 JATS XML，去掉标签。"""
    if not raw:
        return ""
    return re.sub(r"<[^>]+>", " ", raw)


def _parse_item(w: dict[str, Any]) -> dict[str, Any]:
    year = None
    issued = w.get("issued") or {}
    parts = issued.get("date-parts") or []
    if parts and parts[0]:
        year = parts[0][0]

    authors: list[str] = []
    for a in w.get("author") or []:
        name = a.get("family") or ""
        given = a.get("given") or ""
        full = f"{given} {name}".strip()
        if full:
            authors.append(full)

    container = (w.get("container-title") or [""])[0]
    return {
        "title": (w.get("title") or [""])[0],
        "authors": authors,
        "year": year,
        "doi": (w.get("DOI") or "").lower(),
        "venue": container or "",
        "venue_type": w.get("type", ""),
        "citations": int(w.get("is-referenced-by-count") or 0),
        "abstract": _clean_abstract(w.get("abstract")),
        "link": w.get("URL", ""),
        "volume": w.get("volume", ""),
        "issue": w.get("issue", ""),
        "page": w.get("page", ""),
    }


def search_bibliographic(
    query: str,
    *,
    rows: int = 20,
    from_year: int | None = None,
    type_filter: str | None = None,
    sort_by_citations: bool = False,
    title: str = "",
    author: str = "",
    year_from: int = 0,
    year_to: int = 0,
) -> list[dict[str, Any]]:
    """按关键词/结构化条件搜索。返回统一 dict 列表。

    Args:
        query: 标题/关键词。
        rows: 返回条数。
        from_year: 只返回该年份之后的（兼容旧调用）。
        type_filter: Crossref 类型过滤，如 'journal-article'。
        sort_by_citations: True 则按引用数降序。
        title/author/year_from/year_to: 结构化条件，映射到 query.title/query.author/filter 日期。
    """
    from backend.services.papers.query import PaperQuery, build_crossref_params
    q = PaperQuery(keyword=query, title=title, author=author,
                   year_from=year_from or (from_year or 0), year_to=year_to)
    params = build_crossref_params(q, rows=rows, type_filter=type_filter or "",
                                   sort_by_citations=sort_by_citations)
    url = f"{API}?{'&'.join(params)}"
    throttle("crossref", 1.0)
    data = http_get_json(url, route=ROUTE_DIRECT, mailto=CROSSREF_MAILTO)
    return [_parse_item(w) for w in (data.get("message", {}).get("items") or [])]


def resolve_doi(doi: str) -> dict[str, Any] | None:
    """DOI 单查，返回统一 dict；未找到返回 None。"""
    from backend.services.papers.record import normalize_doi

    nd = normalize_doi(doi)
    if not nd:
        return None
    url = f"{API}/{quote(nd)}"
    throttle("crossref", 1.0)
    try:
        data = http_get_json(url, route=ROUTE_DIRECT, mailto=CROSSREF_MAILTO)
    except Exception:
        return None
    msg = data.get("message")
    if not msg:
        return None
    return _parse_item(msg)


def quote(text: str) -> str:
    from urllib.parse import quote
    return quote(text)
