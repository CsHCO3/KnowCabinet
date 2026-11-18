"""M6 批次（候选区）生命周期单测：open / add / confirm / list + 写纪律。"""
from __future__ import annotations

import json
import sqlite3

import pytest

import backend.services.kb.db as kbdb
from backend.services.kb.batch import add_items, confirm, get, lifecycle, list_batches, open_batch

TOPIC = "t6"


@pytest.fixture
def iso(tmp_path, monkeypatch):
    """合成库：3 条文档 + 一张词表；所有写都落 tmp_path，不碰真库。"""
    db_path = tmp_path / "kb.db"
    monkeypatch.setattr(kbdb, "DB_PATH", db_path)
    conn = kbdb.connect(db_path)
    try:
        for doc_id, tier in (("d1", 1), ("d2", 2), ("d3", 3)):
            conn.execute(
                "INSERT INTO documents(doc_id,topic,title,stage,best_tier,tags,org_marks,status) "
                "VALUES(?,?,?,?,?,?,?,'active')",
                (doc_id, TOPIC, f"文档 {doc_id}", "converted", tier, "[]", "[]"),
            )
            conn.execute(
                "INSERT INTO versions(doc_id,tier,kind,path) VALUES(?,?,'paper',?)",
                (doc_id, tier, str(tmp_path / f"{doc_id}.md")),
            )
            path = tmp_path / f"{doc_id}.md"
            path.write_text(f"# {doc_id}\n\n正文。\n", encoding="utf-8", newline="")
        conn.commit()
    finally:
        conn.close()
    vocab = tmp_path / "vocab"
    vocab.mkdir()
    (vocab / f"{TOPIC}.json").write_text(json.dumps({
        "topic": TOPIC, "status": "confirmed",
        "facets": {"knowledge": [{"canonical": "判决树", "aliases": ["判定树"], "selected": True}],
                   "org": [], "process": [], "arxiv": []},
    }, ensure_ascii=False), encoding="utf-8")
    from backend.services.kb import tags as kbtags

    monkeypatch.setattr(kbtags, "VOCAB_DIR", vocab)
    return db_path


