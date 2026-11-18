"""统计报告：面向 AI/人快速了解项目状态与论文库分布。

输出 Markdown，可选 JSON；不依赖网页前端。
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from backend.services.research_project import project_store as store

_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_FULL_PAPERS = _ROOT / "knowledgebase" / "default" / "papers"
_KB_PARTS = ("default", "CBAC")
_BIN_PAPERS = _ROOT / "data" / "papers"
_SEARCHES_DIR = _ROOT / "data" / "paper_searches"


def _read_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _paper_status(paper_id: str) -> dict:
    part_dirs = [(_ROOT / "knowledgebase" / p / "papers" / paper_id) for p in _KB_PARTS]
    dense_dirs = [(_ROOT / "knowledgebase" / p / "dense" / f"{paper_id}.md") for p in _KB_PARTS]
    full = next((d for d in part_dirs if d.is_dir()), _FULL_PAPERS / paper_id)
    bin = _BIN_PAPERS / paper_id
    return {
        "has_pdf": (bin / "paper.pdf").exists(),
        "has_md": (full / "paper.md").exists() or (full / "cn_paper.md").exists()
                  or (full / "paper_ocr.md").exists() or (full / "cn_paper_ocr.md").exists(),
        "has_translate": (full / "cn_paper.md").exists() or (full / "cn_paper_ocr.md").exists(),
        "has_condense": (full / "condensation.md").exists()
                        or any(d.exists() for d in dense_dirs),
    }


def build_stats(slug: str) -> dict:
    proj = store.load_project(slug)
    paper_ids = store.list_project_paper_ids(slug)
    year_counter: Counter = Counter()
    venue_counter: Counter = Counter()
    status_counter: Counter = Counter()
    role_counter: Counter = Counter()
    missing_meta = []

    for pid in paper_ids:
        meta = _read_json(_FULL_PAPERS / pid / "metadata.json") or {}
        year = meta.get("year") or ""
        venue = meta.get("venue") or ""
        if year:
            year_counter[str(year)] += 1
        if venue:
            venue_counter[str(venue)[:40]] += 1
        st = _paper_status(pid)
        for key in ("has_pdf", "has_md", "has_translate", "has_condense"):
            status_counter[key] += 1 if st.get(key) else 0
        if not meta.get("title") or not meta.get("authors") or not meta.get("year"):
            missing_meta.append(pid)
        role = proj.get("roles", {}).get(pid, {})
        if role.get("status") == "confirmed":
            for r in role.get("roles", []):
                role_counter[r] += 1
        elif role.get("status") == "candidate":
            for r in role.get("roles", []):
                role_counter[f"{r}?candidate"] += 1

    stats = {
        "project": slug,
        "name": proj.get("name", slug),
        "updated_at": proj.get("updated_at", ""),
        "candidate_total": 0,
        "selected_count": len(paper_ids),
        "year_distribution": dict(year_counter.most_common()),
        "venue_distribution": dict(venue_counter.most_common(20)),
        "status": {k: status_counter.get(k, 0) for k in
                   ("has_pdf", "has_md", "has_translate", "has_condense")},
        "role_distribution": dict(role_counter.most_common()),
        "missing_meta": missing_meta,
    }
    # 候选总数：从 search_rounds 累计 paper_ids 去重近似
    all_candidates: set[str] = set()
    log = store.search_rounds_path(slug)
    if log.exists():
        for line in log.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
                all_candidates.update(entry.get("paper_ids", []))
            except Exception:
                continue
    stats["candidate_total"] = len(all_candidates)
    return stats


def stats_markdown(stats: dict) -> str:
    lines = [
        f"# 统计报告：{stats.get('name', stats.get('project'))}",
        "",
        f"- 候选论文（去重近似）：{stats.get('candidate_total', 0)}",
        f"- 已选论文：{stats.get('selected_count', 0)}",
        f"- 已下载 PDF：{stats['status'].get('has_pdf', 0)}",
        f"- 已转 md：{stats['status'].get('has_md', 0)}",
        f"- 已翻译：{stats['status'].get('has_translate', 0)}",
        f"- 已浓缩：{stats['status'].get('has_condense', 0)}",
        "",
    ]
    if stats.get("year_distribution"):
        lines += ["## 年份分布", "", "| 年份 | 数量 |", "|---|---|"]
        for y, c in stats["year_distribution"].items():
            lines.append(f"| {y} | {c} |")
        lines.append("")
    if stats.get("venue_distribution"):
        lines += ["## Venue 分布", "", "| Venue | 数量 |", "|---|---|"]
        for v, c in list(stats["venue_distribution"].items())[:20]:
            lines.append(f"| {str(v).replace('|','/')} | {c} |")
        lines.append("")
    if stats.get("role_distribution"):
        lines += ["## 角色分布", "", "| 角色 | 数量 |", "|---|---|"]
        for r, c in stats["role_distribution"].items():
            lines.append(f"| {r} | {c} |")
        lines.append("")
    if stats.get("missing_meta"):
        lines += ["## 缺失元数据", ""]
        for pid in stats["missing_meta"]:
            lines.append(f"- {pid}")
        lines.append("")
    return "\n".join(lines)


def write_stats(slug: str) -> Path:
    stats = build_stats(slug)
    md = stats_markdown(stats)
    work_out = store.work_dir(slug) / "stats.md"
    work_out.parent.mkdir(parents=True, exist_ok=True)
    work_out.write_text(md, encoding="utf-8")
    mirror_out = store.mirror_dir(slug) / "stats.md"
    mirror_out.parent.mkdir(parents=True, exist_ok=True)
    from backend.services.research_project.mirror import _frontmatter_for
    fm = _frontmatter_for(store.load_project(slug), kind="stats")
    mirror_out.write_text(fm + "\n" + md, encoding="utf-8")
    # 同时写 JSON，方便 AI 快速消费
    (store.work_dir(slug) / "stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    return work_out
