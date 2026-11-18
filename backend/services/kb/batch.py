"""批次（候选区）生命周期：open / add / confirm / list —— M6 落地 02 §7 的 `kb-batch`。

流程（00 文档提案 N4 / D11–D12）::

    本地无果 → AI 外搜 → 结果进「候选区」（batch, status=open）不打扰用户
      → 攒一批 → 告知汇总 → 用户确认
      → 一次性选 1 个 topic + 点几个 tag → confirm（批量入库 + 按 topic 写 tags）

设计约束：
- `batch_items` 只记 `doc_id`（外搜入库后已存在于 kb.db；不存在者在 add 时报
  `unknown_doc`，不静默吞）。
- **confirm 才写 documents**：open/add 全程只动批次两张表，候选区不影响检索
  （`_inbox` 之类未确认条目不会因本模块被搜索命中而扰动）。
- 写操作一律走 `db.write_retry`（短事务 + busy 重试 3 次），单写者纪律。
"""
from __future__ import annotations

from typing import Iterable

from backend.services.kb.db import connect, now, write_retry
from backend.services.kb.tags import tag_docs

BATCH_STATUSES = ("open", "confirmed", "cancelled")


def _rid(prefix: str = "r") -> str:
    from uuid import uuid4

    return f"{prefix}-{uuid4().hex[:12]}"


def _norm(values: Iterable[str] | None) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in values or []:
        value = str(raw).strip()
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _row(conn, round_id: str) -> dict | None:
    row = conn.execute(
        "SELECT round_id, topic, label, status, created_at, confirmed_at "
        "FROM batches WHERE round_id=?",
        (round_id,),
    ).fetchone()
    return dict(row) if row else None


