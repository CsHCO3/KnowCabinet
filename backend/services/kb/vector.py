"""M4 chunk manifest + 增量向量（02 §6.2 / §10）。

主容器 = sqlite-vec `vec0`（M0 实测选型 A）；FAISS IndexIDMap2 仅保留接口。
核心算法：
  version 级 content_hash 短路 → chunk 级 text hash 差集 → 容器/表同步增删；
  同一 version 一个事务；`--reembed-stale` 才重算 model_id 不符的旧块。
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol, Sequence

import numpy as np

from backend.services.kb.db import (
    PROJECT_ROOT,
    VEC_DIM,
    connect,
    ensure_vec_table,
    has_vec_table,
    load_sqlite_vec,
    now,
)
from backend.services.kb.ftx import SEARCHABLE_SQL
from backend.services.kb.invariants import assert_invariants

MODEL_ID = "bge-m3@local-1024"
EMBED_BATCH = 256
_TMP_INDEX_BASE = 1_000_000_000


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _abs_path(raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _normalize(vecs: np.ndarray) -> np.ndarray:
    """L2 归一化；零向量保持零向量（避免除零）。"""
    vecs = np.asarray(vecs, dtype="float32")
    if vecs.ndim == 1:
        vecs = vecs.reshape(1, -1)
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vecs / norms


def _default_embed_fn(texts: list[str]) -> np.ndarray:
    from ragsearch.engine.bge_embed import embed_dense

    return embed_dense(texts)


def _embed_texts(embed_fn: Callable[[list[str]], object], texts: list[str],
                 batch_size: int = EMBED_BATCH) -> np.ndarray:
    """分批调用 embed_fn，返回 (len(texts), 1024) 的 float32 归一化矩阵。"""
    if not texts:
        return np.zeros((0, VEC_DIM), dtype="float32")
    batches: list[np.ndarray] = []
    for start in range(0, len(texts), max(1, batch_size)):
        chunk = texts[start:start + max(1, batch_size)]
        raw = np.asarray(embed_fn(chunk), dtype="float32")
        if raw.ndim == 1:
            raw = raw.reshape(1, -1)
        if raw.shape[0] != len(chunk):
            raise RuntimeError(
                f"embed_fn 返回 {raw.shape[0]} 条向量，期望 {len(chunk)} 条"
            )
        if raw.shape[1] != VEC_DIM:
            raise RuntimeError(f"embed_fn 向量维度 {raw.shape[1]}，期望 {VEC_DIM}")
        batches.append(raw)
    return _normalize(np.vstack(batches))


# ── 容器协议 ────────────────────────────────────────────────────────────────


class VecContainer(Protocol):
    """向量容器协议：SQLite vec0 主实现；FAISS 兜底只留接口。"""

    def upsert(self, entries: Sequence[tuple[int, Sequence[float]]]) -> None: ...

    def remove(self, chunk_ids: Sequence[int]) -> None: ...

    def count(self) -> int: ...

    def knn(self, query: Sequence[float], top_k: int) -> list[dict]: ...


class SqliteVecContainer:
    """容器 A：kb.db 内 vec0 虚表，与 chunks 表共享事务。"""

    def __init__(self, conn):
        self.conn = conn

    @staticmethod
    def _serialize(vec: Sequence[float]) -> bytes:
        from sqlite_vec import serialize_float32

        return serialize_float32(np.asarray(vec, dtype="float32"))

    def upsert(self, entries: Sequence[tuple[int, Sequence[float]]]) -> None:
        if not entries:
            return
        # vec0 不支持 INSERT OR REPLACE；先 DELETE 再 INSERT。
        self.conn.executemany(
            "DELETE FROM kb_vec WHERE chunk_id=?", [(int(cid),) for cid, _ in entries]
        )
        self.conn.executemany(
            "INSERT INTO kb_vec(chunk_id, embedding) VALUES (?, ?)",
            [(int(cid), self._serialize(vec)) for cid, vec in entries],
        )

    def remove(self, chunk_ids: Sequence[int]) -> None:
        if not chunk_ids:
            return
        self.conn.executemany(
            "DELETE FROM kb_vec WHERE chunk_id=?", [(int(cid),) for cid in chunk_ids]
        )

    def count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM kb_vec").fetchone()[0])

    def knn(self, query: Sequence[float], top_k: int) -> list[dict]:
        if top_k < 1:
            return []
        rows = self.conn.execute(
            "SELECT chunk_id, distance FROM kb_vec WHERE embedding MATCH ? AND k=? "
            "ORDER BY distance",
            (self._serialize(query), int(top_k)),
        ).fetchall()
        # vec0 返回 L2 距离；归一化向量下 distance∈[0,2]，转成相似度便于调用方理解。
        return [
            {"chunk_id": int(row[0]), "distance": float(row[1]),
             "score": round(1.0 - float(row[1]) / 2.0, 6)}
            for row in rows
        ]


class FaissIdMapContainer:
    """容器 B 占位：M0 选定容器 A，故只保留接口，不参与当前验收。"""

    def __init__(self, *args, **kwargs):
        raise NotImplementedError("FAISS IndexIDMap2 容器为 M0 兜底接口，当前未启用")

    def upsert(self, entries):  # pragma: no cover - 接口占位
        raise NotImplementedError

    def remove(self, chunk_ids):  # pragma: no cover - 接口占位
        raise NotImplementedError

    def count(self):  # pragma: no cover - 接口占位
        raise NotImplementedError

    def knn(self, query, top_k):  # pragma: no cover - 接口占位
        raise NotImplementedError


def container_for(conn) -> VecContainer:
    """返回当前连接可用的向量容器；sqlite-vec 不可用时给出明确错误。"""
    load_sqlite_vec(conn)
    if ensure_vec_table(conn) and has_vec_table(conn):
        return SqliteVecContainer(conn)
    raise RuntimeError(
        "sqlite-vec 不可用，容器 B（FAISS）仅保留接口；请先安装 sqlite-vec 或实现容器 B"
    )


# ── 候选版本 / 范围 ──────────────────────────────────────────────────────────


def _load_topics() -> dict:
    path = PROJECT_ROOT / "data" / "kb" / "topics.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def embed_l1_enabled(topic: str) -> bool:
    """**已废弃**（D44）：现在非 meta 一律嵌入，本开关不再决定是否切块。

    保留函数只为兼容旧配置与测试；新代码请勿依赖。
    """
    config = _load_topics()
    defaults = config.get("defaults") or {}
    value = (config.get("topics") or {}).get(topic) or {}
    return bool(value.get("embed_l1", defaults.get("embed_l1", False)))


def _candidate_versions(conn, topic: str | None) -> list[dict]:
    where = [f"d.status IN {SEARCHABLE_SQL}", "v.kind != 'meta'", "v.tier >= 1"]
    args: list = []
    if topic:
        where.append("d.topic = ?")
        args.append(topic)
    rows = conn.execute(
        f"""
        SELECT v.version_id, v.doc_id, v.tier, v.kind, v.path, v.content_hash, d.topic
        FROM versions v JOIN documents d ON d.doc_id=v.doc_id
        WHERE {' AND '.join(where)}
        ORDER BY d.topic, v.version_id
        """,
        args,
    ).fetchall()
    result: list[dict] = []
    for row in rows:
        # D44（2026-10-19）：嵌入门槛只看"有没有正文"，不再按档位。
        # 密度序下"原文"是 tier=1，若沿用 tier>=2 会把 2.3 万个原文块全部踢出向量库。
        result.append(dict(row))
    return result


def _scope_version_ids(conn, topic: str | None) -> set[int]:
    """选中 topic（或全库）的全部 version_id，用于清理已不再可嵌入的旧 chunks。"""
    if topic:
        rows = conn.execute(
            "SELECT v.version_id FROM versions v JOIN documents d ON d.doc_id=v.doc_id "
            "WHERE d.topic=?", (topic,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT version_id FROM versions").fetchall()
    return {int(row[0]) for row in rows}


# ── 单 version 计划 ─────────────────────────────────────────────────────────


@dataclass
class _VersionPlan:
    version_id: int
    doc_id: str
    path: str
    inserts: list[dict] = field(default_factory=list)
    updates: list[dict] = field(default_factory=list)
    deletes: list[int] = field(default_factory=list)
    embed_specs: list[dict] = field(default_factory=list)
    changed: bool = False
    skipped: bool = False
    error: str | None = None


def _build_plan(conn, version: dict, reembed_stale: bool) -> _VersionPlan:
    plan = _VersionPlan(
        version_id=int(version["version_id"]),
        doc_id=version["doc_id"],
        path=version["path"],
    )
    source_path = _abs_path(version["path"])
    try:
        content = source_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        plan.error = f"{type(exc).__name__}: {exc}"
        return plan

    from ragsearch.engine.chunker import chunk_md

    raw_chunks = chunk_md(content, version["doc_id"], filename=source_path.name)
    old_rows = conn.execute(
        "SELECT chunk_id, chunk_index, text, content_hash, model_id FROM chunks "
        "WHERE version_id=? ORDER BY chunk_index", (plan.version_id,)
    ).fetchall()
    old_by_hash: dict[str, list[dict]] = {}
    for row in old_rows:
        old_by_hash.setdefault(row["content_hash"], []).append(dict(row))

    consumed: set[int] = set()
    for new_index, raw_chunk in enumerate(raw_chunks):
        text = raw_chunk["text"]
        text_hash = _sha16(text)
        pool = old_by_hash.get(text_hash) or []
        reused = None
        while pool:
            candidate = pool.pop(0)
            if candidate["chunk_id"] not in consumed:
                reused = candidate
                break
        if reused is None:
            key = ("insert", new_index)
            plan.inserts.append({"index": new_index, "text": text, "hash": text_hash,
                                 "vector_key": key})
            plan.embed_specs.append({"key": key, "text": text})
            continue

        consumed.add(reused["chunk_id"])
        old_model = reused["model_id"]
        needs_embed = old_model in (None, "") or (
            str(old_model) != MODEL_ID and reembed_stale
        )
        update = {
            "chunk_id": reused["chunk_id"],
            "index": new_index,
            "old_index": reused["chunk_index"],
            "text": text,
            "hash": text_hash,
            "needs_embed": needs_embed,
        }
        plan.updates.append(update)
        if needs_embed:
            plan.embed_specs.append(
                {"key": ("update", reused["chunk_id"]), "text": text}
            )

    plan.deletes = [
        row["chunk_id"] for row in old_rows if row["chunk_id"] not in consumed
    ]
    changed = bool(plan.inserts or plan.deletes)
    if not changed:
        for update in plan.updates:
            if update["index"] != update["old_index"] or update["needs_embed"]:
                changed = True
                break
    plan.changed = changed
    plan.skipped = not changed
    return plan


# ── 同步主流程 ──────────────────────────────────────────────────────────────


def _cleanup_scope(conn, container: VecContainer, scope_ids: set[int],
                   candidate_ids: set[int]) -> int:
    """删除选中范围内已不再可嵌入的 version 的 chunks/向量，返回删除块数。"""
    remove_version_ids = sorted(scope_ids - candidate_ids)
    deleted = 0
    for version_id in remove_version_ids:
        rows = conn.execute(
            "SELECT chunk_id FROM chunks WHERE version_id=?", (version_id,)
        ).fetchall()
        chunk_ids = [int(row[0]) for row in rows]
        if not chunk_ids:
            continue
        container.remove(chunk_ids)
        conn.executemany("DELETE FROM chunks WHERE chunk_id=?", [(cid,) for cid in chunk_ids])
        deleted += len(chunk_ids)
    return deleted


def _cleanup_orphans(conn, container: VecContainer) -> int:
    """清理无 version 的孤儿 chunks 与无 chunk 的孤儿向量、孤儿 FTS 行。"""
    deleted = 0
    # 文档整行删除时 versions/chunks 由 FK 级联清掉，但 kb_fts / kb_vec 没有 FK。
    conn.execute(
        "DELETE FROM kb_fts WHERE doc_id NOT IN (SELECT doc_id FROM documents)"
    )
    orphan_chunks = conn.execute(
        "SELECT chunk_id FROM chunks WHERE version_id NOT IN (SELECT version_id FROM versions)"
    ).fetchall()
    if orphan_chunks:
        ids = [int(row[0]) for row in orphan_chunks]
        container.remove(ids)
        conn.executemany("DELETE FROM chunks WHERE chunk_id=?", [(cid,) for cid in ids])
        deleted += len(ids)
    chunk_ids = {int(row[0]) for row in conn.execute("SELECT chunk_id FROM chunks")}
    vec_ids = {int(row[0]) for row in conn.execute("SELECT chunk_id FROM kb_vec")}
    orphan_vectors = sorted(vec_ids - chunk_ids)
    if orphan_vectors:
        container.remove(orphan_vectors)
        deleted += len(orphan_vectors)
    return deleted


def status(topic: str | None = None) -> dict:
    """`kb-embed --status`：只读盘点 manifest 与容器状态。"""
    conn = connect()
    try:
        if not has_vec_table(conn):
            load_sqlite_vec(conn)
        row = conn.execute(
            """
            SELECT
              SUM(CASE WHEN model_id IS NULL OR model_id='' THEN 1 ELSE 0 END) AS pending,
              SUM(CASE WHEN model_id IS NOT NULL AND model_id!='' AND model_id!=?
                       THEN 1 ELSE 0 END) AS stale,
              SUM(CASE WHEN model_id=? THEN 1 ELSE 0 END) AS current,
              COUNT(*) AS total
            FROM chunks c
            JOIN versions v ON v.version_id=c.version_id
            JOIN documents d ON d.doc_id=v.doc_id
            WHERE (? IS NULL OR d.topic=?)
            """,
            (MODEL_ID, MODEL_ID, topic, topic),
        ).fetchone()
        counts = {key: int(row[key] or 0) for key in ("pending", "stale", "current", "total")}
        candidates = _candidate_versions(conn, topic)
        if has_vec_table(conn):
            container_vectors = int(conn.execute("SELECT COUNT(*) FROM kb_vec").fetchone()[0])
        else:
            container_vectors = None
        return {
            "model_id": MODEL_ID,
            "topic": topic,
            "scanned_versions": len(candidates),
            "chunks": counts,
            "container_vectors": container_vectors,
            "requires_reembed": counts["pending"] + counts["stale"],
            "note": "stale=model_id 不符（默认惰性保留，--reembed-stale 才重算）；"
                    "pending=从未嵌入（下次 sync 自动补）",
        }
    finally:
        conn.close()


def sync(topic: str | None = None, dry_run: bool = False, reembed_stale: bool = False,
         *, embed_fn: Callable[[list[str]], object] | None = None,
         conn=None, batch_size: int = EMBED_BATCH) -> dict:
    """按 topic 增量同步 chunks + 向量；topic=None 代表全库。"""
    started = time.perf_counter()
    own_conn = conn is None
    conn = conn or connect()
    embed = embed_fn or _default_embed_fn
    report = {
        "model_id": MODEL_ID,
        "topic": topic,
        "dry_run": dry_run,
        "reembed_stale": reembed_stale,
        "scanned_versions": 0,
        "skipped": 0,
        "updated": 0,
        "embedded": 0,
        "deleted": 0,
        "embedded_versions": 0,
        "stale": 0,
        "pending": 0,
        "errors": [],
        "seconds": 0.0,
    }
    try:
        candidates = _candidate_versions(conn, topic)
        report["scanned_versions"] = len(candidates)
        candidate_ids = {int(v["version_id"]) for v in candidates}
        scope_ids = _scope_version_ids(conn, topic)

        # 先做范围/孤儿清理（不属于 version 级事务，但单命令内自洽）
        if not dry_run:
            container = container_for(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                report["deleted"] += _cleanup_scope(conn, container, scope_ids, candidate_ids)
                report["deleted"] += _cleanup_orphans(conn, container)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        else:
            container = None

        for version in candidates:
            plan = _build_plan(conn, version, reembed_stale)
            if plan.error:
                report["errors"].append(
                    {"version_id": plan.version_id, "path": plan.path, "error": plan.error}
                )
                continue
            if plan.skipped:
                report["skipped"] += 1
                continue

            report["updated"] += 1
            report["embedded"] += len(plan.embed_specs)
            report["deleted"] += len(plan.deletes)
            if plan.embed_specs:
                report["embedded_versions"] += 1

            if dry_run:
                continue

            vectors = _embed_texts(embed, [spec["text"] for spec in plan.embed_specs],
                                   batch_size=batch_size)
            vector_map = {
                spec["key"]: vectors[index]
                for index, spec in enumerate(plan.embed_specs)
            }

            conn.execute("BEGIN IMMEDIATE")
            try:
                # 先把该 version 的旧行挪到临时负索引，避免 UNIQUE(version_id, chunk_index) 冲突
                if plan.updates or plan.deletes:
                    conn.execute(
                        f"UPDATE chunks SET chunk_index = -{_TMP_INDEX_BASE} - chunk_id "
                        "WHERE version_id=?",
                        (plan.version_id,),
                    )

                # 复用块：更新序号/文本/hash；需重嵌者同时更新 model_id/vector。
                for update in plan.updates:
                    if update["needs_embed"]:
                        conn.execute(
                            "UPDATE chunks SET chunk_index=?, text=?, content_hash=?, "
                            "model_id=?, embedded_at=? WHERE chunk_id=?",
                            (update["index"], update["text"], update["hash"], MODEL_ID,
                             now(), update["chunk_id"]),
                        )
                        container.upsert([(update["chunk_id"],
                                           vector_map[("update", update["chunk_id"])])])
                    else:
                        conn.execute(
                            "UPDATE chunks SET chunk_index=?, text=?, content_hash=? "
                            "WHERE chunk_id=?",
                            (update["index"], update["text"], update["hash"],
                             update["chunk_id"]),
                        )

                # 新增块：插入 manifest 后拿 chunk_id，再写向量。
                for insert in plan.inserts:
                    cursor = conn.execute(
                        "INSERT INTO chunks(version_id, doc_id, chunk_index, text, content_hash, "
                        "model_id, embedded_at) VALUES (?,?,?,?,?,?,?)",
                        (plan.version_id, plan.doc_id, insert["index"], insert["text"],
                         insert["hash"], MODEL_ID, now()),
                    )
                    chunk_id = int(cursor.lastrowid)
                    container.upsert([(chunk_id, vector_map[insert["vector_key"]])])

                if plan.deletes:
                    conn.executemany(
                        "DELETE FROM chunks WHERE chunk_id=?", [(cid,) for cid in plan.deletes]
                    )
                    container.remove(plan.deletes)
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        if not dry_run:
            report["seconds"] = round(time.perf_counter() - started, 3)
            report["invariants"] = assert_invariants(conn)
            # assert_invariants 只读，sync 的每个 version 事务已提交；无需再 commit。
        else:
            report["seconds"] = round(time.perf_counter() - started, 3)
            report["invariants"] = None
        # 盘点 stale/pending（报告用；不做写入）
        found = conn.execute(
            "SELECT "
            "SUM(CASE WHEN model_id IS NULL OR model_id='' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN model_id IS NOT NULL AND model_id!='' AND model_id!=? THEN 1 ELSE 0 END) "
            "FROM chunks c JOIN versions v ON v.version_id=c.version_id "
            "JOIN documents d ON d.doc_id=v.doc_id WHERE (? IS NULL OR d.topic=?)",
            (MODEL_ID, topic, topic),
        ).fetchone()
        report["pending"] = int(found[0] or 0)
        report["stale"] = int(found[1] or 0)
        return report
    finally:
        if own_conn:
            conn.close()
