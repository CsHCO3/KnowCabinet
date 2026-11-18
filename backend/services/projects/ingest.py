"""代码项目入库：建目录 + project.md + metadata.json。

结构：`knowledgebase/full/{调研分区}/projects/{id}/`
  - project.md       # frontmatter(8标准字段) + 正文骨架（能力/算法链/生态/关系）
  - metadata.json    # 富字段（API 自动拉 + 人工补）

与论文同组：调研分区下 papers/ 存论文、projects/ 存项目，registry 各注册一个 KB，
`rag-ask --kb {分区}-projects` 检索项目，`search_all` 跨库同时命中论文+项目。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from backend.services.projects import source as psrc

_ROOT = Path(__file__).resolve().parent.parent.parent.parent


def _projects_dir(kb: str) -> Path:
    """调研分区 → full/{kb}/projects/。"""
    return _ROOT / "knowledgebase" / "full" / kb / "projects"


def _slug(name: str) -> str:
    """项目 id → 文件系统安全名（{owner}-{name}）。"""
    return re.sub(r"[^0-9A-Za-z.\-_]+", "-", name.strip()).strip("-")


def _upgrade_status(cur: str | None, verified: bool) -> str:
    """status 升级：已验证且是 candidate → active；否则保留现有或按 verified 初始化。"""
    if cur:
        return "active" if (verified and cur == "candidate") else cur
    return "active" if verified else "candidate"


def _yaml_str(v) -> str:
    """YAML 安全字符串（需要引号则加引号）。"""
    s = str(v)
    if not s:
        return '""'
    if s != s.strip() or any(ch in s for ch in ':#"{}[],&*!|>\'%@`'):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def _yaml_tags(tags: list[str]) -> str:
    """tags → YAML 列表：`[a, b, c]`（引擎 parse_tags 可解析）。"""
    items = [_yaml_str(t) for t in tags]
    return "[" + ", ".join(items) + "]"


def build_frontmatter(meta: dict) -> str:
    """8 标准字段 frontmatter（title/source/section/tags/year/source_type）。"""
    lines = ["---"]
    t = meta.get("display_name") or meta.get("title")
    if t:
        lines.append(f"title: {_yaml_str(t)}")
    if meta.get("url"):
        lines.append(f"source: {_yaml_str(meta['url'])}")
    section = meta.get("section") or "未分类"
    lines.append(f"section: {_yaml_str(section)}")
    if meta.get("tags"):
        lines.append(f"tags: {_yaml_tags(meta['tags'])}")
    if meta.get("year"):
        lines.append(f"year: {_yaml_str(meta['year'])}")
    lines.append("source_type: project")
    lines.append("---")
    return "\n".join(lines)


def _render_relation(r) -> str:
    """relations 条目 → 文本行。"""
    if isinstance(r, dict):
        parts = [str(r.get("with", ""))]
        if r.get("type"):
            parts.append(str(r["type"]))
        if r.get("note"):
            parts.append(str(r["note"]))
        return "（".join(parts[:2]) + ("，" + parts[2] if len(parts) > 2 else "") + "）" if len(parts) > 1 else str(r)
    return str(r)


def _render_capability(c) -> str:
    """capabilities 条目 → 文本行。"""
    if isinstance(c, dict):
        parts = [str(c.get("name", ""))]
        if c.get("evidence"):
            parts.append(f"证据:{c['evidence']}")
        if c.get("scale"):
            parts.append(f"规模:{c['scale']}")
        if c.get("condition"):
            parts.append(f"前置:{c['condition']}")
        return "｜".join(parts)
    return str(c)


def render_md(meta: dict) -> str:
    """从 metadata（含富字段）渲染 project.md 正文。有值填真值，缺的留"待补充"。"""
    def _join(v, fallback="（待补充）"):
        return "、".join(v) if isinstance(v, list) and v else (str(v) if v else fallback)

    title = meta.get("display_name") or meta.get("title") or meta.get("name") or "未命名项目"

    # 核心算法 / 依赖链
    core = meta.get("core_algorithms") or []
    base = meta.get("base_libraries") or []
    up = meta.get("upstream_projects") or []
    bd = meta.get("build_deps") or []
    ls = meta.get("language_stack") or []
    # 生态地位
    eco = meta.get("eco_layer") or ""
    dlow = meta.get("deps_lower") or []
    sup = meta.get("supports_upper") or []
    comp = meta.get("competitors") or []
    # 能力 / 关系
    caps = meta.get("capabilities") or []
    rels = meta.get("relations") or []

    lines = [f"# {title}", "",
             "## 一句话",
             meta.get("description") or "（待补充：项目是做什么的，解决什么问题）",
             (f"（中文：{meta['cn_note']}）" if meta.get("cn_note") else ""),
             "",
             "## 核心算法 / 底层库 / 依赖链",
             f"- 核心算法：{_join(core)}",
             f"- 底层库：{_join(base)}",
             f"- 上游项目：{_join(up)}",
             f"- 构建依赖：{_join(bd)}",
             f"- 语言/运行时栈：{_join(ls)}",
             "",
             "## 生态地位",
             f"- 所在层：{eco or '（待补充）'}",
             f"- 依赖下层：{_join(dlow)}",
             f"- 支撑上层：{_join(sup)}",
             f"- 同层竞争：{_join(comp)}",
             "",
             "## 能力",
             *([f"- {_render_capability(c)}" for c in caps] if caps
               else ["（待补充：能力名称 + 验证层级 + 规模上限 + 性能 + 前置条件 + 已知限制）"]),
             "",
             "## 已知限制",
             *([f"- {x}" for x in meta.get("known_limitations")] if meta.get("known_limitations")
               else ["（待补充）"]),
             "",
             "## 项目间关系",
             *([f"- {_render_relation(r)}" for r in rels] if rels
               else ["（待补充：互补 / 竞争 / 交错 / 不相干 / 有条件配对 + 配对矛盾）"]),
             "",
             "## 来源",
             f"- 仓库：{meta.get('url') or '（待填）'}",
             f"- host：{meta.get('host') or '（待填）'}",
             f"- 拉取时间：{meta.get('observed_at') or '（待填）'}",
             f"- 验证状态：{'已验证' if meta.get('verified') else '未验证（候选）'}"]

    # 去空行（cn_note 空时留一个空行分隔）
    out = []
    prev_blank = False
    for ln in lines:
        blank = (ln == "")
        if blank and prev_blank:
            continue
        out.append(ln)
        prev_blank = blank
    return build_frontmatter(meta) + "\n" + "\n".join(out).rstrip() + "\n"


def _build_md(meta: dict) -> str:
    return render_md(meta)


def _write_metadata(out_dir: Path, meta: dict) -> None:
    """写 metadata.json（富字段 + 人工补字段骨架 + 状态）。"""
    mpath = out_dir / "metadata.json"
    existing = {}
    if mpath.exists():
        try:
            existing = json.loads(mpath.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
    record = {
        "type": "project",
        "id": meta.get("id", out_dir.name),
        "name": meta.get("name", ""),
        "url": meta.get("url", ""),
        "host": meta.get("host", ""),
        "owner": meta.get("owner", ""),
        "description": meta.get("description", ""),
        "license": meta.get("license", ""),
        "language": meta.get("language", []),
        "stars": int(meta.get("stars") or 0),
        "forks": int(meta.get("forks") or 0),
        "archived": bool(meta.get("archived")),
        "last_commit": meta.get("last_commit", ""),
        "latest_release": meta.get("latest_release", ""),
        "observed_at": (existing.get("observed_at")
                        or datetime.now().strftime("%Y-%m-%d")),
        "last_verified": existing.get("last_verified")
        or (datetime.now().strftime("%Y-%m-%d") if meta.get("verified") else ""),
        "verified": bool(meta.get("verified")),
        # status：已验证且现为 candidate → 升 active；否则保留（用户可能已人工改）
        "status": _upgrade_status(existing.get("status"), meta.get("verified")),
        "section": meta.get("section", ""),
        "tags": meta.get("tags", []),
        "manual_tags": meta.get("manual_tags") or existing.get("manual_tags", []),
        "display_name": meta.get("display_name") or existing.get("display_name", ""),
        "cn_note": meta.get("cn_note") or existing.get("cn_note", ""),
        "provenance": existing.get("provenance") or {"auto": "api"},
        "manual_fields": existing.get("manual_fields", []),
        "conflicts": existing.get("conflicts", []),
        # 富字段（meta 已含 existing 保留值 + API 拉取；无则空骨架）
        "core_algorithms": meta.get("core_algorithms") or existing.get("core_algorithms", []),
        "base_libraries": meta.get("base_libraries") or existing.get("base_libraries", []),
        "upstream_projects": meta.get("upstream_projects") or existing.get("upstream_projects", []),
        "build_deps": meta.get("build_deps") or existing.get("build_deps", []),
        "language_stack": meta.get("language_stack") or existing.get("language_stack", []),
        "capabilities": meta.get("capabilities") or existing.get("capabilities", []),
        "eco_layer": meta.get("eco_layer") or existing.get("eco_layer", ""),
        "deps_lower": meta.get("deps_lower") or existing.get("deps_lower", []),
        "supports_upper": meta.get("supports_upper") or existing.get("supports_upper", []),
        "competitors": meta.get("competitors") or existing.get("competitors", []),
        "relations": meta.get("relations") or existing.get("relations", []),
    }
    mpath.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


# ── D4: 富字段 JSON Schema 校验 ──────────────────
# 关系 type 允许单值，也允许 "/" 连接的组合（如 "交错/上游依赖"）
_RELATION_TYPES = {"互补", "竞争", "交错", "不相干", "有条件配对",
                   "上游依赖", "下游依赖", "弱相关", "并存", "潜在替代"}


def _valid_relation_type(t: str) -> bool:
    """type 合法：单值 或 以 / 连接的多值，每段都在允许集合。"""
    return bool(t) and all(p.strip() in _RELATION_TYPES for p in t.split("/"))


def validate_metadata(meta: dict) -> list[str]:
    """校验富字段结构（D4）。返回问题列表；空 = 通过。"""
    problems: list[str] = []

    def _check_list_of_str(field: str, label: str) -> None:
        v = meta.get(field)
        if v is None:
            return
        if not isinstance(v, list) or any(not isinstance(x, str) for x in v):
            problems.append(f"{field}: 应为字符串列表")

    def _check_list_of_dict(field: str, label: str, keys: set[str]) -> None:
        v = meta.get(field)
        if v is None:
            return
        if not isinstance(v, list):
            problems.append(f"{field}: 应为对象列表")
            return
        for i, item in enumerate(v):
            if not isinstance(item, dict):
                problems.append(f"{field}[{i}]: 应为对象")
                continue
            for k in keys:
                if k not in item:
                    problems.append(f"{field}[{i}]: 缺 {k}")

    _check_list_of_str("core_algorithms", "核心算法")
    _check_list_of_str("base_libraries", "底层库")
    _check_list_of_str("upstream_projects", "上游项目")
    _check_list_of_str("build_deps", "构建依赖")
    _check_list_of_str("language_stack", "语言栈")
    _check_list_of_str("deps_lower", "依赖下层")
    _check_list_of_str("supports_upper", "支撑上层")
    _check_list_of_str("competitors", "同层竞争")
    _check_list_of_dict("capabilities", "能力", {"name"})
    _check_list_of_dict("relations", "关系", {"with", "type"})
    # relations.type 取值校验（允许 / 组合值）
    for i, r in enumerate(meta.get("relations") or []):
        if isinstance(r, dict) and r.get("type") and not _valid_relation_type(r["type"]):
            problems.append(f"relations[{i}].type: 非法取值 '{r['type']}'（允许 {sorted(_RELATION_TYPES)}，可 / 组合）")
    return problems


def _candidates_path(kb: str) -> Path:
    """候选清单放分区根 full/{kb}/_candidates.md（不在 projects 子目录，避免被项目 KB 索引）。"""
    return _ROOT / "knowledgebase" / "full" / kb / "_candidates.md"


def _remove_candidate(kb: str, url: str) -> None:
    """项目验证后，从候选清单移除对应行。"""
    cand = _candidates_path(kb)
    if not cand.exists():
        return
    lines = [ln for ln in cand.read_text(encoding="utf-8").splitlines()
             if url and url not in ln]
    cand.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _append_candidate(kb: str, meta: dict) -> None:
    """未验证项目 → 追加到候选清单。"""
    cand = _candidates_path(kb)
    cand.parent.mkdir(parents=True, exist_ok=True)
    line = f"- {meta.get('title') or meta.get('name')} | {meta.get('url') or ''} | {meta.get('observed_at') or ''}"
    if cand.exists():
        content = cand.read_text(encoding="utf-8")
        if meta.get("url") and meta.get("url") in content:
            return
    else:
        content = "# 项目候选清单（未验证）\n\n> 仅 URL/名字，尚未拉取验证。`project-import --verify` 可升级。\n\n"
    with open(cand, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ── F3: 自动推断 section（topic/description → 主题粗分类）──
_SECTION_RULES: list[tuple[str, list[str]]] = [
    ("同态加密/密态计算", ["homomorphic", "fhe", "fully-homomorphic", "encryption", "seal", "openfhe", "lattigo", "tfhe", "ckks", "bfv", "bgv", "同态"]),
    ("安全多方计算/MPC", ["mpc", "multi-party", "secure-computation", "secret-sharing", "spdz", "多方计算"]),
    ("私密信息检索/PIR", ["pir", "private-information", "private-information-retrieval", "检索"]),
    ("编译器/IR", ["compiler", "mlir", "ir", "llvm", "tensor-compiler", "编译"]),
    ("机器学习/模型", ["machine-learning", "deep-learning", "transformer", "neural", "bert", "llm", "大模型", "模型"]),
    ("向量数据库/检索", ["vector", "database", "index", "similarity-search", "向量", "数据库", "索引"]),
    ("零知识证明/zk", ["zero-knowledge", "zk", "snark", "stark", "proof", "零知识"]),
    ("安全硬件/TEE", ["tee", "secure-enclave", "trusted-execution", "hardware"]),
    ("区块链", ["blockchain", "smart-contract", "chain", "区块链"]),
]


def _auto_section(topics: list[str], description: str = "") -> str:
    """从 topics/description 推断 section。命中多个取第一个规则；都不中 → 未分类。"""
    text = " ".join((topics or []) + ([description] if description else [])).lower()
    for section, kws in _SECTION_RULES:
        if any(k in text for k in kws):
            return section
    return "未分类"


def ingest_project(url_or_name: str, kb: str, *,
                   host: str | None = None, section: str = "", tags: list[str] | None = None,
                   display_name: str = "", force: bool = False,
                   cn_note: str = "") -> dict:
    """拉取并入库一个项目到 {kb}/projects/。

    kb ∈ 调研分区名（1-3-VERA / 3-IMA ...），目录 full/{kb}/projects/{id}/。
    display_name：人工规范名（如 "HEIR" 而非 "heir"）；cn_note：中文一句话。
    --force 重拉时保留已有富字段（不覆盖人工补的内容）。
    返回 {"ok", "id", "dir", "verified", ...}。
    """
    # 已存在时先读旧富字段（--force 重拉保留）
    existing_meta: dict = {}
    meta = psrc.fetch_project(url_or_name, host=host)
    if not meta.get("url"):
        return {"ok": False, "error": f"无法解析项目 URL（{url_or_name}）。若是 owner/repo 或名字，请确认拼写或提供完整 URL。"}

    meta["id"] = _slug(meta.get("name") or meta.get("title") or url_or_name)
    old_meta_path = _projects_dir(kb) / meta["id"] / "metadata.json"
    if old_meta_path.exists():
        try:
            existing_meta = json.loads(old_meta_path.read_text(encoding="utf-8"))
        except Exception:
            existing_meta = {}

    # B4: 人工规范名 / 中文一句话优先
    if display_name:
        meta["display_name"] = display_name
    elif existing_meta.get("display_name"):
        meta["display_name"] = existing_meta["display_name"]
    if cn_note:
        meta["cn_note"] = cn_note
    elif existing_meta.get("cn_note"):
        meta["cn_note"] = existing_meta["cn_note"]

    # 人工补的 section 优先；否则自动推断（F3）；否则保留已有；最后未分类
    if section:
        meta["section"] = section
    elif not existing_meta.get("section") or existing_meta["section"] == "未分类":
        auto_sec = _auto_section(meta.get("topics") or [], meta.get("description") or "")
        meta["section"] = existing_meta.get("section") if existing_meta.get("section") not in ("", "未分类") else auto_sec
    else:
        meta["section"] = existing_meta["section"]
    auto_tags = list(dict.fromkeys(meta.get("topics") or []))
    # 已有的人工 tags（metadata 里记录的 manual_tags；旧数据无该字段时用其 tags 推断）优先
    existing_manual = list(dict.fromkeys(existing_meta.get("manual_tags", [])))
    if not existing_manual and existing_meta.get("tags"):
        # 兼容旧数据：existing.tags 里不在本次 auto_topics 的视为人工 tag
        existing_manual = [t for t in existing_meta["tags"] if t not in auto_tags]
    manual_tags = [t for t in (tags or []) if t] or existing_manual
    meta["manual_tags"] = list(dict.fromkeys(manual_tags))
    meta["tags"] = list(dict.fromkeys(manual_tags + auto_tags))

    # 保留已有富字段（--force 重拉不抹掉人工补的 core_algorithms/relations 等）
    for rich in ("core_algorithms", "base_libraries", "upstream_projects", "build_deps",
                 "language_stack", "capabilities", "eco_layer", "deps_lower",
                 "supports_upper", "competitors", "relations"):
        if existing_meta.get(rich):
            meta[rich] = existing_meta[rich]

    meta["observed_at"] = datetime.now().strftime("%Y-%m-%d")

    # B3: 确保候选清单存在
    ensure_candidates(kb)

    out_dir = _projects_dir(kb) / meta["id"]
    if out_dir.exists() and not force:
        return {"ok": False, "id": meta["id"], "error": f"项目已存在（{out_dir.name}），加 --force 覆盖"}
    out_dir.mkdir(parents=True, exist_ok=True)

    # project.md（已有不覆盖，除非 force）
    pmd = out_dir / "project.md"
    if not pmd.exists() or force:
        pmd.write_text(render_md(meta), encoding="utf-8")

    _write_metadata(out_dir, meta)

    if meta.get("verified"):
        _remove_candidate(kb, meta.get("url", ""))
    else:
        _append_candidate(kb, meta)

    return {
        "ok": True,
        "id": meta["id"],
        "dir": str(out_dir),
        "verified": bool(meta.get("verified")),
        "title": meta.get("display_name") or meta.get("title"),
        "url": meta.get("url"),
        "host": meta.get("host"),
        "stars": meta.get("stars"),
    }


def ensure_candidates(kb: str) -> Path:
    """确保分区候选清单 full/{kb}/_candidates.md 存在（预置模板）。"""
    cand = _candidates_path(kb)
    cand.parent.mkdir(parents=True, exist_ok=True)
    if not cand.exists():
        cand.write_text("# 项目候选清单（未验证）\n\n"
                        "> 仅 URL/名字，尚未拉取验证。`project-import <url> --kb <分区>` 升级为正式条目。\n\n",
                        encoding="utf-8")
    return cand


def render_project(kb: str, project_id: str) -> Path:
    """把单个项目的 metadata.json 富字段渲染回 project.md（B1）。"""
    out_dir = _projects_dir(kb) / project_id
    mpath = out_dir / "metadata.json"
    if not mpath.exists():
        raise FileNotFoundError(f"项目 {project_id} 无 metadata.json（{out_dir}）")
    meta = json.loads(mpath.read_text(encoding="utf-8"))
    # metadata 结构用 name/display_name（无 title），补上 title 供 frontmatter/H1
    meta.setdefault("title", meta.get("display_name") or meta.get("name") or project_id)
    meta.setdefault("host", "")
    meta.setdefault("url", "")
    (out_dir / "project.md").write_text(render_md(meta), encoding="utf-8")
    return out_dir / "project.md"


def render_all(kb: str) -> list[str]:
    """渲染某分区下所有项目的富字段回 project.md。返回渲染的文件路径。"""
    rendered = []
    for proj_dir in sorted(_projects_dir(kb).glob("*/")):
        if proj_dir.name.startswith("_") or not proj_dir.is_dir():
            continue
        if not (proj_dir / "metadata.json").exists():
            continue
        try:
            render_project(kb, proj_dir.name)
            rendered.append(proj_dir.name)
        except Exception as e:
            print(f"  [warn] {proj_dir.name} 渲染失败: {e}")
    return rendered


def project_matrix(kb: str) -> str:
    """生成"项目 × 层 × 关系 × 配对矛盾"矩阵 markdown（F4）。"""
    rows = []
    for proj_dir in sorted(_projects_dir(kb).glob("*/")):
        if proj_dir.name.startswith("_") or not proj_dir.is_dir():
            continue
        mpath = proj_dir / "metadata.json"
        if not mpath.exists():
            continue
        try:
            m = json.loads(mpath.read_text(encoding="utf-8"))
        except Exception:
            continue
        name = m.get("display_name") or m.get("title") or proj_dir.name
        layer = m.get("eco_layer") or "?"
        rels = "; ".join(_render_relation(r) for r in (m.get("relations") or [])) or "—"
        rows.append((name, layer, rels))
    if not rows:
        return f"# 项目生态矩阵（{kb}）\n\n（暂无项目）\n"
    md = [f"# 项目生态矩阵（{kb}）", "", "| 项目 | 所在层 | 关系（互补/竞争/交错/不相干/配对矛盾） |",
          "|---|---|---|"]
    for name, layer, rels in rows:
        rels = rels.replace("|", "/")
        md.append(f"| {name} | {layer} | {rels} |")
    md.append("")
    return "\n".join(md)


def list_stale(days: int = 90) -> list[dict]:
    """列出 last_verified 超期（或从未验证）的项目。"""
    out = []
    today = datetime.now().date()
    for proj_dir in (_ROOT / "knowledgebase" / "full").glob("*/projects/*"):
        if proj_dir.name.startswith("_") or not proj_dir.is_dir():
            continue
        mpath = proj_dir / "metadata.json"
        if not mpath.exists():
            continue
        try:
            meta = json.loads(mpath.read_text(encoding="utf-8"))
        except Exception:
            continue
        lv = (meta.get("last_verified") or "").strip()
        if not lv:
            out.append({"id": proj_dir.name, "last_verified": "", "stale": True})
            continue
        try:
            age = (today - datetime.strptime(lv, "%Y-%m-%d").date()).days
        except ValueError:
            age = 9999
        if age > days:
            out.append({"id": proj_dir.name, "last_verified": lv, "stale": age > days})
    return out
