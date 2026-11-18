"""论文调研项目存储：project.json / selected_papers.json / 留痕日志。

兼容性承诺：
- 只新增 data/research_projects/ 与 knowledgebase/full/research_projects/；
- 不迁移/改写现有 data/paper_searches、data/papers、.data/tasks.json 等。
"""

from __future__ import annotations

import json
import re
import tempfile
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_WORK = _ROOT / "data" / "research_projects"
_MIRROR = _ROOT / "knowledgebase" / "default" / "research_projects"

STAGES = [
    "init", "search", "basic_matrix", "select", "convert",
    "translate", "condense", "classify", "semantic_matrix", "refs", "stats",
]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def slugify(name: str) -> str:
    """项目名 → 文件系统安全 slug。"""
    s = re.sub(r"[^\w\u4e00-\u9fff\-]+", "-", name.strip()).strip("-")
    return s[:60] or "project"


def work_dir(slug: str) -> Path:
    return _WORK / slug


def mirror_dir(slug: str) -> Path:
    return _MIRROR / slug


def project_path(slug: str) -> Path:
    return work_dir(slug) / "project.json"


def selected_path(slug: str) -> Path:
    return work_dir(slug) / "selected_papers.json"


def search_rounds_path(slug: str) -> Path:
    return work_dir(slug) / "search_rounds.jsonl"


def transfer_log_path(slug: str) -> Path:
    return work_dir(slug) / "transfer_log.jsonl"


def manual_notes_path(slug: str) -> Path:
    return work_dir(slug) / "manual_notes.md"


