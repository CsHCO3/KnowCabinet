from __future__ import annotations

import json

import pytest

from backend.services.kb.db import connect
from backend.services.kb.invariants import assert_invariants


def test_assert_invariants_passes_on_valid_fixture():
    conn = connect(":memory:")
    conn.execute("INSERT INTO documents(doc_id,best_tier,tags,status) VALUES('d',3,'[]','active')")
    conn.execute("INSERT INTO versions(doc_id,tier,kind,path) VALUES('d',3,'paper','x.md')")
    conn.execute(
        "INSERT INTO kb_fts(doc_id,title_t,abstract_t,tags_t,body_t) VALUES('d','','','','')"
    )
    assert assert_invariants(conn)["active_documents"] == 1
    conn.close()


def test_assert_invariants_catches_best_tier_and_orphan():
    conn = connect(":memory:")
    conn.execute("INSERT INTO documents(doc_id,best_tier,tags,status) VALUES('d',1,'[]','active')")
    conn.execute("INSERT INTO versions(doc_id,tier,kind,path) VALUES('d',3,'paper','x.md')")
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("INSERT INTO versions(doc_id,tier,kind,path) VALUES('orphan',3,'paper','x.md')")
    conn.execute(
        "INSERT INTO kb_fts(doc_id,title_t,abstract_t,tags_t,body_t) VALUES('d','','','','')"
    )
    with pytest.raises(RuntimeError, match="I4|I3"):
        assert_invariants(conn)
    conn.close()


def test_i2_counts_legacy_l2_as_searchable():
    """M5/D27：legacy_l2 是可检索状态，I2 口径 = active + legacy_l2。"""
    conn = connect(":memory:")
    conn.execute("INSERT INTO documents(doc_id,best_tier,tags,status) VALUES('a',3,'[]','active')")
    conn.execute(
        "INSERT INTO documents(doc_id,best_tier,tags,status) VALUES('l',2,'[]','legacy_l2')"
    )
    conn.execute("INSERT INTO versions(doc_id,tier,kind,path) VALUES('a',3,'paper','x.md')")
    conn.execute(
        "INSERT INTO versions(doc_id,tier,kind,path) VALUES('l',2,'condensation','y.md')"
    )
    for doc_id in ("a", "l"):
        conn.execute(
            "INSERT INTO kb_fts(doc_id,title_t,abstract_t,tags_t,body_t) "
            "VALUES(?,?,'','','')", (doc_id, doc_id))
    report = assert_invariants(conn)
    assert report["active_documents"] == 2, "legacy_l2 必须计入可检索文档"
    assert report["legacy_l2_documents"] == 1
    conn.close()


def test_i2_catches_missing_legacy_l2_fts_row():
    """legacy_l2 文档漏了 FTS 行 → I2 必须报（防静默漏检）。"""
    conn = connect(":memory:")
    conn.execute(
        "INSERT INTO documents(doc_id,best_tier,tags,status) VALUES('l',2,'[]','legacy_l2')"
    )
    conn.execute(
        "INSERT INTO versions(doc_id,tier,kind,path) VALUES('l',2,'condensation','y.md')"
    )
    with pytest.raises(RuntimeError, match="I2"):
        assert_invariants(conn)
    conn.close()


def test_deprecated_and_excluded_stay_out_of_searchable_count():
    conn = connect(":memory:")
    conn.execute("INSERT INTO documents(doc_id,best_tier,tags,status) VALUES('a',3,'[]','active')")
    conn.execute("INSERT INTO versions(doc_id,tier,kind,path) VALUES('a',3,'paper','x.md')")
    conn.execute(
        "INSERT INTO kb_fts(doc_id,title_t,abstract_t,tags_t,body_t) VALUES('a','','','','')"
    )
    conn.execute(
        "INSERT INTO documents(doc_id,best_tier,tags,status) VALUES('d',3,'[]','deprecated')"
    )
    conn.execute("INSERT INTO versions(doc_id,tier,kind,path) VALUES('d',3,'paper','y.md')")
    report = assert_invariants(conn)
    assert report["active_documents"] == 1
    conn.close()
