from __future__ import annotations

import importlib.util
import sqlite3

from backend.services.kb.db import connect


EXPECTED_TABLES = {"meta", "documents", "versions", "vocab", "batches", "batch_items",
                   "kb_fts", "chunks"}


def test_init_creates_schema_and_version(tmp_path):
    conn = connect(tmp_path / "kb.db")
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert EXPECTED_TABLES <= tables
    assert "kb_vec" in tables if importlib.util.find_spec("sqlite_vec") else True
    assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "2"
    columns = {row[1] for row in conn.execute("PRAGMA table_info(documents)")}
    assert "arxiv_cats" in columns
    conn.close()


def test_migrate_v1_adds_chunks_and_arxiv_cats(tmp_path):
    """04 §6.1：手工构造 v1 库，connect 必须自动补 chunks/arxiv_cats/kb_vec 并升版本。"""
    path = tmp_path / "kb.db"
    raw = sqlite3.connect(path)
    raw.executescript(
        """
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta VALUES('schema_version','1');
        CREATE TABLE documents(
          doc_id TEXT PRIMARY KEY,
          topic TEXT NOT NULL DEFAULT '_inbox',
          source_type TEXT NOT NULL DEFAULT '',
          title TEXT NOT NULL DEFAULT '',
          abstract TEXT NOT NULL DEFAULT '',
          authors TEXT NOT NULL DEFAULT '',
          year TEXT NOT NULL DEFAULT '',
          venue TEXT NOT NULL DEFAULT '',
          citations INTEGER,
          doi TEXT NOT NULL DEFAULT '',
          source_uri TEXT NOT NULL DEFAULT '',
          stage TEXT NOT NULL DEFAULT 'found',
          best_tier INTEGER NOT NULL DEFAULT 0,
          tags TEXT NOT NULL DEFAULT '[]',
          org_marks TEXT NOT NULL DEFAULT '[]',
          status TEXT NOT NULL DEFAULT 'active',
          first_seen_at TEXT,
          updated_at TEXT
        );
        CREATE TABLE versions(
          version_id INTEGER PRIMARY KEY,
          doc_id TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
          tier INTEGER NOT NULL,
          kind TEXT NOT NULL,
          path TEXT NOT NULL,
          content_hash TEXT,
          char_count INTEGER,
          updated_at TEXT,
          UNIQUE(doc_id, kind)
        );
        CREATE VIRTUAL TABLE kb_fts USING fts5(
          doc_id UNINDEXED, title_t, abstract_t, tags_t, body_t, tokenize='unicode61');
        """
    )
    raw.commit()
    raw.close()

    conn = connect(path)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "chunks" in tables
        assert "kb_vec" in tables
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "2"
        columns = {row[1] for row in conn.execute("PRAGMA table_info(documents)")}
        assert "arxiv_cats" in columns
        assert conn.execute("SELECT arxiv_cats FROM documents LIMIT 1").fetchone() is None
    finally:
        conn.close()


def test_fk_cascade_delete_doc(tmp_path):
    conn = connect(tmp_path / "kb.db")
    conn.execute(
        "INSERT INTO documents(doc_id,topic) VALUES('doc-1','cbac')"
    )
    cursor = conn.execute(
        "INSERT INTO versions(doc_id,tier,kind,path) VALUES('doc-1',3,'paper','x.md')"
    )
    version_id = cursor.lastrowid
    conn.execute(
        "INSERT INTO chunks(version_id,doc_id,chunk_index,text,content_hash) "
        "VALUES(?,?,0,'hello','h1')", (version_id, "doc-1"),
    )
    conn.commit()
    conn.execute("DELETE FROM documents WHERE doc_id='doc-1'")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM versions WHERE doc_id='doc-1'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM chunks WHERE doc_id='doc-1'").fetchone()[0] == 0
    conn.close()


def test_wal_and_busy_timeout(tmp_path):
    conn = connect(tmp_path / "kb.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    conn.close()


def test_repeated_connect_is_idempotent(tmp_path):
    path = tmp_path / "kb.db"
    first = connect(path)
    first.close()
    second = connect(path)
    assert second.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    assert second.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "2"
    second.close()
