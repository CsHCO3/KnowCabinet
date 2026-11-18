"""文献矩阵：基础矩阵（无 AI）与语义矩阵（AI，可选）。

基础矩阵只读已有元数据，不调 LLM。
语义矩阵读取 condensation.md / paper.md / abstract，用 LLM 提炼。
"""

from __future__ import annotations

import json
from pathlib import Path

from backend.services.research_project import project_store as store
from backend.services.research_project.scoring import score_paper

_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_FULL_PAPERS = _ROOT / "knowledgebase" / "full" / "papers"
_SEARCHES_DIR = _ROOT / "data" / "paper_searches"


def _read_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _paper_meta(paper_id: str) -> dict | None:
    """从 full/papers/{id}/metadata.json 读取论文元数据。"""
    meta = _read_json(_FULL_PAPERS / paper_id / "metadata.json")
    if meta:
        return meta
    return _read_json(_ROOT / "data" / "papers" / paper_id / "metadata.json")


def _search_record_for(paper_id: str) -> dict | None:
    """回退：在 paper_searches/*.json 的 results 里按 entry_id 找。"""
    if not _SEARCHES_DIR.is_dir():
        return None
    for f in _SEARCHES_DIR.glob("*.json"):
        if f.name == "_index.json":
            continue
        rec = _read_json(f)
        if not rec or not isinstance(rec, dict):
            continue
        for r in rec.get("results", []):
            if r.get("entry_id") == paper_id:
                return r
    return None


def collect_basic_records(slug: str, paper_ids: list[str] | None = None,
                          keywords: list[str] | None = None) -> list[dict]:
    """收集项目论文的基础记录，并附无 AI 分数。"""
    if paper_ids is None:
        paper_ids = store.list_project_paper_ids(slug)
    records = []
    for pid in paper_ids:
        meta = _paper_meta(pid) or {}
        src = _search_record_for(pid) or {}
        rec = {
            "id": pid,
            "title": meta.get("title") or src.get("title") or pid,
            "authors": meta.get("authors") or src.get("authors") or "",
            "year": meta.get("year") or src.get("year"),
            "venue": meta.get("venue") or src.get("venue") or "",
            "citations": meta.get("citations") or src.get("citations") or 0,
            "doi": meta.get("doi") or src.get("doi") or "",
            "arxiv_id": meta.get("arxiv_id") or src.get("arxiv_id") or "",
            "abstract": meta.get("abstract") or src.get("abstract") or "",
            "keywords": meta.get("tags") or [],
            "sources": src.get("sources") or meta.get("provenance") or [],
        }
        score_info = score_paper(rec, keywords=keywords or [])
        rec.update(score_info)
        records.append(rec)
    return records


def _fmt_authors(authors) -> str:
    if isinstance(authors, list):
        return ", ".join(str(a) for a in authors[:3]) + (" et al." if len(authors) > 3 else "")
    return str(authors or "")


