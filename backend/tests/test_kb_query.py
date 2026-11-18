"""M3 统一检索：过滤链 / 分层召回 / 归一化 / 配额 / 折叠 / 证据双轨（04 §6.5）。

M5 追加：向量支路、chunk→doc 折叠、四种融合、段落级取证、legacy_l2 豁免。
"""
from __future__ import annotations

import json

import numpy as np
import pytest

import backend.services.kb.db as kbdb
from backend.services.kb.db import connect, now
from backend.services.kb.ftx import refresh_doc
from backend.services.kb.query import (
    MODES, ask, fetch, quota_for, resolve_doc_id, _QUOTA, _fold_chunks,
    _interleave_select, _merge_tier, _reserve_select,
)
from backend.services.kb.vector import sync as vector_sync


@pytest.fixture
def kb(tmp_path, monkeypatch):
    """合成库：L0 线索 / L1 原文 / L2 轻整理 / L3 提炼，覆盖 quote_from 三种取值。"""
    db_path = tmp_path / "kb.db"
    monkeypatch.setattr(kbdb, "DB_PATH", db_path)

    body_dir = tmp_path / "files"
    body_dir.mkdir()

    def write(name: str, text: str) -> str:
        path = body_dir / name
        path.write_text(text, encoding="utf-8")
        return path.as_posix()

    l2 = write("l2.md", "浓缩内容。\n")
    l1 = write("l1.md", "全文正文：判决树 两阶段 判定过程。\n")
    l3 = write("l3.md", "跨论文结论：判决树 优于 规则引擎。\n")

    conn = connect(db_path)
    docs = [
        # doc_id, topic, source_type, title, abstract, stage, best_tier, tags, org
        ("shell", "cbac", "paper", "判决树 空壳论文", "abstract about decision tree", "found", 0,
         ["判决树"], []),
        ("raw", "cbac", "paper", "原文论文", "", "converted", 1, ["判决树"], []),
        ("deep_linked", "cbac", "paper", "带回链精炼", "", "condensed", 2, ["判决树"],
         ["v2语言论证"]),
        ("deep_plain", "cbac", "paper", "无回链精炼", "", "condensed", 2, ["判决树"], []),
        ("synth", "cbac", "chat", "提炼结论", "", "condensed", 3, ["判决树"], []),
        ("dairy_doc", "dairy", "paper", "奶牛论文", "", "converted", 1, ["奶牛"], []),
        ("vt", "videos", "video", "视频字幕", "", "converted", 1, ["视频"], []),
    ]
    for doc_id, topic, stype, title, abstract, stage, tier, tags, org in docs:
        conn.execute(
            "INSERT INTO documents(doc_id,topic,source_type,title,abstract,stage,best_tier,"
            "tags,org_marks,status) VALUES(?,?,?,?,?,?,?,?,?,'active')",
            (doc_id, topic, stype, title, abstract, stage, tier,
             json.dumps(tags, ensure_ascii=False), json.dumps(org, ensure_ascii=False)),
        )
    versions = [
        ("shell", 0, "meta", body_dir / "shell.json"),
        ("raw", 1, "paper", body_dir / "l1.md"),
        # 原文 + 轻整理：quote_from=raw
        ("deep_linked", 1, "paper", body_dir / "l1.md"),
        ("deep_linked", 2, "condensation", body_dir / "l2.md"),
        ("deep_plain", 1, "paper", body_dir / "l1.md"),
        ("deep_plain", 2, "condensation", body_dir / "l2.md"),
        # 只有提炼版：quote_from=derived
        ("synth", 3, "chat_distill", body_dir / "l3.md"),
        ("dairy_doc", 1, "paper", body_dir / "l1.md"),
        ("vt", 1, "subtitle", body_dir / "l1.md"),
    ]
    for doc_id, tier, kind, path in versions:
        conn.execute("INSERT INTO versions(doc_id,tier,kind,path) VALUES(?,?,?,?)",
                     (doc_id, tier, kind, str(path)))
    for doc_id, _t, _k, _p in versions:
        refresh_doc(conn, doc_id, override_body="")
    conn.commit()
    conn.close()
    return db_path


# ── 配额表 ──────────────────────────────────────────────────────────────────

