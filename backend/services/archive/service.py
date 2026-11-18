"""归档服务 — 把散乱的论文/视频/文档产物整理进统一结构。

目标结构（见 docs/2/知识库设计方法.md，三层：dense 高浓度 / full 全文 / data 原始）：
  knowledgebase/dense/paper/{arxiv_id}.md    # L1 论文浓缩（晋升）
  knowledgebase/dense/video/{bvid}.md        # L1 视频摘要（晋升）
  knowledgebase/full/papers/{arxiv_id}/      # L2 论文最优完整版 md + metadata.json
  knowledgebase/full/docs/{stem}/            # L2 文档全文 md
  knowledgebase/full/web/{site}/             # L2 网站教程全文 md
  data/papers/{arxiv_id}/paper.pdf images/   # L3 论文二进制资产（不入 KB）
  data/videos/{bvid}/subtitle.json|txt|summary.md   # L3 视频处理记录（summary 晋升 dense）

复制为主（不删旧文件）；★RAG 文件（.md）带 frontmatter；每对象一个 metadata.json。
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent.parent
# 分区优先（2026-10-20 重构）：每个分区自带 papers/ 与 dense/；full/ 与顶层 dense/ 已取消
_DEFAULT = _ROOT / "knowledgebase" / "default"
_CBAC = _ROOT / "knowledgebase" / "CBAC"
_DENSE_ROOTS = (_DEFAULT / "dense", _CBAC / "dense")
_DENSE = _DEFAULT / "dense"
_DENSE_PAPER = _DENSE            # 浓缩稿直接躺在 <分区>/dense/<id>.md
_DENSE_VIDEO = _ROOT / "knowledgebase" / "community" / "bilibili" / "dense"
_DENSE_DOC = _DENSE
# 正文（BM25 源）
_FULL = _DEFAULT
_FULL_PAPERS = _FULL / "papers"
_FULL_DOCS = _FULL / "docs"
_FULL_WEB = _FULL / "web"
# L3 原始/处理中间区（data/）
_BIN_PAPERS = _ROOT / "data" / "papers"      # 论文二进制资产（paper.pdf / images/）
_VIDEOS = _ROOT / "data" / "videos"          # 视频处理记录（subtitle/summary，summary 晋升 dense）
_KB_LEGACY = _ROOT / "data" / "_kb_legacy"   # 论文收敛时低级版本/冗余副本的备份根（不入 KB、不被索引）

# 论文完整版优先级：翻译+识图 > 仅识图 > 仅翻译 > 无（基底）。每篇论文在 KB 里只留最优的一个完整版。
_FULL_PRIORITY = {"cn_paper_ocr.md": 4, "paper_ocr.md": 3, "cn_paper.md": 2, "paper.md": 1}
_FULL_NAMES = set(_FULL_PRIORITY)

_ARXIV_ID_RE = re.compile(r"(\d{4}\.\d{4,5})(?:v\d+)?")


def _extract_arxiv_id(name: str) -> str | None:
    m = _ARXIV_ID_RE.search(name)
    return m.group(1) if m else None


def _yaml_str(v) -> str:
    s = str(v)
    if not s:
        return '""'
    if s != s.strip() or any(ch in s for ch in ':#"{}[],&*!|>\'%@`'):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


# ── 元数据来源 ──────────────────────────────────

def _paper_kbs() -> list[tuple[str, Path]]:
    """所有「论文库」(name, dir)，含迁移出去的项目库（如 1-3-VERA）；不含 docs/web/dense。

    以 registry.json 为准（与 rag-ask 的动态 --kb 一致），registry 不可用时退化为只认 papers。
    """
    out: list[tuple[str, Path]] = []
    try:
        from ragsearch.config import KNOWLEDGE_BASES
        for name, path in KNOWLEDGE_BASES.items():
            p = Path(path)
            # 论文库 = 默认库 full/papers 或调研分区子目录 full/{分区}/papers；
            # projects/doc/web/dense 等不算论文库
            if p.name == "papers":
                if p.is_dir() and all(n != name for n, _ in out):
                    out.append((name, p))
    except Exception:  # noqa: BLE001
        pass
    if not any(n == "papers" for n, _ in out):
        out.insert(0, ("papers", _FULL_PAPERS))
    return out


def _paper_meta_lookup() -> dict[str, dict]:
    """从 tasks.json survey 结果 + surveys JSON 快照建 arxiv_id(去版本) → meta 查找。

    surveys/*.json 文件名含时间戳，按倒序（新优先）加载；只补缺失字段。
    """
    lookup: dict[str, dict] = {}
    tasks_file = _ROOT / ".data" / "tasks.json"
    if tasks_file.exists():
        try:
            tasks = json.loads(tasks_file.read_text(encoding="utf-8"))
        except Exception:
            tasks = {}
        for t in tasks.values():
            if t.get("type") != "survey":
                continue
            for p in (t.get("result") or {}).get("papers", []):
                aid = _extract_arxiv_id(p.get("arxiv_id", ""))
                if aid and aid not in lookup:
                    lookup[aid] = {
                        "title": p.get("title", ""),
                        "authors": p.get("authors", []),
                        "year": p.get("year"),
                        "source": f"https://arxiv.org/abs/{p.get('arxiv_id', '')}",
                        "score": p.get("score"),
                        "citations": p.get("citations"),
                        "ccf_rank": p.get("ccf_rank"),
                        "primary_category": p.get("primary_category"),
                        "is_accepted": p.get("is_accepted"),
                        "has_code_or_data": p.get("has_code_or_data"),
                        "abstract": p.get("abstract", ""),
                        "arxiv_id": p.get("arxiv_id", ""),
                    }
    surveys_dir = _ROOT / "data" / "arxiv" / "queries" / "surveys"
    if surveys_dir.is_dir():
        for f in sorted(surveys_dir.glob("*.json"), reverse=True):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            for p in data.get("papers", []):
                aid = _extract_arxiv_id(p.get("arxiv_id", ""))
                if not aid:
                    continue
                entry = lookup.setdefault(aid, {
                    "title": "", "authors": [], "year": None, "source": "",
                    "score": None, "citations": None, "ccf_rank": "",
                    "primary_category": "", "is_accepted": None,
                    "has_code_or_data": None, "abstract": "", "arxiv_id": p.get("arxiv_id", ""),
                })
                if not entry["title"]:
                    entry["title"] = p.get("title", "")
                if not entry["authors"]:
                    entry["authors"] = p.get("authors", [])
                if not entry["year"]:
                    entry["year"] = p.get("publication_year") or p.get("year")
                if not entry["source"]:
                    entry["source"] = p.get("entry_url", "")
                if not entry["primary_category"]:
                    entry["primary_category"] = p.get("primary_category", "")
                if not entry["citations"]:
                    entry["citations"] = p.get("cited_by_count")
                if not entry["ccf_rank"]:
                    entry["ccf_rank"] = p.get("ccf_level", "")
    return lookup


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _frontmatter_entries(meta: dict | None, arxiv_id: str | None = None) -> dict:
    """把 meta 映射为 frontmatter 字段字典（只含非空值）。tags 输出 `[a, b]` 形式。"""
    meta = meta or {}
    out: dict = {}
    if meta.get("id"):
        out["id"] = meta["id"]
    if meta.get("title"):
        out["title"] = meta["title"]
    source = meta.get("source") or (f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else "")
    if source:
        out["source"] = source
    authors = meta.get("authors") or ""
    if authors:
        out["authors"] = ", ".join(authors) if isinstance(authors, list) else authors
    if meta.get("year"):
        out["year"] = meta["year"]
    if meta.get("section"):
        out["section"] = meta["section"]
    tags = meta.get("tags") or []
    if tags:
        out["tags"] = "[%s]" % ", ".join(str(t) for t in tags)
    if meta.get("source_type"):
        out["source_type"] = meta["source_type"]
    return out


def _render_frontmatter(entries: dict) -> str:
    lines = ["---"]
    for k, v in entries.items():
        lines.append(f"{k}: {_yaml_str(v)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def _frontmatter(meta: dict | None, title_fallback: str, arxiv_id: str | None = None) -> str:
    meta = meta or {}
    entries = _frontmatter_entries(meta, arxiv_id)
    entries.setdefault("title", title_fallback)
    return _render_frontmatter(entries)


def _fallback_title(text: str, filename: str = "") -> str:
    """从正文取标题：首个 '# ' 行（或首行），去 scraped junk（行内 import/代码片段），过长截断。"""
    first = text.split("\n", 1)[0].lstrip("# ").strip() or filename
    for marker in (" import ", " ### ", " ## ", " from '", ' from "', " ```", " {Callout", " <Card", " {Card"):
        idx = first.find(marker)
        if idx > 0:
            first = first[:idx]
            break
    first = first.strip()
    if len(first) > 200:
        first = first[:200].rstrip() + "…"
    return first or filename


def _ensure_frontmatter(md_path: Path, meta: dict | None, arxiv_id: str | None) -> None:
    # 与 _merge_frontmatter 同语义（补缺失字段，保留已有），供 archive 各归档函数复用
    _merge_frontmatter(md_path, meta, arxiv_id)


def _set_frontmatter_title(md_path: Path, title: str) -> bool:
    """覆盖 frontmatter 的 title（用于补齐时用权威标题修正）。值变化才算改动（幂等）。"""
    text = md_path.read_text(encoding="utf-8", errors="replace")
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return False
    out = []
    for line in m.group(1).splitlines():
        if line.startswith("title:"):
            out.append(f"title: {_yaml_str(title)}")
        else:
            out.append(line)
    new_fm = "---\n" + "\n".join(out) + "\n---\n"
    if new_fm == text[:m.end()]:
        return False
    md_path.write_text(new_fm + text[m.end():], encoding="utf-8")
    return True


def _merge_frontmatter(md_path: Path, meta: dict | None, arxiv_id: str | None = None) -> bool:
    """补 frontmatter：无 frontmatter 则整体生成；已有则只补缺失字段（保留已有）。返回是否改动。

    幂等；对 scraped 单行 md（如 newapi）会以正文标题覆盖 junk title。
    """
    text = md_path.read_text(encoding="utf-8", errors="replace")
    m = _FRONTMATTER_RE.match(text)
    body = text[m.end():] if m else text
    clean_title = _fallback_title(body, md_path.name)
    if not m:
        fm = _frontmatter(meta, clean_title, arxiv_id)
        md_path.write_text(fm + text, encoding="utf-8")
        return True
    # 已有 frontmatter：解析现有字段，只补缺失
    raw: dict = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            raw[k.strip()] = v.strip().strip("\"'")
    wanted = _frontmatter_entries(meta, arxiv_id)
    changed = False
    for k, v in wanted.items():
        if k not in raw or not raw[k]:
            raw[k] = v
            changed = True
    # title 为 junk（含 import / 超长）时用正文标题覆盖（值变化才算改动）
    cur = raw.get("title", "")
    if cur and (len(cur) > 200 or " import " in cur) and clean_title != cur:
        raw["title"] = clean_title
        changed = True
    if not changed:
        return False
    md_path.write_text(_render_frontmatter(raw) + body, encoding="utf-8")
    return True


# ── 论文 section/tags 派生 ──────────────────────

_CATEGORY_SECTION = {
    "cs.CR": "Security & Cryptography",
    "cs.SE": "Software Engineering",
    "cs.DC": "Distributed Computing",
    "cs.CY": "Computers & Society",
    "cs.NI": "Networking",
    "math.NA": "Numerical Analysis",
}


def _build_section_and_tags(meta: dict | None) -> tuple[str, list]:
    """从论文元数据派生 section（大类）与 tags。

    section：meta.section 优先，否则 primary_category 映射为可读大类，再否则 "papers"。
    tags：meta.tags（可含浓缩关键词）优先，确保包含 primary_category。
    """
    meta = meta or {}
    pc = (meta.get("primary_category") or "").strip()
    section = (meta.get("section") or "").strip()
    if not section:
        section = _CATEGORY_SECTION.get(pc, pc or "papers")
    tags = list(meta.get("tags") or [])
    if pc and pc not in tags:
        tags.insert(0, pc)
    return section, tags


def _extract_keywords_from_text(text: str) -> list:
    """从论文 md 正文提取关键词（"关键词:" 行，分隔符 , 、 ;）。防御式，找不到返回 []。"""
    for line in text.splitlines():
        if "关键词" in line and ":" in line:
            _, _, v = line.partition(":")
            return [k.strip() for k in re.split(r"[,、;；]", v) if k.strip()]
    return []


def _ensure_metadata_paper(out_dir: Path, arxiv_id: str, meta: dict | None) -> None:
    """刷新论文 metadata.json：增量合并（保留所有已有字段，不丢 source/is_oa/eprint_id 等）。

    每次调用重算 artifacts/status/rag_ready；已有字段全部保留，只叠加/覆盖计算字段与
    新传入的 meta 字段（防 fill_paper_meta 等重写时丢数据）。
    """
    meta_path = out_dir / "metadata.json"
    existing: dict = {}
    if meta_path.exists():
        try:
            existing = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
    meta = meta or {}
    # 增量：先完整保留已有字段（含 source/is_oa/eprint_id 等非白名单字段）
    record = dict(existing)
    # 覆盖计算字段（artifacts/status 等）
    artifacts, rag_ready = {}, []
    for name, key in [("paper.pdf", "pdf"), ("paper.md", "md"), ("paper_ocr.md", "ocr"),
                      ("cn_paper.md", "translate"), ("cn_paper_ocr.md", "translate_ocr"),
                      ("condensation.md", "condensation")]:
        if (out_dir / name).exists():
            artifacts[key] = name
    for name in (*_FULL_NAMES, "condensation.md"):
        if (out_dir / name).exists():
            rag_ready.append(name)
    record["type"] = "paper"
    record["id"] = arxiv_id
    record["title"] = meta.get("title") or existing.get("title") or arxiv_id
    record["frontmatter"] = {k: meta.get(k) for k in ("title", "source", "authors", "year") if meta.get(k)}
    record["created_at"] = existing.get("created_at") or datetime.now().isoformat(timespec="seconds")
    record["source_tasks"] = existing.get("source_tasks", [])
    record["artifacts"] = artifacts
    record["rag_ready"] = rag_ready
    record["status"] = compute_paper_status(out_dir)
    # 叠加新传入的 meta 字段（白名单 + 保留已有所有字段；不删除任何已有键）
    for k in ("citations", "venue", "venue_type", "ccf_rank", "primary_category",
              "abstract", "doi", "publication_year", "section", "tags",
              "provenance", "manual_fields", "conflicts",
              "source", "is_oa", "eprint_id"):
        if meta.get(k) is not None:
            record[k] = meta[k]
    meta_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 状态检测 / tar 解压 ─────────────────────────

def compute_paper_status(paper_dir: Path | str) -> dict:
    """返回论文各处理阶段是否完成。完整版 = 任一优先级版本存在。

    pdf 检查 data/papers/{id}/（二进制区）；md/ocr/translate/condense 检查 full/papers/{id}/。
    """
    d = Path(paper_dir)
    bin_dir = _BIN_PAPERS / d.name
    has = lambda name: (d / name).exists()  # noqa: E731
    return {
        "pdf": (bin_dir / "paper.pdf").exists(),
        "md": any(has(n) for n in _FULL_NAMES),
        "ocr": has("paper_ocr.md") or has("cn_paper_ocr.md"),
        "translate": has("cn_paper.md") or has("cn_paper_ocr.md"),
        "condense": has("condensation.md") or (_DENSE_PAPER / f"{d.name}.md").exists(),
    }


def extract_tar_into_paper(tar_path: Path, paper_dir: Path) -> int:
    """把 convert 的图片 tar 解压进论文目录 images/，返回解压文件数。"""
    import tarfile
    paper_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    try:
        with tarfile.open(tar_path) as tf:
            for m in tf.getmembers():
                if m.isfile() and ("images/" in m.name
                                   or m.name.endswith((".jpg", ".png", ".jpeg", ".webp"))):
                    tf.extract(m, paper_dir)
                    n += 1
    except Exception:
        pass
    return n


def list_papers() -> list[dict]:
    """列所有已归档论文（id/title/指标/status/missing）。"""
    meta_lookup = _paper_meta_lookup()
    items = []
    all_dirs = [d for _n, kb in _paper_kbs() for d in kb.iterdir()
                if d.is_dir() and not d.name.startswith("_")]
    for d in sorted(all_dirs, key=lambda x: x.name):
        meta = {}
        mp = d / "metadata.json"
        if mp.exists():
            try:
                meta = json.loads(mp.read_text(encoding="utf-8"))
            except Exception:
                pass
        info = meta_lookup.get(d.name, {})
        status = compute_paper_status(d)
        missing = [s for s in ("ocr", "translate", "condense") if not status.get(s)]
        items.append({
            "id": d.name,
            "title": meta.get("title") or info.get("title") or d.name,
            "score": info.get("score"),
            "citations": info.get("citations"),
            "ccf_rank": info.get("ccf_rank"),
            "primary_category": info.get("primary_category"),
            "year": info.get("year"),
            "is_accepted": info.get("is_accepted"),
            "has_code_or_data": info.get("has_code_or_data"),
            "abstract": (info.get("abstract") or meta.get("abstract") or "")[:200],
            "status": status,
            "missing": missing,
            "rag_ready": meta.get("rag_ready", []),
            "section": meta.get("section", ""),
            "tags": meta.get("tags", []),
            "venue": meta.get("venue", ""),
            "doi": meta.get("doi", ""),
            "provenance": meta.get("provenance", {}),
            "dir": str(d),
        })
    return items


def list_videos() -> list[dict]:
    if not _VIDEOS.is_dir():
        return []
    items = []
    for d in sorted(_VIDEOS.iterdir()):
        if not d.is_dir() or d.name.startswith("_"):
            continue
        meta = {}
        mp = d / "metadata.json"
        if mp.exists():
            try:
                meta = json.loads(mp.read_text(encoding="utf-8"))
            except Exception:
                pass
        items.append({
            "id": d.name,
            "title": meta.get("title") or d.name,
            "has_subtitle": (d / "subtitle.json").exists(),
            "has_summary": (d / "summary.md").exists(),
            "dir": str(d),
        })
    return items


# ── 论文 md / pdf ───────────────────────────────

def _classify_md(stem: str) -> tuple[str, str | None]:
    """返回 (kind, arxiv_id)。kind: paper/translate/ocr/translate_ocr/condensation/doc。"""
    lower = stem.lower()
    if lower == "condensation":
        return "condensation", None
    if lower.startswith("cn_") and lower.endswith("_ocr"):
        return "translate_ocr", _extract_arxiv_id(stem)
    if lower.startswith("cn_"):
        return "translate", _extract_arxiv_id(stem)
    if lower.endswith("_ocr"):
        return "ocr", _extract_arxiv_id(stem)
    # `{论文id}--{标题}` 命名（paper-search 下载按论文 id 命名，含 DOI-slug/arxiv 带版本）→ 完整 id 优先
    if "--" in lower:
        pid = lower.split("--", 1)[0].strip()
        if pid:
            return "paper", pid
    aid = _extract_arxiv_id(stem)
    if aid:
        return "paper", aid
    return "doc", None


def archive_md_file(md_path: str, meta_lookup: dict | None = None) -> dict:
    md = Path(md_path)
    stem = md.stem
    kind, aid = _classify_md(stem)
    meta_lookup = meta_lookup if meta_lookup is not None else _paper_meta_lookup()
    meta = meta_lookup.get(aid) if aid else None

    if kind == "doc" or not aid:
        out_dir = _FULL_DOCS / stem[:40]
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / f"{stem}.md"
        if not dest.exists():
            shutil.copy2(str(md), dest)
        return {"ok": True, "kind": "doc", "dest": str(dest)}

    out_dir = _FULL_PAPERS / aid
    out_dir.mkdir(parents=True, exist_ok=True)
    file_map = {"paper": "paper.md", "translate": "cn_paper.md",
                "ocr": "paper_ocr.md", "translate_ocr": "cn_paper_ocr.md",
                "condensation": "condensation.md"}
    dest = out_dir / file_map[kind]
    if not dest.exists():
        shutil.copy2(str(md), dest)
    _ensure_frontmatter(dest, meta, aid)
    _ensure_metadata_paper(out_dir, aid, meta)
    return {"ok": True, "kind": kind, "arxiv_id": aid, "dest": str(dest)}


def finalize_paper_dir(paper_dir: Path | str) -> dict:
    """收敛论文目录为「1 个最优完整版 + 浓缩版」。

    完整版优先级：cn_paper_ocr > paper_ocr > cn_paper > paper。
    低级完整版与非规范 md（如源副本 2210.04644v1_*.md）移到 data/_kb_legacy/{id}/ 备份
    （不入 KB、不被索引），避免歧义与重复。condensation.md 永不移动。

    返回 {"id", "kept", "moved"}。
    """
    d = Path(paper_dir)
    if not d.is_dir():
        return {"id": d.name, "kept": None, "moved": []}
    present = [n for n in _FULL_NAMES if (d / n).exists()]
    kept = max(present, key=lambda n: _FULL_PRIORITY[n]) if present else None
    backup = _KB_LEGACY / d.name
    moved: list[str] = []
    for md in d.glob("*.md"):
        name = md.name
        if name == "condensation.md" or name == kept:
            continue
        backup.mkdir(parents=True, exist_ok=True)
        dst = backup / name
        if not dst.exists():
            shutil.move(str(md), str(dst))
        else:
            md.unlink()
        moved.append(name)
    return {"id": d.name, "kept": kept, "moved": moved}


def finalize_papers() -> list[dict]:
    """对 knowledgebase/full/papers 全部论文目录做收敛（一次性清理 / archive 收尾复用）。"""
    results = []
    all_dirs = [d for _n, kb in _paper_kbs() for d in kb.iterdir() if d.is_dir()]
    for d in sorted(all_dirs, key=lambda x: x.name):
        r = finalize_paper_dir(d)
        if r["moved"]:
            results.append(r)
    return results


# ── dense 晋升（L1 高浓度知识库）──────────────

def promote_to_dense(kind: str, obj_id: str, content: str, meta: dict | None = None) -> Path | None:
    """把高浓度内容晋升到 knowledgebase/dense/{kind}/{obj_id}.md。

    kind: paper（浓缩）/ video（摘要）/ doc（人工精选笔记）。
    写入完整清洗 frontmatter（含 source_type），幂等（覆盖更新，不累积版本）。
    返回目标路径；kind 非法返回 None。
    """
    if kind == "paper":
        # 浓缩稿跟着论文走：论文在哪个分区，就进哪个分区的 dense/
        target = next(
            (root for root in _DENSE_ROOTS if (root.parent / "papers" / obj_id).is_dir()),
            _DENSE_PAPER,
        )
    else:
        target = {"video": _DENSE_VIDEO, "doc": _DENSE_DOC}.get(kind)
    if not target:
        return None
    meta = dict(meta or {})
    meta["source_type"] = meta.get("source_type") or {"paper": "paper", "video": "video", "doc": "docs"}[kind]
    # id 让 doc_id = 对象 id（arxiv_id/bvid/slug），避免被子目录名（paper/video/doc）误导分组
    meta["id"] = obj_id
    target.mkdir(parents=True, exist_ok=True)
    out = target / f"{obj_id}.md"
    # 正文去已有 frontmatter
    m = _FRONTMATTER_RE.match(content)
    body = (content[m.end():] if m else content).lstrip("\n")
    fm = _frontmatter(meta, obj_id, obj_id if kind == "paper" else None)
    out.write_text(fm + body, encoding="utf-8")
    return out


def promote_curated_doc(md_path: str | Path, obj_id: str | None = None) -> Path | None:
    """人工精选文档 → dense/doc/{obj_id}.md（用户判断浓度/来源重要性后调用）。

    obj_id 缺省用文件名 stem。返回目标路径。
    """
    md = Path(md_path)
    text = md.read_text(encoding="utf-8", errors="replace")
    m = _FRONTMATTER_RE.match(text)
    meta = {}
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                meta[k.strip()] = v.strip().strip("\"'")
    oid = obj_id or md.stem
    return promote_to_dense("doc", oid, text, dict(meta, source_type="docs"))


# ── 元数据补齐（arxiv_id/DOI → frontmatter + metadata.json）──────

def fill_paper_meta(arxiv_id: str, doi: str | None = None) -> dict:
    """补齐论文缺失元信息（确定性清洗引擎）。

    解析（arxiv_id/DOI/OpenAlex）→ 字段级信任合并（填空+低→高升级）→
    写 frontmatter + 同步 metadata.json（含 provenance/manual_fields/conflicts）。

    返回 {"ok", "id", "filled", "changed", "title_fixed", "provenance", "conflicts", "error"?}。
    """
    from backend.services.arxiv.meta_clean import clean_paper
    from backend.services.arxiv.meta_resolver import resolve_by_arxiv, resolve_by_doi
    from ragsearch.engine.document import parse_frontmatter

    full_dir = _FULL_PAPERS / arxiv_id
    if not full_dir.is_dir():
        return {"ok": False, "id": arxiv_id, "error": "论文目录不存在"}

    # 已有 frontmatter → existing（作为 T1 保留）
    existing: dict = {}
    pmd = full_dir / "paper.md"
    if pmd.exists():
        fm, _ = parse_frontmatter(pmd.read_text(encoding="utf-8", errors="replace"))
        existing = {k: fm.get(k) for k in ("title", "source", "authors", "year", "section", "tags")
                    if fm.get(k)}
    # 读已有 metadata.json（可能含 paper-search 建的完整元数据 / 上次清洗的 manual_fields）
    existing_meta: dict = {}
    manual_fields: list = []
    mpath = full_dir / "metadata.json"
    if mpath.exists():
        try:
            existing_meta = json.loads(mpath.read_text(encoding="utf-8"))
            manual_fields = existing_meta.get("manual_fields", [])
        except Exception:
            pass

    # B: 目录名不是合法 arxiv_id/DOI-slug（如中文标题目录）时，不要拿它当 id 去解析，
    #    否则会污染 sources（垃圾 arxiv_id）并可能覆盖 title。
    _is_arxiv = bool(_ARXIV_ID_RE.match(arxiv_id or ""))
    _is_doi = bool(re.match(r"10\.\S+", arxiv_id or ""))
    _valid_id = _is_arxiv or _is_doi
    if not _valid_id:
        # 已有 title → 直接保留，不解析（幂等返回）；无 title → 明确报错，不强行改写
        if existing_meta.get("title") or existing.get("title"):
            return {"ok": True, "id": arxiv_id, "filled": {},
                    "changed": False, "title_fixed": False,
                    "note": "目录名非 arxiv_id/DOI，已有 title，跳过解析"}
        return {"ok": False, "id": arxiv_id,
                "error": f"目录名非 arxiv_id/DOI 且无已有 title，跳过（{arxiv_id[:40]}）"}

    # 构建来源（字段级）：已有 title → 复用（DOI-slug 条目 / paper-search 建的，不重复解析）
    _META_FIELDS = ("title", "authors", "year", "doi", "arxiv_id", "venue", "venue_type",
                    "citations", "is_oa", "abstract", "primary_category")
    sources: dict[str, dict] = {}
    if existing_meta.get("title"):
        sources["search"] = {k: existing_meta[k] for k in _META_FIELDS if existing_meta.get(k)}
    else:
        if _is_arxiv:
            am = resolve_by_arxiv(arxiv_id)
            sources["arxiv"] = {k: am.get(k) for k in
                                ("title", "authors", "year", "abstract", "primary_category", "doi", "arxiv_id")
                                if am.get(k)}
            sources["openalex"] = {k: am.get(k) for k in
                                   ("citations", "venue", "venue_type", "ccf_rank", "is_oa")
                                   if am.get(k)}
        if doi:
            dm = resolve_by_doi(doi)
            sources["doi"] = {k: dm.get(k) for k in
                              ("title", "authors", "year", "doi", "arxiv_id", "citations",
                               "venue", "venue_type", "abstract")
                              if dm.get(k)}

    if not sources:
        return {"ok": False, "id": arxiv_id, "error": "无法解析元数据"}

    rec = clean_paper(sources, existing=existing, manual_fields=manual_fields, source_type="paper")
    if not rec.title:
        return {"ok": False, "id": arxiv_id, "error": "无法解析元数据（title 为空）"}

    # B: 已有权威 title 且本次解析没给出更好 title 时，不要覆盖（防垃圾 title 污染）
    if existing.get("title") and not rec.title.strip():
        return {"ok": True, "id": arxiv_id, "changed": False,
                "note": "已有 title，解析无新值，跳过"}

    # 写 frontmatter（补齐缺失 + 权威标题覆盖）
    fm_meta = rec.frontmatter_entries()
    changed = False
    for md in full_dir.glob("*.md"):
        if _merge_frontmatter(md, fm_meta, arxiv_id):
            changed = True
        if _set_frontmatter_title(md, rec.title):
            changed = True
    # 同步 metadata.json（含 provenance/manual_fields/conflicts）
    meta_out = rec.to_dict()
    _ensure_metadata_paper(full_dir, arxiv_id, meta_out)

    filled = {k: meta_out[k] for k in ("title", "authors", "year", "citations", "venue",
                                       "ccf_rank", "primary_category", "doi")
              if meta_out.get(k)}
    return {"ok": True, "id": arxiv_id, "filled": filled, "changed": changed,
            "title_fixed": True, "provenance": rec.provenance, "conflicts": rec.conflicts}


def _doi_slug(doi: str) -> str:
    """DOI → 目录安全名（用于无 arxiv_id 的纯 DOI 论文）。"""
    s = doi.strip().lower()
    for pfx in ("https://doi.org/", "doi:"):
        if s.startswith(pfx):
            s = s[len(pfx):]
    return re.sub(r"[^0-9a-z.\-]", "_", s)


def _eprint_slug(eprint_id: str) -> str:
    """ePrint id（YYYY/NNNN）→ 目录安全名（YYYY-NNNN）。"""
    return re.sub(r"[^0-9A-Za-z.\-]", "-", eprint_id.strip())


def add_paper_by_meta(arxiv_id: str | None = None, doi: str | None = None) -> dict:
    """从 DOI 或 arxiv_id 创建论文 KB 条目（元数据入库，无需文件）。

    解析元数据 → 清洗引擎 → 建 full/papers/{id}/ 目录 + metadata.json（含 provenance）。
    不写占位 md（避免挡后续导入/转换的 paper.md）。论文目录已存在则只补元数据。
    """
    from backend.services.arxiv.meta_clean import clean_paper
    from backend.services.arxiv.meta_resolver import resolve_by_arxiv, resolve_by_doi

    sources: dict[str, dict] = {}
    if arxiv_id:
        am = resolve_by_arxiv(arxiv_id)
        sources["arxiv"] = {k: am.get(k) for k in
                            ("title", "authors", "year", "abstract", "primary_category", "doi", "arxiv_id")
                            if am.get(k)}
        sources["openalex"] = {k: am.get(k) for k in
                               ("citations", "venue", "venue_type", "ccf_rank", "is_oa")
                               if am.get(k)}
    if doi:
        dm = resolve_by_doi(doi)
        sources["doi"] = {k: dm.get(k) for k in
                          ("title", "authors", "year", "doi", "arxiv_id", "citations",
                           "venue", "venue_type", "abstract")
                          if dm.get(k)}
    if not sources:
        return {"ok": False, "error": f"无法解析元数据（{arxiv_id or doi}）"}

    rec = clean_paper(sources, source_type="paper")
    if not rec.title:
        return {"ok": False, "error": f"无法解析元数据（{arxiv_id or doi}）"}
    paper_id = rec.arxiv_id or _doi_slug(rec.doi)
    if not paper_id:
        return {"ok": False, "error": "无法确定论文 id"}
    full_dir = _FULL_PAPERS / paper_id
    full_dir.mkdir(parents=True, exist_ok=True)
    _ensure_metadata_paper(full_dir, paper_id, rec.to_dict())
    return {"ok": True, "id": paper_id, "dir": str(full_dir),
            "meta": {k: getattr(rec, k) for k in ("title", "authors", "year", "citations", "doi")
                     if getattr(rec, k)},
            "provenance": rec.provenance}


def add_paper_from_cleaned(rec) -> dict:
    """把已清洗的 meta_clean.PaperRecord 写入 KB（目录 + metadata.json）。

    搜索结果无 md → 只建元数据条目（不写占位 md，不挡后续下载/转换补 paper.md）。
    重复条目（同 arxiv_id/DOI 已存在）幂等：只刷新 metadata.json。
    """
    paper_id = rec.arxiv_id or _doi_slug(rec.doi)
    if not paper_id and getattr(rec, "eprint_id", ""):
        paper_id = _eprint_slug(rec.eprint_id)
    if not paper_id:
        return {"ok": False, "error": "无法确定论文 id"}
    full_dir = _FULL_PAPERS / paper_id
    full_dir.mkdir(parents=True, exist_ok=True)
    _ensure_metadata_paper(full_dir, paper_id, rec.to_dict())
    return {"ok": True, "id": paper_id, "dir": str(full_dir),
            "meta": {k: getattr(rec, k) for k in ("title", "authors", "year", "citations", "doi")
                     if getattr(rec, k)},
            "provenance": rec.provenance}


def archive_pdf(pdf_path: str, meta_lookup: dict | None = None) -> dict:
    pdf = Path(pdf_path)
    stem = pdf.stem

    # paper-search 下载命名：`{entry_id}--{标题}.pdf`（entry_id 可能是 DOI-slug
    # 如 10.1145_3579845，也可能是 arxiv_id）。完整 entry_id 优先于 arxiv 正则。
    aid = None
    if "--" in stem:
        prefix = stem.split("--", 1)[0].strip()
        if prefix:
            m = _ARXIV_ID_RE.match(prefix)          # arxiv 前缀（含 vN）
            if m:
                aid = m.group(1)
            elif re.match(r"10\.\S+$", prefix):     # DOI-slug 前缀（如 10.1109_xxx）
                aid = prefix
            elif re.match(r"\d{4}-?\d+$", prefix):  # ePrint slug 前缀（2023-708）
                aid = prefix
    if not aid and not re.match(r"10\.", stem):
        # 非 DOI 文件名才回退 arxiv 正则（避免把 DOI 里的 "2019.29058" 误认成 arxiv_id）
        aid = _extract_arxiv_id(stem)
    if not aid:
        return {"ok": False, "file": str(pdf), "error": "无法解析论文 id（无 arxiv_id 且无已入库的 entry_id 前缀）"}
    meta_lookup = meta_lookup if meta_lookup is not None else _paper_meta_lookup()
    bin_dir = _BIN_PAPERS / aid
    bin_dir.mkdir(parents=True, exist_ok=True)
    dest = bin_dir / "paper.pdf"
    if not dest.exists():
        shutil.copy2(str(pdf), dest)
    # metadata.json 对象记录放 full/papers/{id}/（与 RAG md 同处）
    _ensure_metadata_paper(_FULL_PAPERS / aid, aid, meta_lookup.get(aid))
    return {"ok": True, "arxiv_id": aid, "dest": str(dest)}


# ── B站 ────────────────────────────────────────

def _bili_videos_from_db() -> list[tuple]:
    db = _ROOT / "data" / "bili.db"
    if not db.exists():
        return []
    try:
        conn = sqlite3.connect(str(db))
        rows = conn.execute(
            "SELECT bvid, title, author, summary_text FROM videos").fetchall()
        conn.close()
        return rows
    except Exception:
        return []


def _write_video_metadata(out: Path, bvid: str, title: str = "", author: str = "") -> None:
    """刷新视频 metadata.json：每次重算 artifacts/rag_ready（保留 created_at）。"""
    meta_path = out / "metadata.json"
    existing = None
    if meta_path.exists():
        try:
            existing = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            existing = None
    artifacts, rag_ready = {}, []
    for name, key in [("subtitle.json", "subtitle"), ("subtitle.txt", "subtitle_txt"),
                      ("summary.md", "summary")]:
        if (out / name).exists():
            artifacts[key] = name
    if (out / "summary.md").exists():
        rag_ready.append("summary.md")
    record = {
        "type": "video", "id": bvid, "title": title or (existing or {}).get("title") or bvid,
        "frontmatter": {"title": title or (existing or {}).get("title") or bvid,
                        "authors": author or ""},
        "created_at": (existing or {}).get("created_at") or datetime.now().isoformat(timespec="seconds"),
        "source_tasks": (existing or {}).get("source_tasks", []),
        "artifacts": artifacts, "rag_ready": rag_ready,
    }
    meta_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


def archive_bili() -> dict:
    moved = 0
    sub_dir = _ROOT / "data" / "bili_subtitles"
    if sub_dir.is_dir():
        for f in sub_dir.glob("*.json"):
            bvid = f.stem
            out = _VIDEOS / bvid
            out.mkdir(parents=True, exist_ok=True)
            if not (out / "subtitle.json").exists():
                shutil.copy2(str(f), out / "subtitle.json")
            if not (out / "subtitle.txt").exists():
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    text = data.get("text", "")
                    if text:
                        (out / "subtitle.txt").write_text(text, encoding="utf-8")
                except Exception:
                    pass
            _write_video_metadata(out, bvid)
            moved += 1
    # summary 从 DB 落盘 + 晋升 dense/video（L1）
    for bvid, title, author, summary in _bili_videos_from_db():
        if not summary:
            continue
        out = _VIDEOS / bvid
        out.mkdir(parents=True, exist_ok=True)
        smd = out / "summary.md"
        if not smd.exists():
            fm = _frontmatter({"title": title, "authors": author or ""}, title)
            smd.write_text(fm + "\n" + summary.strip() + "\n", encoding="utf-8")
        promote_to_dense("video", bvid, summary, {
            "title": title, "authors": author or "",
            "source": f"https://www.bilibili.com/video/{bvid}",
        })
        _write_video_metadata(out, bvid, title, author)
    return {"moved": moved}


# ── 全量归档 ───────────────────────────────────

def run_archive() -> dict:
    meta_lookup = _paper_meta_lookup()
    report: dict = {"papers": [], "docs": [], "pdfs": [], "videos": 0, "skipped": []}

    def _collect(d: Path) -> None:
        if not d.is_dir():
            return
        for md in d.glob("*.md"):
            r = archive_md_file(str(md), meta_lookup)
            (report["papers"] if r.get("kind") != "doc" else report["docs"]).append(r.get("dest", ""))
            # 解压同目录图片 tar 到论文目录
            if r.get("kind") != "doc" and r.get("arxiv_id"):
                tar = d / (md.stem + ".tar")
                if tar.exists():
                    n = extract_tar_into_paper(tar, _BIN_PAPERS / r["arxiv_id"])
                    if n:
                        report.setdefault("images_extracted", 0)
                        report["images_extracted"] += n

    for root in [(_ROOT / "data" / "remote_pdf"), (_ROOT / "output")]:
        if root.is_dir():
            for d in root.glob("*"):
                if d.is_dir() and d.name != "translate":
                    _collect(d)
    tdir = _ROOT / "output" / "translate"
    if tdir.is_dir():
        for d in tdir.glob("*"):
            _collect(d)

    for pdf_dir in [(_ROOT / "data" / "pdfs"), (_ROOT / "data" / "pdfs" / "手动tmp")]:
        if pdf_dir.is_dir():
            for pdf in pdf_dir.glob("*.pdf"):
                r = archive_pdf(str(pdf), meta_lookup)
                if r.get("ok"):
                    report["pdfs"].append(r["dest"])
                else:
                    report["skipped"].append(str(pdf))

    # 2026-11-04：bili 入库统一走 bili-visual-scan → visual/apply_kb
    # （写 dense/*.md + bili.db，一行一视频）。archive_bili 会把 bili.db.summary_text
    # 灌回 dense 并重建 videos/ 目录，和上面那条路抢同一份文件，故停用。
    report["videos"] = 0
    report["meta_papers"] = sum(len(list(kb.glob("*/metadata.json"))) for _n, kb in _paper_kbs())
    report["meta_videos"] = len(list(_VIDEOS.glob("*/metadata.json"))) if _VIDEOS.exists() else 0

    # 论文版本收敛：每篇只留 1 个最优完整版 + 浓缩版（低级版本/冗余副本移到 _kb_legacy）
    report["papers_finalized"] = finalize_papers()

    # 给有 metadata.json 的论文补 frontmatter（paper-search 建/自动关联的条目；本地清洗，不解析）
    meta_applied = 0
    all_dirs = [d for _n, kb in _paper_kbs() for d in kb.glob("*") if d.is_dir()]
    for d in all_dirs:
        mp = d / "metadata.json"
        if not mp.exists() or not (d / "paper.md").exists():
            continue
        try:
            m = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            continue
        if m.get("title"):
            r = fill_paper_meta(d.name)
            if r.get("ok"):
                meta_applied += 1
    report["meta_applied"] = meta_applied

    # 归档后重建受影响知识库索引（失败不阻塞归档；只重建已登记的 KB）
    try:
        from backend.services.ragsearch.service import rebuild_kb
        kb_names = ["docs", "dense"] + [n for n, _ in _paper_kbs()]
        report["rag_reindex"] = {kb: rebuild_kb(kb) for kb in kb_names}
    except Exception as e:  # noqa: BLE001
        report["rag_reindex_error"] = str(e)

    return report
