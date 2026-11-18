"""镜像刷新：把 data/research_projects 的可检索 md 镜像到 knowledgebase/full/research_projects。"""

from __future__ import annotations

from pathlib import Path

from backend.services.research_project import project_store as store


def _yaml_str(v) -> str:
    s = str(v)
    if not s:
        return '""'
    if s != s.strip() or any(ch in s for ch in ':#"{}[],&*!|>\'%@`'):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def _frontmatter_for(proj: dict, kind: str = "project") -> str:
    slug = proj.get("id", "")
    name = proj.get("name", slug)
    lines = [
        "---",
        f"id: {_yaml_str(slug)}",
        f"title: {_yaml_str(name + ' / ' + kind)}",
        "section: research_projects",
        f"tags: [paper-research, {_yaml_str(slug)}]",
        "source_type: research_project",
        "---",
    ]
    return "\n".join(lines)


def mirror_project(slug: str) -> Path:
    proj = store.load_project(slug)
    lines = [
        f"# {proj.get('name', slug)}",
        "",
        "## 研究问题",
        proj.get("research_question") or "（待补充）",
        "",
        "## 关键词",
        "、".join(proj.get("keywords", [])) or "（待补充）",
        "",
        "## 筛选标准",
        "include: " + "；".join(proj.get("criteria", {}).get("include", [])) or "（待补充）",
        "exclude: " + "；".join(proj.get("criteria", {}).get("exclude", [])) or "（待补充）",
        "",
        "## 目标期刊/会议",
        "、".join(proj.get("target_venues", [])) or "（待补充）",
        "",
        "## 重要实验室/作者",
    ]
    groups = proj.get("important_groups", [])
    if groups:
        for g in groups:
            lines.append(f"- {g.get('name','')}: {g.get('note','')}")
    else:
        lines.append("- （待补充）")
    lines += ["", "## 阶段状态", ""]
    for stage, info in proj.get("stages", {}).items():
        status = info.get("status", "pending") if isinstance(info, dict) else "pending"
        lines.append(f"- {stage}: {status}")
    lines += ["", "## 已选论文", ""]
    for p in proj.get("papers", []):
        pid = p["id"] if isinstance(p, dict) else p
        note = p.get("note", "") if isinstance(p, dict) else ""
        lines.append(f"- {pid}{f'（{note}）' if note else ''}")
    if not proj.get("papers"):
        lines.append("- （暂无）")
    lines.append("")
    md = "\n".join(lines)
    out = store.mirror_dir(slug) / "project.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_frontmatter_for(proj, "project") + "\n" + md, encoding="utf-8")
    return out


def mirror_roles(slug: str) -> Path:
    proj = store.load_project(slug)
    roles_map = proj.get("roles", {})
    lines = [f"# 文献角色分类：{proj.get('name', slug)}", ""]
    order = ["key", "background", "benchmark", "controversial", "gap"]
    buckets: dict[str, list[tuple[str, dict]]] = {}
    for pid, entry in roles_map.items():
        for r in entry.get("roles", []):
            buckets.setdefault(r, []).append((pid, entry))
    for role in order:
        items = buckets.get(role, [])
        if not items:
            continue
        lines.append(f"## {role}")
        for pid, entry in items:
            status = entry.get("status", "candidate")
            conf = entry.get("confidence", "")
            reason = entry.get("reason", "")
            lines.append(f"- {pid}: {reason}（{status}, confidence {conf}）")
        lines.append("")
    if not roles_map:
        lines.append("（暂无角色分类）")
    lines.append("")
    md = "\n".join(lines)
    out = store.mirror_dir(slug) / "roles.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_frontmatter_for(proj, "roles") + "\n" + md, encoding="utf-8")
    return out


def refresh_all(slug: str) -> list[str]:
    """刷新全部镜像 md，并重建 research_projects KB 索引。"""
    paths = [str(mirror_project(slug)), str(mirror_roles(slug))]
    work = store.work_dir(slug)
    for name in ("matrix_meta.md", "matrix_semantic.md", "stats.md"):
        src = work / name
        dst = store.mirror_dir(slug) / name
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            body = src.read_text(encoding="utf-8", errors="replace")
            proj = store.load_project(slug)
            kind = name.replace(".md", "")
            dst.write_text(_frontmatter_for(proj, kind) + "\n" + body, encoding="utf-8")
            paths.append(str(dst))
    try:
        from backend.services.ragsearch.service import rebuild_kb
        rebuild_kb("research_projects")
    except Exception as e:
        paths.append(f"rebuild_warn:{e}")
    return paths