def _items(conn, round_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT doc_id, action FROM batch_items WHERE round_id=? ORDER BY doc_id",
        (round_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _detail(conn, round_id: str) -> dict:
    batch = _row(conn, round_id)
    if batch is None:
        return {"error": "unknown_round", "round_id": round_id}
    items = _items(conn, round_id)
    return {**batch, "item_count": len(items), "items": items}


def open_batch(round_id: str | None = None, topic: str | None = None,
               label: str | None = None) -> dict:
    """开一个批次（幂等：同 round_id 已存在则原样返回，不覆盖 topic/label）。"""
    rid = (round_id or "").strip() or _rid()
    conn = connect()
    try:
        def action():
            conn.execute(
                "INSERT OR IGNORE INTO batches(round_id, topic, label, status, created_at) "
                "VALUES (?,?,?,'open',?)",
                (rid, topic, label or topic, now()),
            )
            conn.commit()
            return True

        write_retry(action)
        detail = _detail(conn, rid)
        detail["created"] = True
        return detail
    finally:
        conn.close()


def add_items(round_id: str, doc_ids: Iterable[str], *, action_name: str = "import",
              create_if_missing: bool = True) -> dict:
    """把 doc_id 加入候选区。未知 doc_id → `unknown_doc`（不静默吞）。

    `create_if_missing=True` 时批次不存在则自动开一个（Skill 里"边搜边攒"更顺手）。
    """
    rid = (round_id or "").strip()
    if not rid:
        return {"error": "invalid_argument", "message": "round_id 不能为空"}
    ids = _norm(doc_ids)
    if not ids:
        return {"error": "invalid_argument", "message": "doc_ids 不能为空"}

    conn = connect()
    try:
        if _row(conn, rid) is None:
            if not create_if_missing:
                return {"error": "unknown_round", "round_id": rid}
            open_batch(rid)
        missing = [
            doc_id for doc_id in ids
            if conn.execute("SELECT 1 FROM documents WHERE doc_id=?", (doc_id,)).fetchone() is None
        ]

        def action():
            conn.executemany(
                "INSERT OR IGNORE INTO batch_items(round_id, doc_id, action) VALUES (?,?,?)",
                [(rid, doc_id, action_name) for doc_id in ids if doc_id not in set(missing)],
            )
            conn.commit()
            return True

        write_retry(action)
        detail = _detail(conn, rid)
        detail["added"] = [doc_id for doc_id in ids if doc_id not in set(missing)]
        detail["unknown_doc"] = missing
        return detail
    finally:
        conn.close()


def confirm(round_id: str, topic: str, tags: Iterable[str]) -> dict:
    """一次性确认：批次 open→confirmed，并把 topic/tags 落进本批 documents。

    - 已 confirmed → `already_confirmed`（幂等拒绝，不重复写）。
    - 空批次 → `empty_round`（拒绝，避免"确认了个空"造成假把握）。
    - topic 必填（主归属，D24/N6）；tags 可为空列表。
    - 每个条目走 `tag_docs` 的短事务写（只写 kb.db，不碰 md）。
    """
    rid = (round_id or "").strip()
    if not rid:
        return {"error": "invalid_argument", "message": "round_id 不能为空"}
    if not (topic or "").strip():
        return {"error": "invalid_argument", "message": "topic 不能为空（主归属必填，D24）"}
    tag_values = _norm(tags)

    conn = connect()
    try:
        batch = _row(conn, rid)
        if batch is None:
            return {"error": "unknown_round", "round_id": rid}
        if batch["status"] == "confirmed":
            return {"error": "already_confirmed", "round_id": rid,
                    "confirmed_at": batch["confirmed_at"],
                    "message": "该批次已确认，未重复写入"}
        items = _items(conn, rid)
        if not items:
            return {"error": "empty_round", "round_id": rid,
                    "message": "候选区为空，无需确认"}
        doc_ids = [item["doc_id"] for item in items]
    finally:
        conn.close()

    tag_report = tag_docs(doc_ids, add=tag_values) if tag_values else {
        "applied": True, "updated": [], "changed": [], "noop": [],
        "ambiguous": [], "not_found": [], "errors": [], "md_written": 0,
        "requested": len(doc_ids), "add": [], "remove": [],
    }

    conn = connect()
    try:
        stamp = now()

        def action():
            conn.execute(
                "UPDATE batches SET topic=?, status='confirmed', confirmed_at=? WHERE round_id=?",
                (topic, stamp, rid),
            )
            conn.execute(
                "UPDATE documents SET topic=?, updated_at=? WHERE doc_id IN "
                "(SELECT doc_id FROM batch_items WHERE round_id=?)",
                (topic, stamp, rid),
            )
            conn.commit()
            return True

        write_retry(action)
        detail = _detail(conn, rid)
    finally:
        conn.close()

    detail.update({
        "confirmed_at": stamp,
        "topic_applied": topic,
        "tags_applied": tag_values,
        "tag_report": tag_report,
        "written_docs": len(doc_ids),
    })
    return detail


def list_batches(status: str | None = None, limit: int = 50) -> dict:
    conn = connect()
    try:
        args: list = []
        where = ""
        if status:
            where = " WHERE status=?"
            args.append(status)
        rows = conn.execute(
            f"SELECT round_id, topic, label, status, created_at, confirmed_at "
            f"FROM batches{where} ORDER BY created_at DESC, round_id LIMIT ?",
            (*args, int(limit)),
        ).fetchall()
        out = []
        for row in rows:
            entry = dict(row)
            entry["item_count"] = conn.execute(
                "SELECT COUNT(*) FROM batch_items WHERE round_id=?", (row["round_id"],)
            ).fetchone()[0]
            out.append(entry)
        counts = {
            row["status"]: row["n"]
            for row in conn.execute(
                "SELECT status, COUNT(*) AS n FROM batches GROUP BY status ORDER BY status")
        }
        return {"kind": "batches", "status_filter": status, "count": len(out),
                "by_status": counts, "items": out, "generated_at": now()}
    finally:
        conn.close()


def get(round_id: str) -> dict:
    conn = connect()
    try:
        return _detail(conn, (round_id or "").strip())
    finally:
        conn.close()


def lifecycle(round_id: str | None, topic: str, tags: Iterable[str],
              doc_ids: Iterable[str] | None = None,
              action: str = "auto") -> dict:
    """MCP `kb_batch_confirm` 的一体化入口：open → add → confirm。

    `action`：`auto`（默认：有 doc_ids 先 add，再 confirm）/ `open` / `add` /
    `confirm` / `list`。`auto` 是给 agent 的顺手形态；显式动作便于分步验收。
    """
    mode = (action or "auto").strip().lower()
    if mode not in ("auto", "open", "add", "confirm", "list"):
        return {"error": "invalid_argument",
                "message": f"action 非法：{action}（expect auto|open|add|confirm|list）"}

    if mode == "list":
        return list_batches()
    if mode == "open":
        return open_batch(round_id, topic)
    if mode == "add":
        if not round_id:
            return {"error": "invalid_argument", "message": "add 需要 round_id"}
        return add_items(round_id, doc_ids or [])

    rid = (round_id or "").strip()
    steps: list[dict] = []
    if mode == "auto":
        if not rid:
            opened = open_batch(None, topic)
            rid = opened.get("round_id", "")
            steps.append({"step": "open", "round_id": rid})
        if doc_ids:
            added = add_items(rid, doc_ids)
            if "error" in added:
                return {**added, "steps": steps, "round_id": rid}
            steps.append({"step": "add", "added": added.get("added", []),
                          "unknown_doc": added.get("unknown_doc", [])})
        result = confirm(rid, topic, tags)
        result["steps"] = steps
        return result

    if not rid:
        return {"error": "invalid_argument", "message": "confirm 需要 round_id"}
    result = confirm(rid, topic, tags)
    result["steps"] = steps
    return result


__all__ = ["BATCH_STATUSES", "open_batch", "add_items", "confirm", "list_batches",
           "get", "lifecycle"]
