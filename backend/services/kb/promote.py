"""D27 / Q11：L2 晋升的回链校验门槛（M5 落地，M6 由 kb_promote 工具复用）。

02 §11.3 证据双轨铁律：
  2. 仅当该 doc 存在 tier=2 version **且带回链**（`[L3:路径#chunk]` 标记或 provenance 头）
     才允许直接用 L2 作答；
  4. 存量 5 条 dense 无回链 → `status='legacy_l2'` 豁免（名单 `knowledgebase/_meta/config/legacy_l2.json`），
     M5 起不再新增此类。

本模块只做**校验 + status 流转**，绝不改 md 源文件（红线）：
  - to_tier=3：要求存在 tier=3 版本（原文）；
  - to_tier=2：要求存在 tier=2 版本，且其正文含**可解析到该 doc 自身 L3 版本**的回链；
    不满足 → 拒绝（`missing_backlink`）；已在 legacy_l2 名单内 → 豁免放行（不新增此类）。
"""
from __future__ import annotations

import re
from pathlib import Path

from backend.services.kb.db import PROJECT_ROOT, connect, now

# 回链形态：`[L1:路径#chunk]`（D44：回链目标是 tier=1 的原文，不再叫 L3）；
# chunk 段可省（仅路径锚）。
BACKLINK_RE = re.compile(r"\[L1:(?P<path>[^\]\s#]+)(?:#(?P<chunk>[^\]\s]+))?\]")
_NUMERIC_CHUNK_RE = re.compile(r"^(?:c|chunk[-_]?|#)?(\d+)$", re.IGNORECASE)
_CONTENT_MAX_CHARS = 2_000_000


def _norm_path(raw: str) -> str:
    text = (raw or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.strip("/")


def _path_matches(link_path: str, version_path: str) -> bool:
    """回链路径 vs 版本路径：全等 / 后缀匹配 / 文件名匹配（容忍省略 knowledgebase 前缀）。"""
    link = _norm_path(link_path)
    target = _norm_path(version_path)
    if not link or not target:
        return False
    if link == target or target.endswith("/" + link) or link.endswith("/" + target):
        return True
    return link == target.rsplit("/", 1)[-1]


def _chunk_index(selector: str) -> int | None:
    """`#c2` / `#chunk-2` / `#2` → 2；非数字锚（章节名）→ None（按段锚接受）。"""
    match = _NUMERIC_CHUNK_RE.match((selector or "").strip())
    return int(match.group(1)) if match else None


def _chunks_of(conn, version_id: int) -> dict[int, int]:
    """version 的块：chunk_index → chunk_id（本 doc 该版本是否已嵌入不影响校验）。"""
    return {
        int(row["chunk_index"]): int(row["chunk_id"])
        for row in conn.execute(
            "SELECT chunk_index, chunk_id FROM chunks WHERE version_id=?", (version_id,)
        )
    }


def _load_versions(conn, doc_id: str) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT version_id, tier, kind, path FROM versions WHERE doc_id=? "
            "ORDER BY tier DESC, kind",
            (doc_id,),
        )
    ]


def check_backlinks(conn, doc_id: str) -> dict:
    """校验某 doc 的派出版本（tier=2）回链：返回可解析回链 / 坏链清单。

    D44：回链必须指向本文档的 **tier=1 原文**版本。
    """
    versions = _load_versions(conn, doc_id)
    derived_versions = [item for item in versions if item["tier"] == 2]
    raw_versions = [item for item in versions if item["tier"] == 1]
    result = {
        "doc_id": doc_id,
        "has_derived_version": bool(derived_versions),
        "has_raw_version": bool(raw_versions),
        "derived_kinds": [item["kind"] for item in derived_versions],
        "valid": [],
        "broken": [],
        "link_count": 0,
        "checked_files": [],
    }
    for version in derived_versions:
        raw = version["path"]
        path = Path(raw)
        if not path.is_absolute():
            path = PROJECT_ROOT / raw
        try:
            text = path.read_text(encoding="utf-8", errors="replace")[:_CONTENT_MAX_CHARS]
        except OSError as exc:
            result["broken"].append({
                "version": version["path"], "reason": f"read_failed: {type(exc).__name__}",
            })
            continue
        result["checked_files"].append(version["path"])
        for match in BACKLINK_RE.finditer(text):
            result["link_count"] += 1
            link_path = match.group("path")
            selector = match.group("chunk")
            target = next(
                (item for item in raw_versions if _path_matches(link_path, item["path"])), None
            )
            if target is None:
                result["broken"].append({
                    "link": match.group(0), "in": version["path"],
                    "reason": "path_not_in_doc_raw_versions",
                    "raw_paths": [item["path"] for item in raw_versions],
                })
                continue
            item = {
                "link": match.group(0), "in": version["path"],
                "raw_path": target["path"], "chunk_selector": selector,
            }
            index = _chunk_index(selector) if selector else None
            if index is not None:
                chunks = _chunks_of(conn, int(target["version_id"]))
                if chunks and index not in chunks:
                    result["broken"].append({
                        **item, "reason": "chunk_index_not_found",
                        "available_chunk_indexes": sorted(chunks)[:20],
                    })
                    continue
                item["chunk_id"] = chunks.get(index)
                item["chunk_verified"] = bool(chunks)
            else:
                item["chunk_verified"] = False
            result["valid"].append(item)
    result["ok"] = bool(result["valid"])
    return result


