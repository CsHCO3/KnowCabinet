"""证据双轨 / 回链门槛（kb_promote）单测。

D44 口径（2026-10-19）：tier 是信息密度序——1=原文、2=轻整理、3=提炼。
回链标记改为 `[L1:路径#块号]`，指向本文档的 **tier=1 原文**版本。

覆盖：回链可解析（路径+块序号）/ 路径指向非本人原文 / 块序号不存在 / 无回链拒绝 /
legacy_l2 存量豁免 / to_tier=3 需同时有提炼版与原文 / apply 只在通过时写 status。
"""
from __future__ import annotations

import json

import pytest

import backend.services.kb.db as kbdb
from backend.services.kb.db import connect, now
from backend.services.kb.promote import check_backlinks, promote


@pytest.fixture
def kbp(tmp_path, monkeypatch):
    """合成库：linked（有效回链）/ plain（无回链）/ legacy（存量豁免）/
    rawonly（只有原文）/ synth（原文+提炼）。"""
    db_path = tmp_path / "kb.db"
    monkeypatch.setattr(kbdb, "DB_PATH", db_path)
    files = tmp_path / "files"
    files.mkdir()

    def write(name: str, body: str) -> str:
        path = files / name
        path.write_text(body, encoding="utf-8", newline="")
        return path.as_posix()

    raw_path = write("linked_raw.md", "# Paper\n" + "L" * 80 + "\n")
    conn = connect(db_path)
    try:
        for doc_id, status, best in (
            ("linked", "active", 2), ("plain", "active", 2),
            ("legacy", "legacy_l2", 2), ("rawonly", "active", 1),
            ("synth", "active", 3),
        ):
            conn.execute(
                "INSERT INTO documents(doc_id,topic,source_type,title,stage,best_tier,"
                "tags,org_marks,arxiv_cats,status) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (doc_id, "cbac", "paper", doc_id, "condensed", best, "[]", "[]", "[]", status),
            )

        def add(doc_id: str, tier: int, kind: str, path: str, h: str, chars: int) -> None:
            conn.execute(
                "INSERT INTO versions(doc_id,tier,kind,path,content_hash,char_count,updated_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (doc_id, tier, kind, path, h, chars, now()),
            )

        # linked：原文(tier=1) + 带 [L1:] 回链的轻整理(tier=2)
        add("linked", 1, "paper", raw_path, "h1", 90)
        linked_l2 = write("linked_l2.md", "浓缩内容。\n[L1:" + raw_path + "#c0]\n")
        add("linked", 2, "condensation", linked_l2, "h2", 30)
        # plain：原文 + 无回链轻整理
        add("plain", 1, "paper", raw_path, "h1", 90)
        plain_l2 = write("plain_l2.md", "浓缩内容，没有回链。\n")
        add("plain", 2, "condensation", plain_l2, "h2", 20)
        # legacy：只有无回链轻整理
        legacy_l2 = write("legacy_l2.md", "存量浓缩，无回链。\n")
        add("legacy", 2, "condensation", legacy_l2, "h2", 20)
        # rawonly：只有原文
        add("rawonly", 1, "paper", raw_path, "h1", 90)
        # synth：原文 + 提炼(tier=3)
        add("synth", 1, "paper", raw_path, "h1", 90)
        synth_t3 = write("synth_t3.md", "跨论文结论。\n")
        add("synth", 3, "synthesis", synth_t3, "h3", 20)
        conn.commit()
        # linked 的原文版本放若干块，用于校验 #cN
        conn.executemany(
            "INSERT INTO chunks(version_id,doc_id,chunk_index,text,content_hash,model_id,embedded_at)"
            " VALUES((SELECT version_id FROM versions WHERE doc_id='linked' AND kind='paper'),"
            "'linked',?,?,?,NULL,NULL)",
            [(0, "第一块 " * 10, "c0"), (1, "第二块 " * 10, "c1")],
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def test_check_backlinks_valid_path_and_chunk(kbp):
    conn = connect()
    try:
        result = check_backlinks(conn, "linked")
    finally:
        conn.close()
    assert result["ok"] is True
    assert result["link_count"] == 1
    link = result["valid"][0]
    assert link["chunk_selector"] == "c0"
    assert link["chunk_id"] is not None and link["chunk_verified"] is True


def test_check_backlinks_rejects_unknown_chunk_index(kbp):
    """块序号指向不存在的块 → 坏链（不能只认路径）。"""
    conn = connect()
    try:
        path = kbdb.DB_PATH.parent / "files" / "linked_l2.md"
        path.write_text(
            "浓缩内容。\n[L1:" + (kbdb.DB_PATH.parent / "files" / "linked_raw.md").as_posix()
            + "#c99]\n", encoding="utf-8")
        result = check_backlinks(conn, "linked")
    finally:
        conn.close()
    assert result["ok"] is False
    assert result["broken"][0]["reason"] == "chunk_index_not_found"


def test_check_backlinks_rejects_foreign_path(kbp):
    """回链路径不是该 doc 的原文版本 → 坏链。"""
    conn = connect()
    try:
        path = kbdb.DB_PATH.parent / "files" / "linked_l2.md"
        path.write_text("浓缩内容。\n[L1:somewhere/else/paper.md#c0]\n", encoding="utf-8")
        result = check_backlinks(conn, "linked")
    finally:
        conn.close()
    assert result["ok"] is False
    assert result["broken"][0]["reason"] == "path_not_in_doc_raw_versions"


def test_promote_l2_requires_backlink(kbp):
    report = promote(["linked", "plain"], to_tier=2)
    assert report["ok"] is False
    assert report["promoted"] == ["linked"]
    assert [item["doc_id"] for item in report["rejected"]] == ["plain"]
    assert report["rejected"][0]["reason"] == "missing_backlink"


def test_promote_l2_legacy_exempt(kbp):
    report = promote(["legacy"], to_tier=2)
    assert report["ok"] is True
    assert report["exempt"] == ["legacy"]
    assert report["promoted"] == []
    # 豁免者保持 legacy_l2，不因一次调用被抹掉
    detail = report["details"][0]
    assert detail["action"] == "keep_legacy_exempt" and detail["exempt"] is True


def test_promote_l3_requires_synthesis_and_raw(kbp):
    """to_tier=3（提炼）必须同时有 tier=3 产物与 tier=1 原文。"""
    ok = promote(["synth"], to_tier=3)
    assert ok["ok"] is True and ok["promoted"] == ["synth"]
    # 只有原文、没有提炼版 → missing_tier3_version
    bad = promote(["rawonly"], to_tier=3)
    assert bad["ok"] is False and bad["rejected"][0]["reason"] == "missing_tier3_version"


def test_promote_unknown_doc_and_bad_tier(kbp):
    report = promote(["nope"], to_tier=2)
    assert report["ok"] is False and report["rejected"][0]["reason"] == "not_found"
    with pytest.raises(ValueError):
        promote(["linked"], to_tier=1)
    with pytest.raises(ValueError):
        promote([], to_tier=2)


def test_promote_strict_apply_writes_nothing_on_failure(kbp):
    report = promote(["linked", "plain"], to_tier=2, apply=True)
    assert report["ok"] is False and report["written"] == []
    conn = connect()
    try:
        statuses = {row["doc_id"]: row["status"] for row in
                    conn.execute("SELECT doc_id,status FROM documents")}
    finally:
        conn.close()
    assert statuses["linked"] == "active" and statuses["plain"] == "active"


def test_promote_non_strict_apply_writes_passing_only(kbp):
    report = promote(["linked", "plain"], to_tier=2, apply=True, strict=False)
    assert report["ok"] is False
    assert report["written"] == ["linked"]


def test_promote_apply_flips_status_to_active(kbp):
    conn = connect()
    try:
        conn.execute("UPDATE documents SET status='candidate' WHERE doc_id='linked'")
        conn.commit()
    finally:
        conn.close()
    report = promote(["linked"], to_tier=2, apply=True)
    assert report["ok"] is True and report["written"] == ["linked"]
    conn = connect()
    try:
        row = conn.execute("SELECT status FROM documents WHERE doc_id='linked'").fetchone()
    finally:
        conn.close()
    assert row["status"] == "active"


def test_promote_report_is_json_serialisable(kbp):
    report = promote(["linked", "plain", "legacy"], to_tier=2)
    assert isinstance(json.dumps(report, ensure_ascii=False), str)