def _write_json_atomic(path: Path, data: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with open(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        Path(tmp).replace(path)
    finally:
        if Path(tmp).exists():
            Path(tmp).unlink(missing_ok=True)


def _read_json(path: Path, default: dict | list | None = None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _append_jsonl(path: Path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def list_projects() -> list[dict]:
    if not _WORK.is_dir():
        return []
    out = []
    for d in sorted(_WORK.iterdir()):
        if not d.is_dir():
            continue
        p = d / "project.json"
        if not p.exists():
            continue
        meta = _read_json(p, {}) or {}
        out.append({
            "id": meta.get("id", d.name),
            "name": meta.get("name", d.name),
            "created_at": meta.get("created_at", ""),
            "updated_at": meta.get("updated_at", ""),
            "papers_count": len(meta.get("papers", [])),
        })
    return out


def _default_project(name: str, question: str, keywords: list[str],
                     criteria: dict, target_venues: list[str],
                     important_groups: list[dict]) -> dict:
    slug = slugify(name)
    now = _now()
    return {
        "id": slug,
        "name": name,
        "created_at": now,
        "updated_at": now,
        "research_question": question,
        "keywords": keywords,
        "important_groups": important_groups,
        "criteria": criteria,
        "target_venues": target_venues,
        "papers": [],
        "roles": {},
        "manual_overrides": {},
        "stages": {s: {"status": "pending"} for s in STAGES},
    }


def init_project(name: str, question: str = "", keywords: list[str] | None = None,
                 criteria: dict | None = None, target_venues: list[str] | None = None,
                 important_groups: list[dict] | None = None) -> dict:
    """初始化论文调研项目。已存在则幂等返回现有项目。"""
    slug = slugify(name)
    wd = work_dir(slug)
    pfile = project_path(slug)
    if pfile.exists():
        proj = _read_json(pfile, {}) or {}
        return {"ok": True, "id": slug, "exists": True, "project": proj}

    proj = _default_project(
        name=name,
        question=question,
        keywords=keywords or [],
        criteria=criteria or {},
        target_venues=target_venues or [],
        important_groups=important_groups or [],
    )
    proj["stages"]["init"]["status"] = "completed"
    wd.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(pfile, proj)
    _write_json_atomic(selected_path(slug), {
        "project": slug,
        "papers": [],
        "updated_at": proj["updated_at"],
    })
    return {"ok": True, "id": slug, "exists": False, "project": proj}


def load_project(slug: str) -> dict:
    pfile = project_path(slug)
    if not pfile.exists():
        raise FileNotFoundError(f"项目不存在: {slug}（先 paper-project init）")
    return _read_json(pfile, {}) or {}


def save_project(proj: dict) -> None:
    slug = proj.get("id", "")
    if not slug:
        raise ValueError("project.id 缺失")
    proj["updated_at"] = _now()
    _write_json_atomic(project_path(slug), proj)
    _write_json_atomic(selected_path(slug), {
        "project": slug,
        "papers": [p.get("id") if isinstance(p, dict) else p for p in proj.get("papers", [])],
        "updated_at": proj["updated_at"],
    })


def set_stage(proj: dict, stage: str, status: str, **extra) -> None:
    if stage not in proj["stages"]:
        proj["stages"][stage] = {}
    entry = proj["stages"].get(stage, {})
    entry["status"] = status
    entry.update(extra)
    proj["stages"][stage] = entry


def append_search_round(slug: str, entry: dict) -> None:
    entry.setdefault("created_at", _now())
    _append_jsonl(search_rounds_path(slug), entry)


def append_transfer_log(slug: str, entry: dict) -> None:
    entry.setdefault("created_at", _now())
    _append_jsonl(transfer_log_path(slug), entry)


def add_papers(slug: str, paper_ids: list[str], reason: str = "",
               search_id: str | None = None) -> dict:
    """把论文加入项目。返回新增/已存在/失败。"""
    proj = load_project(slug)
    current = proj.get("papers", [])
    current_ids = {p["id"] if isinstance(p, dict) else p for p in current}
    added, exists = [], []
    for pid in paper_ids:
        pid = str(pid).strip()
        if not pid:
            continue
        if pid in current_ids:
            exists.append(pid)
            continue
        current.append({"id": pid, "added_at": _now(), "note": reason or ""})
        current_ids.add(pid)
        added.append(pid)
    proj["papers"] = current
    save_project(proj)
    for pid in added:
        append_transfer_log(slug, {
            "paper_id": pid,
            "action": "add",
            "from": "通用库" if not search_id else f"search:{search_id}",
            "to": f"project:{slug}",
            "reason": reason or "",
        })
    return {"ok": True, "id": slug, "added": added, "exists": exists}


def remove_papers(slug: str, paper_ids: list[str], reason: str = "") -> dict:
    proj = load_project(slug)
    keep = []
    removed = []
    for p in proj.get("papers", []):
        pid = p["id"] if isinstance(p, dict) else p
        if pid in paper_ids:
            removed.append(pid)
            continue
        keep.append(p)
    proj["papers"] = keep
    save_project(proj)
    for pid in removed:
        append_transfer_log(slug, {
            "paper_id": pid,
            "action": "remove",
            "from": f"project:{slug}",
            "to": "通用库",
            "reason": reason or "",
        })
    return {"ok": True, "id": slug, "removed": removed}


def move_papers(slug: str, paper_ids: list[str], to_project: str, reason: str = "") -> dict:
    """从当前项目移到另一个项目（当前移除 + 目标新增）。"""
    r = remove_papers(slug, paper_ids, reason=reason)
    if r.get("removed"):
        target = add_papers(to_project, r["removed"], reason=reason)
        for pid in r["removed"]:
            append_transfer_log(slug, {
                "paper_id": pid,
                "action": "move_out",
                "from": f"project:{slug}",
                "to": f"project:{to_project}",
                "reason": reason or "",
            })
        return {"ok": True, "id": slug, "moved": r["removed"], "target": target}
    return {"ok": True, "id": slug, "moved": []}


def set_field(slug: str, key: str, value, note: str = "") -> dict:
    """人工覆盖字段，记录 manual_overrides。"""
    proj = load_project(slug)
    old = proj.get(key)
    proj[key] = value
    mo = proj.setdefault("manual_overrides", {})
    mo[key] = {
        "value": value,
        "old_value": old,
        "updated_at": _now(),
        "note": note or "",
    }
    save_project(proj)
    if note:
        mp = manual_notes_path(slug)
        mp.parent.mkdir(parents=True, exist_ok=True)
        with open(mp, "a", encoding="utf-8") as f:
            f.write(f"\n## {_now()} {key}\n\n{note}\n")
    return {"ok": True, "id": slug, "key": key, "value": value}


def set_paper_role(slug: str, paper_id: str, roles: list[str] | str,
                   reason: str = "", confirmed: bool = True,
                   confidence: float | None = None) -> dict:
    """写入/确认某篇论文的角色。roles 可为逗号分隔字符串。"""
    if isinstance(roles, str):
        roles = [x.strip() for x in roles.split(",") if x.strip()]
    proj = load_project(slug)
    roles_map = proj.setdefault("roles", {})
    entry = dict(roles_map.get(paper_id, {}))
    entry["roles"] = roles
    entry["reason"] = reason or entry.get("reason", "")
    entry["confidence"] = confidence if confidence is not None else entry.get("confidence", 0.8)
    entry["status"] = "confirmed" if confirmed else "candidate"
    entry["confirmed_by"] = "human" if confirmed else None
    entry["updated_at"] = _now()
    roles_map[paper_id] = entry
    proj["roles"] = roles_map
    save_project(proj)
    return {"ok": True, "id": slug, "paper_id": paper_id, "roles": roles}


def project_status(slug: str) -> dict:
    """读取项目状态并检查常见产物是否存在。"""
    proj = load_project(slug)
    wd = work_dir(slug)
    mirror = mirror_dir(slug)
    checks = {
        "project_json": project_path(slug).exists(),
        "selected_papers_json": selected_path(slug).exists(),
        "search_rounds_log": search_rounds_path(slug).exists(),
        "matrix_meta_md": (mirror / "matrix_meta.md").exists(),
        "matrix_semantic_md": (mirror / "matrix_semantic.md").exists(),
        "roles_md": (mirror / "roles.md").exists(),
        "stats_md": (mirror / "stats.md").exists(),
    }
    return {"ok": True, "project": proj, "checks": checks}


def list_project_paper_ids(slug: str) -> list[str]:
    proj = load_project(slug)
    return [p["id"] if isinstance(p, dict) else p for p in proj.get("papers", [])]
