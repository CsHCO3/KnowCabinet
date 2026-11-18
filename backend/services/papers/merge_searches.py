# -*- coding: utf-8 -*-
"""合并已存多份 paper-search 检索结果 → 全局去重 → 关键词过滤 → 引用排序 → top-N。

数据源: data/paper_searches/_index.json（搜索索引）+ {id}.json（每次搜索的 results）。
用途: 调研时把多批检索结果并成一个全局清单，按课题关键词筛相关子集、按引用量取 top-N，
     可选批量入库（add_paper_by_meta 幂等，只刷新元数据）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from backend.path_setup import PROJECT_ROOT as _ROOT

_SEARCHES_DIR = _ROOT / "data" / "paper_searches"

_ARXIV_ID_RE = re.compile(r"(\d{4}\.\d{4,5})")


def list_searches() -> list[dict]:
    """从 _index.json 读搜索索引（按创建时间倒序）。无索引返回 []。"""
    idx_path = _SEARCHES_DIR / "_index.json"
    if not idx_path.exists():
        return []
    try:
        return sorted(json.loads(idx_path.read_text(encoding="utf-8")),
                      key=lambda e: e.get("created_at", ""), reverse=True)
    except Exception:
        return []


def _merge_group(group: list[dict]) -> dict:
    """把同一论文的多个记录合并：首非空字段 + citations 取最大 + sources 并集。"""
    out: dict = {}
    for r in group:
        for k, v in r.items():
            if k == "citations":
                out[k] = max(out.get(k, 0) or 0, v or 0)
            elif k == "sources":
                out[k] = sorted(set(out.get(k, []) + (v or [])))
            elif not out.get(k) and v:
                out[k] = v
    return out


def _norm_doi(d: str) -> str:
    s = (d or "").strip().lower()
    for pfx in ("https://doi.org/", "http://doi.org/", "doi:"):
        if s.startswith(pfx):
            s = s[len(pfx):]
    return s


def _norm_title(t: str) -> str:
    return re.sub(r"\s+", "", (t or "").lower())


def load_papers(search_ids: list[str] | None = None) -> list[dict]:
    """加载所有/指定搜索的 results，跨检索合并去重，返回全局论文清单。

    去重：arxiv_id(去版本) / DOI 为主键，**标题交叉匹配兜底**——同一篇论文
    有的源给了 DOI、有的源没给（只有标题）时，仍能合并成一条（避免重复）。
    """
    files = []
    if search_ids:
        for sid in search_ids:
            p = _SEARCHES_DIR / f"{sid}.json"
            if p.exists():
                files.append(p)
    else:
        files = sorted(_SEARCHES_DIR.glob("*.json"))
        files = [f for f in files if f.name != "_index.json"]

    all_results: list[dict] = []
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        all_results.extend(d.get("results") or [])

    groups: list[dict] = []
    key_map: dict[tuple, int] = {}   # (arxiv|doi, key) -> group index
    title_map: dict[str, int] = {}   # norm_title -> group index

    for r in all_results:
        aid_m = _ARXIV_ID_RE.search(r.get("arxiv_id") or "")
        doi = _norm_doi(r.get("doi"))
        nt = _norm_title(r.get("title"))

        primary = ("arxiv", aid_m.group(1)) if aid_m else (("doi", doi) if doi else None)
        target = key_map.get(primary) if primary else None
        if target is None and nt:
            target = title_map.get(nt)          # 主键没命中 → 标题交叉匹配

        if target is None:
            target = len(groups)
            groups.append(dict(r))
        else:
            groups[target] = _merge_group([groups[target], r])

        if primary:
            key_map.setdefault(primary, target)
        if nt:
            title_map.setdefault(nt, target)
    return groups


def filter_keywords(papers: list[dict], keywords: list[str]) -> list[dict]:
    """按关键词过滤（title 大小写不敏感子串，任一命中）。空关键词返回全部。"""
    if not keywords:
        return papers
    kws = [k.strip().lower() for k in keywords if k.strip()]
    if not kws:
        return papers
    out = []
    for p in papers:
        title = (p.get("title") or "").lower()
        if any(k in title for k in kws):
            out.append(p)
    return out


def rank_by_citations(papers: list[dict], min_citations: int = 0) -> list[dict]:
    """按引用量降序；可设最小引用门槛。"""
    out = [p for p in papers if (p.get("citations") or 0) >= min_citations]
    out.sort(key=lambda p: p.get("citations") or 0, reverse=True)
    return out


def import_to_kb(papers: list[dict]) -> list[dict]:
    """把论文清单批量建 KB 元数据条目（幂等；有 arxiv_id 优先，否则按 DOI）。"""
    from backend.services.archive.service import add_paper_by_meta
    added = []
    for p in papers:
        try:
            if p.get("arxiv_id"):
                res = add_paper_by_meta(arxiv_id=p["arxiv_id"])
            elif p.get("doi"):
                res = add_paper_by_meta(doi=p["doi"])
            else:
                continue
            if res.get("ok"):
                added.append({"id": res["id"], "title": (p.get("title") or "")[:60]})
        except Exception:
            continue
    return added


def export_json(papers: list[dict], path: str) -> int:
    Path(path).write_text(json.dumps(papers, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(papers)
