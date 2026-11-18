"""M3 评测与 FTS 查询侧（04 §6.3 查询部分 / §6.7）。"""
from __future__ import annotations

import json

import pytest

import backend.services.kb.db as kbdb
from backend.services.kb import evalx
from backend.services.kb.db import connect
from backend.services.kb.ftx import (
    build_match, delete_doc, match, refresh_doc, scope_to_meta_columns, tokens,
)

# ── build_match：必须走写入端同一套分词，且天然抗注入 ──────────────────────


def test_build_match_uses_tokens_not_raw_string():
    """M1 复核 F1 实证：原始连续串命中 0，分词后才命中。"""
    expr = build_match("判决树")
    assert expr is not None
    assert "判决" in expr, f"未走分词: {expr}"
    assert build_match("") is None
    assert build_match("   ") is None
    assert build_match("!!!") is None, "纯符号必须返回 None，避免 MATCH 语法报错"


def test_build_match_multi_token_has_phrase_and_terms():
    expr = build_match("smart contract access control")
    assert expr.startswith('"')
    assert " OR " in expr


@pytest.mark.parametrize("query", [
    '"', "NOT", "(", ")", "a AND (b)", "NEAR(", "OR OR", 'he said "hi"', "*",
    "判决树 NOT (x)", "a-b_c", "%", "^",
])
def test_build_match_injection_safe(query):
    """04 §11：引号/NOT/括号等注入式查询不得抛错。"""
    expr = build_match(query)
    conn = connect(":memory:")
    refresh_doc(conn, "d")  # 无该行 → 仅建索引位，不插入
    conn.execute("INSERT INTO documents(doc_id,title) VALUES('d1','abc 判决树')")
    refresh_doc(conn, "d1", override_body="")
    conn.commit()
    if expr is not None:
        match(conn, query, limit=10)  # 不得抛 sqlite3.OperationalError
    conn.close()


def test_scope_to_meta_columns_wraps_expression():
    assert scope_to_meta_columns("x") == "{title_t abstract_t tags_t} : (x)"


# ── 列权重 / 两字中文 / 删除 ────────────────────────────────────────────────


def _seed(conn):
    conn.execute("INSERT INTO documents(doc_id,title,abstract,status) "
                 "VALUES('title_hit','判决树 研究','','active')")
    conn.execute("INSERT INTO documents(doc_id,title,abstract,status) "
                 "VALUES('body_only','其他题目','','active')")
    conn.commit()
    refresh_doc(conn, "title_hit", override_body="")
    refresh_doc(conn, "body_only", override_body="这里也讨论 判决树 但是只在正文出现")


def test_fts_column_weight_title_first():
    conn = connect(":memory:")
    _seed(conn)
    hits = match(conn, "判决树", limit=10)
    ids = [hit["doc_id"] for hit in hits]
    assert ids.index("title_hit") < ids.index("body_only"), "标题命中必须排在仅正文命中之前"
    conn.close()


def test_fts_two_char_query_hit():
    conn = connect(":memory:")
    _seed(conn)
    hits = match(conn, "判决", limit=10)
    # jieba 主路把「判决」切成一个词，命中 title_t/body_t 里的「判决」
    assert "title_hit" in [hit["doc_id"] for hit in hits], "两字中文词必须能命中"
    conn.close()


def test_fts_delete_doc():
    conn = connect(":memory:")
    _seed(conn)
    assert match(conn, "判决树", limit=10)
    delete_doc(conn, "title_hit")
    assert "title_hit" not in [hit["doc_id"] for hit in match(conn, "判决树", limit=10)]
    conn.close()


def test_fts_snippet_is_readable():
    conn = connect(":memory:")
    _seed(conn)
    hits = match(conn, "判决树", limit=10)
    assert any(hit["snippet"] for hit in hits)
    conn.close()


# ── evalx 指标 ──────────────────────────────────────────────────────────────


def test_recall_at_k_hand_computed():
    assert evalx.recall_at_k(["a", "b", "c"], ["a", "c", "d"], 10) == pytest.approx(2 / 3)
    assert evalx.recall_at_k(["a"], ["b"], 10) == 0.0
    assert evalx.recall_at_k(["a"], ["a"], 10) == 1.0
    assert evalx.recall_at_k(["a", "b"], ["a", "b"], 1) == pytest.approx(0.5)


def test_reciprocal_rank_hand_computed():
    assert evalx.reciprocal_rank(["x", "a"], ["a"], 10) == pytest.approx(0.5)
    assert evalx.reciprocal_rank(["a"], ["a"], 10) == 1.0
    assert evalx.reciprocal_rank(["x"], ["a"], 10) == 0.0


