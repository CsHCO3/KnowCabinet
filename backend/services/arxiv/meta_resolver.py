"""论文元数据解析：arxiv_id 或 DOI → 完整元数据（补齐缺失信息用）。

复用现成能力：
  arXiv API   —— backend.services.arxiv.search（按 id 抓取 title/authors/year/abstract/category/doi）
  OpenAlex    —— backend.services.arxiv.enrich（引用量/venue/类型/年份）与 works/{doi}（DOI 直查）

输出统一 dict（键：title/authors/year/source/primary_category/abstract/citations/venue/venue_type/
ccf_rank/doi/arxiv_id）。
"""

from __future__ import annotations

import re

from backend.services.arxiv.ccf import get_ccf_level
from backend.services.arxiv.enrich import (
    _extract_authors, _get_venue, _get_venue_type, _throttled_open,
)
from backend.services.arxiv.search import ArxivPaper, ArxivSearchParams, fetch_arxiv_feed, parse_arxiv_feed

OPENALEX_BASE = "https://api.openalex.org"

# 统一的"空"meta 结构
_EMPTY = {
    "arxiv_id": "", "doi": "", "title": "", "authors": [], "year": None,
    "source": "", "primary_category": "", "abstract": "",
    "citations": 0, "venue": "", "venue_type": "", "ccf_rank": "",
}


def _reconstruct_abstract(w: dict) -> str:
    """OpenAlex 摘要为倒排索引，重建为文本。"""
    inv = w.get("abstract_inverted_index") or {}
    if not inv:
        return ""
    pos: dict[int, str] = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[i] = word
    return " ".join(pos[i] for i in sorted(pos))


def fetch_arxiv_paper(arxiv_id: str) -> ArxivPaper | None:
    """按 arxiv_id 抓 arXiv API 单篇。"""
    try:
        feed = fetch_arxiv_feed(ArxivSearchParams(raw_query=f"id:{arxiv_id}"))
        papers = parse_arxiv_feed(feed)
        return papers[0] if papers else None
    except Exception:
        return None


def resolve_by_arxiv(arxiv_id: str) -> dict:
    """arxiv_id → 合并 arXiv 基础元数据 + OpenAlex 丰富。"""
    out = dict(_EMPTY, arxiv_id=arxiv_id)
    paper = fetch_arxiv_paper(arxiv_id)
    if paper:
        year = None
        if paper.published and paper.published[:4].isdigit():
            year = int(paper.published[:4])
        out.update({
            "title": paper.title,
            "authors": paper.authors,
            "year": year,
            "abstract": paper.summary,
            "primary_category": paper.primary_category,
            "doi": paper.doi,
            "source": f"https://arxiv.org/abs/{arxiv_id}",
        })
        # OpenAlex 丰富（引用/venue/ccf）
        try:
            from backend.services.arxiv.enrich import enrich_paper
            er = enrich_paper(paper)
            if er.get("enrich_status") != "error":
                out.update({
                    "citations": er.get("cited_by_count", 0),
                    "venue": er.get("venue", ""),
                    "venue_type": er.get("venue_type", ""),
                    "ccf_rank": get_ccf_level(er.get("venue", "")),
                })
                if not out["year"] and er.get("publication_year"):
                    out["year"] = er["publication_year"]
                if not out["doi"] and er.get("doi"):
                    out["doi"] = er["doi"]
        except Exception:
            pass
    return out


def resolve_by_doi(doi: str) -> dict:
    """DOI → OpenAlex works（filter=doi）全元数据，并尽量反推 arxiv_id。"""
    import urllib.parse
    out = dict(_EMPTY, doi=doi)
    try:
        data = _throttled_open(f"{OPENALEX_BASE}/works?filter=doi:{urllib.parse.quote(doi.strip(), safe='')}&per-page=1")
    except Exception:
        return out
    works = data.get("results") or []
    if not works:
        return out
    w = works[0]
    out.update({
        "title": w.get("title", ""),
        "authors": _extract_authors(w),
        "year": w.get("publication_year"),
        "citations": w.get("cited_by_count", 0),
        "venue": _get_venue(w),
        "venue_type": _get_venue_type(w),
        "abstract": _reconstruct_abstract(w),
        "ccf_rank": get_ccf_level(_get_venue(w)),
        "source": f"https://doi.org/{doi}",
    })
    # 从 locations 反推 arxiv_id
    for loc in w.get("locations") or []:
        url = (loc.get("landing_page_url") or "") + (loc.get("pdf_url") or "")
        m = re.search(r"(?:/abs/|/pdf/)(\d{4}\.\d{4,5})", url)
        if m:
            out["arxiv_id"] = m.group(1)
            break
    return out


def resolve_paper(arxiv_id: str | None = None, doi: str | None = None) -> dict:
    """综合解析：有 doi 先按 DOI，缺字段再用 arxiv_id 补。返回统一 meta dict。"""
    meta = dict(_EMPTY)
    if doi:
        meta.update(resolve_by_doi(doi))
    if arxiv_id:
        am = resolve_by_arxiv(arxiv_id)
        for k in ("title", "authors", "year", "abstract", "primary_category", "source"):
            if not meta.get(k):
                meta[k] = am.get(k)
        if not meta.get("doi"):
            meta["doi"] = am.get("doi")
        if not meta.get("arxiv_id"):
            meta["arxiv_id"] = arxiv_id
        # 引用/venue 以 arxiv enrich 为准（更准），doi 兜底
        if not meta.get("citations"):
            meta["citations"] = am.get("citations", 0)
        if not meta.get("venue"):
            meta["venue"] = am.get("venue", "")
        if not meta.get("ccf_rank"):
            meta["ccf_rank"] = am.get("ccf_rank", "")
    return meta