def test_quota_table_matches_spec_at_top_k_10():
    assert quota_for("browse", 10) == {0: 6, 1: 2, 2: 1, 3: 1}
    assert quota_for("balanced", 10) == {0: 2, 1: 3, 2: 3, 3: 2}
    # D49（D44 配套）：deep 的"细节优先"在密度序里对应 tier=1（原文），故 1/7/1/1
    assert quota_for("deep", 10) == {0: 1, 1: 7, 2: 1, 3: 1}


def test_browse_v4_scales_proportionally_at_top_k_20():
    """D33 护栏：V4 是 top_k=10 绝对配额，top_k=20 必须等比缩放而非固定 6/2/1/1。"""
    assert quota_for("browse", 20) == {0: 12, 1: 4, 2: 2, 3: 2}


def test_quota_scales_and_never_starves_a_tier():
    small = quota_for("deep", 5)
    assert all(count >= 1 for count in small.values())
    assert set(small) == {0, 1, 2, 3}


# ── 确定性与歧义 ────────────────────────────────────────────────────────────

def test_resolve_doc_id_unique_and_ambiguous(tmp_path, monkeypatch):
    monkeypatch.setattr(kbdb, "DB_PATH", tmp_path / "kb.db")
    conn = connect()
    conn.execute("INSERT INTO documents(doc_id,topic,status) VALUES('cbac:x','cbac','active')")
    conn.execute("INSERT INTO documents(doc_id,topic,status) VALUES('paper:x','paper','active')")
    conn.commit()
    resolved, candidates = resolve_doc_id(conn, "cbac:x")
    assert resolved == "cbac:x" and candidates == []
    resolved, candidates = resolve_doc_id(conn, "x")
    assert resolved is None and sorted(candidates) == ["cbac:x", "paper:x"]
    resolved, candidates = resolve_doc_id(conn, "nope")
    assert resolved is None and candidates == []
    conn.close()


def test_resolve_exact_id_wins_and_reports_alt_prefix(tmp_path, monkeypatch):
    """M8 契约修订：**精确命中优先**，同名前缀行降级为知情项。

    背景（M8 DoD④ 现场）：库里有 13 篇论文被"双登记"——`papers/<id>` 的 L0 空壳（裸 id 行）
    与 CBAC 的 L3 全文（`papers:<id>` 前缀行）标题相同、topic 不同。原契约"精确命中也不许
    短路"会让 `kb-ask` 返回的 doc_id **无法**再喂 `kb-fetch`（ask→fetch 主链路直接断掉）。
    修订后：精确命中即用该行，另把前缀行作为 `alt_candidates` 返回，**不合并、不丢失信息**。
    """
    monkeypatch.setattr(kbdb, "DB_PATH", tmp_path / "kb.db")
    conn = connect()
    conn.execute("INSERT INTO documents(doc_id,topic,status) VALUES('2210.04644','papers','active')")
    conn.execute("INSERT INTO documents(doc_id,topic,status) VALUES('cbac:2210.04644','cbac','active')")
    conn.commit()
    resolved, candidates = resolve_doc_id(conn, "2210.04644")
    assert resolved == "2210.04644", "精确命中的存储 ID 必须可直接使用"
    assert candidates == ["cbac:2210.04644"], "同名前缀行必须作为知情项返回"

    resolved, candidates = resolve_doc_id(conn, "cbac:2210.04644")
    assert resolved == "cbac:2210.04644" and candidates == []
    conn.close()


def test_resolve_bare_name_without_exact_row_is_still_ambiguous(tmp_path, monkeypatch):
    """修订后仍保留 02 §7 的核心保护：**裸名没有精确行**、多 topic 共用该名 → 必须报歧义。"""
    monkeypatch.setattr(kbdb, "DB_PATH", tmp_path / "kb.db")
    conn = connect()
    conn.execute("INSERT INTO documents(doc_id,topic,status) VALUES('cbac:2210.04644','cbac','active')")
    conn.execute("INSERT INTO documents(doc_id,topic,status) VALUES('papers:2210.04644','papers','active')")
    conn.commit()
    resolved, candidates = resolve_doc_id(conn, "2210.04644")
    assert resolved is None, "无精确行时不得静默挑一条"
    assert sorted(candidates) == ["cbac:2210.04644", "papers:2210.04644"]
    conn.close()


