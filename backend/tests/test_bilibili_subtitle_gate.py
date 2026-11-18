"""字幕可用性闸（施工单 D）：低覆盖字幕跳过不入库 + 每条结果落 _runs.jsonl。

口径（用户已定）：字幕覆盖率 <15% → 字幕不可用 → 跳过（不扫帧、不浓缩、不入库）；
--force-scan 不救——它救"画面诊断漏判"，救不了"字幕烂"。
纯 mock，不联网、不写真实缓存目录。
"""
from __future__ import annotations

import json

import pytest

from backend.services.bilibili.visual import digest, runner, seg_filter

# parse_bvid_entry 要求 BV+10位，测试样本用规整假号
BV_LOW = "BV1000000001"     # 覆盖 7%（对应实测 BV1Ghpy6gE5x）
BV_GOOD = "BV2000000002"    # 覆盖 99%（对应实测 BV1Pkx76ZEQG）


def write_cache(tmp, bv, duration, max_end):
    """造一份字幕缓存：闸门只看 max(segments[].end)/duration。"""
    p = tmp / f"{bv}.json"
    p.write_text(json.dumps({
        "ok": True, "bvid": bv, "duration": duration,
        "segments": [{"start": 0.0, "end": max_end, "text": "词"}],
    }, ensure_ascii=False), encoding="utf-8")
    return p


@pytest.fixture
def cache_in_tmp(tmp_path, monkeypatch):
    """缓存路径与 visual 根都指到 tmp，别碰 data/bilibili 真目录。"""
    monkeypatch.setattr(runner, "subtitle_cache_path",
                        lambda bv, page=1: tmp_path / f"{bv}.json")
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    return tmp_path


@pytest.fixture
def counters(monkeypatch):
    """下游三个花钱环节的桩：被调一次计一次，闸门测试断言全为 0。"""
    seen = {"sections": 0, "scan": 0, "digest": 0}
    monkeypatch.setattr(runner, "get_sections",
                        lambda bv, model:
                        (seen.__setitem__("sections", seen["sections"] + 1), {})[1])
    monkeypatch.setattr(seg_filter, "scan",
                        lambda bv, thr:
                        (seen.__setitem__("scan", seen["scan"] + 1),
                         {"keep_frames": []})[1])
    monkeypatch.setattr(digest, "digest_one",
                        lambda bv, mode:
                        (seen.__setitem__("digest", seen["digest"] + 1),
                         {"chars": 1, "path": "x"})[1])
    return seen


# ── D1: subtitle_usable 判定口径 ─────────────────────────────

def test_no_cache_unusable(cache_in_tmp):
    ok, why = runner.subtitle_usable("BV3000000003")
    assert (ok, why) == (False, "没有字幕缓存")


def test_low_coverage_unusable(cache_in_tmp):
    write_cache(cache_in_tmp, BV_LOW, duration=211, max_end=15.38)
    ok, why = runner.subtitle_usable(BV_LOW)
    assert ok is False
    assert "7%" in why and "不可用" in why
    assert "15s/211s" in why


def test_full_coverage_usable(cache_in_tmp):
    write_cache(cache_in_tmp, BV_GOOD, duration=572, max_end=566)
    ok, why = runner.subtitle_usable(BV_GOOD)
    assert (ok, why) == (True, "字幕覆盖 99%")


def test_zero_duration_passes(cache_in_tmp):
    write_cache(cache_in_tmp, "BV4000000004", duration=0, max_end=15.38)
    ok, why = runner.subtitle_usable("BV4000000004")
    assert (ok, why) == (True, "时长未知，保守放行")


def test_min_cover_param_overrides_default(cache_in_tmp):
    """50% 覆盖默认放行；--min-sub-cover 0.6 时同一份缓存要拦。"""
    write_cache(cache_in_tmp, "BV5000000005", duration=100, max_end=50)
    assert runner.subtitle_usable("BV5000000005")[0] is True
    assert runner.subtitle_usable("BV5000000005", min_cover=0.6)[0] is False


# ── D2: 闸在 run_one 里拦住，下游一个都不碰 ───────────────────

@pytest.fixture
def offline_run_one(cache_in_tmp, monkeypatch):
    """抓字幕/简介收集不联网的桩。"""
    monkeypatch.setattr(runner, "ensure_subtitle",
                        lambda bv, force=False, page=1: {"title": "t"})
    monkeypatch.setattr(runner, "push_desc_resources", lambda bv, info: 0)


def test_unusable_subtitle_skips_all_downstream(cache_in_tmp, counters, offline_run_one):
    write_cache(cache_in_tmp, BV_LOW, duration=211, max_end=15.38)
    r = runner.run_one(BV_LOW, 3, "v4flash")
    assert r["ok"] is False
    assert r["status"] == "no_usable_subtitle"
    assert "7%" in r["error"]
    assert counters == {"sections": 0, "scan": 0, "digest": 0}
    assert not (cache_in_tmp / BV_LOW).exists()       # 没建 visual/<bv>/ 目录


def test_force_scan_does_not_rescue_bad_subtitle(cache_in_tmp, counters, offline_run_one):
    write_cache(cache_in_tmp, BV_LOW, duration=211, max_end=15.38)
    r = runner.run_one(BV_LOW, 3, "v4flash", force_scan=True)
    assert r["status"] == "no_usable_subtitle"
    assert counters == {"sections": 0, "scan": 0, "digest": 0}


