"""库存体检：snapshot / gap / recent / 紧凑渲染。"""
from __future__ import annotations

import json
import sqlite3

from backend.services.kb.db import connect, now
from backend.services.kb.ftx import SEARCHABLE_SQL


def _where(topic: str | None, tag: str | None, args: list) -> str:
    clauses = [f"status IN {SEARCHABLE_SQL}"]
    if topic:
        clauses.append("topic=?")
        args.append(topic)
    if tag:
        clauses.append("EXISTS (SELECT 1 FROM json_each(documents.tags) WHERE json_each.value=?)")
        args.append(tag)
    return " WHERE " + " AND ".join(clauses)


def _tag_counts(conn, topic: str | None, limit: int = 20) -> list[dict]:
    args: list = []
    where = _where(topic, None, args)
    rows = conn.execute(
        f"""
        SELECT je.value AS tag, COUNT(*) AS n
        FROM documents d, json_each(d.tags) je
        {where.replace(" WHERE ", " WHERE ")}
        GROUP BY je.value ORDER BY n DESC, je.value LIMIT ?
        """,
        (*args, limit),
    ).fetchall()
    return [{"tag": row["tag"], "count": row["n"]} for row in rows]


def _org_counts(conn, topic: str | None, limit: int = 20) -> list[dict]:
    """D32：org_marks 作为 topic 内的二级分组（项目线 / 组织标记）分布。"""
    args: list = []
    where = _where(topic, None, args)
    rows = conn.execute(
        f"""
        SELECT je.value AS org, COUNT(*) AS n
        FROM documents d, json_each(d.org_marks) je
        {where.replace(" WHERE ", " WHERE ")}
        GROUP BY je.value ORDER BY n DESC, je.value LIMIT ?
        """,
        (*args, limit),
    ).fetchall()
    return [{"org": row["org"], "count": row["n"]} for row in rows]


def snapshot(topic: str | None = None, tag: str | None = None) -> dict:
    conn = connect()
    try:
        args: list = []
        where = _where(topic, tag, args)
        row = conn.execute(f"SELECT COUNT(*) AS n FROM documents{where}", args).fetchone()
        tiers = {str(i): 0 for i in range(4)}
        for tier_row in conn.execute(
            f"SELECT best_tier, COUNT(*) AS n FROM documents{where} GROUP BY best_tier", args
        ):
            tiers[str(tier_row["best_tier"])] = tier_row["n"]
        stages = {
            stage_row["stage"]: stage_row["n"]
            for stage_row in conn.execute(
                f"SELECT stage, COUNT(*) AS n FROM documents{where} GROUP BY stage ORDER BY stage", args
            )
        }
        sources = {
            source_row["source_type"]: source_row["n"]
            for source_row in conn.execute(
                f"SELECT source_type, COUNT(*) AS n FROM documents{where} GROUP BY source_type ORDER BY source_type", args
            )
        }
        return {
            "kind": "snapshot",
            "topic": topic or "_all",
            "tag": tag,
            "total_docs": row["n"],
            "by_tier": tiers,
            "by_stage": stages,
            "by_source_type": sources,
            "tag_counts": _tag_counts(conn, topic),
            "org_counts": _org_counts(conn, topic),
            "generated_at": now(),
        }
    finally:
        conn.close()


def gap(topic: str | None = None, limit: int = 20) -> dict:
    conn = connect()
    try:
        args: list = []
        where = _where(topic, None, args)
        rows = conn.execute(
            f"""
            SELECT je.value AS tag,
                   SUM(CASE WHEN d.best_tier=0 THEN 1 ELSE 0 END) AS l0,
                   SUM(CASE WHEN d.best_tier=1 THEN 1 ELSE 0 END) AS l1,
                   SUM(CASE WHEN d.best_tier=2 THEN 1 ELSE 0 END) AS l2,
                   SUM(CASE WHEN d.best_tier=3 THEN 1 ELSE 0 END) AS l3
            FROM documents d, json_each(d.tags) je
            {where.replace(" WHERE ", " WHERE ")}
            GROUP BY je.value
            HAVING l0 > 0
            ORDER BY l0 DESC, l2 ASC, l3 ASC, je.value
            LIMIT ?
            """,
            (*args, limit),
        ).fetchall()
        return {
            "kind": "gap",
            "topic": topic or "_all",
            "gaps": [
                {
                    "tag": row["tag"], "L0": row["l0"], "L1": row["l1"],
                    "L2": row["l2"], "L3": row["l3"],
                }
                for row in rows
            ],
            "generated_at": now(),
        }
    finally:
        conn.close()


def recent(days: int = 7, topic: str | None = None) -> dict:
    conn = connect()
    try:
        where_args: list = []
        where = _where(topic, None, where_args)
        args: list = [*where_args, f"-{int(days)} days"]
        rows = conn.execute(
            f"""
            SELECT doc_id, topic, title, best_tier, stage, updated_at
            FROM documents{where} AND updated_at >= datetime('now', ?)
            ORDER BY updated_at DESC LIMIT 100
            """,
            args,
        ).fetchall()
        return {
            "kind": "recent", "topic": topic or "_all", "days": days,
            "items": [dict(row) for row in rows], "generated_at": now(),
        }
    finally:
        conn.close()


def render_compact(snap: dict) -> str:
    topic = snap.get("topic", "_all")
    tag = snap.get("tag")
    label = f"{topic}@{tag}" if tag else topic
    tiers = snap.get("by_tier", {})
    source_text = "/".join(f"{key} {value}" for key, value in snap.get("by_source_type", {}).items())
    line = (
        f"[{label}] {snap.get('total_docs', 0)} 条｜"
        f"L0仅元数据 {tiers.get('0', 0)}｜L1轻整理 {tiers.get('1', 0)}｜"
        f"L2已提炼 {tiers.get('2', 0)}｜L3全文 {tiers.get('3', 0)}"
    )
    if source_text:
        line += f"｜{source_text}"
    tags = snap.get("tag_counts", [])[:8]
    if tags:
        line += "\n  子方向: " + " ".join(f"{item['tag']}×{item['count']}" for item in tags)
        line += "（口径: 目录含空壳; tags 待 M2 归一）"
    orgs = snap.get("org_counts", [])[:8]
    if orgs:
        # D32：org_marks 是 topic 内的二级分组；注意也含 M2 遗留的 arXiv 分区码
        line += "\n  项目线: " + " ".join(f"{item['org']}×{item['count']}" for item in orgs)
        line += "（org_marks; 含 M2 遗留的 arXiv 分区码）"
    return line


def render_gap(data: dict) -> str:
    topic = data.get("topic", "_all")
    rows = data.get("gaps", [])[:10]
    if not rows:
        return f"[{topic} gap] 无 L0 缺口"
    values = " ".join(
        f"{row['tag']} L2={row['L2']}/L0={row['L0']}" for row in rows
    )
    return f"[{topic} gap] {values}"
