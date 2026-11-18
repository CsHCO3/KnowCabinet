"""arXiv Survey Pipeline 编排入口。

流程: 搜索 → enrich → CCF 分级 → 排序 → 导出 → 下载。
"""
from __future__ import annotations

import csv
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from backend.services.arxiv.search import (
    ArxivPaper,
    ArxivSearchParams,
    search_arxiv_bulk,
    download_paper_from_source,
)
from backend.services.arxiv.enrich import batch_enrich
from backend.services.arxiv.ccf import get_ccf_level
from backend.services.arxiv.ranker import rank_papers

logger = logging.getLogger(__name__)


def _safe_slug(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^A-Za-z0-9_一-鿿]+", "_", text).strip("_")
    return slug[:max_len] if len(slug) > max_len else slug


def _merge_enrich(paper: ArxivPaper, enrich_result: dict[str, Any]) -> dict[str, Any]:
    ccf_level = get_ccf_level(enrich_result.get("venue", ""))
    return {
        "arxiv_id": paper.arxiv_id,
        "title": paper.title,
        "authors": paper.authors,
        "published": paper.published,
        "primary_category": paper.primary_category,
        "categories": paper.categories,
        "entry_url": paper.entry_url,
        "pdf_url": paper.pdf_url or f"https://arxiv.org/pdf/{paper.arxiv_id}.pdf",
        "comment": paper.comment,
        "journal_ref": paper.journal_ref,
        "is_accepted": paper.is_accepted,
        "has_code_or_data": paper.has_code_or_data,
        "code_data_links": paper.code_data_links,
        "abstract": (paper.summary or "")[:300],
        "cited_by_count": enrich_result.get("cited_by_count", 0),
        "venue": enrich_result.get("venue", ""),
        "venue_type": enrich_result.get("venue_type", ""),
        "publication_year": enrich_result.get("publication_year"),
        "is_oa": enrich_result.get("is_oa", False),
        "openalex_id": enrich_result.get("openalex_id", ""),
        "enrich_status": enrich_result.get("enrich_status", "no_match"),
        "ccf_level": ccf_level,
    }


def run_survey(
    *,
    raw_query: str,
    max_papers: int = 200,
    top_n: int = 15,
    output_dir: str | None = None,
    download: bool = False,
    workers: int = 5,
    cit_weight: float = 0.5,
    ccf_weight: float = 0.3,
    time_weight: float = 0.2,
    progress_callback: Callable[[int, str], None] | None = None,
) -> dict[str, Any]:
    """运行完整 survey pipeline。

    Args:
        progress_callback: 可选的回调函数，接收 (percent, current_step) 用于进度报告。

    Returns:
        {"query", "total_searched", "enriched", "enrich_failed",
         "json_path", "csv_path", "top_downloads", "summary"}
    """
    start_time = time.time()

    def cb(pct: int, step: str) -> None:
        if progress_callback:
            progress_callback(pct, step)

    cb(2, "准备搜索 arXiv ...")
    params = ArxivSearchParams(raw_query=raw_query, max_results=100)
    print(f"搜索 arXiv: {raw_query}")
    papers = search_arxiv_bulk(params, max_total=max_papers)
    print(f"  找到 {len(papers)} 篇论文")
    if not papers:
        cb(100, "未找到结果")
        return {"error": "arXiv 未返回结果", "total_searched": 0}

    cb(10, f"搜索完成，共 {len(papers)} 篇，正在查询 OpenAlex 引用数据 ...")
    print("查询 OpenAlex 引用数据 ...")

    def _on_enrich_progress(done: int, total: int) -> None:
        """将 enrich 进度 (done/total) 映射到 10%→90% 区间。"""
        pct = 10 + int(done * 80 / total) if total > 0 else 10
        cb(pct, f"查询 OpenAlex 引用数据 ({done}/{total}) ...")

    enrich_results = batch_enrich(
        papers, workers=workers, show_progress=True,
        progress_callback=_on_enrich_progress,
    )
    enriched_count = sum(
        1 for r in enrich_results if r and r.get("enrich_status") not in ("no_match", "error")
    )
    failed_count = sum(
        1 for r in enrich_results if r and r.get("enrich_status") == "error"
    )
    print(f"  {enriched_count} 篇丰富成功, {failed_count} 篇失败")

    cb(90, f"引用数据丰富完成，{enriched_count} 篇成功，正在排序 ...")
    merged = []
    for p, r in zip(papers, enrich_results):
        if r:
            merged.append(_merge_enrich(p, r))
        else:
            merged.append(_merge_enrich(p, {
                "cited_by_count": 0, "venue": "", "venue_type": "",
                "publication_year": None, "is_oa": False, "openalex_id": "",
                "enrich_status": "error",
            }))

    ranked = rank_papers(
        merged,
        cit_weight=cit_weight,
        ccf_weight=ccf_weight,
        time_weight=time_weight,
    )

    cb(93, "排序完成，正在导出结果 ...")
    from backend.utils.paths import Paths
    output_path = Path(output_dir) if output_dir else Paths().arxiv_queries_surveys
    output_path.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = _safe_slug(raw_query[:60])
    base_name = f"{timestamp}_{slug}"

    json_path = str(output_path / f"{base_name}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({
            "meta": {
                "query": raw_query,
                "total_searched": len(papers),
                "enriched": enriched_count,
                "enrich_failed": failed_count,
                "timestamp": datetime.now().isoformat(),
                "sort_weights": {
                    "citation": cit_weight,
                    "ccf": ccf_weight,
                    "time": time_weight,
                },
            },
            "papers": ranked,
        }, f, ensure_ascii=False, indent=2)
    print(f"  导出 JSON: {json_path}")

    csv_path = str(output_path / f"{base_name}.csv")
    _export_csv(csv_path, ranked)
    print(f"  导出 CSV: {csv_path}")

    cb(97, "导出完成")

    top_downloads: list[str] = []
    if download and ranked:
        from backend.utils.paths import Paths
        pdf_dir = Paths().pdfs
        pdf_dir.mkdir(parents=True, exist_ok=True)
        top_n = min(top_n, len(ranked))
        print(f"下载 top {top_n} PDF ...")
        for i in range(top_n):
            paper = ranked[i]
            cb(97 + int(3 * (i + 1) / top_n), f"下载 PDF ({i+1}/{top_n}) ...")
            try:
                source = paper.get("arxiv_id") or paper.get("pdf_url", "")
                if not source:
                    continue
                path = download_paper_from_source(
                    source=source,
                    output_dir=str(pdf_dir),
                    title_hint=paper.get("title", "")[:60],
                    timeout=60,
                )
                top_downloads.append(path)
                print(f"  [{i+1}/{top_n}] {paper.get('title', '')[:50]:50s} → {path}")
            except Exception as exc:
                print(f"  [{i+1}/{top_n}] {paper.get('title', '')[:50]:50s} → 下载失败: {exc}")

    elapsed = time.time() - start_time
    cb(100, f"完成，耗时 {elapsed:.0f}s")
    print(f"完成，耗时 {elapsed:.0f}s")

    summary = _build_summary(ranked, enriched_count, failed_count, elapsed)
    print(summary)

    return {
        "query": raw_query,
        "total_searched": len(papers),
        "enriched": enriched_count,
        "enrich_failed": failed_count,
        "json_path": json_path,
        "csv_path": csv_path,
        "top_downloads": top_downloads,
        "summary": summary,
    }


