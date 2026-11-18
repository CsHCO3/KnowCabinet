"""kb-* CLI 级回归：命令路径必须真的能跑通（单测全绿但命令炸的盲区）。

M2 复核 P1：cmd_kb_rebuild 用裸 sqlite3.connect 打开连接，未设 row_factory，
交给 assert_invariants 后按字符串取列即 TypeError——本文件锁定该缺陷。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from backend.services.kb import cli, db


class _FakeReport:
    def to_dict(self) -> dict:
        return {"scanned": 1, "docs_upserted": 1, "versions_upserted": 1, "fts_upserted": 1}


def _seed(conn) -> None:
    """最小自洽库：documents / versions / kb_fts 三表口径一致。

    D44：d1 有原文(tier=1) + 提炼(tier=3)，所以 to_tier=3 可过、to_tier=2 因缺 tier=2 被拒。
    """
    conn.execute(
        "INSERT INTO documents(doc_id,topic,title,best_tier,tags,status) "
        "VALUES('d1','cbac','判决树研究',3,'[\"判决树\"]','active')"
    )
    conn.execute("INSERT INTO versions(doc_id,tier,kind,path) VALUES('d1',1,'paper','x.md')")
    conn.execute("INSERT INTO versions(doc_id,tier,kind,path) VALUES('d1',3,'synthesis','y.md')")
    conn.execute(
        "INSERT INTO kb_fts(doc_id,title_t,abstract_t,tags_t,body_t) VALUES('d1','判决 树','','','')"
    )
    conn.commit()


@pytest.fixture
def tmp_kb(tmp_path, monkeypatch):
    """把 cli 与 db 两处 DB_PATH 都指向临时库（两个模块各自绑定了一份）。"""
    target = tmp_path / "kb.db"
    monkeypatch.setattr(db, "DB_PATH", target)
    monkeypatch.setattr(cli, "DB_PATH", target)
    return target


def test_kb_rebuild_command(tmp_kb, monkeypatch, capsys):
    """复核 P1 回归：kb-rebuild 必须退出码 0，且行数与不变量都对。"""

    def fake_scan_all(force=False, source="all"):
        conn = db.connect(tmp_kb)
        _seed(conn)
        conn.close()
        return _FakeReport()

    monkeypatch.setattr(cli, "scan_all", fake_scan_all)

    rc = cli.cmd_kb_rebuild(argparse.Namespace(keep_vector=False))

    captured = capsys.readouterr()
    assert rc == 0, f"kb-rebuild 退出码 {rc}；stderr={captured.err}"
    payload = json.loads(captured.out.strip().splitlines()[-1])
    assert payload["row_counts"] == {"documents": 1, "versions": 2, "kb_fts": 1}
    assert payload["invariants"]["active_documents"] == 1
    assert payload["invariants"]["best_tier_mismatches"] == 0
    assert payload["invariants"]["invalid_tags"] == 0


def test_kb_rebuild_command_reports_invariant_violation(tmp_kb, monkeypatch, capsys):
    """不变量被破坏时必须退出码 2 且进 stderr，不许静默成功。"""

    def bad_scan_all(force=False, source="all"):
        conn = db.connect(tmp_kb)
        _seed(conn)
        conn.execute("UPDATE documents SET best_tier=0 WHERE doc_id='d1'")  # 制造 I4 违规
        conn.commit()
        conn.close()
        return _FakeReport()

    monkeypatch.setattr(cli, "scan_all", bad_scan_all)

    rc = cli.cmd_kb_rebuild(argparse.Namespace(keep_vector=False))

    captured = capsys.readouterr()
    assert rc == 2
    assert "I4" in captured.err


def test_kb_rebuild_uses_row_factory_connection(tmp_kb, monkeypatch, capsys):
    """回归根因本身：rebuild 用到的连接必须支持字符串取列。"""

    def fake_scan_all(force=False, source="all"):
        conn = db.connect(tmp_kb)
        _seed(conn)
        conn.close()
        return _FakeReport()

    monkeypatch.setattr(cli, "scan_all", fake_scan_all)
    cli.cmd_kb_rebuild(argparse.Namespace(keep_vector=False))
    capsys.readouterr()

    conn = db.connect(tmp_kb)
    try:
        row = conn.execute("SELECT doc_id, tags FROM documents").fetchone()
        assert row["doc_id"] == "d1"
        assert json.loads(row["tags"]) == ["判决树"]
    finally:
        conn.close()


def test_all_kb_subcommands_registered():
    """04 §12.4：追加 kb-* 绑定不得破坏 argparse。

    必须走子进程：进程内 `import backend.main` 会连带 import
    `backend.services.bilibili.subtitle`，M3 前后者在 win32 下于**导入期**替换
    `sys.stdout/sys.stderr`，会踩坏 pytest capture 流。M4 已改成原地 reconfigure，
    但子进程仍是最贴近真实调用方式的验证。
    """
    import subprocess
    import sys

    py = sys.executable
    proc = subprocess.run([py, "-m", "backend.main", "--help"],
                          cwd=str(Path(__file__).resolve().parents[2]),
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert proc.returncode == 0, f"--help 退出码 {proc.returncode}: {proc.stderr[:500]}"
    for name in ("kb-scan", "kb-inventory", "kb-rebuild", "kb-tags",
                 "kb-ask", "kb-fetch", "kb-eval", "kb-embed", "kb-promote"):
        assert name in proc.stdout, f"--help 未列出子命令 {name}"

    for name in ("kb-ask", "kb-fetch", "kb-eval", "kb-embed", "kb-promote"):
        sub = subprocess.run([py, "-m", "backend.main", name, "--help"],
                             cwd=str(Path(__file__).resolve().parents[2]),
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
        assert sub.returncode == 0, f"{name} --help 退出码 {sub.returncode}: {sub.stderr[:300]}"


def test_kb_ask_help_exposes_m5_switches():
    """M5 新增 --fusion/--fold/--vector/--no-vector 必须可发现（契约可见性）。"""
    import subprocess
    import sys

    proc = subprocess.run([sys.executable, "-m", "backend.main", "kb-ask", "--help"],
                          cwd=str(Path(__file__).resolve().parents[2]),
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert proc.returncode == 0
    for flag in ("--fusion", "--fold", "--vector", "--no-vector"):
        assert flag in proc.stdout, f"kb-ask --help 缺 {flag}"

    fetch_help = subprocess.run([sys.executable, "-m", "backend.main", "kb-fetch", "--help"],
                                cwd=str(Path(__file__).resolve().parents[2]),
                                capture_output=True, text=True, encoding="utf-8",
                                errors="replace")
    assert fetch_help.returncode == 0
    assert "--chunk-ids" in fetch_help.stdout and "--context" in fetch_help.stdout


def test_kb_promote_command_ok_and_reject(tmp_kb, monkeypatch, capsys):
    def fake_scan_all(force=False, source="all"):
        conn = db.connect(tmp_kb)
        _seed(conn)  # d1 有 tier=3 paper 版本
        conn.close()
        return _FakeReport()

    monkeypatch.setattr(cli, "scan_all", fake_scan_all)
    assert cli.cmd_kb_rebuild(argparse.Namespace(keep_vector=False)) == 0
    capsys.readouterr()

    rc = cli.cmd_kb_promote(argparse.Namespace(doc_ids=["d1"], to_tier=3, apply=False,
                                               no_strict=False, pretty=True))
    out = capsys.readouterr().out
    assert rc == 0 and "[OK] d1" in out

    rc = cli.cmd_kb_promote(argparse.Namespace(doc_ids=["d1"], to_tier=2, apply=False,
                                               no_strict=False, pretty=False))
    assert rc == 1, "无 L2 版本时必须拒绝"
