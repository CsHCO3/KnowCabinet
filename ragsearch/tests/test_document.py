from ragsearch.engine.document import (
    DocEntry, SearchResult, doc_id_from_path, extract_title, normalize_meta,
    parse_frontmatter, parse_tags,
)

RUNOOB = """---
title: 向量数据库
section: AI Agent(智能体) 教程
source: https://www.runoob.com/ai-agent/vector-database.html
---

# 向量数据库

本文介绍向量数据库。"""

NEWAPI = """# 功能指南概述

正文没有 frontmatter。"""


def test_parse_frontmatter_runoob():
    meta, body = parse_frontmatter(RUNOOB)
    assert meta["title"] == "向量数据库"
    assert meta["section"] == "AI Agent(智能体) 教程"
    assert "---" not in body
    assert extract_title(body, meta, "x.md") == "向量数据库"


def test_parse_frontmatter_no_frontmatter():
    meta, body = parse_frontmatter(NEWAPI)
    assert meta == {}
    assert body == NEWAPI
    assert extract_title(body, meta, "x.md") == "功能指南概述"


def test_title_fallback_to_filename():
    meta, body = parse_frontmatter("无标题正文\n内容")
    assert extract_title(body, meta, "fallback.md") == "fallback.md"


def test_parse_tags_formats():
    assert parse_tags("[共识, 轻量级]") == ["共识", "轻量级"]
    assert parse_tags("共识, 轻量级") == ["共识", "轻量级"]
    assert parse_tags("共识 轻量级") == ["共识", "轻量级"]
    assert parse_tags("单标签") == ["单标签"]
    assert parse_tags(None) == []
    assert parse_tags("") == []


def test_normalize_meta():
    meta = {"title": "T", "source": "S", "authors": "张三, 李四",
            "year": "2022", "section": "区块链", "tags": "[a, b]", "source_type": "paper"}
    m = normalize_meta(meta)
    assert m["title"] == "T"
    assert m["authors"] == "张三, 李四"
    assert m["year"] == "2022"
    assert m["tags"] == ["a", "b"]
    assert m["source_type"] == "paper"
    empty = normalize_meta({})
    assert empty["tags"] == [] and empty["year"] == "" and empty["id"] == ""
    assert empty["source_type"] == ""


def test_doc_id_from_path():
    assert doc_id_from_path("2210.04644/paper.md") == "2210.04644"
    assert doc_id_from_path("sub/dir/paper.md") == "sub"
    assert doc_id_from_path("guide_foo.md") == "guide_foo"
    assert doc_id_from_path("2210.04644/paper.md", meta_id="custom") == "custom"
    # Windows 反斜杠
    assert doc_id_from_path(r"2210.04644\paper.md") == "2210.04644"


def test_docentry_fields():
    d = DocEntry(filename="2210.04644/paper.md", doc_id="2210.04644", title="t",
                 section="s", source="u", authors="a", year="2022", tags=["x"],
                 source_type="paper", body="b", char_count=1)
    assert d.title == "t" and d.doc_id == "2210.04644" and d.year == "2022"
    assert d.source_type == "paper"


def test_searchresult_defaults():
    r = SearchResult(filename="f.md", doc_id="d", title="t", snippet="s",
                     score=1.0, rank=1)
    assert r.tags == [] and r.year == "" and r.source == ""
    assert r.source_type == ""
