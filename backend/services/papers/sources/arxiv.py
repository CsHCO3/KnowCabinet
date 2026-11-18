"""arXiv 检索源（best-effort，带长退避）。

探路结论：
- arXiv API 从本机被限流（429 "Rate exceeded"），且走 Clash 共享出口 IP 更易被限流。
- 对策：HTTP 走 http_client **直连路由**；请求间隔 ≥3s；429/超时用长退避
  （5s/20s/60s）；最终失败**降权**——返回空列表并告警，不阻断 pipeline 其它源。
- 复用 search.py 的查询构造与 Atom 解析（纯函数，无 HTTP），HTTP 自己走直连。
"""

from __future__ import annotations

import logging
import time
from typing import Any

import urllib.error

from backend.services.papers.http_client import ROUTE_DIRECT, http_get, throttle
from backend.services.papers.search import ArxivSearchParams, _build_request_url, parse_arxiv_feed

logger = logging.getLogger(__name__)

# arXiv 长退避序列（秒）；429 后静默冷却更久
_ARXIV_BACKOFF = [5.0, 20.0, 60.0]


def _paper_to_record(p: Any) -> dict[str, Any]:
    """ArxivPaper → 统一 dict（与 Pipeline/record 兼容）。"""
    year = None
    if p.published and p.published[:4].isdigit():
        year = int(p.published[:4])
    return {
        "title": p.title,
        "authors": p.authors,
        "year": year,
        "doi": p.doi,
        "arxiv_id": p.arxiv_id,
        "venue": "",          # arXiv 预印本无 venue，留给 enrich 补
        "venue_type": "repository",
        "citations": 0,
        "is_oa": True,
        "oa_url": p.pdf_url or f"https://arxiv.org/pdf/{p.arxiv_id}.pdf",
        "abstract": p.summary,
        "journal_ref": p.journal_ref,
        "comment": p.comment,
    }


def search_records(
    query: str,
    *,
    max_results: int = 20,
    date_from: str = "",
    date_to: str = "",
    sort_by: str = "relevance",
    title: str = "",
    author: str = "",
    category: str = "",
    year_from: int = 0,
    year_to: int = 0,
) -> list[dict[str, Any]]:
    """按关键词/结构化条件搜 arXiv（best-effort）。失败/限流返回空列表并告警。

    title/author/category/year_from/year_to 会映射到 arXiv 高级语法（ti:/au:/cat:/submittedDate）。
    """
    from backend.services.papers.query import PaperQuery, build_arxiv_query
    q = PaperQuery(keyword=query, title=title, author=author, category=category,
                   year_from=year_from, year_to=year_to)
    params = ArxivSearchParams(
        raw_query=build_arxiv_query(q),
        max_results=max_results,
        date_from=date_from,
        date_to=date_to,
        sort_by=sort_by,
        sort_order="descending",
    )
    url = _build_request_url(params)
    body: bytes | None = None

    for attempt, backoff in enumerate(_ARXIV_BACKOFF):
        throttle("arxiv", 3.0)
        try:
            _, body = http_get(url, route=ROUTE_DIRECT, timeout=25, retries=1)
            break
        except urllib.error.HTTPError as e:
            if e.code == 429:
                logger.warning("arxiv 429 (attempt %d/%d)，冷却 %.0fs", attempt + 1, len(_ARXIV_BACKOFF), backoff)
                time.sleep(backoff)
                continue
            logger.warning("arxiv HTTP %s", e.code)
            time.sleep(backoff)
            continue
        except Exception as e:
            logger.warning("arxiv 请求失败 %s", type(e).__name__)
            time.sleep(backoff)
            continue
    else:
        logger.warning("arxiv 多次尝试仍失败，降权跳过该源（best-effort）")
        return []

    try:
        papers = parse_arxiv_feed(body.decode("utf-8"))
    except Exception as e:
        logger.warning("arxiv 解析失败: %s", e)
        return []
    return [_paper_to_record(p) for p in papers]