def test_subtitle_fetch_failure_is_no_subtitle(cache_in_tmp, counters, monkeypatch):
    monkeypatch.setattr(runner, "ensure_subtitle",
                        lambda bv, force=False, page=1:
                        (_ for _ in ()).throw(RuntimeError("抓字幕失败：无字幕轨")))
    r = runner.run_one("BV6000000006", 3, "v4flash")
    assert r["status"] == "no_subtitle"
    assert "无字幕轨" in r["error"]
    assert counters == {"sections": 0, "scan": 0, "digest": 0}


# ── D3: 落盘 ─────────────────────────────────────────────────

@pytest.fixture
def runs_in_tmp(tmp_path, monkeypatch):
    p = tmp_path / "_runs.jsonl"
    monkeypatch.setattr(runner, "_RUNS_JSONL", p)
    return p


def _stub_run_one(monkeypatch, status="no_usable_subtitle", ok=False):
    def fake(bv, *a, **k):
        return {"bvid": bv, "page": 1, "ok": ok, "status": status,
                "error": "字幕只覆盖 7%（15s/211s），判定不可用",
                "sub_gate": "字幕只覆盖 7%", "elapsed": 0.1}
    monkeypatch.setattr(runner, "run_one", fake)


def test_run_batch_records_one_line_per_video(runs_in_tmp, monkeypatch):
    _stub_run_one(monkeypatch)
    out = runner.run_batch([BV_LOW], no_index=True)
    assert len(out) == 1
    lines = runs_in_tmp.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["bvid"] == BV_LOW
    assert row["page"] == 1
    assert row["status"] == "no_usable_subtitle"
    # 施工单07 给 _runs.jsonl 加了 sub_source/asr_model/asr_elapsed 三个字段（只增不改）；
    # 开关没开时三者留空，语义与老行一致
    assert set(row) == {"ts", "bvid", "page", "page_count", "partial", "status",
                        "reason", "retryable", "error", "gate", "visual_verdict",
                        "n_keep", "chars", "kb_ok", "elapsed",
                        "sub_source", "asr_model", "asr_elapsed"}
    # 三件事要能分开记：字幕覆盖低是可重试的，不是"字幕抓错"
    assert row["reason"] == "subtitle_low_coverage"
    assert row["retryable"] is True


def test_run_batch_appends_not_overwrites(runs_in_tmp, monkeypatch):
    """追加而不是覆盖：分两批各跑一条 → 两行都在。"""
    _stub_run_one(monkeypatch)
    runner.run_batch([BV_LOW], no_index=True)
    runner.run_batch([BV_GOOD], no_index=True)
    lines = runs_in_tmp.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert [json.loads(line)["bvid"] for line in lines] == [BV_LOW, BV_GOOD]


def test_record_failure_does_not_kill_batch(tmp_path, monkeypatch):
    """写盘失败只警告，整批照常返回结果。"""
    bad = tmp_path / "_runs.jsonl"
    bad.mkdir()   # 同名目录让 open() 必炸
    monkeypatch.setattr(runner, "_RUNS_JSONL", bad)
    _stub_run_one(monkeypatch, status="ok", ok=True)
    out = runner.run_batch([BV_GOOD], no_index=True)
    assert out[0]["ok"] is True

# ── 复核接口挂掉（429/超时）不许把片子静默丢掉（2026-11-15 实测） ─────────

def _cache_with_title(tmp, bv, duration, max_end, title="AI Agent 开发"):
    p = tmp / f"{bv}.json"
    p.write_text(json.dumps({
        "ok": True, "bvid": bv, "duration": duration, "title": title,
        "segments": [{"start": 0.0, "end": max_end, "text": "讲解" * 100}],
    }, ensure_ascii=False), encoding="utf-8")
    return p


@pytest.mark.parametrize("score,usable_expected", [(-2.0, True), (-9.0, False)])
def test_judge_api_failure_does_not_silently_drop(cache_in_tmp, monkeypatch,
                                                  score, usable_expected):
    """闸门的主题复核靠云端 LLM；它 429 的时候以前的写法等于判"字幕不可用"，
    整条视频静默消失。现在按本地 reranker 分数分流：强负当错轨（可去 ASR），弱负先放行。"""
    from backend.services.bilibili import quality

    _cache_with_title(cache_in_tmp, BV_GOOD, 600, 594)
    monkeypatch.setattr(quality, "video_topic", lambda bv, title: "AI Agent 开发")
    monkeypatch.setattr(quality, "topic_score", lambda topic, body: score)
    monkeypatch.setattr(
        quality, "judge_subtitle_llm",
        lambda topic, body: (None, "判不了：LLM call failed after 3 retries: HTTP 429"))
    ok, why = runner.subtitle_usable(BV_GOOD)
    assert ok is usable_expected
    assert ("先放行" in why) is usable_expected
    if not usable_expected:
        from backend.services.bilibili.reasons import subtitle_reason
        assert subtitle_reason(why) == "subtitle_off_topic"   # 让下游去试 ASR