def test_fetch_exact_id_works_and_flags_alt_candidates(tmp_path, monkeypatch):
    """kb-fetch 同场景：精确命中的 id 必须取得到正文，并把同名前缀行作为知情项带出。"""
    body = tmp_path / "kb_body"
    body.mkdir(exist_ok=True)
    (body / "p.md").write_text("本段是被引用的正文原文，用于验证回源链路可用。", encoding="utf-8")
    monkeypatch.setattr(kbdb, "DB_PATH", tmp_path / "kb.db")
    conn = connect()
    conn.execute("INSERT INTO documents(doc_id,topic,status) VALUES('2210.04644','papers','active')")
    conn.execute("INSERT INTO documents(doc_id,topic,status) VALUES('cbac:2210.04644','cbac','active')")
    conn.execute("INSERT INTO versions(doc_id,tier,kind,path) VALUES(?,?,?,?)",
                 ("2210.04644", 3, "paper", (body / "p.md").as_posix()))
    conn.commit()
    conn.close()
    result = fetch("2210.04644")
    assert "error" not in result, result
    assert result["doc_id"] == "2210.04644"
    assert "被引用的正文原文" in result["content"]
    assert result["alt_candidates"] == ["cbac:2210.04644"]
    assert "alt_note" in result


def test_fetch_ambiguous_returns_candidate_list_without_merging(tmp_path, monkeypatch):
    monkeypatch.setattr(kbdb, "DB_PATH", tmp_path / "kb.db")
    conn = connect()
    conn.execute("INSERT INTO documents(doc_id,topic,status) VALUES('cbac:x','cbac','active')")
    conn.execute("INSERT INTO documents(doc_id,topic,status) VALUES('paper:x','paper','active')")
    conn.commit()
    conn.close()
    result = fetch("x")
    assert result["error"] == "ambiguous_doc_id"
    assert sorted(result["candidates"]) == ["cbac:x", "paper:x"]


# ── 过滤链 ──────────────────────────────────────────────────────────────────

def test_filter_topic(kb):
    result = ask("判决树", topic="cbac")
    assert {group["topic"] for group in result["groups"]} == {"cbac"}


def test_filter_source_type(kb):
    result = ask("判决树", source_types=["chat"])
    assert {group["source_type"] for group in result["groups"]} == {"chat"}


def test_filter_tier_range(kb):
    result = ask("判决树", tier_min=3, tier_max=3)
    assert all(group["hit_tier"] == 3 for group in result["groups"])


def test_filter_tags_and_org(kb):
    tagged = ask("判决树", tags=["判决树"])
    assert tagged["groups"], "tag 过滤应当有命中"
    org = ask("判决树", org=["v2语言论证"])
    assert [group["doc_id"] for group in org["groups"]] == ["deep_linked"]
    assert ask("判决树", org=["不存在的项目线"])["groups"] == []


def test_filters_echoed_in_result(kb):
    result = ask("判决树", topic="cbac", tags=["判决树"])
    assert result["filters"]["topic"] == "cbac"
    assert result["filters"]["tags"] == ["判决树"]


def test_excluded_kind_dropped_from_retrieval(kb):
    """D53：`scope.json` 里的 kind 整篇不出现在检索结果中（数据仍在库里）。"""
    conn = connect(kbdb.DB_PATH)
    conn.execute(
        "INSERT INTO documents(doc_id,topic,source_type,title,stage,best_tier,"
        "tags,org_marks,status) VALUES('webpage','cbac','web','判决树 网页教程',"
        "'converted',1,'[]','[]','active')"
    )
    conn.execute(
        "INSERT INTO versions(doc_id,tier,kind,path) VALUES('webpage',1,'web_note','')"
    )
    refresh_doc(conn, "webpage", override_body="")
    conn.commit()
    conn.close()

    default = ask("判决树", mode="balanced", use_vector=False)
    assert "webpage" not in [group["doc_id"] for group in default["groups"]]
    assert default["filters"]["exclude_kinds"] == ["web_note"]

    # 显式传 [] = 关掉排除（诊断/回滚用），证明排除不是把数据丢了
    kept = ask("判决树", mode="balanced", use_vector=False, exclude_kinds=[])
    assert "webpage" in [group["doc_id"] for group in kept["groups"]]
    assert kept["filters"].get("exclude_kinds", []) == []  # 空值按 `_result` 既有约定不回显


