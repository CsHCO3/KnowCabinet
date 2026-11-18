"""D55：交叉编码器重排（注入打分函数，不加载真实模型）。"""
from __future__ import annotations

import pytest

from backend.services.kb import query as query_mod
from backend.services.kb import rerank


def test_rerank_pool_is_three_times_top_k():
    assert rerank.RERANK_POOL == 3


def test_disabled_by_env(monkeypatch):
    monkeypatch.setenv("KB_RERANK", "0")
    assert rerank.available() is False


def test_rerank_groups_sorts_desc_and_truncates():
    groups = [{"doc_id": f"d{i}", "title": f"t{i}", "snippet": ""} for i in range(5)]
    picked, stats = rerank.rerank_groups(
        "q", groups, 2, scorer=lambda q, texts: [0.1, 0.9, 0.5, 0.7, 0.3])
    assert [g["doc_id"] for g in picked] == ["d1", "d3"]
    assert picked[0]["rerank_score"] == 0.9
    assert stats["enabled"] is True and stats["pool"] == 5 and stats["returned"] == 2


def test_rerank_groups_falls_back_when_scorer_unavailable():
    groups = [{"doc_id": f"d{i}", "title": f"t{i}", "snippet": ""} for i in range(5)]
    picked, stats = rerank.rerank_groups("q", groups, 2, scorer=lambda q, texts: None)
    assert [g["doc_id"] for g in picked] == ["d0", "d1"], "不可用时保持原顺序截断"
    assert stats["enabled"] is False and stats["error"]


def test_rerank_groups_tolerates_wrong_length():
    groups = [{"doc_id": "a"}, {"doc_id": "b"}]
    picked, stats = rerank.rerank_groups("q", groups, 1, scorer=lambda q, texts: [1.0])
    assert [g["doc_id"] for g in picked] == ["a"]
    assert stats["enabled"] is False


def test_doc_text_uses_title_and_snippet():
    text = rerank.doc_text({"title": "标题", "snippet": "片段"})
    assert text == "标题 片段"
    assert len(rerank.doc_text({"title": "x" * 5000, "snippet": "y" * 5000})) <= rerank.MAX_DOC_CHARS


# ── 与 ask() 的接线 ────────────────────────────────────────────────────────

@pytest.fixture
def kb(tmp_path, monkeypatch):
    """最小语料：4 篇同主题文档，足够观测排序变化。"""
    from backend.services.kb import db as kbdb
    from backend.services.kb.db import connect
    from backend.services.kb.ftx import refresh_doc

    db_path = tmp_path / "kb.db"
    monkeypatch.setattr(kbdb, "DB_PATH", db_path)
    conn = connect(db_path)
    for i in range(6):
        conn.execute(
            "INSERT INTO documents(doc_id,topic,source_type,title,stage,best_tier,"
            "tags,org_marks,status) VALUES(?,?,?,?,?,?,?,?,'active')",
            (f"d{i}", "cbac", "paper", f"判决树 论文 {i}", "converted", 1, "[]", "[]"),
        )
        conn.execute("INSERT INTO versions(doc_id,tier,kind,path) VALUES(?,1,'paper','')",
                     (f"d{i}",))
        refresh_doc(conn, f"d{i}", override_body="")
    conn.commit()
    conn.close()
    return db_path


ALL_TIER1 = {0: 0, 1: 20, 2: 0, 3: 0}


def test_ask_uses_pool_and_records_rerank_stats(kb, monkeypatch):
    monkeypatch.setenv("KB_RERANK", "1")
    pool = query_mod.ask("判决树", mode="balanced", top_k=2, use_vector=False,
                         quota=ALL_TIER1, rerank=False)
    assert pool["stats"]["rerank"]["enabled"] is False
    assert pool["stats"]["rerank"]["pool"] == 2
    assert len(pool["groups"]) == 2

    # 注入"越靠后分越高"的打分：重排后顺序应整体反转
    result = query_mod.ask(
        "判决树", mode="balanced", top_k=2, use_vector=False, quota=ALL_TIER1,
        rerank=True,
        rerank_scorer=lambda q, texts: [float(i) for i in range(len(texts))],
    )
    stats = result["stats"]["rerank"]
    assert stats["enabled"] is True
    assert stats["pool"] == 6, "召回池应为 top_k×3"
    assert len(result["groups"]) == 2
    assert all("rerank_score" in group for group in result["groups"])
    assert result["groups"][0]["rerank_score"] > result["groups"][1]["rerank_score"]
    pool_ids = [g["doc_id"] for g in pool["groups"]]
    assert result["groups"][0]["doc_id"] not in pool_ids, "池放大后应能捞到原本 top_k 外的文档"


def test_ask_rerank_false_keeps_original_behaviour(kb, monkeypatch):
    monkeypatch.setenv("KB_RERANK", "1")
    off = query_mod.ask("判决树", mode="balanced", top_k=2, use_vector=False,
                        quota=ALL_TIER1, rerank=False)
    assert off["stats"]["rerank"]["enabled"] is False
    assert off["stats"]["rerank"]["pool"] == 2
    assert "rerank_score" not in off["groups"][0]