def _export_csv(path: str, papers: list[dict[str, Any]]) -> None:
    if not papers:
        return
    fields = [
        "rank", "rank_score", "arxiv_id", "title", "cited_by_count",
        "ccf_level", "venue", "venue_type", "publication_year",
        "is_oa", "is_accepted", "has_code_or_data",
        "primary_category", "authors", "published",
        "entry_url", "pdf_url",
    ]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for p in papers:
            row = {
                "rank": p.get("rank", ""),
                "rank_score": p.get("rank_score", ""),
                "arxiv_id": p.get("arxiv_id", ""),
                "title": p.get("title", ""),
                "cited_by_count": p.get("cited_by_count", 0),
                "ccf_level": p.get("ccf_level", ""),
                "venue": p.get("venue", ""),
                "venue_type": p.get("venue_type", ""),
                "publication_year": p.get("publication_year", ""),
                "is_oa": p.get("is_oa", False),
                "is_accepted": p.get("is_accepted", False),
                "has_code_or_data": p.get("has_code_or_data", False),
                "primary_category": p.get("primary_category", ""),
                "authors": "; ".join(p.get("authors", [])),
                "published": p.get("published", ""),
                "entry_url": p.get("entry_url", ""),
                "pdf_url": p.get("pdf_url", ""),
            }
            writer.writerow(row)


def _build_summary(
    ranked: list[dict[str, Any]],
    enriched: int,
    failed: int,
    elapsed: float,
) -> str:
    lines = [
        "\n" + "=" * 50,
        "Survey 完成摘要",
        "=" * 50,
        f"  搜索论文:     {len(ranked)} 篇",
        f"  引用数丰富:   {enriched} 篇成功",
        f"  丰富失败:     {failed} 篇",
        f"  耗时:         {elapsed:.0f}s",
    ]
    if ranked:
        lines.append("")
        lines.append("  Top 5 论文:")
        for p in ranked[:5]:
            lines.append(
                f"    #{p.get('rank', '?')}: {p.get('title', '')[:70]}"
            )
            lines.append(
                f"        引用={p.get('cited_by_count', 0)}, "
                f"CCF={p.get('ccf_level', '') or '-'}, "
                f"分数={p.get('rank_score', 0)}"
            )
    lines.append("=" * 50)
    return "\n".join(lines)
