"""M3 修复回归：org_marks / arxiv_tags 行的规划与写回。

背景（M3 施工实测发现，M2 复核未覆盖）：原 `_replace_frontmatter` 在
`org_marks:` 行已存在时跳过写入，计划中的组织标记被静默丢弃——
现实中导致 25 个 CBAC 文件的 `v2语言论证` 只剩派生库有、真相源 md 丢失，
kb-rebuild 一旦执行该标记即永久消失（违反 DoD-5「从真相源完整重建」）。
"""
from __future__ import annotations

import json

import pytest

import backend.services.kb.tags as tags
from backend.services.kb.db import connect
from backend.services.kb.tags import (
    _build_clean_plan,
    _frontmatter_fields,
    _replace_frontmatter,
    _merge_unique,
)


def _write(path, fm_lines: list[str], body: str = "# T\n\n正文。\n") -> None:
    path.write_bytes(("---\n" + "\n".join(fm_lines) + "\n---\n" + body).encode("utf-8"))


# ── 规划器：必须看见已有的 org_marks / arxiv_tags 行 ────────────────────────


def test_frontmatter_fields_reads_all_three_lines(tmp_path):
    path = tmp_path / "p.md"
    _write(path, ["id: x", "tags: [区块链]", "org_marks: [实验室内]", "arxiv_tags: [cs.CR]"])
    fields, style, status = _frontmatter_fields(path)
    assert status == "ok" and style == "normal"
    assert fields["tags"] == ["区块链"]
    assert fields["org_marks"] == ["实验室内"]
    assert fields["arxiv_tags"] == ["cs.CR"]


def test_merge_unique_keeps_base_order():
    assert _merge_unique(["实验室内"], ["v2语言论证"]) == ["实验室内", "v2语言论证"]
    assert _merge_unique(["a"], ["a", "b"]) == ["a", "b"]
    assert _merge_unique([], []) == []


# ── 写回器：已有行必须被覆盖，而不是跳过 ────────────────────────────────────


def test_replace_frontmatter_overwrites_existing_org_line(tmp_path):
    """缺陷本体：org_marks 行已存在时，计划值必须写进去。"""
    path = tmp_path / "p.md"
    _write(path, ["id: x", "tags: [v2语言论证]", "org_marks: [实验室内]"])
    _replace_frontmatter(path, [], ["实验室内", "v2语言论证"], [])
    text = path.read_text(encoding="utf-8")
    assert "tags: []" in text
    assert "org_marks: [实验室内, v2语言论证]" in text
    assert text.count("org_marks:") == 1  # 不许追加出第二行


def test_replace_frontmatter_overwrites_existing_arxiv_line(tmp_path):
    path = tmp_path / "p.md"
    _write(path, ["id: x", "tags: [区块链]", "arxiv_tags: [cs.AI]"])
    _replace_frontmatter(path, ["区块链"], [], ["cs.AI", "cs.CR"])
    text = path.read_text(encoding="utf-8")
    assert "arxiv_tags: [cs.AI, cs.CR]" in text
    assert text.count("arxiv_tags:") == 1


def test_replace_frontmatter_appends_when_line_absent(tmp_path):
    path = tmp_path / "p.md"
    _write(path, ["id: x", "tags: [实验室内]"])
    _replace_frontmatter(path, [], ["实验室内"], [])
    assert "org_marks: [实验室内]" in path.read_text(encoding="utf-8")


def test_replace_frontmatter_keeps_untouched_body_bytes(tmp_path):
    path = tmp_path / "p.md"
    _write(path, ["id: x", "tags: [实验室内]", "org_marks: [硕博]"], body="# T\n\n正文**粗体**。\n")
    before = path.read_bytes().split(b"---\n", 2)[-1]
    _replace_frontmatter(path, [], ["硕博", "实验室内"], [])
    after = path.read_bytes().split(b"---\n", 2)[-1]
    assert after == before


# ── 端到端：规划→写回→（模拟重扫）可从文件复原，即幂等且自愈 ────────────────