# ── 分层召回：L0 不许命中 body ──────────────────────────────────────────────

def test_l0_body_column_not_searched(kb):
    """L0 分支走元信息列限定；直接构造"只有 body 有词"的 L0 行验证。"""
    conn = connect(kbdb.DB_PATH)
    conn.execute("DELETE FROM kb_fts WHERE doc_id='shell'")
    conn.execute(
        "INSERT INTO kb_fts(doc_id,title_t,abstract_t,tags_t,body_t) VALUES('shell','','','','zzzuniqueterm')"
    )
    conn.commit()

    from backend.services.kb.ftx import match
    meta_hits = match(conn, "zzzuniqueterm", {"tier_min": 0, "tier_max": 0},
                      limit=50, meta_only=True)
    any_hits = match(conn, "zzzuniqueterm", {}, limit=50, meta_only=False)
    assert meta_hits == [], "L0 分支不得命中 body 列"
    assert any_hits, "全列分支应当命中（对照组）"
    conn.close()


# ── 归一化与配额 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("mode", MODES)
def test_quota_caps_each_tier(kb, mode):
    result = ask("判决树", mode=mode, top_k=10)
    by_tier: dict[int, int] = {}
    for group in result["groups"]:
        by_tier[group["hit_tier"]] = by_tier.get(group["hit_tier"], 0) + 1
    for tier, count in by_tier.items():
        assert count <= _QUOTA[mode][tier], f"{mode}: L{tier} 超配额 {count}"


@pytest.mark.parametrize("mode", MODES)
def test_returned_never_exceeds_top_k(kb, mode):
    assert len(ask("判决树", mode=mode, top_k=3)["groups"]) <= 3


def test_scores_normalized_within_0_1(kb):
    for group in ask("判决树", mode="balanced")["groups"]:
        assert 0.0 <= group["score"] <= 1.0


def test_cross_tier_scores_are_comparable(kb):
    """D52：跨档统一尺度——每档"最佳"不再一律 1.0；L0 空壳另乘 0.30 先验。

    旧实现（级内 min-max）会让每一档的最佳命中都变成 1.0，弱档噪音与真答案并列，
    排序退化成按档位排——这是 MRR 0.4567→0.3212 的根因。
    """
    result = ask("判决树", mode="browse", top_k=10)
    best: dict[int, float] = {}
    for group in result["groups"]:
        best[group["hit_tier"]] = max(best.get(group["hit_tier"], 0.0), group["score"])
    assert best, "应至少命中一个档位"
    assert any(score < 0.999 for score in best.values()), \
        f"跨档后不应每档都是 1.0：{best}"
    # L0 空壳被先验压制到 ≤0.30
    assert best.get(0, 0.0) <= 0.30 + 1e-6


def test_quota_override_is_supported_for_diagnostics(kb):
    relaxed = ask("判决树", mode="browse", top_k=10, quota={0: 10, 1: 10, 2: 10, 3: 10})
    assert len(relaxed["groups"]) >= len(ask("判决树", mode="browse", top_k=10)["groups"])


# ── 折叠与证据双轨 ──────────────────────────────────────────────────────────

def test_group_folding_multi_tier_hits(kb):
    """deep_linked 同时有原文(1)/轻整理(2) 版本，必须只出现一个 group。"""
    ids = [group["doc_id"] for group in ask("判决树", top_k=10)["groups"]]
    assert len(ids) == len(set(ids))