def rows(db_path, sql, args=()):
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(sql, args)
        columns = [item[0] for item in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        conn.close()


# ── open / add ───────────────────────────────────────────────────────────────


def test_open_is_idempotent_and_autogenerates_id(iso):
    first = open_batch(None, TOPIC)
    assert first["status"] == "open" and first["round_id"].startswith("r-")
    again = open_batch(first["round_id"], "别的主题")
    assert again["round_id"] == first["round_id"]
    assert again["topic"] == TOPIC  # 已存在不覆盖
    assert again["item_count"] == 0


def test_add_rejects_unknown_doc_and_accepts_known(iso):
    rid = open_batch(None, TOPIC)["round_id"]
    out = add_items(rid, ["d1", "d2", "ghost"])
    assert out["added"] == ["d1", "d2"]
    assert out["unknown_doc"] == ["ghost"]
    assert out["item_count"] == 2
    assert rows(iso, "SELECT COUNT(*) AS n FROM batch_items WHERE round_id=?", (rid,))[0]["n"] == 2


def test_add_auto_opens_batch_and_dedupes(iso):
    out = add_items("r-auto", ["d1", "d1"])
    assert out["round_id"] == "r-auto" and out["added"] == ["d1"]
    assert out["item_count"] == 1


def test_add_empty_doc_ids_is_error(iso):
    rid = open_batch(None, TOPIC)["round_id"]
    out = add_items(rid, [])
    assert out["error"] == "invalid_argument"


# ── confirm ──────────────────────────────────────────────────────────────────


def test_confirm_lifecycle_writes_topic_and_canonical_tags(iso):
    rid = open_batch(None, TOPIC)["round_id"]
    add_items(rid, ["d1", "d2"])
    out = confirm(rid, TOPIC, ["判定树"])          # alias → canonical
    assert out["status"] == "confirmed"
    assert out["topic_applied"] == TOPIC
    assert out["tags_applied"] == ["判定树"]
    assert out["written_docs"] == 2
    assert sorted(out["tag_report"]["updated"]) == ["d1", "d2"]
    tags = {row["doc_id"]: json.loads(row["tags"])
            for row in rows(iso, "SELECT doc_id, tags FROM documents WHERE doc_id LIKE 'd%'")}
    assert tags["d1"] == ["判决树"] and tags["d2"] == ["判决树"]
    assert out["confirmed_at"]


def test_confirm_is_rejected_when_already_confirmed(iso):
    rid = open_batch(None, TOPIC)["round_id"]
    add_items(rid, ["d1"])
    confirm(rid, TOPIC, ["判决树"])
    again = confirm(rid, TOPIC, ["判决树"])
    assert again["error"] == "already_confirmed"
    assert again["confirmed_at"]


def test_confirm_empty_round_is_rejected(iso):
    rid = open_batch(None, TOPIC)["round_id"]
    out = confirm(rid, TOPIC, ["判决树"])
    assert out["error"] == "empty_round"


def test_confirm_unknown_round_and_blank_topic(iso):
    assert confirm("r-nope", TOPIC, [])["error"] == "unknown_round"
    rid = open_batch(None, TOPIC)["round_id"]
    add_items(rid, ["d1"])
    assert confirm(rid, "   ", ["判决树"])["error"] == "invalid_argument"


def test_confirm_with_no_tags_still_writes_topic(iso):
    rid = open_batch(None, TOPIC)["round_id"]
    add_items(rid, ["d1"])
    out = confirm(rid, "新主题", [])
    assert out["status"] == "confirmed"
    assert rows(iso, "SELECT topic FROM documents WHERE doc_id='d1'")[0]["topic"] == "新主题"


# ── list / lifecycle ─────────────────────────────────────────────────────────


def test_list_batches_counts_by_status(iso):
    a = open_batch(None, TOPIC)["round_id"]
    b = open_batch(None, TOPIC)["round_id"]
    add_items(a, ["d1"])
    confirm(a, TOPIC, [])
    data = list_batches()
    assert data["count"] == 2
    assert data["by_status"] == {"confirmed": 1, "open": 1}
    listed = {item["round_id"]: item for item in data["items"]}
    assert listed[a]["item_count"] == 1 and listed[b]["item_count"] == 0
    assert list_batches("open")["count"] == 1


def test_lifecycle_auto_opens_adds_and_confirms(iso):
    out = lifecycle(None, TOPIC, ["判定树"], doc_ids=["d1", "d2"], action="auto")
    assert out["status"] == "confirmed"
    assert [step["step"] for step in out["steps"]] == ["open", "add"]
    assert out["written_docs"] == 2


def test_lifecycle_rejects_bad_action(iso):
    out = lifecycle("r1", TOPIC, [], action="explode")
    assert out["error"] == "invalid_argument"


def test_lifecycle_list_action(iso):
    open_batch(None, TOPIC)
    out = lifecycle(None, TOPIC, [], action="list")
    assert out["kind"] == "batches" and out["count"] == 1


def test_get_unknown_round(iso):
    assert get("r-nope")["error"] == "unknown_round"


def test_confirm_syncs_fts_so_new_tag_is_searchable(iso):
    """D12 的实证：confirm 之后按新 tag 必须能检索到（FTS 同步）。"""
    from backend.services.kb.ftx import match as fts_match

    rid = open_batch(None, TOPIC)["round_id"]
    add_items(rid, ["d1"])
    confirm(rid, TOPIC, ["判决树"])
    conn = kbdb.connect(iso)
    try:
        hits = [row["doc_id"] for row in fts_match(conn, "判决树")]
    finally:
        conn.close()
    assert "d1" in hits