def test_recall_ignores_topic_prefix():
    """存储 ID 带 topic 前缀时，与旧系统裸 doc_id 仍能对齐。"""
    assert evalx.recall_at_k(["cbac:2210.04644"], ["2210.04644"], 10) == 1.0
    assert evalx.natural_id("cbac:2210.04644") == "2210.04644"
    assert evalx.natural_id("2210.04644") == "2210.04644"


def test_natural_id_keeps_dunder_ids():
    assert evalx.natural_id("__weird") == "__weird"


def test_load_set_rejects_bad_line(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text('{"id":"e1","query":"x"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="expect_doc_ids"):
        evalx.load_set(path)


def test_load_set_rejects_empty(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_text("# only comment\n", encoding="utf-8")
    with pytest.raises(ValueError, match="评测集为空"):
        evalx.load_set(path)


def test_load_set_skips_blank_and_comment_lines(tmp_path):
    path = tmp_path / "ok.jsonl"
    path.write_text('{"id":"e1","query":"x","expect_doc_ids":["a"]}\n\n', encoding="utf-8")
    assert len(evalx.load_set(path)) == 1


def test_evaluate_set_quality_counts_l0_and_issues():
    quality = evalx.evaluate_set_quality([
        {"id": "e1", "query": "x", "expect_doc_ids": ["a"], "mode": "browse", "tier_expect": "L0"},
        {"id": "e2", "query": "y", "expect_doc_ids": [], "mode": "nope"},
    ])
    assert quality["n"] == 2 and quality["l0_cases"] == 1
    assert any("非法 mode" in issue for issue in quality["issues"])
    assert any("为空" in issue for issue in quality["issues"])


def test_baseline_uses_search_all(tmp_path, monkeypatch):
    """基线必须调用旧服务 search_all，且结果缓存可复用。"""
    monkeypatch.setattr(evalx, "BASELINE_CACHE", tmp_path / "baseline.json")
    calls = []

    class _FakeResult(dict):
        pass

    import backend.services.ragsearch.service as old_service
    monkeypatch.setattr(old_service, "search_all",
                        lambda query, top_k=10, **kw: (calls.append(query),
                                                       [{"doc_id": "d1"}, {"doc_id": "d2"}])[1])
    entries = [{"id": "e1", "query": "q1", "expect_doc_ids": ["d1"]}]
    got = evalx.collect_baseline(entries, top_k=10, refresh=True)
    assert got == {"q1": ["d1", "d2"]}
    assert calls == ["q1"]

    # 第二次：缓存命中该 query，不应再调用旧服务
    got2 = evalx.collect_baseline(entries, top_k=10)
    assert got2 == {"q1": ["d1", "d2"]}
    assert calls == ["q1"], "缓存命中时不得重复调用旧服务"


def test_collect_baseline_only_fetches_missing_queries(tmp_path, monkeypatch):
    monkeypatch.setattr(evalx, "BASELINE_CACHE", tmp_path / "baseline.json")
    calls = []
    import backend.services.ragsearch.service as old_service
    monkeypatch.setattr(old_service, "search_all",
                        lambda query, top_k=10, **kw: (calls.append(query), [])[1])
    evalx.collect_baseline([{"id": "e1", "query": "q1", "expect_doc_ids": ["d"]}], refresh=True)
    evalx.collect_baseline([
        {"id": "e1", "query": "q1", "expect_doc_ids": ["d"]},
        {"id": "e2", "query": "q2", "expect_doc_ids": ["d"]},
    ])
    assert calls == ["q1", "q2"], "只应补采缺失 query"


def test_baseline_failure_is_isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(evalx, "BASELINE_CACHE", tmp_path / "baseline.json")
    import backend.services.ragsearch.service as old_service

    def boom(query, top_k=10, **kw):
        raise RuntimeError("index broken")

    monkeypatch.setattr(old_service, "search_all", boom)
    got = evalx.collect_baseline([{"id": "e1", "query": "q1", "expect_doc_ids": ["d"]}],
                                 refresh=True)
    assert got == {"q1": []}, "基线失败不应抛穿，应记为 0 命中"


def test_render_report_contains_pass_flag():
    report = {
        "n": 1, "top_k": 10, "recall@10": 0.5, "mrr@10": 0.5, "pass": True,
        "baseline": {"recall@10": 0.1, "mrr@10": 0.1},
        "l0_cases": {"n": 1, "new_hit": 1, "baseline_hit": 0},
        "regressions": [], "diagnostics": {"relaxed_quota_recall@10": 0.6},
        "per_query": [{"id": "e1", "mode": "browse", "query": "q", "new_recall": 0.5,
                       "baseline_recall": 0.1, "relaxed_recall": 0.6, "missing": []}],
    }
    text = evalx.render_report(report)
    assert "诊断" in text and "L0 反例 1/1" in text and "判定: PASS" in text
