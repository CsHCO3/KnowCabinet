"""多源检索 pipeline：search(关键词) → 多源检索 → 去重合并 → 字段补齐 → 排序 → 导出。

用法：
    python pipeline.py search "industrial internet" --rows 30 --export-dir exports
"""

from __future__ import annotations

import argparse
import json
import math
import os
from typing import Any

from backend.services.papers.record import PaperRecord, merge_records
from backend.services.papers.sources import arxiv, crossref, google_scholar, openalex, semantic_scholar, wanfang

CURRENT_YEAR = 2026


# ── 检索发现 ─────────────────────────────────────

def search(
    keyword: str,
    *,
    rows: int = 30,
    from_year: int | None = None,
    type_filter: str | None = None,
    use_crossref: bool = True,
    use_openalex: bool = True,
    use_arxiv: bool = True,
    use_gs: bool = False,
    use_wanfang: bool = False,
    enrich_s2: bool = True,
    title: str = "",
    author: str = "",
    year_from: int = 0,
    year_to: int = 0,
    category: str = "",
) -> list[PaperRecord]:
    """多源检索关键词/结构化条件，返回去重合并后的 PaperRecord 列表。

    title/author/year_from/year_to/category 映射到 API 三平台（arXiv/Crossref/OpenAlex）
    的高级语法；GS/万方/知网 仍用关键词。
    arXiv 是 best-effort 源：限流/失败时静默降权，不阻断其余源。
    """
    all_recs: list[PaperRecord] = []

    if use_crossref:
        try:
            for d in crossref.search_bibliographic(
                keyword, rows=rows, from_year=from_year, type_filter=type_filter,
                title=title, author=author, year_from=year_from, year_to=year_to,
            ):
                all_recs.append(PaperRecord(
                    title=d["title"], authors=d["authors"], year=d["year"], doi=d["doi"],
                    venue=d["venue"], venue_type=d["venue_type"], citations=d["citations"],
                    abstract=d["abstract"], sources=["crossref"],
                    links={"official": d.get("link", "")},
                ))
        except Exception as e:
            print(f"[warn] crossref 检索失败: {e}")

    if use_openalex:
        try:
            for d in openalex.search_records(
                keyword, per_page=rows, title=title, author=author,
                year_from=year_from, year_to=year_to, category=category,
            ):
                all_recs.append(PaperRecord(
                    title=d["title"], authors=d["authors"], year=d["year"], doi=d["doi"],
                    arxiv_id=d["arxiv_id"], venue=d["venue"], venue_type=d["venue_type"],
                    citations=d["citations"], is_oa=d["is_oa"], oa_url=d["oa_url"],
                    abstract=d["abstract"], sources=["openalex"],
                ))
        except Exception as e:
            print(f"[warn] openalex 检索失败: {e}")

    if use_arxiv:
        try:
            for d in arxiv.search_records(
                keyword, max_results=rows, title=title, author=author, category=category,
                year_from=year_from, year_to=year_to,
            ):
                all_recs.append(PaperRecord(
                    title=d["title"], authors=d["authors"], year=d["year"], doi=d["doi"],
                    arxiv_id=d["arxiv_id"], is_oa=True, oa_url=d["oa_url"],
                    abstract=d["abstract"], sources=["arxiv"],
                ))
        except Exception as e:
            print(f"[warn] arxiv 检索失败: {e}")

    if use_gs:
        try:
            for d in google_scholar.search_records(
                keyword, rows=rows, author=author, title=title,
                year_from=year_from or None, year_to=year_to or None,
            ):
                all_recs.append(PaperRecord(
                    title=d["title"], authors=[a.strip() for a in d["authors"].split(",") if a.strip()],
                    year=d["year"], venue=d["venue"], citations=0,
                    is_oa=bool(d["pdf_url"]), oa_url=d["pdf_url"],
                    abstract=d["snippet"], sources=["google_scholar"],
                    links={"official": d["link"]},
                ))
        except Exception as e:
            print(f"[warn] google scholar 检索失败: {e}")

    if use_wanfang:
        try:
            for d in wanfang.search_records(keyword, rows=rows):
                marks = "/".join(d["core_marks"]) if d.get("core_marks") else ""
                all_recs.append(PaperRecord(
                    title=d["title"], authors=d["authors"], year=d["year"],
                    venue=d["venue"], doi=d["doi"], citations=0,
                    sources=["wanfang"],
                    links={"official": d["link"], "core_marks": marks, "pdf_path": d.get("pdf_path", "")},
                ))
        except Exception as e:
            print(f"[warn] wanfang 检索失败（cookie 失效？需重新登录导出 data/paper_sessions/wanfang/storage_state.json）: {e}")

    merged = merge_records(all_recs)
    enrich(merged, s2_check=enrich_s2)
    return merged


