#!/usr/bin/env python3
"""综合多门户并行搜索编排器（A1，脱离 agent）。

一次性任务流程：
  1. 并行搜索 6 个门户：Crossref / OpenAlex / arXiv / Google Scholar / 万方 / 知网
  2. 合并去重（DOI/标题）
  3. 元信息补全（OpenAlex/Crossref/S2）
  4. 打分排序
  5. 下载源门：开放版→直接下；机构版(万方/知网)→标记可下载；无→记 no_source
  6. 导出 JSON/MD + no_source 清单

用法:
    python run_task.py "关键词" [--rows 15] [--download-dir downloads] [--no-gs]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed


from backend.services.papers.download import download_paper_pdf
from backend.services.papers.pipeline import CURRENT_YEAR, enrich, rank
from backend.services.papers.record import PaperRecord, merge_records
from backend.services.papers.sources import arxiv, cnki, crossref, eprint, google_scholar, openalex, semantic_scholar, unpaywall, wanfang


# ── 各源搜索（线程安全，返回 list[PaperRecord]）────

def _crossref(kw: str, rows: int, **q) -> list[PaperRecord]:
    out = []
    for d in crossref.search_bibliographic(
        kw, rows=rows, title=q.get("title", ""), author=q.get("author", ""),
        year_from=q.get("year_from", 0), year_to=q.get("year_to", 0),
    ):
        out.append(PaperRecord(
            title=d["title"], authors=d["authors"], year=d["year"], doi=d["doi"],
            venue=d["venue"], venue_type=d["venue_type"], citations=d["citations"],
            abstract=d["abstract"], sources=["crossref"],
            links={"official": d.get("link", "")},
        ))
    return out


def _openalex(kw: str, rows: int, **q) -> list[PaperRecord]:
    out = []
    for d in openalex.search_records(
        kw, per_page=rows, title=q.get("title", ""), author=q.get("author", ""),
        year_from=q.get("year_from", 0), year_to=q.get("year_to", 0),
        category=q.get("category", ""),
    ):
        out.append(PaperRecord(
            title=d["title"], authors=d["authors"], year=d["year"], doi=d["doi"],
            arxiv_id=d["arxiv_id"], venue=d["venue"], venue_type=d["venue_type"],
            citations=d["citations"], is_oa=d["is_oa"], oa_url=d["oa_url"],
            abstract=d["abstract"], sources=["openalex"],
        ))
    return out


def _arxiv(kw: str, rows: int, **q) -> list[PaperRecord]:
    out = []
    for d in arxiv.search_records(
        kw, max_results=rows, title=q.get("title", ""), author=q.get("author", ""),
        category=q.get("category", ""), year_from=q.get("year_from", 0),
        year_to=q.get("year_to", 0),
    ):
        out.append(PaperRecord(
            title=d["title"], authors=d["authors"], year=d["year"], doi=d["doi"],
            arxiv_id=d["arxiv_id"], is_oa=True, oa_url=d["oa_url"],
            abstract=d["abstract"], sources=["arxiv"],
        ))
    return out


def _gs(kw: str, rows: int, **q) -> list[PaperRecord]:
    out = []
    for d in google_scholar.search_records(
        kw, rows=rows, author=q.get("author", ""), title=q.get("title", ""),
        year_from=q.get("year_from", 0) or None, year_to=q.get("year_to", 0) or None,
    ):
        out.append(PaperRecord(
            title=d["title"], authors=[a.strip() for a in d["authors"].split(",") if a.strip()],
            year=d["year"], venue=d["venue"], is_oa=bool(d["pdf_url"]), oa_url=d["pdf_url"],
            abstract=d["snippet"], sources=["google_scholar"],
            links={"official": d["link"]},
        ))
    return out


def _wanfang(kw: str, rows: int, **q) -> list[PaperRecord]:
    out = []
    db_types = q.get("db_types") or None
    if isinstance(db_types, str):
        db_types = [t.strip() for t in db_types.split(",") if t.strip()]
    for d in wanfang.search_records(
        kw, rows=rows, author=q.get("author", ""), title=q.get("title", ""),
        db_types=db_types,
    ):
        marks = "/".join(d.get("core_marks") or [])
        out.append(PaperRecord(
            title=d["title"], authors=d["authors"], year=d["year"],
            venue=d["venue"], doi=d["doi"], sources=["wanfang"],
            links={"official": d.get("link", ""), "core_marks": marks,
                   "pdf_path": d.get("pdf_path", ""), "file_id": d.get("file_id", "")},
        ))
    return out


def _cnki(kw: str, rows: int, **q) -> list[PaperRecord]:
    out = []
    for d in cnki.search_records(kw, rows=rows, author=q.get("author", ""), title=q.get("title", "")):
        out.append(PaperRecord(
            title=d["title"], authors=d["authors"], year=d["year"],
            venue=d["venue"], sources=["cnki"],
            links={"official": d.get("link", "")},
        ))
    return out


def _eprint(kw: str, rows: int, **q) -> list[PaperRecord]:
    """ePrint 密码学预印本源：PDF 全开放直下，venue 存密码学类目。"""
    out = []
    for d in eprint.search_records(
        kw, max_results=rows,
        title=q.get("title", ""), author=q.get("author", ""),
        category=q.get("category", ""),
        year_from=q.get("year_from", 0), year_to=q.get("year_to", 0),
    ):
        out.append(PaperRecord(
            title=d["title"], authors=d["authors"], year=d["year"],
            eprint_id=d["paper_id"],               # 如 "2023/708"，作归档 id（slug 化）
            is_oa=True, oa_url=d["pdf_url"],       # ePrint PDF 全开放
            venue=d.get("category", ""),           # 存密码学类目，供展示/筛选
            venue_type="repository",
            abstract=d["abstract"], sources=["eprint"],
            links={"eprint_id": d["paper_id"], "official": d["page_url"],
                   "short_url": d["short_url"]},
        ))
    return out


SOURCES = {
    "crossref": _crossref,
    "openalex": _openalex,
    "arxiv": _arxiv,
    "google_scholar": _gs,
    "wanfang": _wanfang,
    "cnki": _cnki,
    "eprint": _eprint,
}

# 反爬较严的源：每源行数上限（降低封禁/负载；用户 --rows 更大也封顶这里）
_ROW_CAP = {"google_scholar": 8, "wanfang": 8, "cnki": 8}


def search_all(keyword: str, *, rows: int = 15, use_gs: bool = True,
               title: str = "", author: str = "", year_from: int = 0,
               year_to: int = 0, category: str = "",
               db_types: list[str] | None = None,
               s2_check: bool = False,
               only_sources: list[str] | None = None,
               exclude_sources: list[str] | None = None,
               ) -> tuple[list[PaperRecord], dict[str, int]]:
    """并行搜索全部门户。返回 (合并去重后的记录, 每源命中数)。

    title/author/year_from/year_to/category 映射到 API 三平台（arXiv/Crossref/OpenAlex）高级语法。
    s2_check=False 时跳过 Semantic Scholar 逐 DOI 查询（默认跳过，显著提速；下载场景再由调用方开启）。

    源选择：
    - only_sources（白名单）：非空时只用这些源，其余全关。
    - exclude_sources（排除）：非空时关闭这些源（在白名单基础上再剔除）。
    - use_gs=False 等价于 exclude google_scholar。
    优先级：全开 → only_sources 白名单 → exclude_sources/use_gs 剔除。
    """
    enabled = dict(SOURCES)
    if only_sources:
        enabled = {k: v for k, v in enabled.items() if k in set(only_sources)}
    if exclude_sources:
        for s in exclude_sources:
            enabled.pop(s, None)
    if not use_gs:
        enabled.pop("google_scholar", None)

    counts: dict[str, int] = {}
    results: dict[str, list[PaperRecord]] = {}
    q = {"title": title, "author": author, "year_from": year_from,
         "year_to": year_to, "category": category, "db_types": db_types}

    def run(name: str):
        cap = _ROW_CAP.get(name)
        r = max(1, min(rows, cap)) if cap else rows
        try:
            return name, enabled[name](keyword, r, **q)
        except Exception as e:
            print(f"  [warn] {name} 搜索失败: {e}")
            return name, []

    with ThreadPoolExecutor(max_workers=len(enabled)) as pool:
        futures = [pool.submit(run, name) for name in enabled]
        for fut in as_completed(futures):
            name, recs = fut.result()
            results[name] = recs
            counts[name] = len(recs)

    all_recs = [r for name in enabled for r in results.get(name, [])]
    merged = merge_records(all_recs)
    filter_irrelevant(merged, keyword)
    enrich(merged, s2_check=s2_check)
    return merged, counts


def _sig_terms(query: str) -> set[str]:
    """提取查询的显著词：CJK 序列(≥2字) + 拉丁词(≥3字符)。"""
    q = unicodedata.normalize("NFC", query).lower()
    terms: set[str] = set()
    for cjk in re.findall(r"[一-鿿]+", q):
        if len(cjk) >= 2:
            terms.add(cjk)
    for en in re.findall(r"[a-z0-9]{3,}", q):
        terms.add(en)
    return terms


def filter_irrelevant(records: list[PaperRecord], query: str) -> None:
    """剔除无关记录：万方/知网是精确关键词检索(保留)；其它源(OpenAlex/Crossref/arXiv/GS 模糊检索)
    若标题不含查询的任一显著词，视为噪声剔除。原地修改 records。"""
    terms = _sig_terms(query)
    if not terms:
        return
    keep: list[PaperRecord] = []
    dropped = 0
    for r in records:
        if "wanfang" in r.sources or "cnki" in r.sources:
            keep.append(r)
            continue
        t = unicodedata.normalize("NFC", r.title).lower()
        if any(term in t for term in terms):
            keep.append(r)
        else:
            dropped += 1
    if dropped:
        print(f"  [相关过滤] 剔除无关记录 {dropped} 条（OpenAlex/Crossref/GS 模糊命中）")
    records[:] = keep


# ── 下载源门 ─────────────────────────────────────

def gate_download(records: list[PaperRecord], *, third_party: bool = True,
                  workers: int = 6) -> dict[str, int]:
    """分类下载源：open(开放版) / institution(机构版) / none(无源)。

    第三方尝试（third_party=True）：无开放版但有 DOI 的记录，用 Unpaywall 按 DOI
    找开放副本；找到则标记 open。万方中文论文多为 ISTIC-DOI，Unpaywall 通常查不到，
    此时才回落机构版。Unpaywall 查询并发执行以提速。
    """
    stats = {"open": 0, "institution": 0, "none": 0}
    need_unpaywall = [r for r in records
                      if not r.oa_url and third_party and r.doi
                      and "wanfang" not in r.sources and "cnki" not in r.sources]

    def _unpaywall(r: PaperRecord) -> PaperRecord:
        oa = unpaywall.find_oa(r.doi)
        if oa and (oa.get("url_for_pdf") or oa.get("url")):
            r.oa_url = oa.get("url_for_pdf") or oa["url"]
            r.is_oa = True
            r.links["third_party"] = "unpaywall"
        return r

    if need_unpaywall:
        if len(need_unpaywall) == 1:
            need_unpaywall[0] = _unpaywall(need_unpaywall[0])
        else:
            with ThreadPoolExecutor(max_workers=max(1, min(workers, len(need_unpaywall)))) as pool:
                futures = [pool.submit(_unpaywall, r) for r in need_unpaywall]
                for fut in as_completed(futures):
                    fut.result()

    for r in records:
        if r.oa_url:
            r.links["download_source"] = "open"
            stats["open"] += 1
            continue
        # 机构版判定：先按来源（万方/知网），再按 DOI 前缀识别 IEEE/ACM
        host = None
        if "wanfang" in r.sources:
            host = "wanfang"
        elif "cnki" in r.sources:
            host = "cnki"
        elif r.doi:
            d = r.doi.lower()
            if d.startswith("10.1145/"):
                host = "acm"
            elif d.startswith("10.1109/"):
                host = "ieee"
        if host:
            r.links["download_source"] = "institution"
            r.links["inst_host"] = host
            stats["institution"] += 1
        else:
            r.links["download_source"] = "none"
            stats["none"] += 1
    return stats


# ── 导出 ─────────────────────────────────────────

def export(records: list[PaperRecord], no_source: list[PaperRecord], out_dir: str, basename: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, basename + ".json"), "w", encoding="utf-8") as f:
        json.dump([r.to_dict() for r in records], f, ensure_ascii=False, indent=2)
    md_lines = ["# 综合多门户检索任务结果", ""]
    for i, r in enumerate(records, 1):
        src = r.links.get("download_source", "")
        md_lines += [
            f"## {i}. {r.title}",
            f"- 年份: {r.year} ｜ 引用: {r.citations} ｜ 下载源: {src}",
            f"- Venue: {r.venue or 'N/A'}",
            f"- DOI: {r.doi or 'N/A'} ｜ arXiv: {r.arxiv_id or 'N/A'}",
            f"- 来源: {', '.join(r.sources)}",
            "",
        ]
    with open(os.path.join(out_dir, basename + ".md"), "w", encoding="utf-8") as f:
        f.write("\n".join(md_lines))
    with open(os.path.join(out_dir, basename + "_no_source.json"), "w", encoding="utf-8") as f:
        json.dump([r.to_dict() for r in no_source], f, ensure_ascii=False, indent=2)


# ── main ─────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="综合多门户并行搜索任务")
    parser.add_argument("keyword", help="检索关键词")
    parser.add_argument("--rows", type=int, default=15, help="每源取多少条")
    parser.add_argument("--no-gs", action="store_true", help="不用 Google Scholar")
    parser.add_argument("--download-dir", default="downloads", help="开放版 PDF 下载目录")
    parser.add_argument("--export-dir", default="exports", help="导出目录")
    parser.add_argument("--basename", default="task_result", help="导出文件名前缀")
    parser.add_argument("--max-download", type=int, default=10, help="开放版最多下几篇")
    parser.add_argument("--inst-download", type=int, default=0,
                        help="机构版(知网/万方/IEEE/ACM)下载篇数；默认0不自动下（万方每篇约扣机构3元；"
                             "IEEE走elib前缀，ACM需先 run node acm_agent.js <doi> 导出cf_clearance）")
    args = parser.parse_args()

    print(f"=== 综合门户搜索: {args.keyword} ===")
    print("并行搜索: Crossref / OpenAlex / arXiv / Google Scholar / 万方 / 知网\n")

    # 网络环境检测：配置的 HTTP 代理 决定 GS/Sci-Hub；机构下载默认直连（elib 自带机构身份）
    import netctx
    print(f"网络状态: {netctx.describe()}")
    if not netctx.clash_online() and not args.no_gs:
        print("  [提示] Clash 离线，Google Scholar 将失败；可开 Clash 或加 --no-gs 跳过")
    if args.inst_download > 0:
        print("  [提示] IEEE/ACM 走 elib 反向代理（直连即可，不需要校园网也不需要 wssocks）")
        print("  [提示] 知网/万方走机构 cookie；隧道仅在按源 IP 判权的源上才需要（--proxy=socks5）\n")

    recs, counts = search_all(args.keyword, rows=args.rows, use_gs=not args.no_gs)
    print("\n各源命中: " + ", ".join(f"{k}={v}" for k, v in counts.items()))

    recs = rank(recs)
    print(f"合并去重后: {len(recs)} 条\n")

    stats = gate_download(recs)
    print(f"下载源分类: 开放版 {stats['open']} ｜ 机构版(万方/知网/IEEE/ACM) {stats['institution']} ｜ 无源 {stats['none']}")

    open_recs = [r for r in recs if r.links.get("download_source") == "open"]
    no_source = [r for r in recs if r.links.get("download_source") == "none"]

    print("\n=== 下载开放版 PDF ===")
    dl_ok = 0
    for r in open_recs[: args.max_download]:
        status, info = download_paper_pdf(r, args.download_dir)
        if status == "downloaded":
            dl_ok += 1
            print(f"  [下载] {r.title[:40]} -> {info}")
        elif status == "oa_link_only":
            print(f"  [仅链接] {r.title[:40]}")
    print(f"\n开放版下载成功 {dl_ok}/{min(len(open_recs), args.max_download)}")

    # 机构版（知网/万方/IEEE/ACM）下载：调 browser/download_institution.js
    inst_recs = [r for r in recs if r.links.get("download_source") == "institution"]
    if args.inst_download > 0 and inst_recs:
        print(f"\n=== 机构版下载（知网/万方/IEEE/ACM，{args.inst_download} 篇）===")
        inst_input = os.path.join(args.export_dir, args.basename + "_inst.json")
        payload = []
        for r in inst_recs[: args.inst_download]:
            host = r.links.get("inst_host", "cnki")
            if host in ("wanfang", "cnki"):
                link = r.links.get("official", "")
                if link:
                    payload.append({"source": host, "title": r.title, "link": link})
            elif host == "acm":
                if r.doi:
                    payload.append({"source": "acm", "title": r.title, "doi": r.doi})
            elif host == "ieee":
                # arnumber 由 download_institution.js 从 DOI 直接推出（10.1109/….<arnumber>）
                if r.doi:
                    payload.append({"source": "ieee", "title": r.title, "doi": r.doi})
        if not payload:
            print("  [提示] 机构版记录缺少可下载字段（link/doi）")
        else:
            with open(inst_input, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            # 脚本在 browser/ 子目录下（原先写成 papers/ 根目录 → FileNotFoundError 被吞掉）
            browser_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "browser")
            try:
                result = subprocess.run(
                    ["node", "download_institution.js", inst_input, args.download_dir,
                     str(args.inst_download), "--proxy=auto"],
                    cwd=browser_dir,
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                )
                if result.stdout:
                    print(result.stdout)
                if result.returncode != 0:
                    print(f"  [warn] 机构版下载退出码 {result.returncode}: {result.stderr[-500:]}")
            except (FileNotFoundError, subprocess.CalledProcessError) as e:
                print(f"  [warn] 机构版下载未执行: {e}")
    elif inst_recs:
        print(f"\n[提示] 机构版 {len(inst_recs)} 篇未下载（默认不自动下）；加 --inst-download N 可下 N 篇")
        print("       IEEE/ACM 走 elib 代理与 cf_clearance，无需校园网；万方每篇约扣机构 3 元")

    export(recs, no_source, args.export_dir, args.basename)
    print(f"\n导出: {args.export_dir}/{args.basename}.json / .md / _no_source.json")

    # 概览
    print("\n=== 结果概览（按分数排序 top10）===")
    for r in recs[:10]:
        src = r.links.get("download_source", "")
        print(f"  [{r.year}] {r.title[:36]} | {r.venue[:16] or '-'.join(r.sources)} | 下载:{src} | rank={r.rank_score}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
