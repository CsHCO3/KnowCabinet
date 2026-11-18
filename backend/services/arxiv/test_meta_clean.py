"""元数据清洗引擎测试：幂等 / 单调 / 可审计 + section/tags 规则。

运行：`python -m pytest backend/services/arxiv/test_meta_clean.py`（从项目根）。
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.services.arxiv.meta_clean import (  # noqa: E402
    clean_paper, clean_section, clean_tags, merge_metadata, parse_keywords,
)


# ── section 映射 ─────────────────────────────────

def test_section_arxiv_exact():
    assert clean_section("cs.CR") == "计算机安全"
    assert clean_section("cs.LG") == "机器学习"
    assert clean_section("math.NA") == "数学"


def test_section_prefix_fallback():
    assert clean_section("cs.XYZ") == "计算机科学"   # 细分未命中 → 前缀
    assert clean_section("math.XX") == "数学"


def test_section_unclassified_bucket():
    assert clean_section("zzz.qq") == "未分类"
    assert clean_section("") == "未分类"


def test_section_openalex_fallback():
    assert clean_section("", ["Computer Science", "Computer Networks", "IoT and Edge"]) == "计算机网络"
    # 都不命中 → 英文 field 兜底，不瞎造中文
    s = clean_section("", ["Foo", "Bar Unknown"])
    assert s == "Bar Unknown" or s == "未分类"


# ── 关键词解析 ───────────────────────────────────

def test_parse_keywords_tolerant():
    assert parse_keywords("# t\n- 关键词: 区块链, 共识; 轻量级、零知识") == ["区块链", "共识", "轻量级", "零知识"]
    assert parse_keywords("关键词：单一") == ["单一"]
    assert parse_keywords("无关键词行") == []
    assert parse_keywords("") == []


# ── tags 三路合并 + 确定性 ───────────────────────

def test_tags_deterministic():
    a = clean_tags("cs.CR", ["Security", "Blockchain"], "关键词: 共识、区块链")
    b = clean_tags("cs.CR", ["Security", "Blockchain"], "关键词: 共识、区块链")
    assert a == b
    assert a == ["Blockchain", "Security", "cs.CR", "共识", "区块链"]  # 排序稳定


def test_tags_manual_priority():
    out = clean_tags("cs.CR", [], "", manual_tags=["人工标签"])
    assert out[0] == "人工标签"
    assert "cs.CR" in out


# ── 合并：单调 + 人工保留 ────────────────────────

def test_merge_monotonic_higher_trust_wins():
    sources = {
        "arxiv": {"title": "A", "authors": ["X"], "year": 2020, "primary_category": "cs.CR"},
        "openalex": {"citations": 10, "venue": "IEEE TIFS"},
    }
    rec, _ = merge_metadata(sources)
    assert rec.title == "A" and rec.citations == 10
    assert rec.provenance["citations"] == "openalex"


def test_merge_manual_preserved():
    sources = {"arxiv": {"title": "新标题", "year": 2024}}
    rec, _ = merge_metadata(sources, existing={"title": "旧标题"}, manual_fields=["title"])
    assert rec.title == "旧标题"   # 人工字段不被自动覆盖
    assert rec.provenance["title"].startswith("manual")


def test_merge_fill_empty_only():
    sources = {"arxiv": {"title": "A"}}
    rec, _ = merge_metadata(sources, existing={"year": 2021})
    assert rec.title == "A" and rec.year == 2021  # 已有 year 保留，只填空


# ── 幂等（第 0 条验收）───────────────────────────

def test_clean_paper_idempotent():
    sources = {
        "arxiv": {"title": "T", "authors": ["A", "B"], "year": 2022,
                  "abstract": "abs", "primary_category": "cs.CR", "doi": "10.1/x"},
        "openalex": {"citations": 5, "venue": "V", "venue_type": "journal"},
    }
    r1 = clean_paper(sources)
    r2 = clean_paper(sources)
    assert r1.to_dict() == r2.to_dict()  # 纯函数，两次零 diff


# ── 黄金集（已知答案）────────────────────────────

def test_golden_set():
    # 2302.08496 已知：cs.DC、2023、5 作者
    sources = {
        "arxiv": {"title": "A review of Smart Contract Blockchain", "authors": ["Norah M. Alshahrani", "M. L. Mat Kiah"],
                  "year": 2023, "primary_category": "cs.DC", "arxiv_id": "2302.08496"},
        "openalex": {"citations": 18, "venue": "Computers, materials & continua"},
    }
    rec = clean_paper(sources)
    assert rec.section == "分布式计算"
    assert rec.year == 2023
    assert "cs.DC" in rec.tags
    assert rec.source_type == "paper"
    assert rec.provenance["title"] == "arxiv"