# ── 字段补齐（补全即停，非全源全查）──────────────

def _enrich_one(rec: PaperRecord, *, oa_check: bool = True, s2_check: bool = True) -> None:
    """补齐单条记录。"""
    if not rec.doi:
        return
    # 缺 OA 信息（且该记录非 openalex 来源自带）
    if oa_check and not rec.is_oa and not rec.oa_url:
        oa = openalex.resolve_doi(rec.doi)
        if oa:
            rec.is_oa = rec.is_oa or oa["is_oa"]
            rec.oa_url = rec.oa_url or oa["oa_url"]
            rec.citations = max(rec.citations, oa["citations"])
            if not rec.venue:
                rec.venue = oa["venue"]
                rec.venue_type = oa["venue_type"]
            if not rec.arxiv_id:
                rec.arxiv_id = oa["arxiv_id"]
    # 缺 venue（很可能是 openalex 预印本记录）
    if not rec.venue:
        cr = crossref.resolve_doi(rec.doi)
        if cr:
            rec.venue = cr["venue"]
            rec.venue_type = cr["venue_type"]
            rec.citations = max(rec.citations, cr["citations"])
            if not rec.year:
                rec.year = cr["year"]
            if not rec.abstract:
                rec.abstract = cr["abstract"]
    # Semantic Scholar：补 tldr + 开放 PDF（S2 免费层只做 DOI 查询）
    if s2_check:
        s2 = semantic_scholar.resolve_doi(rec.doi)
        if s2:
            if not rec.tldr:
                rec.tldr = s2["tldr"]
            if s2["oa_url"]:
                rec.links["s2_open_pdf"] = s2["oa_url"]
                rec.is_oa = True
                if not rec.oa_url:
                    rec.oa_url = s2["oa_url"]
            rec.citations = max(rec.citations, s2["citations"])


def enrich(records: list[PaperRecord], *, oa_check: bool = True, s2_check: bool = True,
           workers: int = 8) -> None:
    """按"还缺什么补什么"原则补齐（并发）。"""
    if not records:
        return
    if len(records) == 1:
        _enrich_one(records[0], oa_check=oa_check, s2_check=s2_check)
        return
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(records)))) as pool:
        futures = [pool.submit(_enrich_one, rec, oa_check=oa_check, s2_check=s2_check)
                   for rec in records]
        for fut in as_completed(futures):
            fut.result()  # 让异常在 as_completed 时暴露，但不阻断其他记录


# ── 排序（引用 + venue 质量 + 时效）──────────────

def rank(records: list[PaperRecord], *, cit_weight: float = 0.5, venue_weight: float = 0.3, time_weight: float = 0.2) -> list[PaperRecord]:
    """加权排序，注入 rank_score 与 rank_components，返回降序列表。"""
    from backend.services.papers.venue_score import get_ccf_score, score_venue

    if not records:
        return []
    max_cit = max((r.citations for r in records), default=1)
    max_cit = max(max_cit, 1)

    for rec in records:
        cit_score = (math.log1p(rec.citations) / math.log1p(max_cit)) * 100 if max_cit > 0 else 0
        vs = score_venue(rec.venue) if rec.venue else {"ccf_score": 0, "score": 0}
        venue_score = vs["score"] if vs.get("score") else get_ccf_score(rec.venue)
        age = CURRENT_YEAR - (rec.year if rec.year else CURRENT_YEAR)
        time_score = max(0.0, 1.0 - age / 5.0) * 100

        total_w = cit_weight + venue_weight + time_weight or 1.0
        rec.rank_score = round(
            (cit_score * cit_weight + venue_score * venue_weight + time_score * time_weight) / total_w, 1
        )

    records.sort(key=lambda r: -r.rank_score)
    return records


