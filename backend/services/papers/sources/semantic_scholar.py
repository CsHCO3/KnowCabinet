"""Semantic Scholar 源：仅 paper/DOI 查询（丰富用）。

探路结论：search 端点免费层共享池长期 429，但 `paper/DOI:` 端点稳定。
所以本模块只做 DOI → tldr/openAccessPdf/citationCount 查询，不做关键词搜索。
免费层限速约 1/s，节流设 1.2s。
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

from backend.services.papers.http_client import ROUTE_DIRECT, http_get_json, throttle

logger = logging.getLogger(__name__)

API = "https://api.semanticscholar.org/graph/v1"
_FIELDS = "title,year,citationCount,openAccessPdf,tldr,venue,externalIds,paperId,authors"


def resolve_doi(doi: str) -> dict[str, Any] | None:
    """DOI → S2 记录 dict（含 openAccessPdf/tldr/citationCount）。"""
    from backend.services.papers.record import normalize_doi

    nd = normalize_doi(doi)
    if not nd:
        return None
    url = f"{API}/paper/DOI:{quote(nd)}?fields={_FIELDS}"
    throttle("s2", 1.2)
    try:
        d = http_get_json(url, route=ROUTE_DIRECT, retries=2, backoff=2.0)
    except Exception as e:
        logger.debug("S2 resolve_doi(%s) 失败: %s", nd, e)
        return None
    if not d or "title" not in d:
        return None
    oa = d.get("openAccessPdf") or {}
    tldr = d.get("tldr") or {}
    return {
        "paper_id": d.get("paperId", ""),
        "title": d.get("title", ""),
        "authors": [a.get("name", "") for a in (d.get("authors") or []) if a.get("name")],
        "year": d.get("year"),
        "venue": d.get("venue", ""),
        "citations": int(d.get("citationCount") or 0),
        "is_oa": bool(oa.get("url")),
        "oa_url": oa.get("url", ""),
        "oa_status": oa.get("status", ""),
        "tldr": tldr.get("text", ""),
        "external_ids": d.get("externalIds") or {},
    }