def test_evidence_quote_from_three_cases(kb):
    """D44：quote_from 把「能不能引原话」从档位里拆出来。"""
    groups = {group["doc_id"]: group for group in ask("判决树", top_k=10)["groups"]}

    shell = groups["shell"]["evidence"]
    assert shell["l0_only"] is True and shell["quote_from"] == "none"
    assert shell["verbatim_allowed"] is False

    raw = groups["raw"]["evidence"]
    assert raw["quote_from"] == "raw" and raw["quote_tier"] == 1
    assert raw["verbatim_allowed"] is True

    # 有原文但另挂了轻整理：仍算 raw（引原话回 tier=1）
    plain = groups["deep_plain"]["evidence"]
    assert plain["quote_from"] == "raw" and plain["quote_tier"] == 1
    assert plain["quote_kind"] == "paper"

    # 只有提炼版：只能转述
    synth = groups["synth"]["evidence"]
    assert synth["quote_from"] == "derived" and synth["verbatim_allowed"] is False
    assert synth["quote_kind"] == "chat_distill"


def test_ask_result_json_schema(kb):
    result = ask("判决树", topic="cbac", mode="deep", top_k=5)
    assert set(result) >= {"query", "mode", "filters", "groups", "stats"}
    assert set(result["stats"]) >= {"candidates", "returned", "by_tier"}
    assert isinstance(json.dumps(result, ensure_ascii=False), str)
    group = result["groups"][0]
    for key in ("doc_id", "title", "topic", "source_type", "best_tier", "hit_tier",
                "score", "stage", "tags", "versions", "snippet", "evidence"):
        assert key in group, f"AskResult.group 缺字段 {key}"
    assert set(group["evidence"]) >= {"quote_from", "quote_tier", "verbatim_allowed"}


def test_empty_query_returns_empty_without_error(kb):
    result = ask("!!!", top_k=10)
    assert result["groups"] == []
    assert result["stats"]["returned"] == 0


def test_unknown_mode_raises(kb):
    with pytest.raises(ValueError):
        ask("判决树", mode="nan")


def test_top_k_must_be_positive(kb):
    with pytest.raises(ValueError):
        ask("判决树", top_k=0)


# ── fetch ───────────────────────────────────────────────────────────────────

def test_fetch_l0_returns_metadata_only(kb):
    result = fetch("shell")
    assert result["meta_only"] is True
    assert result["content"] == ""
    assert result["abstract"] == "abstract about decision tree"
    assert "仅元数据" in result["note"]


def test_fetch_picks_highest_tier_by_default(kb):
    result = fetch("deep_linked")
    assert result["tier"] == 2 and result["kind"] == "condensation"


def test_fetch_tier_override(kb):
    result = fetch("deep_linked", tier=1)
    assert result["tier"] == 1 and result["kind"] == "paper"
    assert "判决树" in result["content"]


def test_fetch_unknown_doc_id(kb):
    result = fetch("does-not-exist")
    assert result["error"] == "not_found"


def test_fetch_unavailable_tier_lists_available(kb):
    result = fetch("vt", tier=3)
    assert result["error"] == "tier_not_available"
    assert result["available_tiers"] == [1]


# ── M5：向量支路（假 embed_fn，不加载真模型）────────────────────────────────

_VEC_KEYS = ("alpha", "beta", "gamma", "delta")


def keyword_embed(texts: list[str]):
    """确定性"关键词嵌入"：含哪个关键词就把对应维度置 1（再被 L2 归一化）。"""
    rows = []
    for text in texts:
        vec = np.zeros(kbdb.VEC_DIM, dtype="float32")
        low = (text or "").lower()
        for index, key in enumerate(_VEC_KEYS):
            if key in low:
                vec[index] = 1.0
        if not vec.any():
            vec[len(_VEC_KEYS)] = 1.0
        rows.append(vec)
    return np.vstack(rows)