# ── 导出 ─────────────────────────────────────────

def export_json(records: list[PaperRecord], path: str) -> str:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump([r.to_dict() for r in records], f, ensure_ascii=False, indent=2)
    return path


def export_md(records: list[PaperRecord], path: str) -> str:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    lines = ["# 多源检索结果", ""]
    for i, r in enumerate(records, 1):
        lines.append(f"## {i}. {r.title}")
        lines.append(f"- 年份: {r.year or 'N/A'} ｜ 引用: {r.citations}")
        lines.append(f"- Venue: {r.venue or 'N/A'} ({r.venue_type or ''})")
        lines.append(f"- DOI: {r.doi or 'N/A'}")
        lines.append(f"- arXiv: {r.arxiv_id or 'N/A'}")
        lines.append(f"- OA: {'是' if r.is_oa else '否'} {r.oa_url or ''}".strip())
        lines.append(f"- 作者: {', '.join(r.authors[:6]) if r.authors else 'N/A'}")
        lines.append(f"- 来源: {', '.join(r.sources)}")
        if r.tldr:
            lines.append(f"- TLDR: {r.tldr[:180]}")
        if r.abstract:
            lines.append(f"- 摘要: {r.abstract[:180]}...")
        lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


# ── CLI ──────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="多源论文检索 pipeline")
    sub = parser.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("search", help="按关键词检索")
    sp.add_argument("keyword", help="关键词/标题")
    sp.add_argument("--rows", type=int, default=30, help="每源取多少条（默认30）")
    sp.add_argument("--from-year", type=int, default=None, help="只取该年份之后")
    sp.add_argument("--type", dest="type_filter", default=None, help="Crossref 类型过滤，如 journal-article")
    sp.add_argument("--no-crossref", action="store_true")
    sp.add_argument("--no-openalex", action="store_true")
    sp.add_argument("--use-gs", action="store_true", help="启用 Google Scholar 检索（走 Clash，限速6s/次）")
    sp.add_argument("--use-wanfang", action="store_true", help="启用万方检索（A1 纯HTTP，需 data/paper_sessions/wanfang/storage_state.json cookie）")
    sp.add_argument("--no-rank", action="store_true", help="不排序")
    sp.add_argument("--export-dir", default="exports", help="导出目录")
    sp.add_argument("--basename", default="results", help="导出文件名前缀")
    sp.add_argument("--download-dir", default="", help="指定后自动下载开放 PDF 到该目录")

    args = parser.parse_args()

    if args.command == "search":
        recs = search(
            args.keyword,
            rows=args.rows,
            from_year=args.from_year,
            type_filter=args.type_filter,
            use_crossref=not args.no_crossref,
            use_openalex=not args.no_openalex,
            use_gs=args.use_gs,
            use_wanfang=args.use_wanfang,
        )
        if not args.no_rank:
            recs = rank(recs)
        print(f"合并去重后 {len(recs)} 条结果（按相关性排序）\n")
        for i, r in enumerate(recs[:15], 1):
            print(f"{i:2d}. [{r.year or '?'}] {r.title[:55]}")
            print(f"      {r.venue or '(no venue)'} ｜ cit={r.citations} ｜ OA={'Y' if r.is_oa else 'N'}"
                  f" ｜ {'; '.join(r.sources)}")
            if r.doi:
                print(f"      doi={r.doi}")
        if len(recs) > 15:
            print(f"  ... 其余 {len(recs) - 15} 条见导出文件")
        base = os.path.join(args.export_dir, args.basename)
        print(f"\n导出: {export_json(recs, base + '.json')}")
        print(f"导出: {export_md(recs, base + '.md')}")

        if args.download_dir:
            from backend.services.papers.download import download_batch
            print(f"\n=== 下载开放 PDF 到 {args.download_dir} ===")
            counts = download_batch(recs, args.download_dir, verbose=True)
            print(f"\n下载统计: 成功 {counts['downloaded']} ｜ 仅链接 {counts['oa_link_only']} ｜ 付费墙 {counts['paywall']}")
        return 0

    parser.error("未知命令")


if __name__ == "__main__":
    raise SystemExit(main())
