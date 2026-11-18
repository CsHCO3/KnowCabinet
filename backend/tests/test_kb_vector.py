"""M4 增量向量单测（04 §6.6）：注入假 embed_fn，四边界 + 容器 CRUD + I9 对账。"""
from __future__ import annotations

import hashlib
import json

import numpy as np
import pytest

import backend.services.kb.db as kbdb
import backend.services.kb.vector as kbvector
from backend.services.kb.db import connect, has_vec_table, now
from backend.services.kb.ftx import refresh_doc
from backend.services.kb.invariants import assert_invariants
from backend.services.kb.vector import MODEL_ID, container_for, sync


class FakeEmbed:
    """确定性假嵌入：按文本 hash 播种；可模拟第 N 次调用中断。"""

    def __init__(self, fail_on_call: int | None = None):
        self.calls: list[list[str]] = []
        self.fail_on_call = fail_on_call

    def __call__(self, texts: list[str]):
        self.calls.append(list(texts))
        if self.fail_on_call is not None and len(self.calls) == self.fail_on_call:
            raise RuntimeError("simulated kill before writing this version")
        vectors = []
        for text in texts:
            seed = int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16)
            rng = np.random.default_rng(seed)
            vectors.append(rng.normal(size=kbdb.VEC_DIM).astype("float32"))
        return np.vstack(vectors)


def _hash16(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _add_doc(conn, tmp_path, doc_id: str, topic: str, tier: int, kind: str,
             filename: str, body: str) -> str:
    path = tmp_path / "files" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8", newline="")
    conn.execute(
        "INSERT INTO documents(doc_id,topic,source_type,title,stage,best_tier,tags,"
        "org_marks,arxiv_cats,status) VALUES(?,?,?,?,?,?,?,?,?,'active')",
        (doc_id, topic, "paper", doc_id, "converted", tier, "[]", "[]", "[]"),
    )
    cursor = conn.execute(
        "INSERT INTO versions(doc_id,tier,kind,path,content_hash,char_count,updated_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (doc_id, tier, kind, path.as_posix(), _hash16(path.read_bytes()),
         len(body), now()),
    )
    refresh_doc(conn, doc_id, override_body=body)
    return path.as_posix()


@pytest.fixture
def kb(tmp_path, monkeypatch):
    """合成库：d1(L3 3块) / d2(L2 3块) / dairy(L1 1块) / meta(L0)。"""
    db_path = tmp_path / "kb.db"
    monkeypatch.setattr(kbdb, "DB_PATH", db_path)
    conn = connect(db_path)
    try:
        _add_doc(conn, tmp_path, "d1", "cbac", 3, "paper", "p1.md",
                 "# Alpha\n" + "A" * 60 + "\n# Beta\n" + "B" * 60 +
                 "\n# Gamma\n" + "C" * 60 + "\n")
        _add_doc(conn, tmp_path, "d2", "cbac", 2, "condensation", "p2.md",
                 "# Only\n" + "D" * 60 + "\n")
        _add_doc(conn, tmp_path, "dairy1", "dairy", 1, "summary", "p3.md",
                 "# Dairy\n" + "E" * 60 + "\n")
        _add_doc(conn, tmp_path, "m1", "cbac", 0, "meta", "m1.json",
                 json.dumps({"title": "meta only"}))
        conn.commit()
    finally:
        conn.close()
    return {"db_path": db_path, "tmp_path": tmp_path}


def _open(kb):
    return connect(kb["db_path"])


def _counts(conn):
    chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    embedded = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE model_id IS NOT NULL").fetchone()[0]
    vectors = conn.execute("SELECT COUNT(*) FROM kb_vec").fetchone()[0]
    return chunks, embedded, vectors


# ── 四边界 + 基础回归 ───────────────────────────────────────────────────────


def test_sync_skip_all_unchanged(kb):
    first = sync(topic="cbac", embed_fn=FakeEmbed())
    assert first["embedded"] > 0
    conn = _open(kb)
    try:
        before = {row[0] for row in conn.execute("SELECT chunk_id FROM chunks")}
        assert assert_invariants(conn)["container_vectors"] == first["embedded"]
    finally:
        conn.close()

    second_fake = FakeEmbed()
    second = sync(topic="cbac", embed_fn=second_fake)
    assert second["embedded"] == 0
    assert second["deleted"] == 0
    assert second["skipped"] == second["scanned_versions"] == 2
    assert second_fake.calls == [], "零改重跑不得调用 embed_fn"
    conn = _open(kb)
    try:
        after = {row[0] for row in conn.execute("SELECT chunk_id FROM chunks")}
        assert after == before
    finally:
        conn.close()


