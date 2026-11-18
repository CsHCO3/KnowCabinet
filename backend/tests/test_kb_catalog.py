from __future__ import annotations

import itertools

import pytest

from backend.services.kb.catalog import (
    UnknownKindError, _split_org_marks, _write_document, derive_stage, derive_tier,
)
from backend.services.kb.db import connect


@pytest.mark.parametrize(
    "condense,translate,md,ocr,pdf,expected",
    [
        (c, t, m, o, p, (
            "condensed" if c else "translated" if t else
            "converted" if (m or o) else "downloaded" if p else "found"
        ))
        for c, t, m, o, p in itertools.product([True, False], repeat=5)
    ],
)
def test_derive_stage_matrix(condense, translate, md, ocr, pdf, expected):
    assert derive_stage({
        "condense": condense, "translate": translate, "md": md,
        "ocr": ocr, "pdf": pdf,
    }) == expected


@pytest.mark.parametrize("kind,tier", [
    # D44 信息密度序：0=线索 1=原文 2=轻整理 3=提炼
    ("meta", 0),
    ("paper", 1), ("cn_paper", 1), ("paper_ocr", 1), ("cn_paper_ocr", 1),
    ("chat_raw", 1), ("原文留存", 1), ("subtitle", 1), ("note", 1), ("web_note", 1),
    ("condensation", 2),
    ("chat_distill", 3), ("tieba_distill", 3), ("analysis", 3), ("synthesis", 3),
])
def test_derive_tier_by_kind(kind, tier):
    assert derive_tier(kind) == tier


def test_derive_tier_unknown_kind_raises():
    """D45：未登记的 kind 必须报错，禁止静默兜底成 L1。"""
    with pytest.raises(UnknownKindError):
        derive_tier("index")          # 管理类文件已改为不入库
    with pytest.raises(UnknownKindError):
        derive_tier("summary")        # 视频三遍法已改名为 analysis
    with pytest.raises(UnknownKindError):
        derive_tier("guide_some_random_filename")


def test_upsert_preserves_nonempty_fields():
    conn = connect(":memory:")
    _write_document(
        conn, "d1", topic="cbac", source_type="paper", title="T", abstract="A",
        authors="Alice", year="2020", venue="V", citations=3, doi="10.1/x",
        source_uri="https://example", stage="converted", tags=["decision"],
    )
    _write_document(
        conn, "d1", topic="", source_type="", title="", abstract="", authors="",
        year="", venue="", citations=None, doi="", source_uri="", stage="found",
        tags=[],
    )
    row = conn.execute("SELECT * FROM documents WHERE doc_id='d1'").fetchone()
    assert row["title"] == "T" and row["abstract"] == "A"
    assert row["authors"] == "Alice" and row["stage"] == "converted"
    conn.close()


def test_split_arxiv_cats_from_org_marks():
    org, cats = _split_org_marks(["实验室内", "cs.CR", "v2语言论证", "cs.DC", "math.LO"])
    assert org == ["实验室内", "v2语言论证"]
    assert cats == ["cs.CR", "cs.DC", "math.LO"]


def test_write_document_persists_arxiv_cats():
    conn = connect(":memory:")
    _write_document(
        conn, "d2", topic="cbac", source_type="paper", title="T", abstract="",
        authors="", year="", venue="", citations=None, doi="", source_uri="",
        stage="converted", tags=[], org_marks=["实验室内"], arxiv_cats=["cs.CR"],
    )
    row = conn.execute("SELECT org_marks, arxiv_cats FROM documents WHERE doc_id='d2'").fetchone()
    assert row["org_marks"] == '["实验室内"]'
    assert row["arxiv_cats"] == '["cs.CR"]'
    conn.close()


def test_write_document_applies_frozen_legacy_l2(monkeypatch):
    """D27 冻结名单：入库/重建时按 knowledgebase/_meta/config/legacy_l2.json 复现 legacy_l2 状态。

    这条是"kb-rebuild 后豁免不丢"的单测守门人（真库上另有 test_real_legacy_l2_dense_docs）。
    """
    import json  # noqa: F401  （名单真相是 JSON 文件，不是硬编码列表）

    from backend.services.kb import catalog

    monkeypatch.setattr(catalog, "legacy_l2_ids", lambda: frozenset({"2210.04644"}))

    conn = connect(":memory:")
    for doc_id in ("2210.04644", "10.1109_tc.2010.274"):
        _write_document(
            conn, doc_id, topic="cbac", source_type="paper", title=doc_id, abstract="",
            authors="", year="", venue="", citations=None, doi="", source_uri="",
            stage="converted", tags=[],
        )
    statuses = {
        row["doc_id"]: row["status"]
        for row in conn.execute("SELECT doc_id, status FROM documents")
    }
    assert statuses["2210.04644"] == "legacy_l2"
    assert statuses["10.1109_tc.2010.274"] == "active"

    # 重扫（upsert 同一条）不得把豁免状态冲回 active
    _write_document(
        conn, "2210.04644", topic="cbac", source_type="paper", title="new", abstract="",
        authors="", year="", venue="", citations=None, doi="", source_uri="",
        stage="converted", tags=[],
    )
    row = conn.execute("SELECT status, title FROM documents WHERE doc_id='2210.04644'").fetchone()
    assert row["status"] == "legacy_l2" and row["title"] == "new"
    conn.close()
