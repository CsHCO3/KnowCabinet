from __future__ import annotations

import json
import sqlite3

import pytest

import backend.services.kb.tags as tags
from backend.services.kb.db import connect
from backend.services.kb.tags import _replace_frontmatter, _split_tag_value, draft_vocab


def test_split_tag_value_handles_quoted_list():
    assert _split_tag_value('"[区块链, 实验室内]"') == ["区块链", "实验室内"]
    assert _split_tag_value("v2语言论证, 实验室内") == ["v2语言论证", "实验室内"]
    assert _split_tag_value("[]") == []


def test_draft_uses_only_existing_tags(tmp_path, monkeypatch):
    db_path = tmp_path / "kb.db"
    conn = connect(db_path)
    conn.execute(
        "INSERT INTO documents(doc_id,topic,tags,status) VALUES('d','test','[\"blockchains\",\"blockchain\"]','active')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(tags, "connect", lambda: connect(db_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tags, "VOCAB_DIR", tmp_path / "vocab")
    data = draft_vocab("test", top_k=10, min_count=1)
    assert data["knowledge_candidates"] == 2
    saved = json.loads((tmp_path / "vocab" / "test.json").read_text(encoding="utf-8"))
    assert {item["canonical"] for item in saved["facets"]["knowledge"]} == {"blockchains", "blockchain"}
    suggestion = next(item for item in saved["merge_suggestions"] if item["canonical"] == "blockchain")
    assert suggestion["aliases"] == ["blockchains"]


def test_replace_frontmatter_preserves_crlf_body(tmp_path):
    path = tmp_path / "paper.md"
    body = "# Title\r\n\r\n正文段落。\r\n"
    path.write_bytes(b"\xef\xbb\xbf---\r\ntags: [old]\r\n---\r\n" + body.encode("utf-8"))
    _replace_frontmatter(path, ["new"], ["实验室内"], ["cs.CR"])
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert b"\r\r\n" not in raw
    text = raw.decode("utf-8-sig")
    assert "tags: [new]" in text and "org_marks: [实验室内]" in text
    assert text[text.index("---\r\n", 4) + 5:] == body