def test_edit_one_section_not_whole_doc(kb):
    sync(topic="cbac", embed_fn=FakeEmbed())
    conn = _open(kb)
    try:
        old_ids = {row[0] for row in conn.execute(
            "SELECT chunk_id FROM chunks c JOIN versions v ON v.version_id=c.version_id "
            "WHERE v.doc_id='d1'")}
        chunk_count = len(old_ids)
    finally:
        conn.close()
    assert chunk_count == 3

    path = kb["tmp_path"] / "files" / "p1.md"
    content = path.read_text(encoding="utf-8")
    path.write_text(content.replace("B" * 60, "B" * 59 + "x"), encoding="utf-8", newline="")

    fake = FakeEmbed()
    report = sync(topic="cbac", embed_fn=fake)
    assert report["embedded"] == 1, f"只改一段应只重嵌 1 块，实际 {report['embedded']}"
    assert len(fake.calls) == 1 and len(fake.calls[0]) == 1
    conn = _open(kb)
    try:
        new_ids = {row[0] for row in conn.execute(
            "SELECT chunk_id FROM chunks c JOIN versions v ON v.version_id=c.version_id "
            "WHERE v.doc_id='d1'")}
        assert len(new_ids) == 3
        assert len(old_ids & new_ids) == 2, "A/C 两块应复用旧 chunk_id 与向量"
        assert _counts(conn)[1] == _counts(conn)[2]
    finally:
        conn.close()


def test_delete_doc_removes_vectors(kb):
    sync(topic="cbac", embed_fn=FakeEmbed())
    conn = _open(kb)
    try:
        doc1_ids = {row[0] for row in conn.execute(
            "SELECT chunk_id FROM chunks WHERE doc_id='d1'")}
        assert doc1_ids
        conn.execute("DELETE FROM documents WHERE doc_id='d1'")
        conn.commit()
    finally:
        conn.close()

    report = sync(topic="cbac", embed_fn=FakeEmbed())
    assert report["deleted"] >= len(doc1_ids)
    conn = _open(kb)
    try:
        assert conn.execute("SELECT COUNT(*) FROM chunks WHERE doc_id='d1'").fetchone()[0] == 0
        vec_ids = {row[0] for row in conn.execute("SELECT chunk_id FROM kb_vec")}
        assert not (doc1_ids & vec_ids), "级联删文档后向量不得残留"
        assert _counts(conn)[1] == _counts(conn)[2]
    finally:
        conn.close()


def test_rename_keeps_embedding(kb):
    sync(topic="cbac", embed_fn=FakeEmbed())
    old = kb["tmp_path"] / "files" / "p1.md"
    new = kb["tmp_path"] / "files" / "p1_renamed.md"
    new.write_text(old.read_text(encoding="utf-8"), encoding="utf-8", newline="")
    old.unlink()

    conn = _open(kb)
    try:
        before = {row[0] for row in conn.execute(
            "SELECT chunk_id FROM chunks c JOIN versions v ON v.version_id=c.version_id "
            "WHERE v.doc_id='d1'")}
        conn.execute("UPDATE versions SET path=? WHERE doc_id='d1' AND kind='paper'",
                     (new.as_posix(),))
        conn.commit()
    finally:
        conn.close()

    fake = FakeEmbed()
    report = sync(topic="cbac", embed_fn=fake)
    assert report["embedded"] == 0
    assert fake.calls == []
    conn = _open(kb)
    try:
        after = {row[0] for row in conn.execute(
            "SELECT chunk_id FROM chunks c JOIN versions v ON v.version_id=c.version_id "
            "WHERE v.doc_id='d1'")}
        assert after == before
        assert conn.execute(
            "SELECT path FROM versions WHERE doc_id='d1' AND kind='paper'").fetchone()[0].endswith(
            "p1_renamed.md")
    finally:
        conn.close()


def test_model_id_expiry_lazy(kb):
    sync(topic="cbac", embed_fn=FakeEmbed())
    conn = _open(kb)
    try:
        conn.execute("UPDATE chunks SET model_id='old-model-512'")
        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    finally:
        conn.close()

    lazy = sync(topic="cbac", embed_fn=FakeEmbed())
    assert lazy["embedded"] == 0
    assert lazy["stale"] == total
    conn = _open(kb)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE model_id=?", (MODEL_ID,)).fetchone()[0] == 0
    finally:
        conn.close()

    refreshed = sync(topic="cbac", reembed_stale=True, embed_fn=FakeEmbed())
    assert refreshed["embedded"] == total
    conn = _open(kb)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE model_id=?", (MODEL_ID,)).fetchone()[0] == total
        assert _counts(conn)[1] == _counts(conn)[2]
    finally:
        conn.close()


def test_interrupt_then_rerun_consistent(kb):
    """模拟进程在第 2 个 version 嵌入前被 kill：第 1 个已提交，第 2 个回滚。"""
    fake = FakeEmbed(fail_on_call=2)
    with pytest.raises(RuntimeError):
        sync(topic="cbac", embed_fn=fake)
    conn = _open(kb)
    try:
        partial_chunks, partial_embedded, partial_vectors = _counts(conn)
        assert partial_chunks > 0, "第一个 version 应已提交"
        assert partial_embedded == partial_vectors
        assert_invariants(conn)
    finally:
        conn.close()

    report = sync(topic="cbac", embed_fn=FakeEmbed())
    assert report["embedded"] > 0, "中断点之后的 version 应在重跑时补上"
    conn = _open(kb)
    try:
        assert _counts(conn)[1] == _counts(conn)[2]
        assert_invariants(conn)
    finally:
        conn.close()


