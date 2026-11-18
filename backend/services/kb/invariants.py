"""派生层数据不变量；scan/rebuild/embed 收尾自动校验。"""
from __future__ import annotations

import json
import sqlite3

from backend.services.kb.db import has_vec_table
from backend.services.kb.ftx import SEARCHABLE_STATUSES


_STAGES = {"found", "downloaded", "converted", "translated", "condensed"}


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def assert_invariants(conn: sqlite3.Connection) -> dict:
    """校验 04 测试计划 §5 中 M1/M2/M4 适用的派生层不变量。"""
    violations: list[str] = []

    # I2：FTS 行数 == 可检索状态文档数（active + legacy_l2，02 §5/D27）。
    # legacy_l2 是"可检索但免回链"的存量状态，必须与 FTS 口径一致，否则会被静默漏检。
    status_placeholders = ",".join("?" * len(SEARCHABLE_STATUSES))
    active_docs = conn.execute(
        f"SELECT COUNT(*) FROM documents WHERE status IN ({status_placeholders})",
        SEARCHABLE_STATUSES,
    ).fetchone()[0]
    legacy_l2_docs = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE status='legacy_l2'"
    ).fetchone()[0]
    fts_docs = conn.execute("SELECT COUNT(*) FROM kb_fts").fetchone()[0]
    if fts_docs != active_docs:
        violations.append(f"I2 FTS count {fts_docs} != searchable documents {active_docs}")

    orphan_versions = conn.execute(
        "SELECT COUNT(*) FROM versions v LEFT JOIN documents d ON d.doc_id=v.doc_id "
        "WHERE d.doc_id IS NULL"
    ).fetchone()[0]
    if orphan_versions:
        violations.append(f"I3 orphan versions={orphan_versions}")

    orphan_chunks = 0
    meta_chunks = 0
    if _has_table(conn, "chunks"):
        orphan_chunks = conn.execute(
            "SELECT COUNT(*) FROM chunks c LEFT JOIN versions v ON v.version_id=c.version_id "
            "WHERE v.version_id IS NULL"
        ).fetchone()[0]
        if orphan_chunks:
            violations.append(f"I3 orphan chunks={orphan_chunks}")
        meta_chunks = conn.execute(
            "SELECT COUNT(*) FROM chunks c JOIN versions v ON v.version_id=c.version_id "
            "WHERE v.kind='meta'"
        ).fetchone()[0]
        if meta_chunks:
            violations.append(f"I7 meta chunks embedded={meta_chunks}")

    bad_version_tier = conn.execute(
        "SELECT COUNT(*) FROM versions WHERE tier < 0 OR tier > 3"
    ).fetchone()[0]
    if bad_version_tier:
        violations.append(f"I4 version tier violations={bad_version_tier}")
    bad_best_tier = conn.execute(
        """
        SELECT doc_id
        FROM (
          SELECT d.doc_id AS doc_id, d.best_tier AS stored,
                 COALESCE((SELECT MAX(v.tier) FROM versions v WHERE v.doc_id=d.doc_id),0) AS actual
          FROM documents d
        )
        WHERE stored != actual
        """
    ).fetchall()
    if bad_best_tier:
        violations.append(f"I4 best_tier mismatches={len(bad_best_tier)}")

    bad_stages = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE stage NOT IN "
        "('found','downloaded','converted','translated','condensed')"
    ).fetchone()[0]
    if bad_stages:
        violations.append(f"I5 stage violations={bad_stages}")

    bad_tags: list[str] = []
    for row in conn.execute("SELECT doc_id, tags FROM documents"):
        try:
            value = json.loads(row["tags"])
            if not isinstance(value, list):
                raise ValueError("not array")
        except (TypeError, ValueError, json.JSONDecodeError):
            bad_tags.append(row["doc_id"])
    if bad_tags:
        violations.append(f"I8 invalid tags JSON={len(bad_tags)}")

    # I9（M4）：容器向量数 == chunks WHERE model_id IS NOT NULL，且双向无孤儿。
    model_count = 0
    vector_count = 0
    missing_vectors = 0
    orphan_vectors = 0
    if _has_table(conn, "chunks"):
        model_count = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE model_id IS NOT NULL"
        ).fetchone()[0]
        if not has_vec_table(conn):
            if model_count:
                violations.append(f"I9 container missing but embedded chunks={model_count}")
        else:
            vector_count = conn.execute("SELECT COUNT(*) FROM kb_vec").fetchone()[0]
            if vector_count != model_count:
                violations.append(f"I9 container vectors {vector_count} != embedded chunks {model_count}")
            chunk_ids = {
                row[0] for row in conn.execute(
                    "SELECT chunk_id FROM chunks WHERE model_id IS NOT NULL"
                )
            }
            vec_ids = {row[0] for row in conn.execute("SELECT chunk_id FROM kb_vec")}
            missing_vectors = len(chunk_ids - vec_ids)
            orphan_vectors = len(vec_ids - chunk_ids)
            if missing_vectors:
                violations.append(f"I9 embedded chunks missing vectors={missing_vectors}")
            if orphan_vectors:
                violations.append(f"I9 orphan vectors={orphan_vectors}")

    if violations:
        raise RuntimeError("; ".join(violations))
    return {
        "active_documents": active_docs,
        "legacy_l2_documents": legacy_l2_docs,
        "fts_rows": fts_docs,
        "orphan_versions": orphan_versions,
        "orphan_chunks": orphan_chunks,
        "meta_chunks": meta_chunks,
        "best_tier_mismatches": len(bad_best_tier),
        "invalid_tags": len(bad_tags),
        "embedded_chunks": model_count,
        "container_vectors": vector_count,
        "missing_vectors": missing_vectors,
        "orphan_vectors": orphan_vectors,
    }
