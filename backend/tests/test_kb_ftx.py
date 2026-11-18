from __future__ import annotations

import sqlite3

from backend.services.kb.db import connect
from backend.services.kb.ftx import refresh_doc, tokens


def test_tokens_chinese_search_mode():
    result = tokens("判决树")
    assert "判决" in result or "判决树" in result


def test_query_must_use_write_side_tokens():
    conn = connect(":memory:")
    conn.execute("INSERT INTO documents(doc_id,title) VALUES('d1','判决树研究')")
    refresh_doc(conn, "d1", override_body="")
    assert conn.execute("SELECT COUNT(*) FROM kb_fts WHERE kb_fts MATCH ?", ("判决树",)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM kb_fts WHERE kb_fts MATCH ?", (tokens("判决树"),)).fetchone()[0] == 1
    conn.close()


def test_fts_upsert_replaces():
    conn = connect(":memory:")
    conn.execute("INSERT INTO documents(doc_id,title) VALUES('d1','first')")
    refresh_doc(conn, "d1", override_body="")
    refresh_doc(conn, "d1", override_body="")
    assert conn.execute("SELECT COUNT(*) FROM kb_fts WHERE doc_id='d1'").fetchone()[0] == 1
    conn.close()


def test_empty_query_safe():
    assert tokens("") == ""
    assert isinstance(tokens("!!!"), str)