def test_meta_kind_never_embedded(kb):
    report = sync(topic="cbac", embed_fn=FakeEmbed())
    conn = _open(kb)
    try:
        assert all(v["kind"] != "meta"
                   for v in kbvector._candidate_versions(conn, "cbac"))
        assert conn.execute(
            "SELECT COUNT(*) FROM chunks c JOIN versions v ON v.version_id=c.version_id "
            "WHERE v.kind='meta'").fetchone()[0] == 0
        assert_invariants(conn)
    finally:
        conn.close()
    assert report["scanned_versions"] == 2


def test_non_meta_always_embedded(kb, monkeypatch):
    """D44：嵌入门槛只看"有没有正文"——tier=1 原文照样嵌入，meta 永不嵌入。

    旧口径用 topics.json 的 embed_l1 决定 tier=1 是否嵌入；密度序下 tier=1 是原文，
    若沿用旧门槛会把两万多个原文块全部踢出向量库，故改为非 meta 全嵌。
    """
    monkeypatch.setattr(kbvector, "embed_l1_enabled", lambda topic: False)
    report = sync(topic="dairy", embed_fn=FakeEmbed())
    assert report["embedded"] == 1
    conn = _open(kb)
    try:
        assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM chunks c JOIN versions v ON v.version_id=c.version_id "
            "WHERE v.tier=0").fetchone()[0] == 0
    finally:
        conn.close()


def test_dry_run_writes_nothing(kb):
    fake = FakeEmbed()
    report = sync(topic="cbac", dry_run=True, embed_fn=fake)
    assert report["embedded"] > 0
    assert fake.calls == [], "dry-run 不得调用嵌入"
    conn = _open(kb)
    try:
        assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM kb_vec").fetchone()[0] == 0
    finally:
        conn.close()


def test_container_impl_crud(tmp_path):
    conn = connect(tmp_path / "vec.db")
    try:
        container = container_for(conn)
        container.upsert([
            (1, [1.0, 0.0, 0.0] + [0.0] * (kbdb.VEC_DIM - 3)),
            (2, [0.0, 1.0, 0.0] + [0.0] * (kbdb.VEC_DIM - 3)),
            (3, [0.7, 0.7, 0.0] + [0.0] * (kbdb.VEC_DIM - 3)),
        ])
        conn.commit()
        assert container.count() == 3
        hits = container.knn([1.0, 0.0, 0.0] + [0.0] * (kbdb.VEC_DIM - 3), 3)
        assert [hit["chunk_id"] for hit in hits] == [1, 3, 2]
        container.remove([2])
        conn.commit()
        assert container.count() == 2
        assert 2 not in {row["chunk_id"] for row in container.knn(
            [0.0, 1.0, 0.0] + [0.0] * (kbdb.VEC_DIM - 3), 2)}
    finally:
        conn.close()


def test_status_reports_pending_and_stale(kb):
    sync(topic="cbac", embed_fn=FakeEmbed())
    conn = _open(kb)
    try:
        conn.execute("UPDATE chunks SET model_id='old-model-512'")
        conn.execute(
            "UPDATE chunks SET model_id=NULL WHERE chunk_id=(SELECT MIN(chunk_id) FROM chunks)")
        conn.commit()
    finally:
        conn.close()
    data = kbvector.status(topic="cbac")
    assert data["chunks"]["pending"] == 1
    assert data["chunks"]["stale"] >= 1
    assert data["requires_reembed"] == data["chunks"]["pending"] + data["chunks"]["stale"]
    assert data["container_vectors"] is not None


def test_sync_keeps_legacy_l2_docs(kb):
    """D27 回归：legacy_l2 仍是可检索状态 → 向量候选/清理范围必须包含它。

    M5 实施中曾出现 `_candidate_versions` 写死 status='active'，导致 legacy_l2 文档
    被当成"不再可嵌入"，sync 会把它整篇的 chunks+向量当孤儿删掉（含 5 条 dense）。
    """
    first = sync(topic="cbac", embed_fn=FakeEmbed())
    assert first["embedded"] > 0
    conn = _open(kb)
    try:
        conn.execute("UPDATE documents SET status='legacy_l2' WHERE doc_id='d1'")
        conn.commit()
        before = conn.execute(
            "SELECT COUNT(*) FROM chunks c JOIN versions v ON v.version_id=c.version_id "
            "WHERE v.doc_id='d1'").fetchone()[0]
    finally:
        conn.close()
    assert before > 0

    report = sync(topic="cbac", embed_fn=FakeEmbed())
    conn = _open(kb)
    try:
        after = conn.execute(
            "SELECT COUNT(*) FROM chunks c JOIN versions v ON v.version_id=c.version_id "
            "WHERE v.doc_id='d1'").fetchone()[0]
        assert assert_invariants(conn)["container_vectors"] == first["embedded"]
    finally:
        conn.close()
    assert after == before, "legacy_l2 文档的 chunk/向量不得被 sync 当孤儿清掉"
    assert report["deleted"] == 0