def promote(doc_ids, to_tier: int = 2, *, apply: bool = False, strict: bool = True,
            conn=None) -> dict:
    """晋升校验：to_tier=2 必须带回链（legacy_l2 存量豁免）；to_tier=3 必须有提炼版且原文在。

    D44 口径：2=轻整理、3=提炼，两者都必须能回链到本文档的 **tier=1 原文**。
    `apply=False` 只校验（默认，绝不写库）；`apply=True` 通过后把 status 落为 active
    （legacy_l2 豁免者保持 legacy_l2 不变——豁免是存量事实，不因一次调用被抹掉）。
    `strict=True`（默认）任一 doc 不通过则不写库，避免半晋升状态。
    """
    if to_tier not in (2, 3):
        raise ValueError("to_tier 只支持 2（轻整理）或 3（提炼）")
    targets = [str(item).strip() for item in (doc_ids or []) if str(item).strip()]
    if not targets:
        raise ValueError("doc_ids 不能为空")

    own = conn is None
    conn = conn or connect()
    report = {
        "to_tier": to_tier,
        "apply": bool(apply),
        "strict": bool(strict),
        "requested": len(targets),
        "ok": True,
        "promoted": [],
        "exempt": [],
        "rejected": [],
        "details": [],
        "written": [],
    }
    try:
        for doc_id in targets:
            row = conn.execute(
                "SELECT doc_id, status FROM documents WHERE doc_id=?", (doc_id,)
            ).fetchone()
            if row is None:
                report["rejected"].append({"doc_id": doc_id, "reason": "not_found"})
                report["details"].append({"doc_id": doc_id, "ok": False,
                                          "reason": "not_found"})
                continue
            status = row["status"]
            versions = _load_versions(conn, doc_id)
            detail = {
                "doc_id": doc_id, "status": status, "to_tier": to_tier,
                "available_tiers": sorted({item["tier"] for item in versions}),
            }
            if to_tier == 3:
                has_derived = any(item["tier"] == 3 for item in versions)
                has_raw = any(item["tier"] == 1 for item in versions)
                ok = has_derived and has_raw
                detail.update(
                    ok=ok, action="promote_l3" if ok else "reject",
                    reason=(None if ok else
                            ("missing_tier3_version" if not has_derived else "missing_raw_version")),
                )
            elif status == "legacy_l2":
                check = check_backlinks(conn, doc_id)
                detail.update(ok=True, action="keep_legacy_exempt", exempt=True,
                              reason="D27 存量豁免（不新增此类）",
                              link_count=check["link_count"])
            else:
                check = check_backlinks(conn, doc_id)
                detail.update(
                    ok=check["ok"],
                    action="promote_l2" if check["ok"] else "reject",
                    reason=None if check["ok"] else "missing_backlink",
                    link_count=check["link_count"],
                    backlinks=check["valid"],
                    broken=check["broken"],
                    has_derived_version=check["has_derived_version"],
                )
            report["details"].append(detail)
            if not detail["ok"]:
                report["rejected"].append({"doc_id": doc_id, "reason": detail["reason"],
                                           "detail": detail})
            elif detail.get("exempt"):
                report["exempt"].append(doc_id)
            else:
                report["promoted"].append(doc_id)
        report["ok"] = not report["rejected"]

        if apply and (report["ok"] or not strict):
            stamp = now()
            for doc_id in report["promoted"]:
                conn.execute(
                    "UPDATE documents SET status='active', updated_at=? WHERE doc_id=?",
                    (stamp, doc_id),
                )
            conn.commit()
            report["written"] = list(report["promoted"])
    finally:
        if own:
            conn.close()
    return report
