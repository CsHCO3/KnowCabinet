"""M8-3：目录残渣块判据（`backend/services/kb/chunkq.py`）单元测试。

判据来源与阈值依据见 `temp/m8_toc_heading.py`（全库 23,403 个 L3 块的形态扫描）与
`temp/m8_toc_sensitivity.py`（点线率阈值 0.15–0.5 判出的块数几乎不变 → 判据是双峰而非连续）。
"""
from __future__ import annotations

import pytest

from backend.services.kb.chunkq import is_toc, partition_toc, toc_score

pytestmark = pytest.mark.unit


DOTTED_TOC = (
    "1.1 研究背景 ...................................................................... 1\n"
    "1.2 研究现状 ...................................................................... 3\n"
    "1.3 研究问题与内容 ................................................................ 6\n"
    "1.4 本文组织结构 .................................................................. 8\n"
)

NUMBERED_TOC = (
    "第1章 绪论 1\n"
    "第2章 相关工作 12\n"
    "第3章 方法 25\n"
    "第4章 实验 40\n"
    "第5章 结论 52\n"
)

CHINESE_BODY = (
    "本文以我国现行法律法规为基础，结合领域特定语言和形式化方法等理论，"
    "开展了智能法律合约的语法设计、合约检查规则形式化建模以及生成系统的实现工作。"
    "实验结果表明，所提方法在合约条款覆盖率和检查准确率上均优于基线方案。"
)

ENGLISH_BODY_NUMBERED = (
    "3. *Capability Revocation*: The capability revocation considers two scenarios: "
    "partial access right revocation and full revocation. In the system design, only "
    "the administrator or domain masters are allowed to trigger this operation, and "
    "every revocation is journaled so that later audits can reconstruct the history.\n"
    "4. *Verify the conditions*: Even though the action on a target resource is "
    "permitted after the access validation, the context-awareness constraints must "
    "still be evaluated on the local device before the request is finally served."
)


def test_toc_detects_dotted_leader_toc():
    info = toc_score(DOTTED_TOC)
    assert info["is_toc"] is True
    assert info["rule"] == "dotted_leader"
    assert info["dotted_ratio"] >= 0.5


def test_toc_detects_numbered_short_lines_toc():
    info = toc_score(NUMBERED_TOC)
    assert info["is_toc"] is True
    assert info["rule"] == "numbered_short_lines"


def test_toc_ignores_chinese_body_paragraph():
    info = toc_score(CHINESE_BODY)
    assert info["is_toc"] is False, info


def test_toc_ignores_english_numbered_body():
    """英文论文里的编号正文列表不得误判（这是本判据最容易踩的坑）。"""
    info = toc_score(ENGLISH_BODY_NUMBERED)
    assert info["is_toc"] is False, info


def test_toc_ignores_short_chunk():
    """短块（<80 字符）一律不判——避免误伤标题块/单句。"""
    short = "1.1 研究背景 ....... 1\n1.2 研究现状 ....... 3\n"
    assert toc_score(short)["is_toc"] is False
    assert is_toc(short) is False


def test_partition_toc_drops_toc_chunks_and_keeps_order():
    body_hi = {"chunk_id": 1, "score": 0.9, "text": CHINESE_BODY}
    toc_chunk = {"chunk_id": 2, "score": 0.8, "text": DOTTED_TOC}
    body_lo = {"chunk_id": 3, "score": 0.7, "text": ENGLISH_BODY_NUMBERED}
    kept, dropped = partition_toc([body_hi, toc_chunk, body_lo])
    assert [chunk["chunk_id"] for chunk in kept] == [1, 3]
    assert [chunk["chunk_id"] for chunk in dropped] == [2]


def test_partition_toc_never_kills_a_document():
    """整篇都是目录块时原样返回——宁可留残渣，也不能让文档检索不到。"""
    chunks = [{"chunk_id": 1, "score": 0.9, "text": DOTTED_TOC},
              {"chunk_id": 2, "score": 0.8, "text": DOTTED_TOC}]
    kept, dropped = partition_toc(chunks)
    assert len(kept) == 2 and dropped == []


def test_partition_toc_handles_empty():
    assert partition_toc([]) == ([], [])
