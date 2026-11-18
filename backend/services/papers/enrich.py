"""OpenAlex 论文引用/venue 丰富模块。

提供基于 arXiv paper 标题、ID、作者、年份在 OpenAlex 中
匹配并获取引用量、发表 venue、CCF 等级等信息的函数。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from backend.services.arxiv.search import ArxivPaper

logger = logging.getLogger(__name__)

OPENALEX_BASE = "https://api.openalex.org"
USER_AGENT = "Infosystem/1.0 (arxiv-survey-pipeline)"
_REQUEST_INTERVAL = 0.1  # 100ms between request dispatches (~10 req/s)
_throttle_lock = threading.Lock()
_last_request_time = 0.0

# ── 低层级 API ─────────────────────────────────────


def _throttled_open(url: str, timeout: int = 15) -> dict[str, Any]:
    """OpenAlex API GET，带线程安全速率控制。"""
    global _last_request_time
    with _throttle_lock:
        now = time.monotonic()
        elapsed = now - _last_request_time
        if _last_request_time > 0 and elapsed < _REQUEST_INTERVAL:
            time.sleep(_REQUEST_INTERVAL - elapsed)
        _last_request_time = time.monotonic()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    resp = urllib.request.urlopen(req, timeout=timeout)
    return json.loads(resp.read())


def _search_by_title(title: str, per_page: int = 20) -> list[dict[str, Any]]:
    """按标题搜索 OpenAlex，返回 works 列表。"""
    url = f"{OPENALEX_BASE}/works?search={urllib.parse.quote(title)}&per-page={per_page}"
    data = _throttled_open(url)
    return data.get("results", [])


# ── 匹配逻辑 ───────────────────────────────────────


def _get_venue(w: dict[str, Any]) -> str:
    """从 OpenAlex work 中提取 venue 名。"""
    loc = w.get("primary_location") or {}
    src = loc.get("source") or {}
    return src.get("display_name") or ""


def _get_venue_type(w: dict[str, Any]) -> str:
    """从 OpenAlex work 中提取 venue 类型。"""
    loc = w.get("primary_location") or {}
    src = loc.get("source") or {}
    return src.get("type") or ""


def _check_arxiv_link(w: dict[str, Any], arxiv_id: str) -> bool:
    """检查 OpenAlex work 的 locations 中是否包含指定 arxiv ID 的链接。"""
    target_abs = f"/abs/{arxiv_id}"
    target_pdf = f"/pdf/{arxiv_id}"
    for loc in w.get("locations") or []:
        if not loc:
            continue
        landing = loc.get("landing_page_url") or ""
        if target_abs in landing or target_pdf in landing:
            return True
    return False


def _check_author_match(w: dict[str, Any], authors: list[str]) -> bool:
    """检查 OpenAlex work 的作者列表中是否包含任一给定作者的末位姓氏。"""
    if not authors:
        return False
    last_names = {a.strip().lower().split()[-1] for a in authors if a.strip()}
    if not last_names:
        return False
    for authorship in w.get("authorships") or []:
        author = authorship.get("author") or {}
        display_name = (author.get("display_name") or "").lower()
        if any(ln in display_name for ln in last_names):
            return True
    return False


def _extract_authors(w: dict[str, Any]) -> list[str]:
    """从 OpenAlex work 中提取作者名列表。"""
    names = []
    for authorship in w.get("authorships") or []:
        author = authorship.get("author") or {}
        name = author.get("display_name")
        if name:
            names.append(name)
    return names


# ── 公开函数 ───────────────────────────────────────


def enrich_paper(
    paper: ArxivPaper,
    *,
    year_window: int = 2,
) -> dict[str, Any]:
    """查 OpenAlex 获取单篇 arXiv 论文的引用量 / venue / 类型。

    Args:
        paper: arXiv 论文对象（含 arxiv_id, title, authors, published）。
        year_window: 年份匹配允许的偏差（±year_window）。

    Returns:
        {
            "cited_by_count": int,
            "venue": str,
            "venue_type": str,
            "publication_year": int | None,
            "is_oa": bool,
            "openalex_id": str,
            "matched_title": str,
            "matched_authors": list[str],
            "enrich_status": "exact_arxiv_link" | "author_year_match" | "no_match" | "error",
            "error": str | None,  # 仅 enrich_status=="error" 时有值
        }
    """
    # 解析发表年份
    pub_year: int | None = None
    if paper.published:
        try:
            pub_year = int(paper.published[:4])
        except (ValueError, TypeError):
            pass

    first_author_last = ""
    if paper.authors:
        parts = paper.authors[0].strip().lower().split()
        if parts:
            first_author_last = parts[-1]

    result: dict[str, Any] = {
        "cited_by_count": 0,
        "venue": "",
        "venue_type": "",
        "publication_year": pub_year,
        "is_oa": False,
        "openalex_id": "",
        "matched_title": paper.title,
        "matched_authors": [],
        "enrich_status": "no_match",
        "error": None,
    }

    try:
        works = _search_by_title(paper.title)
    except Exception as exc:
        result["enrich_status"] = "error"
        result["error"] = f"OpenAlex search failed: {exc}"
        logger.warning("enrich_paper(%s): %s", paper.arxiv_id, result["error"])
        return result

    if not works:
        return result

    # 评分每条候选
    scored: list[tuple[dict[str, Any], int]] = []  # (work, score)
    for w in works:
        score = 0

        # arXiv 链接精确匹配（最强信号）
        if _check_arxiv_link(w, paper.arxiv_id):
            score += 100

        # 作者末位姓氏匹配
        if first_author_last and _check_author_match(w, paper.authors):
            score += 50

        # 年份匹配
        wy = w.get("publication_year")
        if pub_year and wy and abs(wy - pub_year) <= year_window:
            score += 20

        if score > 0:
            scored.append((w, score))

    if not scored:
        return result

    # 按 (分数, 引用量) 降序取最佳
    scored.sort(key=lambda x: (x[1], x[0].get("cited_by_count", 0)), reverse=True)
    best = scored[0][0]
    best_score = scored[0][1]

    # 判定匹配类型
    is_exact = _check_arxiv_link(best, paper.arxiv_id)
    status = "exact_arxiv_link" if is_exact else "author_year_match"

    result.update({
        "cited_by_count": best.get("cited_by_count", 0),
        "venue": _get_venue(best),
        "venue_type": _get_venue_type(best),
        "publication_year": best.get("publication_year"),
        "is_oa": best.get("open_access", {}).get("is_oa", False),
        "openalex_id": best.get("id", ""),
        "matched_title": best.get("title", ""),
        "matched_authors": _extract_authors(best),
        "enrich_status": status,
    })

    return result


def batch_enrich(
    papers: list[ArxivPaper],
    workers: int = 10,
    show_progress: bool = True,
    progress_callback: Callable[[int, int], None] | None = None,
) -> list[dict[str, Any]]:
    """并发批量查 OpenAlex 丰富论文信息。

    Args:
        papers: arXiv 论文列表。
        workers: 并发线程数。
        show_progress: 是否打印进度到 stderr。
        progress_callback: 可选回调 (done, total)，每完成一篇调用一次。

    Returns:
        与输入等长的 enrich 结果列表（顺序一致）。
    """
    results: list[dict[str, Any] | None] = [None] * len(papers)

    def _do(i: int, p: ArxivPaper) -> tuple[int, dict[str, Any]]:
        return i, enrich_paper(p)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_do, i, p): i for i, p in enumerate(papers)}
        done = 0
        total = len(papers)
        for future in as_completed(futures):
            idx = futures[future]
            try:
                _, data = future.result()
                results[idx] = data
            except Exception as exc:
                results[idx] = {
                    "cited_by_count": 0,
                    "venue": "",
                    "venue_type": "",
                    "publication_year": None,
                    "is_oa": False,
                    "openalex_id": "",
                    "matched_title": papers[idx].title if idx < len(papers) else "",
                    "matched_authors": [],
                    "enrich_status": "error",
                    "error": f"Unexpected error: {exc}",
                }
            done += 1
            if progress_callback:
                try:
                    progress_callback(done, total)
                except Exception:
                    pass
            if show_progress and (done % 10 == 0 or done == total):
                print(
                    f"  [enrich] {done}/{total}", flush=True,
                )

    return results  # type: ignore[return-value]


# ── 命令行测试 ─────────────────────────────────────


def _demo():
    """用几篇知名论文演示 enrich 功能。"""
    papers = [
        ArxivPaper(
            arxiv_id="1706.03762",
            title="Attention is all you need",
            authors=["Ashish Vaswani", "Noam Shazeer", "Niki Parmar"],
            published="2017-06-12",
        ),
        ArxivPaper(
            arxiv_id="1512.03385",
            title="Deep residual learning for image recognition",
            authors=["Kaiming He", "Xiangyu Zhang", "Shaoqing Ren"],
            published="2015-12-10",
        ),
        ArxivPaper(
            arxiv_id="1603.04467",
            title="TensorFlow: a system for large-scale machine learning",
            authors=["Martín Abadi", "Ashish Agarwal"],
            published="2016-03-14",
        ),
        ArxivPaper(
            arxiv_id="1810.04805",
            title="BERT: pre-training of deep bidirectional transformers for language understanding",
            authors=["Jacob Devlin", "Ming-Wei Chang", "Kenton Lee"],
            published="2018-10-11",
        ),
    ]

    print(f"{'arxiv_id':<15} {'status':20s} {'cit':>6s}  {'year':4s}  {'venue':30s}")
    print("-" * 80)
    for p in papers:
        r = enrich_paper(p)
        status = r["enrich_status"]
        print(
            f"{p.arxiv_id:<15} {status:<20s} {r['cited_by_count']:>6d}  "
            f"{str(r['publication_year'] or '?'):4s}  {r['venue'][:30]:30s}"
        )
        time.sleep(0.5)


if __name__ == "__main__":
    _demo()
