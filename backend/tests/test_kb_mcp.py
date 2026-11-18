"""M6 MCP server 单测：六工具契约、结构化错误语义、写操作只落 kb.db。

不启子进程（真 stdio 的端到端在 `knowledgebase/_meta/reports/acceptance/M6.log`）；这里用 mcp 2.x 的
`Client(server)` 内存传输，覆盖同一批 handler 逻辑。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

import backend.services.kb.db as kbdb
import backend.services.kb.ftx as kbftx
import backend.services.kb.inventory as kbinv
import backend.services.kb.promote as kbpromo
import backend.services.kb.query as kbquery
import backend.services.kb.tags as kbtags
from backend.services.kb.mcp_server import TOOL_NAMES, tool_schemas

EXPECTED_PARAMS = {
    "kb_ask": ["query", "topic", "tags", "org", "mode", "top_k"],
    "kb_inventory": ["topic", "tag"],
    "kb_fetch": ["doc_id", "tier", "chunk_ids"],
    "kb_tag": ["doc_ids", "add", "remove"],
    "kb_promote": ["doc_ids", "to_tier"],
    "kb_batch_confirm": ["round_id", "topic", "tags", "doc_ids", "action"],
}


# ── 契约（不需要库）─────────────────────────────────────────────────────────


def test_six_tools_with_expected_params():
    schemas = tool_schemas()
    assert [item["name"] for item in schemas] == list(TOOL_NAMES)
    assert len(schemas) == 6
    for item in schemas:
        assert item["parameters"] == EXPECTED_PARAMS[item["name"]]
        assert item["param_count"] <= 6, "04 §8-M6：每 tool 参数 ≤6"


def test_module_is_runnable_entrypoint():
    """`python -m backend.services.kb.mcp_server` 必须可达 main()。"""
    import backend.services.kb.mcp_server as server

    assert callable(server.main)
    assert server.SERVER_NAME == "kb"
    assert callable(server.get_mcp)


def test_inprocess_client_lists_six_tools():
    from mcp import Client
    from mcp.server.mcpserver import MCPServer

    import backend.services.kb.mcp_server as server

    srv = MCPServer(server.SERVER_NAME, version=server.SERVER_VERSION)
    server._register_tools(srv)

    async def go():
        async with Client(srv) as client:
            return await client.list_tools()

    listing = asyncio.run(go())
    names = [tool.name for tool in listing.tools]
    assert names == list(TOOL_NAMES)
    for tool in listing.tools:
        props = list((tool.input_schema or {}).get("properties") or {})
        assert props == EXPECTED_PARAMS[tool.name]
        assert tool.description, f"{tool.name} 需要 description"


# ── 隔离库 + 六个 tool 的真实行为 ───────────────────────────────────────────


@pytest.fixture
def env(tmp_path, monkeypatch):
    """合成 kb.db（FTS + versions + 回链/无回链 L2）；所有写落 tmp_path。"""
    db_path = tmp_path / "kb.db"
    monkeypatch.setattr(kbdb, "DB_PATH", db_path)
    monkeypatch.setattr(kbtags, "connect", lambda: kbdb.connect(db_path))
    monkeypatch.setattr(kbinv, "connect", lambda: kbdb.connect(db_path))
    monkeypatch.setattr(kbquery, "connect", lambda: kbdb.connect(db_path))
    monkeypatch.setattr(kbpromo, "connect", lambda: kbdb.connect(db_path))

    files = tmp_path / "files"
    files.mkdir()
    l3 = files / "t1_l3.md"
    l3.write_text("# 判决树两阶段判定\n\n我们先判定主体资格，再判定权限范围。\n",
                  encoding="utf-8", newline="")
    linked_l2 = files / "t2_l2.md"
    linked_l2.write_text(f"浓缩结论：两阶段更稳。\n[L1:{l3.as_posix()}#c0]\n",
                         encoding="utf-8", newline="")
    plain_l2 = files / "t3_l2.md"
    plain_l2.write_text("浓缩结论：没有回链。\n", encoding="utf-8", newline="")
    synth_t3 = files / "t2_t3.md"
    synth_t3.write_text("跨论文提炼：两阶段判定优于单阶段。\n", encoding="utf-8", newline="")
    meta_path = files / "t4_meta.md"

    docs = [
        ("t1", "cbac", "paper", "判决树 两阶段判定 完整版", 1, ["判决树"], "active"),
        ("t2", "cbac", "paper", "判决树 精炼稿（带回链）", 3, ["判决树"], "active"),
        ("t3", "cbac", "paper", "判决树 精炼稿（无回链）", 2, ["判决树"], "active"),
        ("t4", "cbac", "paper", "只有元数据的论文", 0, ["判决树"], "active"),
    ]
    conn = kbdb.connect(db_path)
    try:
        for doc_id, topic, source_type, title, tier, tags, status in docs:
            conn.execute(
                "INSERT INTO documents(doc_id,topic,source_type,title,abstract,stage,best_tier,"
                "tags,org_marks,status,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'active',?)",
                (doc_id, topic, source_type, title, f"{title} 的摘要", "condensed" if tier else "found",
                 tier, json.dumps(tags, ensure_ascii=False), "[]", "2026-10-15T00:00:00Z"),
            )
            conn.execute("UPDATE documents SET status=? WHERE doc_id=?", (status, doc_id))
        versions = [
            ("t1", 1, "paper", str(l3), "h1"),
            ("t2", 1, "paper", str(l3), "h1"),
            ("t2", 2, "condensation", str(linked_l2), "h2"),
            ("t2", 3, "synthesis", str(synth_t3), "h3"),
            ("t3", 2, "condensation", str(plain_l2), "h3"),
            ("t4", 0, "meta", str(meta_path), None),
        ]
        for doc_id, tier, kind, path, digest in versions:
            conn.execute(
                "INSERT INTO versions(doc_id,tier,kind,path,content_hash,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (doc_id, tier, kind, path, digest, "2026-10-15T00:00:00Z"),
            )
        conn.commit()
    finally:
        conn.close()

    for doc_id in ("t1", "t2", "t3", "t4"):
        conn = kbdb.connect(db_path)
        try:
            kbftx.refresh_doc(conn, doc_id)
            conn.commit()
        finally:
            conn.close()

    monkeypatch.setattr(kbquery, "_default_embed_fn",
                        lambda texts: (_ for _ in ()).throw(AssertionError("单测不加载 bge")))
    return db_path


def call(tool: str, **args) -> dict:
    """直接调 handler（等价于 MCP 调用，少一层传输），解析 JSON。"""
    import backend.services.kb.mcp_server as server

    payload = getattr(server, tool)(**args)
    assert isinstance(payload, str), "handler 必须返回 JSON 文本（MCP TextContent）"
    return json.loads(payload)


# ── kb_ask ──────────────────────────────────────────────────────────────────


def test_kb_ask_returns_groups_and_evidence(env):
    # top_k 拉满，确保四条合成文档都在返回里（默认 10 会按配额截断）
    out = call("kb_ask", query="判决树", mode="balanced", top_k=10)
    assert out["ok"] is True and out["tool"] == "kb_ask"
    assert out["stats"]["returned"] >= 1
    by_id = {group["doc_id"]: group for group in out["groups"]}
    assert "t1" in by_id
    assert by_id["t1"]["evidence"]["quote_from"] == "raw"
    assert by_id["t1"]["evidence"]["quote_tier"] == 1
    assert "t2" in by_id, f"有原文与回链的文档应可命中；实得 {sorted(by_id)}"
    assert by_id["t2"]["evidence"]["quote_from"] == "raw"
    assert by_id["t4"]["evidence"]["l0_only"] is True
    assert out["evidence_rule"]


def test_kb_ask_topic_and_tags_filter(env):
    hit = call("kb_ask", query="判决树", tags="判决树")
    assert {group["doc_id"] for group in hit["groups"]} <= {"t1", "t2", "t3", "t4"}
    empty = call("kb_ask", query="判决树", topic="不存在的主题")
    assert empty["groups"] == []
    assert empty["stats"]["candidates"] == 0


def test_kb_ask_invalid_mode_is_structured_error(env):
    out = call("kb_ask", query="判决树", mode="nonsense")
    assert out["ok"] is False
    assert out["error"]["code"] == "invalid_mode"
    assert "browse" in out["error"]["message"]


def test_kb_ask_bad_top_k_and_empty_query(env):
    assert call("kb_ask", query="判决树", top_k=0)["error"]["code"] == "invalid_argument"
    assert call("kb_ask", query="判决树", top_k=999)["error"]["code"] == "invalid_argument"
    assert call("kb_ask", query="   ")["error"]["code"] == "invalid_argument"
    assert call("kb_ask", query="判决树", top_k="abc")["error"]["code"] == "invalid_argument"


def test_kb_ask_deep_degrades_when_vector_arm_fails(env):
    """向量支路异常只降级为纯 FTS，不炸 server（02 §9.1）。"""
    out = call("kb_ask", query="判决树", mode="deep")
    assert out["ok"] is True
    assert out["stats"]["vector"]["error"], "应记录降级原因"
    assert out["groups"], "降级后 FTS 仍应有结果"


# ── kb_inventory ────────────────────────────────────────────────────────────


def test_kb_inventory_compact_and_snapshot(env):
    out = call("kb_inventory", topic="cbac")
    assert out["ok"] is True
    assert out["snapshot"]["total_docs"] == 4
    assert out["snapshot"]["by_tier"]["3"] == 1
    assert "[cbac]" in out["compact"] and "L0仅元数据" in out["compact"]


def test_kb_inventory_all_and_tag_filter(env):
    assert call("kb_inventory")["snapshot"]["topic"] == "_all"
    tagged = call("kb_inventory", tag="判决树")
    assert tagged["snapshot"]["tag"] == "判决树"
    assert call("kb_inventory", tag="不存在的标签")["snapshot"]["total_docs"] == 0


# ── kb_fetch ────────────────────────────────────────────────────────────────


def test_kb_fetch_whole_raw(env):
    out = call("kb_fetch", doc_id="t1", tier=1)
    assert out["ok"] is True
    assert out["tier"] == 1 and out["meta_only"] is False
    assert "两阶段" in out["content"]
    assert out["segments"] == []


def test_kb_fetch_meta_only_is_marked(env):
    out = call("kb_fetch", doc_id="t4")
    assert out["ok"] is True and out["meta_only"] is True
    assert "仅元数据" in out["note"] and out["content"] == ""


def test_kb_fetch_unknown_doc_is_structured_error(env):
    out = call("kb_fetch", doc_id="no-such-doc")
    assert out["ok"] is False
    assert out["error"]["code"] == "not_found"


def test_kb_fetch_tier_not_available(env):
    out = call("kb_fetch", doc_id="t1", tier=3)
    assert out["ok"] is False and out["error"]["code"] == "tier_not_available"
    assert out["error"]["available_tiers"] == [1]


def test_kb_fetch_bad_tier_and_empty_doc_id(env):
    assert call("kb_fetch", doc_id="t1", tier=9)["error"]["code"] == "invalid_argument"
    assert call("kb_fetch", doc_id="t1", tier="x")["error"]["code"] == "invalid_argument"
    assert call("kb_fetch", doc_id="")["error"]["code"] == "invalid_argument"


def test_kb_fetch_chunk_ids_segment_mode(env):
    """段落取证：直接从 chunks 表取一个真实 chunk_id（chunk manifest 由 kb-embed 建）。"""
    conn = sqlite3.connect(env)
    try:
        conn.row_factory = sqlite3.Row
        version = conn.execute(
            "SELECT version_id FROM versions WHERE doc_id='t1' AND tier=3").fetchone()
        if version is None:
            pytest.skip("合成库无 t1 L3 版本")
        conn.execute(
            "INSERT INTO chunks(version_id,doc_id,chunk_index,text,content_hash) "
            "VALUES(?,?,?,?,?)", (version["version_id"], "t1", 0, "第一段：主体资格判定。", "ch0"))
        conn.execute(
            "INSERT INTO chunks(version_id,doc_id,chunk_index,text,content_hash) "
            "VALUES(?,?,?,?,?)", (version["version_id"], "t1", 1, "第二段：权限范围判定。", "ch1"))
        conn.commit()
        chunk_id = conn.execute(
            "SELECT chunk_id FROM chunks WHERE doc_id='t1' AND chunk_index=0").fetchone()[0]
    finally:
        conn.close()
    out = call("kb_fetch", doc_id="t1", tier=3, chunk_ids=str(chunk_id))
    assert out["ok"] is True and out["segment_count"] == 1
    segment = out["segments"][0]
    assert segment["chunk_id"] == chunk_id and segment["chunk_index"] == 0
    assert "主体资格" in segment["text"]
    assert segment["path"] and segment["prev_chunk_id"] is None


def test_kb_fetch_rejects_mismatched_chunk_id(env):
    out = call("kb_fetch", doc_id="t1", tier=3, chunk_ids="999999")
    assert out["ok"] is False and out["error"]["code"] == "chunk_not_found"
    assert out["error"]["missing"] == [999999]
    bad = call("kb_fetch", doc_id="t1", tier=3, chunk_ids="abc")
    assert bad["ok"] is False and bad["error"]["code"] == "invalid_chunk_id"


# ── kb_tag（只写 kb.db）─────────────────────────────────────────────────────


def test_kb_tag_writes_db_only(env):
    out = call("kb_tag", doc_ids="t1,t2", add="新标签")
    assert out["ok"] is True
    assert out["md_written"] == 0
    assert sorted(out["updated"]) == ["t1", "t2"]
    conn = sqlite3.connect(env)
    try:
        tags = {row[0]: json.loads(row[1])
                for row in conn.execute("SELECT doc_id, tags FROM documents")}
    finally:
        conn.close()
    assert "新标签" in tags["t1"] and "新标签" in tags["t2"]
    assert "新标签" not in tags["t3"]


def test_kb_tag_remove_and_noop(env):
    call("kb_tag", doc_ids="t1", add="临时")
    removed = call("kb_tag", doc_ids="t1", remove="临时")
    assert removed["ok"] is True
    noop = call("kb_tag", doc_ids="t1", remove="从未存在的标签")
    assert noop["ok"] is True and noop["noop"]


def test_kb_tag_unknown_doc(env):
    out = call("kb_tag", doc_ids="ghost", add="x")
    assert out["ok"] is False and out["error"]["code"] == "unknown_doc"


def test_kb_tag_requires_something_to_do(env):
    assert call("kb_tag", doc_ids="t1")["error"]["code"] == "invalid_argument"
    assert call("kb_tag", doc_ids="")["error"]["code"] == "invalid_argument"


def test_kb_tag_syncs_fts(env):
    call("kb_tag", doc_ids="t3", add="独特词元")
    conn = kbdb.connect(env)
    try:
        hits = [row["doc_id"] for row in kbftx.match(conn, "独特词元")]
    finally:
        conn.close()
    assert "t3" in hits


# ── kb_promote ──────────────────────────────────────────────────────────────


def test_kb_promote_rejects_missing_backlink(env):
    out = call("kb_promote", doc_ids="t3", to_tier=2)
    assert out["ok"] is False
    assert out["error"]["code"] == "missing_backlink"
    assert out["error"]["report"]["written"] == []


def test_kb_promote_accepts_backlinked_l2(env):
    out = call("kb_promote", doc_ids="t2", to_tier=2)
    assert out["ok"] is True
    assert out["promoted"] == ["t2"] and out["written"] == ["t2"]


def test_kb_promote_to_tier_3_needs_synthesis(env):
    """to_tier=3（提炼）要求同时有 tier=3 产物与 tier=1 原文。"""
    ok = call("kb_promote", doc_ids="t2", to_tier=3)
    assert ok["ok"] is True and ok["written"] == ["t2"]
    bad = call("kb_promote", doc_ids="t1", to_tier=3)
    assert bad["ok"] is False and bad["error"]["code"] == "missing_tier3_version"


def test_kb_promote_bad_args(env):
    assert call("kb_promote", doc_ids="t1", to_tier=1)["error"]["code"] == "invalid_argument"
    assert call("kb_promote", doc_ids="t1", to_tier="x")["error"]["code"] == "invalid_argument"
    assert call("kb_promote", doc_ids="", to_tier=2)["error"]["code"] == "invalid_argument"
    assert call("kb_promote", doc_ids="ghost", to_tier=2)["error"]["code"] == "unknown_doc"


# ── kb_batch_confirm ────────────────────────────────────────────────────────


def test_kb_batch_confirm_auto_lifecycle(env):
    out = call("kb_batch_confirm", round_id="", topic="cbac", tags="判决树",
               doc_ids="t1,t2", action="auto")
    assert out["ok"] is True
    assert out["status"] == "confirmed" and out["written_docs"] == 2
    assert [step["step"] for step in out["steps"]] == ["open", "add"]


def test_kb_batch_confirm_stepwise(env):
    opened = call("kb_batch_confirm", round_id="r-manual", topic="cbac", tags="",
                  action="open")
    assert opened["ok"] is True and opened["status"] == "open"
    added = call("kb_batch_confirm", round_id="r-manual", topic="cbac", tags="",
                 doc_ids="t1", action="add")
    assert added["ok"] is True and added["added"] == ["t1"]
    confirmed = call("kb_batch_confirm", round_id="r-manual", topic="cbac", tags="判决树",
                     action="confirm")
    assert confirmed["ok"] is True and confirmed["status"] == "confirmed"


def test_kb_batch_confirm_list_and_errors(env):
    listed = call("kb_batch_confirm", round_id="", topic="", tags="", action="list")
    assert listed["ok"] is True and listed["kind"] == "batches"
    bad_action = call("kb_batch_confirm", round_id="r1", topic="cbac", tags="", action="explode")
    assert bad_action["error"]["code"] == "invalid_argument"
    blank_topic = call("kb_batch_confirm", round_id="r1", topic="", tags="", action="confirm")
    assert blank_topic["error"]["code"] == "invalid_argument"
    unknown = call("kb_batch_confirm", round_id="r-nope", topic="cbac", tags="", action="confirm")
    assert unknown["error"]["code"] == "unknown_round"
    # 空批次：auto 会自动开批（round_id 留空）→ 无 doc_ids → confirm 拒绝
    empty = call("kb_batch_confirm", round_id="", topic="cbac", tags="", action="auto")
    assert empty["error"]["code"] == "empty_round"


def test_handlers_never_raise_on_garbage(env):
    """04 §8-M6 ③：任何垃圾输入都不得让 server 崩——一律结构化 JSON。

    （参数缺省也走同一条路：handler 的必填项都有默认值 + 显式校验，
    这样 MCP schema 的 required 缺失不会变成"工具调用异常"。）
    """
    cases = [
        ("kb_ask", {}),
        ("kb_ask", {"query": None, "mode": None}),
        ("kb_ask", {"query": "判决树", "mode": None}),
        ("kb_ask", {"query": "判决树", "top_k": None}),
        ("kb_ask", {"query": "判决树", "top_k": []}),
        ("kb_fetch", {}),
        ("kb_fetch", {"doc_id": None}),
        ("kb_fetch", {"doc_id": "t1", "tier": None}),
        ("kb_tag", {}),
        ("kb_tag", {"doc_ids": None}),
        ("kb_tag", {"doc_ids": "t1", "add": None, "remove": None}),
        ("kb_promote", {}),
        ("kb_promote", {"doc_ids": None, "to_tier": None}),
        ("kb_batch_confirm", {}),
        ("kb_inventory", {"topic": 123, "tag": 456}),
        ("kb_inventory", {"topic": None, "tag": None}),
    ]
    for tool, args in cases:
        out = call(tool, **args)
        assert isinstance(out, dict) and "ok" in out, f"{tool} 未返回结构化结果：{out}"
        if out["ok"] is False:
            assert out["error"]["code"], f"{tool} 缺 error.code"