@pytest.fixture
def cbac_like(tmp_path, monkeypatch):
    """造一个「已确认词表 + 一个文件」的最小 cbac 环境。

    刻意复刻事故现场：文件**已有** `org_marks:` 行（M2 清洗留下的「实验室内」），
    而 tags 行里还带着待降级的组织标记 `v2语言论证`。
    """
    papers = tmp_path / "papers"
    (papers / "d1").mkdir(parents=True)
    md = papers / "d1" / "paper.md"
    _write(md, ["id: d1", "tags: [v2语言论证]", "org_marks: [实验室内]"])

    db_path = tmp_path / "kb.db"
    conn = connect(db_path)
    conn.execute(
        "INSERT INTO documents(doc_id,topic,title,tags,status) "
        "VALUES('d1','cbac','t','[]','active')"
    )
    conn.execute("INSERT INTO versions(doc_id,tier,kind,path) VALUES('d1',3,'paper',?)",
                 (md.as_posix(),))
    conn.commit()
    conn.close()

    monkeypatch.setattr(tags, "connect", lambda: connect(db_path))
    monkeypatch.setattr(tags, "_md_files", lambda topic: [
        {"doc_id": "d1", "title": "t", "path": md},
    ])
    monkeypatch.setattr(tags, "_load_vocab", lambda topic, confirmed_only=False: {
        "status": "confirmed",
        "facets": {
            "knowledge": [],
            "org": [{"canonical": "实验室内"}, {"canonical": "v2语言论证", "selected": True}],
            "process": [],
            "arxiv": [],
        },
    })
    return md


def test_plan_moves_org_tag_out_of_tags_line(cbac_like):
    _vocab, plans = _build_clean_plan("cbac")
    plan = plans[0]
    assert plan["action"] == "replace"
    assert plan["after"] == []
    # 已有行在前，新拆出的值追加在后
    assert plan["org_marks"] == ["实验室内", "v2语言论证"]


def test_plan_without_existing_org_line_uses_tags_order(tmp_path, monkeypatch):
    """没有 org_marks 行时，值按 tags 行原顺序落位（不被字典序打乱）。"""
    papers = tmp_path / "papers"
    (papers / "d1").mkdir(parents=True)
    md = papers / "d1" / "paper.md"
    _write(md, ["id: d1", "tags: [硕博, 实验室内]"])

    db_path = tmp_path / "kb.db"
    conn = connect(db_path)
    conn.execute("INSERT INTO documents(doc_id,topic,tags,status) VALUES('d1','cbac','[]','active')")
    conn.execute("INSERT INTO versions(doc_id,tier,kind,path) VALUES('d1',3,'paper',?)", (md.as_posix(),))
    conn.commit()
    conn.close()

    monkeypatch.setattr(tags, "connect", lambda: connect(db_path))
    monkeypatch.setattr(tags, "_md_files", lambda topic: [{"doc_id": "d1", "title": "t", "path": md}])
    monkeypatch.setattr(tags, "_load_vocab", lambda topic, confirmed_only=False: {
        "status": "confirmed",
        "facets": {"knowledge": [], "org": [], "process": [], "arxiv": []},
    })
    _vocab, plans = _build_clean_plan("cbac")
    assert plans[0]["org_marks"] == ["硕博", "实验室内"]


def test_plan_is_idempotent_after_write(cbac_like):
    """写回后再规划必须变成 skip——否则每次 clean 都会重写全部文件。"""
    _vocab, plans = _build_clean_plan("cbac")
    plan = plans[0]
    _replace_frontmatter(cbac_like, plan["after"], plan["org_marks"], plan["arxiv_tags"])

    _vocab2, plans2 = _build_clean_plan("cbac")
    assert plans2[0]["action"] == "skip"
    assert plans2[0]["reason"] == "already-canonical"


def test_existing_org_line_is_not_lost_on_replan(cbac_like):
    """自愈语义：文件里已有的组织标记，即使 tags 行没有，也不得被规划器抹掉。"""
    _replace_frontmatter(cbac_like, [], ["实验室内", "v2语言论证"], [])
    _vocab, plans = _build_clean_plan("cbac")
    assert plans[0]["org_marks"] == ["实验室内", "v2语言论证"]
    assert plans[0]["action"] == "skip"
