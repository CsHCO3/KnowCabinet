"""参考文献重要权威挖掘。

优先 OpenAlex：
  1. 对项目已选论文（有 DOI/arXiv）取 referenced_works；
  2. 对每篇参考文献只查 cited_by_count；
  3. 排除已在库/已选论文；
  4. 按被引用量排序，输出 candidate_references.json + reference_authorities.md。

若 OpenAlex 不可用或负担过重，退化为本地频次统计（多批入库结果中重复出现）。
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from backend.services.papers.http_client import http_get_json
from backend.services.research_project import project_store as store

_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_FULL_PAPERS = _ROOT / "knowledgebase" / "full" / "papers"
_SEARCHES_DIR = _ROOT / "data" / "paper_searches"
_OPENALEX_WORK = "https://api.openalex.org/works/{id}?select=id,doi,title,cited_by_count,publication_year,authorships"
_OPENALEX_DOI = "https://api.openalex.org/works/doi:{doi}?select=id,doi,title,cited_by_count,referenced_works"
_OPENALEX_ARXIV = "https://api.openalex.org/works?filter=ids.arxiv:{arxiv}&select=id,doi,title,cited_by_count,referenced_works"


def _known_ids() -> set[str]:
    """本地已知论文 id/DOI/arxiv 集合，用于排除。"""
    known: set[str] = set()
    if _FULL_PAPERS.is_dir():
        for d in _FULL_PAPERS.iterdir():
            if d.is_dir():
                known.add(d.name)
                meta_path = d / "metadata.json"
                if meta_path.exists():
                    try:
                        meta = json.loads(meta_path.read_text(encoding="utf-8"))
                        if meta.get("doi"):
                            known.add(meta["doi"].lower())
                        if meta.get("arxiv_id"):
                            known.add(str(meta["arxiv_id"]).lower())
                    except Exception:
                        pass
    if _SEARCHES_DIR.is_dir():
        for f in _SEARCHES_DIR.glob("*.json"):
            if f.name == "_index.json":
                continue
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            for r in rec.get("results", []):
                if r.get("entry_id"):
                    known.add(str(r["entry_id"]))
                if r.get("doi"):
                    known.add(str(r["doi"]).lower())
                if r.get("arxiv_id"):
                    known.add(str(r["arxiv_id"]).lower())
    return known


def _get_referenced_works(paper_id: str) -> list[dict]:
    """返回 [{openalex_id, doi, title, cited_by_count, year}]。"""
    full = _FULL_PAPERS / paper_id
    meta = {}
    mp = full / "metadata.json"
    if mp.exists():
        try:
            meta = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    doi = meta.get("doi", "")
    arxiv = meta.get("arxiv_id", "")
    work = None
    try:
        if doi:
            work = http_get_json(_OPENALEX_DOI.format(doi=doi), timeout=20)
        elif arxiv:
            data = http_get_json(_OPENALEX_ARXIV.format(arxiv=arxiv), timeout=20)
            results = data.get("results") or []
            work = results[0] if results else None
    except Exception:
        work = None
    if not work:
        return []
    ref_ids = work.get("referenced_works") or []
    refs = []
    # 只查被引用量，限制单篇参考文献数量，避免负担过重
    for ref_id in ref_ids[:200]:
        try:
            info = http_get_json(_OPENALEX_WORK.format(id=ref_id), timeout=15)
        except Exception:
            continue
        refs.append({
            "openalex_id": info.get("id", ref_id),
            "doi": (info.get("doi") or "").replace("https://doi.org/", ""),
            "title": info.get("title") or ref_id,
            "cited_by_count": info.get("cited_by_count") or 0,
            "year": info.get("publication_year"),
        })
    return refs


def _local_frequency_fallback() -> list[dict]:
    """降级：统计多批搜索记录中重复出现的未入库论文。"""
    counter: Counter = Counter()
    info: dict[str, dict] = {}
    if not _SEARCHES_DIR.is_dir():
        return []
    for f in _SEARCHES_DIR.glob("*.json"):
        if f.name == "_index.json":
            continue
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for r in rec.get("results", []):
            key = r.get("doi") or r.get("arxiv_id") or r.get("entry_id")
            if not key:
                continue
            key = str(key).lower()
            counter[key] += 1
            info.setdefault(key, {
                "openalex_id": "",
                "doi": r.get("doi", ""),
                "title": r.get("title", ""),
                "cited_by_count": r.get("citations") or 0,
                "year": r.get("year"),
                "frequency": 0,
            })
    out = []
    for key, cnt in counter.most_common(50):
        item = info[key]
        item["frequency"] = cnt
        out.append(item)
    return out


def mine_references(slug: str, use_fallback: bool = True) -> dict:
    """挖掘项目已选论文参考文献中的重要权威。"""
    paper_ids = store.list_project_paper_ids(slug)
    if not paper_ids:
        return {"ok": False, "error": "项目还没有已选论文"}
    known = _known_ids()
    all_refs: dict[str, dict] = {}
    errors = []

    for pid in paper_ids:
        try:
            refs = _get_referenced_works(pid)
        except Exception as e:
            errors.append({"paper_id": pid, "error": str(e)})
            refs = []
        for ref in refs:
            key = (ref.get("doi") or "").lower() or ref.get("openalex_id", "")
            if not key:
                continue
            if key in known or ref["openalex_id"] in known:
                continue
            # 标题也可能撞；简单按 DOI/openalex_id 去重
            all_refs[key] = ref

    if not all_refs and use_fallback:
        fallback = _local_frequency_fallback()
        if fallback:
            all_refs = {f"{x.get('doi','') or x.get('title','')}": x for x in fallback}

    candidates = sorted(all_refs.values(), key=lambda x: x.get("cited_by_count") or 0, reverse=True)

    out_json = store.work_dir(slug) / "candidate_references.json"
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps({
        "project": slug,
        "candidates": candidates,
        "errors": errors,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    md = _render_authorities_md(slug, candidates, errors)
    work_md = store.work_dir(slug) / "reference_authorities.md"
    work_md.write_text(md, encoding="utf-8")
    mirror_md = store.mirror_dir(slug) / "reference_authorities.md"
    mirror_md.parent.mkdir(parents=True, exist_ok=True)
    from backend.services.research_project.mirror import _frontmatter_for
    fm = _frontmatter_for(store.load_project(slug), kind="reference_authorities")
    mirror_md.write_text(fm + "\n" + md, encoding="utf-8")

    return {
        "ok": True,
        "candidate_count": len(candidates),
        "errors": errors,
        "json_path": str(out_json),
        "md_path": str(work_md),
    }


def _render_authorities_md(slug: str, candidates: list[dict], errors: list[dict]) -> str:
    proj = store.load_project(slug)
    lines = [f"# 参考文献重要权威：{proj.get('name', slug)}", ""]
    if errors:
        lines += ["## 获取错误（部分论文未取到参考文献）", ""]
        for e in errors:
            lines.append(f"- {e.get('paper_id')}: {e.get('error')}")
        lines.append("")
    if not candidates:
        lines.append("（未发现未入库的重要参考文献）")
        lines.append("")
        return "\n".join(lines)
    lines += ["## 候选权威（按被引用量排序）", "",
              "| title | DOI | cited_by | year |", "|---|---|---|---|"]
    for c in candidates[:50]:
        lines.append(
            f"| {str(c.get('title',''))[:60].replace('|','/')} | "
            f"{str(c.get('doi',''))[:40].replace('|','/')} | "
            f"{c.get('cited_by_count','')} | {c.get('year','')} |"
        )
    lines.append("")
    return "\n".join(lines)
