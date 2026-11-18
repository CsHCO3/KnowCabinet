from __future__ import annotations

import backend.services.kb.inventory as inventory
from backend.services.kb.db import connect
from backend.services.kb.inventory import gap, render_compact, snapshot


def _fixture(conn):
    docs = [
        ("l0", 0, ["decision", "abc"], []),
        ("l2", 2, ["decision"], ["v2语言论证"]),
        ("l3", 3, ["xacml"], ["实验室内", "v2语言论证"]),
        ("index-doc", 1, [], []),
    ]
    for doc_id, tier, tags, orgs in docs:
        import json
        conn.execute(
            "INSERT INTO documents(doc_id,topic,best_tier,tags,org_marks,status) "
            "VALUES(?,?,?,?,?,?)",
            (doc_id, "test", tier, json.dumps(tags, ensure_ascii=False),
             json.dumps(orgs, ensure_ascii=False), "active"),
        )
        if tier:
            conn.execute(
                "INSERT INTO versions(doc_id,tier,kind,path) VALUES(?,?,?,?)",
                (doc_id, tier, "notes", "x.md"),
            )
    conn.commit()


def _with_fixture():
    """在磁盘临时库上装 fixture，并临时替换 inventory 的连接工厂。"""
    import tempfile
    from contextlib import contextmanager
    from pathlib import Path

    @contextmanager
    def _cm():
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "kb.db"
            conn = connect(db_path)
            _fixture(conn)
            conn.close()
            original_connect = inventory.connect
            inventory.connect = lambda: connect(db_path)
            try:
                yield
            finally:
                inventory.connect = original_connect
    return _cm()


def test_snapshot_band_counts_and_tags():
    # inventory owns its connection, so use an on-disk fixture database.
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "kb.db"
        conn = connect(db_path)
        _fixture(conn)
        conn.close()
        original_connect = inventory.connect
        inventory.connect = lambda: connect(db_path)
        try:
            data = snapshot("test")
        finally:
            inventory.connect = original_connect
    assert data["total_docs"] == 4
    assert data["by_tier"] == {"0": 1, "1": 1, "2": 1, "3": 1}
    tags = {item["tag"]: item["count"] for item in data["tag_counts"]}
    assert tags["decision"] == 2 and tags["xacml"] == 1


def test_gap_flags_direction_without_l2():
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "kb.db"
        conn = connect(db_path)
        _fixture(conn)
        conn.close()
        original_connect = inventory.connect
        inventory.connect = lambda: connect(db_path)
        try:
            data = gap("test")
        finally:
            inventory.connect = original_connect
    by_tag = {row["tag"]: row for row in data["gaps"]}
    assert by_tag["decision"]["L0"] == 1 and by_tag["decision"]["L2"] == 1


def test_render_compact_is_short():
    data = snapshot("test")
    text = render_compact(data)
    assert len(text.splitlines()) <= 6
    assert "L0仅元数据" in text


def test_snapshot_groups_by_org_marks_d32():
    """D32：org_marks 作为 topic 内二级分组，snapshot 必须给出分布。"""
    with _with_fixture():
        data = snapshot("test")
    orgs = {item["org"]: item["count"] for item in data["org_counts"]}
    assert orgs == {"v2语言论证": 2, "实验室内": 1}, orgs


def test_render_compact_shows_project_line():
    with _with_fixture():
        data = snapshot("test")
    text = render_compact(data)
    assert "项目线" in text
    assert "v2语言论证×2" in text
    # 口径必须写明包含 M2 遗留的 arXiv 分区码，避免用户误读
    assert "arXiv" in text


def test_render_compact_omits_project_line_when_no_org_marks():
    import json
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "kb.db"
        conn = connect(db_path)
        conn.execute("INSERT INTO documents(doc_id,topic,best_tier,tags,org_marks,status) "
                     "VALUES('a','t',3,?, '[]','active')", (json.dumps(["x"]),))
        conn.commit()
        conn.close()
        original_connect = inventory.connect
        inventory.connect = lambda: connect(db_path)
        try:
            text = render_compact(snapshot("t"))
        finally:
            inventory.connect = original_connect
    assert "项目线" not in text
