"""受控词表草案与 frontmatter tag 清洗。"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from backend.services.kb.catalog import scan_all
from backend.services.kb.ftx import SEARCHABLE_SQL
from backend.services.kb.db import META_DIR, PROJECT_ROOT, connect, now


VOCAB_DIR = META_DIR / "vocab"
_ARXIV_RE = re.compile(r"^[a-z]{2}\.[A-Z]{2}$")
_DEFAULT_ORG = {"实验室内", "硕博", "课题组", "硕论"}
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_TAG_LINE_RE = re.compile(r"^tags\s*:\s*(.*)$", re.IGNORECASE)
_FIELD_LINE_RE = re.compile(r"^(tags|org_marks|arxiv_tags)\s*:\s*(.*)$", re.IGNORECASE)


def _vocab_path(topic: str) -> Path:
    return VOCAB_DIR / f"{topic}.json"


def _load_vocab(topic: str, confirmed_only: bool = False) -> dict | None:
    path = _vocab_path(topic)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if confirmed_only and data.get("status") != "confirmed":
        return None
    return data


def _save_vocab(topic: str, data: dict) -> Path:
    VOCAB_DIR.mkdir(parents=True, exist_ok=True)
    path = _vocab_path(topic)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="")
    return path


def _version_path(path: str) -> Path:
    value = Path(path)
    return value if value.is_absolute() else PROJECT_ROOT / value


def _md_files(topic: str) -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            f"""
            SELECT DISTINCT d.doc_id, d.title, v.path
            FROM documents d JOIN versions v ON v.doc_id=d.doc_id
            WHERE d.topic=? AND d.status IN {SEARCHABLE_SQL} AND v.kind != 'meta' AND lower(v.path) LIKE '%.md'
            ORDER BY d.doc_id, v.path
            """,
            (topic,),
        ).fetchall()
        output = []
        for row in rows:
            path = _version_path(row["path"])
            if path.exists():
                output.append({"doc_id": row["doc_id"], "title": row["title"], "path": path})
        return output
    finally:
        conn.close()


def _read_frontmatter_lines(path: Path) -> tuple[list[str], str | None, str, bool]:
    raw = path.read_bytes()
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig", errors="replace")
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return [], None, text, has_bom
    return match.group(1).splitlines(), match, text, has_bom


def _split_tag_value(value: str) -> list[str]:
    value = value.strip()
    value = value.strip().strip("\"'")
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    if not value:
        return []
    return [item.strip().strip("\"'") for item in value.split(",") if item.strip()]


def _frontmatter_fields(path: Path) -> tuple[dict[str, list[str]], str | None, str]:
    """一次读出 tags / org_marks / arxiv_tags 三行。

    M3 修复：旧实现只看 tags 行，导致 ① 已存在的 org_marks 行被规划器忽略 →
    重复执行不幂等、文件与库漂移无法自愈；② 写回时因 org_marks 行已存在而跳过 →
    计划中的组织标记被静默丢弃（`v2语言论证` 25 条丢盘事件）。
    """
    lines, match, _text, _has_bom = _read_frontmatter_lines(path)
    if match is None:
        return {}, None, "no-frontmatter"
    fields: dict[str, list[str]] = {}
    style = None
    for line in lines:
        field_match = _FIELD_LINE_RE.match(line.strip())
        if not field_match:
            continue
        key = field_match.group(1).lower()
        if key == "tags" and "tags" not in fields:
            style = "quoted-list" if re.match(r"^tags\s*:\s*[\"']\s*\[", line, re.IGNORECASE) else "normal"
        fields.setdefault(key, _split_tag_value(field_match.group(2)))
    if "tags" not in fields:
        return fields, style, "tags-missing"
    return fields, style, "ok"


def _raw_tags(path: Path) -> tuple[list[str], str | None, str]:
    fields, style, status = _frontmatter_fields(path)
    return fields.get("tags", []), style, status


def _merge_unique(base: Iterable[str], extra: Iterable[str]) -> list[str]:
    """保留 base 顺序，追加 extra 中未出现过的值（org_marks/arxiv_tags 的并集语义）。"""
    merged = list(base)
    for value in extra:
        if value not in merged:
            merged.append(value)
    return merged


def _db_tags(topic: str) -> tuple[Counter, int, int]:
    conn = connect()
    try:
        rows = conn.execute(
            f"SELECT tags FROM documents WHERE topic=? AND status IN {SEARCHABLE_SQL}", (topic,)
        ).fetchall()
        counts: Counter = Counter()
        for row in rows:
            try:
                values = json.loads(row["tags"])
            except json.JSONDecodeError:
                continue
            counts.update(str(value) for value in values)
        doc_count = conn.execute(
            f"SELECT COUNT(*) FROM documents WHERE topic=? AND status IN {SEARCHABLE_SQL}", (topic,)
        ).fetchone()[0]
        md_count = conn.execute(
            f"""
            SELECT COUNT(DISTINCT d.doc_id) FROM documents d
            JOIN versions v ON v.doc_id=d.doc_id
            WHERE d.topic=? AND d.status IN {SEARCHABLE_SQL} AND v.kind!='meta'
              AND lower(v.path) LIKE '%.md'
            """,
            (topic,),
        ).fetchone()[0]
        return counts, doc_count, md_count
    finally:
        conn.close()


def stats(topic: str) -> dict:
    """现状统计：DB tags、源 md frontmatter、可清洗异常。"""
    db_counts, doc_count, md_doc_count = _db_tags(topic)
    files = _md_files(topic)
    raw_counts: Counter = Counter()
    issues: list[dict] = []
    styles: Counter = Counter()
    for item in files:
        values, style, status = _raw_tags(item["path"])
        if style:
            styles[style] += 1
        raw_counts.update(values)
        if status != "ok":
            issues.append({"doc_id": item["doc_id"], "path": item["path"].as_posix(), "issue": status})
            continue
        for value in values:
            issue = None
            if _ARXIV_RE.match(value):
                issue = "arxiv-code"
            elif value in _DEFAULT_ORG:
                issue = "org-mark"
            elif value in {"论文题目", "待OCR重跑"}:
                issue = "suspect-process"
            if style == "quoted-list":
                issue = issue or "quoted-list-format"
            if issue:
                issues.append({
                    "doc_id": item["doc_id"], "path": item["path"].as_posix(),
                    "tag": value, "issue": issue,
                })
    return {
        "topic": topic,
        "documents": doc_count,
        "md_documents": md_doc_count,
        "md_files_scanned": len(files),
        "db_distinct_tags": len(db_counts),
        "db_tag_frequency": [{"tag": tag, "count": count} for tag, count in db_counts.most_common()],
        "raw_distinct_tags": len(raw_counts),
        "frontmatter_styles": dict(styles),
        "issues": issues,
        "generated_at": now(),
    }


def _merge_suggestions(counts: Counter) -> list[dict]:
    """只从已有 tag 做大小写/复数/-based 合并建议，不引入新词。"""
    groups: dict[str, dict[str, set[str]]] = {}
    for raw, count in counts.items():
        key = raw
        if raw.endswith("s") and counts.get(raw[:-1], 0) > 0:
            key = raw[:-1]
        if raw.endswith("-based") and counts.get(raw[:-6], 0) > 0:
            key = key[:-6] if key.endswith("-based") else raw[:-6]
        group = groups.setdefault(key.lower(), {})
        group.setdefault(key, set()).add(raw)

    def _prefer(key: str, variants: set[str]) -> str:
        uppercase = [value for value in variants if value.isupper() and any(ch.isalpha() for ch in value)]
        if uppercase:
            return sorted(uppercase)[0]
        if key in variants:
            return key
        return sorted(
            variants,
            key=lambda value: (
                0 if value == value.lower() else 1,
                value,
            ),
        )[0]

    suggestions: list[dict] = []
    for variants in groups.values():
        all_variants: set[str] = set()
        for related in variants.values():
            all_variants.update(related)
        preferred = _prefer(key, all_variants)
        aliases = all_variants - {preferred}
        if aliases:
            suggestions.append({
                "canonical": preferred, "count": counts[preferred],
                "aliases": sorted(aliases),
            })
    return sorted(suggestions, key=lambda item: item["canonical"].lower())


def _confirmed_drop_unmapped(vocab: dict) -> bool:
    return vocab.get("status") == "confirmed"


def draft_vocab(topic: str, top_k: int = 60, min_count: int = 2, force: bool = False) -> dict:
    """从 DB/源 md 现有 tags 生成候选；canonical 一律来自已有 tag。"""
    existing = _load_vocab(topic)
    if existing and existing.get("status") == "confirmed" and not force:
        return {"topic": topic, "status": "confirmed", "path": _vocab_path(topic).as_posix(),
                "message": "词表已确认；如需重建请使用 force 语义（当前实现由实施者显式编辑确认文件）"}
    db_counts, doc_count, md_count = _db_tags(topic)
    all_counts = db_counts.copy()
    for item in _md_files(topic):
        values, _, _ = _raw_tags(item["path"])
        all_counts.update(values)

    knowledge: list[dict] = []
    org: list[dict] = []
    process: list[dict] = []
    arxiv: list[dict] = []
    excluded: list[dict] = []
    for raw, count in all_counts.most_common():
        item = {"canonical": raw, "count": count}
        if raw in _DEFAULT_ORG:
            org.append(item)
        elif _ARXIV_RE.match(raw):
            arxiv.append(item)
        elif raw in {"论文题目", "待OCR重跑"}:
            process.append(item)
        elif count >= min_count and len(knowledge) < top_k:
            knowledge.append(item)
        else:
            excluded.append(item)

    aliases = _merge_suggestions(all_counts)
    data = {
        "schema": "kb-vocab-draft-v1",
        "topic": topic,
        "status": "draft",
        "generated_at": now(),
        "documents": doc_count,
        "md_documents": md_count,
        "selection": "pending",
        "facets": {
            "knowledge": knowledge,
            "org": org,
            "process": process,
            "arxiv": arxiv,
        },
        "merge_suggestions": aliases,
        "excluded_low_frequency": excluded,
    }
    path = _save_vocab(topic, data)
    return {
        "topic": topic, "status": "draft", "path": path.as_posix(),
        "knowledge_candidates": len(knowledge), "org_candidates": len(org),
        "arxiv_candidates": len(arxiv), "excluded_count": len(excluded),
        "merge_suggestions": aliases,
    }


def _mapping(vocab: dict) -> tuple[dict[str, str], set[str], set[str], set[str]]:
    alias_map: dict[str, str] = {}
    canonical: set[str] = set()
    org: set[str] = set()
    process: set[str] = set()
    for entry in vocab.get("facets", {}).get("knowledge", []):
        if entry.get("selected", False):
            canonical.add(entry["canonical"])
            alias_map[entry["canonical"]] = entry["canonical"]
            for alias in entry.get("aliases", []):
                alias_map[alias] = entry["canonical"]
    for entry in vocab.get("facets", {}).get("org", []):
        if entry.get("selected", True):
            org.add(entry["canonical"])
    for entry in vocab.get("facets", {}).get("process", []):
        if entry.get("selected", True):
            process.add(entry["canonical"])
    for entry in vocab.get("facets", {}).get("arxiv", []):
        if entry.get("selected", True):
            org.add(entry["canonical"])
    return alias_map, canonical, org, process


def canonicalize(topic: str, raw: str) -> str:
    vocab = _load_vocab(topic, confirmed_only=True)
    if not vocab:
        return raw
    alias_map, _, _, _ = _mapping(vocab)
    return alias_map.get(raw, raw)


def _build_clean_plan(topic: str) -> tuple[dict, list[dict]]:
    vocab = _load_vocab(topic, confirmed_only=True)
    if not vocab:
        raise RuntimeError(f"topic {topic} has no confirmed vocabulary")
    alias_map, canonical_values, org_values, process_values = _mapping(vocab)
    plans: list[dict] = []
    for item in _md_files(topic):
        fields, style, status = _frontmatter_fields(item["path"])
        if status != "ok":
            plans.append({
                "doc_id": item["doc_id"], "path": item["path"].as_posix(),
                "action": "skip", "reason": status, "before": [], "after": [],
                "org_marks": [], "arxiv_tags": [], "unmapped": [], "dropped": [],
            })
            continue
        values = fields.get("tags", [])
        existing_org = fields.get("org_marks", [])
        existing_arxiv = fields.get("arxiv_tags", [])
        after: list[str] = []
        org_from_tags: list[str] = []
        arxiv_from_tags: list[str] = []
        unmapped: list[str] = []
        dropped: list[str] = []
        for value in values:
            if value in org_values or value in _DEFAULT_ORG:
                if value not in org_from_tags:
                    org_from_tags.append(value)
            elif _ARXIV_RE.match(value):
                if value not in arxiv_from_tags:
                    arxiv_from_tags.append(value)
            elif value in process_values:
                pass
            elif value in alias_map:
                mapped = alias_map[value]
                if mapped not in after:
                    after.append(mapped)
            else:
                if _confirmed_drop_unmapped(vocab):
                    dropped.append(value)
                else:
                    unmapped.append(value)
        # 期望态 = 文件已有行 ∪ 从 tags 行新拆出的值（保证重复执行幂等、漂移可自愈）
        org_marks = _merge_unique(existing_org, org_from_tags)
        arxiv_tags = _merge_unique(existing_arxiv, arxiv_from_tags)
        changed = (
            after != values
            or org_marks != existing_org
            or arxiv_tags != existing_arxiv
            or style == "quoted-list"
        )
        plans.append({
            "doc_id": item["doc_id"], "path": item["path"].as_posix(),
            "action": "replace" if changed else "skip",
            "reason": "" if changed else "already-canonical",
            "before": values, "after": after,
            "org_marks": org_marks, "arxiv_tags": arxiv_tags,
            "unmapped": unmapped, "dropped": dropped,
        })
    return vocab, plans


def clean_dryrun(topic: str) -> dict:
    """默认 dry-run；unmapped 非空时表示词表尚未覆盖所有应保留 tag。"""
    vocab, plans = _build_clean_plan(topic)
    replace = sum(plan["action"] == "replace" for plan in plans)
    review = sum(plan["action"] == "review" for plan in plans)
    unmapped = sorted({value for plan in plans for value in plan["unmapped"]})
    dropped = sorted({value for plan in plans for value in plan["dropped"]})
    return {
        "topic": topic, "apply": False, "vocab_status": vocab["status"],
        "files": len(plans), "replace_plans": replace, "review_plans": review,
        "unmapped_tags": unmapped, "dropped_tags": dropped,
        "plans": plans, "generated_at": now(),
    }


def _render_tags_line(name: str, values: Iterable[str]) -> str:
    values = list(values)
    return f"{name}: [{', '.join(values)}]" if values else f"{name}: []"


def _replace_frontmatter(path: Path, after: list[str], org_marks: list[str], arxiv_tags: list[str]) -> None:
    lines, match, text, has_bom = _read_frontmatter_lines(path)
    if match is None:
        raise RuntimeError(f"frontmatter disappeared: {path}")
    original_tag_index = None
    existing_org = None
    existing_arxiv = None
    for index, line in enumerate(lines):
        lowered = line.lower()
        if lowered.startswith("tags:") and original_tag_index is None:
            original_tag_index = index
        elif lowered.startswith("org_marks:"):
            existing_org = index
        elif lowered.startswith("arxiv_tags:"):
            existing_arxiv = index
    if original_tag_index is None:
        raise RuntimeError(f"tags line not found: {path}")
    lines[original_tag_index] = _render_tags_line("tags", after)
    # M3 修复：org_marks / arxiv_tags 行已存在时必须覆盖写入（旧实现直接跳过 →
    # 计划中的组织标记被静默丢弃）。
    if org_marks:
        rendered_org = _render_tags_line("org_marks", org_marks)
        if existing_org is None:
            lines.append(rendered_org)
        else:
            lines[existing_org] = rendered_org
    if arxiv_tags:
        rendered_arxiv = _render_tags_line("arxiv_tags", arxiv_tags)
        if existing_arxiv is None:
            lines.append(rendered_arxiv)
        else:
            lines[existing_arxiv] = rendered_arxiv
    newline = "\r\n" if "\r\n" in text else "\n"
    frontmatter = "---" + newline + newline.join(lines) + newline + "---" + newline
    # Preserve the original body bytes/text exactly; never normalize line endings.
    output = frontmatter + text[match.end():]
    data = output.encode("utf-8")
    if has_bom:
        data = b"\xef\xbb\xbf" + data
    path.write_bytes(data)


def clean_apply(topic: str) -> dict:
    """写前全量备份；任一文件存在 unmapped/review 则拒绝写入。"""
    dry = clean_dryrun(topic)
    if dry["unmapped_tags"] or dry["review_plans"]:
        raise RuntimeError("refusing apply: unmapped or review files exist")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    written: list[dict] = []
    for plan in dry["plans"]:
        if plan["action"] != "replace":
            continue
        path = _version_path(plan["path"])
        backup = path.with_name(path.name + f".bak-kb-tags-{stamp}")
        backup.write_bytes(path.read_bytes())
        _replace_frontmatter(path, plan["after"], plan["org_marks"], plan["arxiv_tags"])
        written.append({"path": path.as_posix(), "backup": backup.as_posix()})

    scan_report = scan_all(source="registry").to_dict()
    db_update = canonicalize_db_tags(topic)
    return {
        "topic": topic, "apply": True, "written": written,
        "written_count": len(written), "scan_report": scan_report,
        "db_canonicalization": db_update, "timestamp": stamp,
    }


def canonicalize_db_tags(topic: str) -> dict:
    """只改派生库：把 L0 metadata tags 映射为 confirmed canonical，并把 org/arxiv 拆出。"""
    vocab = _load_vocab(topic, confirmed_only=True)
    if not vocab:
        return {"topic": topic, "updated": 0}
    alias_map, _, org_values, process_values = _mapping(vocab)
    conn = connect()
    updated = 0
    try:
        rows = conn.execute(
            f"SELECT doc_id,tags,org_marks FROM documents WHERE topic=? AND status IN {SEARCHABLE_SQL}", (topic,)
        ).fetchall()
        for row in rows:
            try:
                values = json.loads(row["tags"])
                marks = json.loads(row["org_marks"])
            except (json.JSONDecodeError, TypeError):
                continue
            after: list[str] = []
            new_marks = list(marks)
            new_arxiv: list[str] = []
            for value in values:
                if value in org_values or value in _DEFAULT_ORG:
                    if value not in new_marks:
                        new_marks.append(value)
                elif _ARXIV_RE.match(str(value)):
                    if value not in new_arxiv:
                        new_arxiv.append(value)
                elif value in process_values:
                    pass
                elif value in alias_map:
                    mapped = alias_map[value]
                    if mapped not in after:
                        after.append(mapped)
                elif vocab.get("status") == "confirmed":
                    pass
                elif value not in after:
                    after.append(value)
            if after != values or new_marks != marks or new_arxiv:
                conn.execute(
                    "UPDATE documents SET tags=?, org_marks=?, updated_at=? WHERE doc_id=?",
                    (json.dumps(after, ensure_ascii=False, separators=(",", ":")),
                     json.dumps(new_marks, ensure_ascii=False, separators=(",", ":")), now(), row["doc_id"]),
                )
                from backend.services.kb.ftx import refresh_doc
                refresh_doc(conn, row["doc_id"])
                updated += 1
        conn.commit()
        return {"topic": topic, "updated": updated, "arxiv_side_table": "not-created"}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── M6：写标（kb_tag）——只写派生库 kb.db，绝不回写 md（02 §16）──────────────


def _loads_list(raw) -> list[str]:
    try:
        values = json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return [str(value) for value in values] if isinstance(values, list) else []


def _ordered(values: Iterable[str]) -> list[str]:
    """去重且保序（JSON 数组顺序稳定 ⇒ 幂等比较可用）。"""
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def tag_docs(doc_ids: list[str], add: list[str] | None = None, remove: list[str] | None = None,
             *, apply: bool = True) -> dict:
    """给若干文档增删 tags。**只写 kb.db**（documents.tags + kb_fts），不碰任何 md。

    - 存储 ID：支持全局 doc_id 与唯一裸 id；歧义裸 id 进 `ambiguous`，不静默选一条。
    - canonical：按各 doc 自身 topic 的 confirmed 词表 `canonicalize()` 归一。
    - 写纪律：一篇一个短事务（`db.write_retry`，busy 重试 3 次）并同步刷新 FTS。
    """
    from backend.services.kb.db import write_retry  # 局部导入：避免模块级循环
    from backend.services.kb.ftx import refresh_doc
    from backend.services.kb.query import resolve_doc_id

    add_raw = [str(value) for value in (add or []) if str(value).strip()]
    remove_raw = [str(value) for value in (remove or []) if str(value).strip()]
    requested = [str(value) for value in (doc_ids or []) if str(value).strip()]

    report = {
        "applied": bool(apply),
        "requested": len(requested),
        "add": add_raw,
        "remove": remove_raw,
        "updated": [],
        "changed": [],
        "noop": [],
        "ambiguous": [],
        "not_found": [],
        "errors": [],
        "md_written": 0,
    }
    if not requested:
        report["errors"].append({"code": "invalid_argument", "message": "doc_ids 不能为空"})
        return report
    if not add_raw and not remove_raw:
        report["errors"].append(
            {"code": "invalid_argument", "message": "add 与 remove 至少要给一个"})
        return report

    for raw_id in requested:
        conn = connect()
        try:
            resolved, candidates = resolve_doc_id(conn, raw_id)
            if resolved is None:
                if candidates:
                    report["ambiguous"].append({"input": raw_id, "candidates": candidates})
                else:
                    report["not_found"].append(raw_id)
                continue
            row = conn.execute(
                "SELECT topic, tags FROM documents WHERE doc_id=?", (resolved,)
            ).fetchone()
            if row is None:
                report["not_found"].append(raw_id)
                continue

            before = _loads_list(row["tags"])
            add_values = [_canon(row["topic"], value) for value in add_raw]
            remove_values = list(remove_raw)
            drop = set(remove_values) | set(add_values)
            after = _ordered(
                [value for value in before if value not in drop] + add_values
            )
            if after == before:
                report["noop"].append({"doc_id": resolved, "tags": after})
                continue
            if not apply:
                report["changed"].append(
                    {"doc_id": resolved, "before": before, "after": after})
                report["updated"].append(resolved)
                continue

            payload = json.dumps(after, ensure_ascii=False, separators=(",", ":"))

            def action(doc_id=resolved, payload=payload):
                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.execute(
                        "UPDATE documents SET tags=?, updated_at=? WHERE doc_id=?",
                        (payload, now(), doc_id),
                    )
                    refresh_doc(conn, doc_id)
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                return True

            write_retry(action)
            report["updated"].append(resolved)
            report["changed"].append(
                {"doc_id": resolved, "before": before, "after": after})
        except sqlite3.Error as exc:
            report["errors"].append(
                {"code": "db_error", "doc_id": raw_id, "message": f"{type(exc).__name__}: {exc}"})
        finally:
            conn.close()
    return report


def _canon(topic: str, value: str) -> str:
    try:
        return canonicalize(topic, value)
    except Exception:  # noqa: BLE001 — 词表缺失/损坏时退回原值，不阻断打标
        return value
