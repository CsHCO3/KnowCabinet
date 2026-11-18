"""OpenAlex 检索源：标题搜索（评分匹配）+ filter=doi 解析 + OA 字段。

探路结论：
- OpenAlex 标题搜索的 top 命中可能是低质预印本/错误记录（实测 "Attention is all
  you need" 首条指向垃圾 DOI），必须用 评分匹配（arXiv 链接 +100 / 作者姓氏 +50 / 年份±2 +20）
  而不是直接取第一条。
- primary_location.source 对 arXiv 预印本记录常为空 → venue 以 Crossref 为准，此处兜底。
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from backend.services.papers.http_client import ROUTE_DIRECT, http_get_json, throttle

API = "https://api.openalex.org"
UA_MAILTO = ""


# ── 低层 ─────────────────────────────────────────

def _get(url: str) -> dict[str, Any]:
    throttle("openalex", 0.1)
    return http_get_json(url, route=ROUTE_DIRECT, mailto=UA_MAILTO)


def _reconstruct_abstract(w: dict[str, Any]) -> str:
    inv = w.get("abstract_inverted_index") or {}
    if not inv:
        return ""
    pos: dict[int, str] = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[i] = word
    return " ".join(pos[i] for i in sorted(pos))


def _venue(w: dict[str, Any]) -> tuple[str, str]:
    """返回 (venue_name, venue_type)，优先 primary_location.source，兜底 best_oa。"""
    loc = w.get("primary_location") or {}
    src = loc.get("source") or {}
    if src.get("display_name"):
        return src["display_name"], src.get("type") or ""
    loc2 = w.get("best_oa_location") or {}
    src2 = loc2.get("source") or {}
    return src2.get("display_name") or "", src2.get("type") or ""


def _authors(w: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for au in w.get("authorships") or []:
        a = au.get("author") or {}
        if a.get("display_name"):
            names.append(a["display_name"])
    return names


def _arxiv_id_from_locations(w: dict[str, Any]) -> str:
    for loc in w.get("locations") or []:
        url = (loc.get("landing_page_url") or "") + (loc.get("pdf_url") or "")
        m = re.search(r"(?:/abs/|/pdf/)(\d{4}\.\d{4,5})", url)
        if m:
            return m.group(1)
    return ""


def _to_record(w: dict[str, Any]) -> dict[str, Any]:
    venue, vtype = _venue(w)
    oa = w.get("open_access") or {}
    return {
        "title": w.get("title", ""),
        "authors": _authors(w),
        "year": w.get("publication_year"),
        "doi": re.sub(r"^https?://doi\.org/", "", w.get("doi") or "").lower(),
        "arxiv_id": _arxiv_id_from_locations(w),
        "venue": venue,
        "venue_type": vtype,
        "citations": int(w.get("cited_by_count") or 0),
        "is_oa": bool(oa.get("is_oa")),
        "oa_url": oa.get("oa_url") or "",
        "abstract": _reconstruct_abstract(w),
        "openalex_id": w.get("id", ""),
        "oa_status": oa.get("oa_status", ""),
    }


# ── 检索 ─────────────────────────────────────────

def search_works(query: str, *, per_page: int = 20, title: str = "", author: str = "",
                 year_from: int = 0, year_to: int = 0, category: str = "",
                 **filters: Any) -> list[dict[str, Any]]:
    """按关键词/结构化条件搜索 works，返回原始 work 列表。

    title/author/year_from/year_to/category 映射到 OpenAlex filter（title.search/author.search/日期/topics.search）。
    """
    from backend.services.papers.query import PaperQuery, build_openalex_params
    q = PaperQuery(keyword=query, title=title, author=author,
                   year_from=year_from, year_to=year_to, category=category)
    search, filter_str = build_openalex_params(q, per_page=per_page)
    url = f"{API}/works?search={quote(search or ' ')}&per-page={per_page}"
    if filter_str:
        url += f"&filter={quote(filter_str)}"
    for k, v in filters.items():
        url += f"&filter={quote(str(v))}"
    data = _get(url)
    return data.get("results") or []


def search_records(query: str, *, per_page: int = 20, title: str = "", author: str = "",
                   year_from: int = 0, year_to: int = 0, category: str = "",
                   **filters: Any) -> list[dict[str, Any]]:
    """按关键词/结构化条件搜索并返回统一记录 dict 列表。"""
    return [_to_record(w) for w in search_works(
        query, per_page=per_page, title=title, author=author,
        year_from=year_from, year_to=year_to, category=category, **filters)]


def resolve_doi(doi: str) -> dict[str, Any] | None:
    """filter=doi 直查，返回统一记录 dict；未找到返回 None。"""
    from backend.services.papers.record import normalize_doi

    nd = normalize_doi(doi)
    if not nd:
        return None
    try:
        data = _get(f"{API}/works?filter=doi:{quote(nd)}&per-page=1")
    except Exception:
        return None
    results = data.get("results") or []
    if not results:
        return None
    return _to_record(results[0])


# ── 评分匹配（来自 infosystem enrich.py 的思路，自包含实现）──

def _has_arxiv_link(w: dict[str, Any], arxiv_id: str) -> bool:
    if not arxiv_id:
        return False
    a, p = f"/abs/{arxiv_id}", f"/pdf/{arxiv_id}"
    for loc in w.get("locations") or []:
        landing = (loc.get("landing_page_url") or "") + (loc.get("pdf_url") or "")
        if a in landing or p in landing:
            return True
    return False


def _author_last_match(w: dict[str, Any], authors: list[str]) -> bool:
    if not authors:
        return False
    last_names = {a.strip().lower().split()[-1] for a in authors if a.strip()}
    if not last_names:
        return False
    for au in w.get("authorships") or []:
        name = ((au.get("author") or {}).get("display_name") or "").lower()
        if any(ln in name for ln in last_names):
            return True
    return False


def best_work_by_title(
    title: str,
    authors: list[str] | None = None,
    year: int | None = None,
    *,
    per_page: int = 20,
) -> dict[str, Any] | None:
    """按标题搜 OpenAlex，用 (arXiv链接/作者姓氏/年份) 打分取最佳，返回统一记录 dict。"""
    works = search_works(title, per_page=per_page)
    if not works:
        return None

    # 期望的 arxiv_id：从标题记录本身无法预知，先按作者+年份匹配
    scored: list[tuple[int, dict[str, Any]]] = []
    for w in works:
        score = 0
        # 作者末位姓氏匹配
        if authors and _author_last_match(w, authors):
            score += 50
        # 年份匹配（±2）
        wy = w.get("publication_year")
        if year and wy and abs(wy - year) <= 2:
            score += 20
        if score > 0:
            scored.append((score, w))

    if not scored:
        return None
    scored.sort(key=lambda x: (x[0], x[1].get("cited_by_count") or 0), reverse=True)
    return _to_record(scored[0][1])


# ── 相似文献（可选）──────────────────────────────

def related_works(openalex_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
    """基于 openalex_id 找相似文献，返回统一记录 dict 列表。"""
    try:
        data = _get(f"{API}/works/{openalex_id}/related_works?per-page={limit}")
    except Exception:
        return []
    return [_to_record(w) for w in (data.get("results") or [])]