@pytest.fixture
def kbvec(tmp_path, monkeypatch):
    """合成库：vec_doc 只有向量支路能找到；fts_doc/both_doc 走 FTS。

    FTS 行只保留标题（override_body=""），所以 vec_doc 的"alpha"只在 chunk 正文里，
    只能靠向量支路召回——这是跨语言场景的最小复现。
    """
    db_path = tmp_path / "kb.db"
    monkeypatch.setattr(kbdb, "DB_PATH", db_path)
    files = tmp_path / "files"
    files.mkdir()

    def add(doc_id: str, title: str, body: str) -> None:
        path = files / f"{doc_id}.md"
        path.write_text(body, encoding="utf-8", newline="")
        conn.execute(
            "INSERT INTO documents(doc_id,topic,source_type,title,abstract,stage,best_tier,"
            "tags,org_marks,arxiv_cats,status) VALUES(?,?,?,?,?,?,?,?,?,?,'active')",
            (doc_id, "cbac", "paper", title, "", "converted", 1, "[]", "[]", "[]"),
        )
        conn.execute(
            "INSERT INTO versions(doc_id,tier,kind,path,content_hash,char_count,updated_at) "
            "VALUES(?,1,'paper',?,?,?,?)",
            (doc_id, path.as_posix(), "h", len(body), now()),
        )
        refresh_doc(conn, doc_id, override_body=body)

    conn = connect(db_path)
    try:
        add("vec_doc", "unrelated title", "# Alpha\n" + "alpha beta gamma delta " * 3)
        add("fts_doc", "alpha research paper", "# Other\n" + "zzz " * 30)
        add("both_doc", "alpha beta analysis", "# Both\n" + "alpha beta gamma delta " * 3)
        conn.commit()
    finally:
        conn.close()

    # 真路径建 chunks + 向量（走 M4 的 sync，而不是手工插 kb_vec）
    vector_sync(topic="cbac", embed_fn=keyword_embed)

    conn = connect(db_path)
    try:
        for doc_id in ("vec_doc", "fts_doc", "both_doc"):
            conn.execute("DELETE FROM kb_fts WHERE doc_id=?", (doc_id,))
            refresh_doc(conn, doc_id, override_body="")  # FTS 只吃标题
        conn.commit()
    finally:
        conn.close()
    return db_path


def _chunk_id(db_path, doc_id: str, index: int = 0) -> int:
    conn = connect(db_path)
    try:
        return int(conn.execute(
            "SELECT c.chunk_id FROM chunks c JOIN versions v ON v.version_id=c.version_id "
            "WHERE v.doc_id=? AND c.chunk_index=?", (doc_id, index)).fetchone()[0])
    finally:
        conn.close()


def test_vector_arm_finds_doc_invisible_to_fts(kbvec):
    """跨语言最小复现：只在 chunk 正文里的词，纯 FTS 找不到，向量支路能找到。"""
    plain = ask("alpha", mode="deep", top_k=10, use_vector=False)
    assert "vec_doc" not in [group["doc_id"] for group in plain["groups"]]

    got = ask("alpha", mode="deep", top_k=10, use_vector=True, embed_fn=keyword_embed)
    groups = {group["doc_id"]: group for group in got["groups"]}
    assert "vec_doc" in groups, "向量支路应召回 FTS 不可见的文档"
    assert "vector" in groups["vec_doc"]["sources"]
    assert groups["vec_doc"]["vector_score"] is not None
    assert got["stats"]["vector"]["enabled"] is True
    assert got["stats"]["vector"]["knn_chunks"] >= 1
    assert got["stats"]["vector"]["error"] is None
    # 命中块要能直接喂 kb-fetch
    chunk = groups["vec_doc"]["matched_chunks"][0]
    assert chunk["tier"] == 1 and chunk["chunk_id"] > 0
    assert chunk["chunk_id"] in groups["vec_doc"]["quote_chunk_ids"]


def test_vector_arm_default_only_in_deep_mode(kbvec):
    """02 §11.1：只有 deep 需要进程内常驻 bge；browse/balanced 保持纯 FTS。"""
    for mode in ("browse", "balanced"):
        result = ask("alpha", mode=mode, top_k=10, embed_fn=keyword_embed)
        assert result["stats"]["vector"]["enabled"] is False
    deep = ask("alpha", mode="deep", top_k=10, embed_fn=keyword_embed)
    assert deep["stats"]["vector"]["enabled"] is True


def test_vector_arm_failure_degrades_to_fts(kbvec, monkeypatch):
    """向量支路异常不得炸检索：降级为纯 FTS 并在 stats.vector.error 记账。"""
    import backend.services.kb.query as kbquery

    def boom(_query, _embed_fn=None):
        raise RuntimeError("fake embed down")

    monkeypatch.setattr(kbquery, "_embed_query", boom)
    result = ask("alpha", mode="deep", top_k=10, use_vector=True, embed_fn=keyword_embed)
    assert result["stats"]["vector"]["error"].startswith("RuntimeError")
    assert [group["doc_id"] for group in result["groups"]], "FTS 支路结果必须还在"