def basic_matrix_markdown(slug: str, paper_ids: list[str] | None = None,
                          keywords: list[str] | None = None) -> str:
    proj = store.load_project(slug)
    records = collect_basic_records(slug, paper_ids, keywords or proj.get("keywords", []))
    lines = [
        f"# 基础矩阵：{proj.get('name', slug)}",
        "",
        "| id | title | authors | year | venue | citations | score | DOI/arXiv | status |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in sorted(records, key=lambda x: x.get("score", 0), reverse=True):
        doi_arxiv = r.get("doi") or r.get("arxiv_id") or r.get("id", "")
        status = "meta-only"
        if (_FULL_PAPERS / r["id"] / "condensation.md").exists():
            status = "condensed"
        elif (_FULL_PAPERS / r["id"] / "paper.md").exists():
            status = "fulltext"
        lines.append(
            f"| {r['id']} | {str(r.get('title',''))[:60].replace('|','/')} | "
            f"{_fmt_authors(r.get('authors')).replace('|','/')} | {r.get('year','')} | "
            f"{str(r.get('venue',''))[:30].replace('|','/')} | {r.get('citations','')} | "
            f"{r.get('score','')} | {str(doi_arxiv)[:40].replace('|','/')} | {status} |"
        )
    lines.append("")
    return "\n".join(lines)


def write_basic_matrix(slug: str, paper_ids: list[str] | None = None,
                       keywords: list[str] | None = None) -> Path:
    md = basic_matrix_markdown(slug, paper_ids, keywords)
    work_out = store.work_dir(slug) / "matrix_meta.md"
    work_out.parent.mkdir(parents=True, exist_ok=True)
    work_out.write_text(md, encoding="utf-8")
    mirror_out = store.mirror_dir(slug) / "matrix_meta.md"
    mirror_out.parent.mkdir(parents=True, exist_ok=True)
    from backend.services.research_project.mirror import _frontmatter_for
    fm = _frontmatter_for(store.load_project(slug), kind="matrix_meta")
    mirror_out.write_text(fm + "\n" + md, encoding="utf-8")
    return work_out


def semantic_matrix_markdown(slug: str, paper_ids: list[str] | None = None,
                             model_key: str = "deepseek-chat") -> str:
    """语义矩阵：优先从 condensation.md 提炼，其次用 LLM 读摘要/全文片段。"""
    proj = store.load_project(slug)
    if paper_ids is None:
        paper_ids = store.list_project_paper_ids(slug)
    lines = [
        f"# 语义矩阵：{proj.get('name', slug)}",
        "",
        "| id | 研究方法 | 研究对象/语料 | 核心发现 | 局限 |",
        "|---|---|---|---|---|",
    ]
    for pid in paper_ids:
        cell = _semantic_cell(pid, model_key)
        lines.append(
            f"| {pid} | {cell['method']} | {cell['object']} | {cell['finding']} | {cell['limitation']} |"
        )
    lines.append("")
    return "\n".join(lines)


def _semantic_cell(paper_id: str, model_key: str) -> dict:
    full_dir = _FULL_PAPERS / paper_id
    cond = full_dir / "condensation.md"
    if cond.exists():
        text = cond.read_text(encoding="utf-8", errors="replace")
    else:
        pmd = full_dir / "paper.md"
        if pmd.exists():
            text = pmd.read_text(encoding="utf-8", errors="replace")[:4000]
        else:
            meta = _paper_meta(paper_id) or {}
            text = meta.get("abstract") or ""
    if not text.strip():
        return {"method": "待补充", "object": "待补充", "finding": "待补充", "limitation": "待补充"}

    try:
        from backend.config.settings import Settings
        from backend.pipelines._prompts import load_prompt
        llm = Settings().get_llm_client(model_key=model_key)
        sys_prompt, user_prompt = load_prompt(
            "paper_matrix_semantic", sub_dir="review",
            paper_text=text[:20000],
            paper_id=paper_id,
        )
        result = llm.call(
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=800,
            temperature=0.2,
        )
        if not result or not result.strip():
            return {"method": "待补充", "object": "待补充", "finding": "待补充", "limitation": "待补充"}
        # 简单按行解析：期望一行“方法 | 对象 | 发现 | 局限”
        first = result.strip().splitlines()[0].strip("|").strip()
        parts = [p.strip() for p in first.split("|")]
        while len(parts) < 4:
            parts.append("待补充")
        return {
            "method": parts[0],
            "object": parts[1],
            "finding": parts[2],
            "limitation": parts[3],
        }
    except Exception as e:
        return {"method": f"LLM失败:{e}", "object": "待补充", "finding": "待补充", "limitation": "待补充"}


def write_semantic_matrix(slug: str, paper_ids: list[str] | None = None,
                          model_key: str = "deepseek-chat") -> Path:
    md = semantic_matrix_markdown(slug, paper_ids, model_key)
    work_out = store.work_dir(slug) / "matrix_semantic.md"
    work_out.parent.mkdir(parents=True, exist_ok=True)
    work_out.write_text(md, encoding="utf-8")
    mirror_out = store.mirror_dir(slug) / "matrix_semantic.md"
    mirror_out.parent.mkdir(parents=True, exist_ok=True)
    from backend.services.research_project.mirror import _frontmatter_for
    fm = _frontmatter_for(store.load_project(slug), kind="matrix_semantic")
    mirror_out.write_text(fm + "\n" + md, encoding="utf-8")
    return work_out
