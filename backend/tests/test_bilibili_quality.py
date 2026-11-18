"""P-07 / D51：视频入库质量闸门单测。"""
from __future__ import annotations

import json

import pytest

from backend.services.bilibili import quality


@pytest.fixture
def qpath(tmp_path, monkeypatch):
    target = tmp_path / "video_quarantine.json"
    monkeypatch.setattr(quality, "QUARANTINE_PATH", target)
    return target


def test_subtitle_too_short_boundary():
    assert quality.MIN_SUBTITLE_CHARS == 200
    assert quality.subtitle_too_short(None) is True
    assert quality.subtitle_too_short("") is True
    assert quality.subtitle_too_short("   ") is True
    assert quality.subtitle_too_short("字" * 199) is True
    assert quality.subtitle_too_short("字" * 200) is False


def test_record_quarantine_dedup_and_list(qpath):
    quality.record_quarantine("BV1", "标题一", "subtitle_too_short", 48)
    quality.record_quarantine("BV2", "标题二", "no_subtitle", 0)
    quality.record_quarantine("BV1", "标题一（重试）", "subtitle_too_short", 52)

    entries = quality.list_quarantine()
    assert len(entries) == 2, "同一 bvid 只保留最近一条"
    by_id = {item["bvid"]: item for item in entries}
    assert by_id["BV1"]["subtitle_chars"] == 52
    assert by_id["BV2"]["reason"] == "no_subtitle"

    raw = json.loads(qpath.read_text(encoding="utf-8"))
    assert isinstance(raw["entries"], list) and len(raw["entries"]) == 2


def test_missing_file_returns_empty(qpath):
    assert quality.list_quarantine() == []


def test_corrupt_file_is_tolerated(qpath):
    qpath.write_text("{not json", encoding="utf-8")
    assert quality.list_quarantine() == []
    quality.record_quarantine("BV9", "t", "subtitle_too_short", 1)
    assert [item["bvid"] for item in quality.list_quarantine()] == ["BV9"]


# ── 主题自检（D54，bge-reranker-v2-m3 相关性分）─────────────────────────────

def test_topic_gate_threshold_uses_calibrated_boundary():
    """阈值锚定在标定结果上：真视频最低 +2.65，错配最高 −3.53，取 0。"""
    assert quality.TOPIC_MIN_LOGIT == 0.0


def test_subtitle_off_topic_flags_below_threshold():
    # 标定实测：错配字幕 logit ≈ −8.4 ~ −3.5，真字幕 ≈ +2.7 ~ +6.0
    assert quality.subtitle_off_topic("Jev 是什么", "三国演义 张飞 ...", scorer=lambda t, x: -6.6) is True
    assert quality.subtitle_off_topic("Jev 是什么", "判决模型 ...", scorer=lambda t, x: 5.96) is False
    assert quality.subtitle_off_topic("标题", "正文", scorer=lambda t, x: 0.0) is False


def test_subtitle_off_topic_returns_none_when_scorer_unavailable():
    """模型缺失/显存不足 → None，调用方必须跳过闸门而不是误杀。"""
    assert quality.subtitle_off_topic("标题", "正文", scorer=lambda t, x: None) is None


def test_topic_score_empty_inputs_skip_model():
    assert quality.topic_score("", "正文") is None
    assert quality.topic_score("标题", "   ") is None


# ── LLM 复核（2026-11-07）：标题 vs 字幕是不是同一条视频 ─────────────────

def test_judge_subtitle_llm_flags_lyrics():
    """BV1mqso66E26 实测：字幕全是歌词，reranker 之外再让 LLM 看一眼。"""
    ok, why = quality.judge_subtitle_llm(
        "从感知到决策，VLX 如何让物理 AI 走向现实世界？",
        "逍遥的夜 星光洒满 人间...",
        judge=lambda p: '{"same": false, "why": "字幕全是歌词"}')
    assert ok is False
    assert "歌词" in why


def test_judge_subtitle_llm_flags_wrong_track():
    """BV1JGt869EEi 实测：标题是游戏宣传片，字幕在讲海底珊瑚保育。"""
    ok, why = quality.judge_subtitle_llm(
        "【星夜颂歌测试延长】桌面化演示", "潜水员在海底打电钻，珊瑚苗圃...",
        judge=lambda p: '{"same": false, "why": "标题讲游戏，字幕讲珊瑚"}')
    assert ok is False


def test_judge_subtitle_llm_accepts_same_topic():
    ok, _ = quality.judge_subtitle_llm("vLLM 推理引擎", "今天讲 paged attention 和 KV cache",
                                       judge=lambda p: '{"same": true, "why": "同一主题"}')
    assert ok is True


def test_judge_subtitle_llm_no_verdict_is_none():
    """模型偷懒/不返回 JSON → None，调用方按"判不了"处理，不能当误杀。"""
    ok, why = quality.judge_subtitle_llm("标题", "正文", judge=lambda p: "我无法判断")
    assert ok is None
    assert "没给结论" in why


def test_judge_subtitle_llm_prompt_carries_title_and_both_ends():
    seen = {}

    def fake(prompt: str) -> str:
        seen["p"] = prompt
        return '{"same": true, "why": "ok"}'

    body = "开头" * 500 + "结尾内容"
    quality.judge_subtitle_llm("某标题", body, judge=fake, head_chars=900, tail_chars=300)
    assert "某标题" in seen["p"]
    assert "开头" in seen["p"] and "结尾内容" in seen["p"]


# ── 标题 + 官方标签一起当判据（2026-11-07）─────────────────────────────

def test_flatten_tags_handles_every_source_format():
    """标签来源有三种形态：list、`'["a", "b"]'` 字符串、逗号/顿号串。"""
    assert quality._flatten_tags(["人工智能", "程序员"]) == ["人工智能", "程序员"]
    assert quality._flatten_tags(['["人工智能", "程序员"]']) == ["人工智能", "程序员"]
    assert quality._flatten_tags(["科技科普、数据库索引、SQL"]) == ["科技科普", "数据库索引", "SQL"]
    assert quality._flatten_tags(["AI, 学习, AI"]) == ["AI", "学习"]      # 去重保序
    assert quality._flatten_tags(None) == []


def test_video_topic_appends_tags(monkeypatch):
    monkeypatch.setattr(quality, "_tags_of",
                        lambda bv: ["数据库索引", "SQL", "数据库优化"])
    topic = quality.video_topic("BV1sKbS6WEvK", "为什么加了索引，SQL 反而更慢了？")
    assert "为什么加了索引" in topic
    assert "数据库索引" in topic and "SQL" in topic


def test_video_topic_without_tags_is_just_title(monkeypatch):
    monkeypatch.setattr(quality, "_tags_of", lambda bv: [])
    assert quality.video_topic("BVx", "某标题") == "某标题"


def test_video_topic_does_not_stack_suffix(monkeypatch):
    """已经是"标题 ｜ 标签：…"的串再传进来，不能把标签拼两遍。"""
    monkeypatch.setattr(quality, "_tags_of", lambda bv: ["AI"])
    once = quality.video_topic("BVx", "标题 ｜ 标签：旧标签")
    assert once.count("｜ 标签") == 1