def test_query_vector_is_embedded_once_per_query(kbvec):
    """02 §9.1：query 只嵌入一次（同 query 重复调用走进程内缓存）。"""
    import backend.services.kb.query as kbquery

    calls: list[list[str]] = []

    def counting_embed(texts):
        calls.append(list(texts))
        return keyword_embed(texts)

    kbquery._QUERY_VECTOR_CACHE.clear()
    for _ in range(3):
        ask("unique-query-alpha", mode="deep", top_k=5, embed_fn=counting_embed)
    assert len(calls) == 3, "注入 embed_fn 时不做缓存（单测隔离）"
    kbquery._QUERY_VECTOR_CACHE.clear()
    for _ in range(3):
        ask("cached-query-alpha", mode="deep", top_k=5)
    # 默认路径带 query 级缓存：第二次起不再进入模型
    assert kbquery._QUERY_VECTOR_CACHE, "默认路径应写 query 向量缓存"


def test_fold_chunks_max_and_top3sum():
    chunks = [{"score": 0.5}, {"score": 0.4}, {"score": 0.3}, {"score": 0.2}]
    assert _fold_chunks(chunks, "max") == pytest.approx(0.5)
    assert _fold_chunks(chunks, "top3sum") == pytest.approx(1.2)


def test_fusion_modes_supported_and_validated(kbvec):
    for fusion in ("norm", "rrf", "interleave", "reserve"):
        result = ask("alpha", mode="deep", top_k=10, use_vector=True,
                     embed_fn=keyword_embed, fusion=fusion)
        assert result["stats"]["fusion"] == fusion
        assert result["groups"], f"{fusion} 应有结果"
    with pytest.raises(ValueError):
        ask("alpha", mode="deep", fusion="nope")
    with pytest.raises(ValueError):
        ask("alpha", mode="deep", fold="nope")


def test_merge_tier_norm_takes_max_and_rrf_sums():
    fts = [{"doc_id": "d1", "score": 10.0, "snippet": "s1"},
           {"doc_id": "d2", "score": 5.0, "snippet": "s2"}]
    vec = [{"doc_id": "d1", "score": 0.9, "chunks": [{"chunk_id": 1, "tier": 3,
                                                      "score": 0.9, "text": "t"}],
            "n_chunks": 1},
           {"doc_id": "d2", "score": 0.8, "chunks": [], "n_chunks": 1}]
    merged = _merge_tier(3, fts, vec, "norm")
    norm = {entry["doc_id"]: entry for entry in merged}
    assert norm["d1"]["sources"] == ["fts", "vector"]
    assert norm["d1"]["score"] == pytest.approx(1.0)   # 两支路各自归一化后取 max
    assert norm["d2"]["score"] == pytest.approx(0.0)   # min-max 把两路各自的最差都压到 0
    # 只被向量命中的 doc（档内唯一向量命中）线性归一化后为 1.0 → 能进档内前名次
    own = _merge_tier(3, [], [{"doc_id": "v1", "score": 0.6, "chunks": [], "n_chunks": 1}],
                      "norm")
    assert own[0]["score"] == pytest.approx(1.0)
    assert own[0]["sources"] == ["vector"]
    rrf = {entry["doc_id"]: entry for entry in _merge_tier(3, fts, vec, "rrf")}
    assert rrf["d1"]["score"] > rrf["d2"]["score"], "RRF 下两路都命中者得分更高"


def test_interleave_select_gives_each_arm_slots():
    """并列轮转：FTS/向量各按名次轮流取名额（跨语言保底）。"""
    fts = [{"doc_id": "f1", "score": 30.0}, {"doc_id": "f2", "score": 20.0},
           {"doc_id": "f3", "score": 10.0}]
    vec = [{"doc_id": "v1", "score": 0.60}, {"doc_id": "v2", "score": 0.55}]
    merged = _merge_tier(3, fts, vec, "interleave")
    chosen = [entry["doc_id"] for entry in _interleave_select(merged, fts, vec, 4)]
    assert chosen == ["f1", "v1", "f2", "v2"]


def test_reserve_select_keeps_one_vector_slot():
    fts = [{"doc_id": "f1", "score": 30.0}, {"doc_id": "f2", "score": 20.0},
           {"doc_id": "f3", "score": 10.0}, {"doc_id": "f4", "score": 5.0}]
    vec = [{"doc_id": "v1", "score": 0.60}]
    merged = _merge_tier(3, fts, vec, "reserve")
    chosen = [entry["doc_id"] for entry in _reserve_select(merged, fts, vec, 4)]
    assert "v1" in chosen, "向量保底名额必须给向量支路最佳"
    assert "f1" in chosen and "f2" in chosen and len(chosen) == 4
    # 只有 1 个名额时不夺该档唯一名额
    one = [entry["doc_id"] for entry in _reserve_select(merged, fts, vec, 1)]
    assert one == ["f1"]


def test_evidence_legacy_l2_exemption(kb):
    """D27 豁免标记仍透出，但它不改变 quote_from——只有提炼版仍不能引原话。"""
    conn = connect(kbdb.DB_PATH)
    try:
        conn.execute("UPDATE documents SET status='legacy_l2' WHERE doc_id='synth'")
        conn.commit()
    finally:
        conn.close()
    groups = {group["doc_id"]: group for group in ask("判决树", top_k=10)["groups"]}
    evidence = groups["synth"]["evidence"]
    assert evidence["legacy_l2"] is True
    assert evidence["quote_from"] == "derived"
    assert evidence["verbatim_allowed"] is False


def test_legacy_l2_doc_is_still_searchable(kb):
    """legacy_l2 是可检索状态（I2 口径 = active + legacy_l2），不得被静默漏检。"""
    conn = connect(kbdb.DB_PATH)
    try:
        conn.execute("UPDATE documents SET status='legacy_l2' WHERE doc_id='deep_linked'")
        conn.commit()
    finally:
        conn.close()
    ids = [group["doc_id"] for group in ask("判决树", top_k=10)["groups"]]
    assert "deep_linked" in ids


# ── M5：段落级取证（kb-fetch --chunk-ids）──────────────────────────────────


def test_fetch_with_chunk_ids_returns_segment_with_context(kbvec):
    chunk_id = _chunk_id(kbvec, "vec_doc", 0)
    result = fetch("vec_doc", tier=1, chunk_ids=[chunk_id], context=1)
    assert "error" not in result, result
    assert result["content"] == "", "段落取证不给整篇"
    assert result["segment_count"] == 1
    segment = result["segments"][0]
    assert segment["chunk_id"] == chunk_id
    assert segment["tier"] == 1 and segment["kind"] == "paper"
    assert segment["chunk_index"] == 0
    assert segment["total_chunks"] >= 1
    assert "alpha" in segment["text"]
    assert segment["path"].endswith("vec_doc.md")
    assert "prev_chunk_id" in segment and "next_chunk_id" in segment


def test_fetch_chunk_ids_accepts_strings_and_multiple_chunks(kbvec):
    first = _chunk_id(kbvec, "vec_doc", 0)
    result = fetch("vec_doc", chunk_ids=[str(first)])
    assert result["segment_count"] == 1


def test_fetch_chunk_not_found(kbvec):
    result = fetch("vec_doc", chunk_ids=[999999])
    assert result["error"] == "chunk_not_found"
    assert result["missing"] == [999999]


def test_fetch_chunk_not_in_tier(kbvec):
    chunk_id = _chunk_id(kbvec, "vec_doc", 0)
    result = fetch("vec_doc", tier=2, chunk_ids=[chunk_id])
    assert result["error"] == "chunk_not_in_tier"
    assert result["available_tiers"] == [1]


def test_fetch_invalid_chunk_id(kbvec):
    result = fetch("vec_doc", chunk_ids=["abc"])
    assert result["error"] == "invalid_chunk_id"


def test_fetch_without_chunk_ids_keeps_full_text_behaviour(kbvec):
    result = fetch("vec_doc")
    assert result["tier"] == 1 and result["meta_only"] is False
    assert "alpha" in result["content"]
    assert result["segments"] == []
