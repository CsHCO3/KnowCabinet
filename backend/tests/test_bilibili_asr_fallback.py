"""ASR 兜底接入主流程（施工单 07）：触发决策 / 救回 / no_speech / 环境降级 / 缓存契约。

红线：真跑 ASR、联网、读 1.5GB 模型一律不许（SPEC §10）。两条路都打桩：
- runner 兜底编排：桩掉 asr.transcribe / asr.available，只测触发决策与记账；
- asr.transcribe 本体：桩掉取流/下载/模型三段，驱动真实的缓存落盘与幂等逻辑。

记账断言走 run_batch（_record_run 在 batch 层调用，run_one 直调不落盘）。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from backend.services.bilibili import asr as asr_mod
from backend.services.bilibili.visual import digest, runner, seg_filter

# 规整假号（BV+10位）
BV_BAD = "BV7100000001"    # 官方轨覆盖 7%（对应实测 BV1LB4a6DEWL：轨是别的视频的）
BV_BAD2 = "BV7100000009"   # 批量串行测试的第二条
BV_GOOD = "BV7200000002"   # 字幕正常（对应实测 BV1wckaBwEjf：绝不能误触发）
BV_MUTE = "BV7300000003"   # ASR 判无语音（对应实测 BV1bLW76bExn：64s UI 演示）

SPEECH = "大家好今天我们讲一下这个模型的架构和实现细节还有工程上的取舍"


# ---------------- 公共桩 ----------------

@pytest.fixture
def env(tmp_path, monkeypatch):
    """字幕缓存/音频/模型根全指到 tmp。runner 与 asr 各有一份路径绑定，都要改。"""
    sub = tmp_path / "sub"
    sub.mkdir()
    map_sub = lambda bv, page=1: sub / f"{bv}.json"  # noqa: E731
    monkeypatch.setattr(runner, "subtitle_cache_path", map_sub)
    monkeypatch.setattr(runner, "ROOT", tmp_path / "visual")
    monkeypatch.setattr(asr_mod, "subtitle_cache_path", map_sub)
    monkeypatch.setattr(asr_mod, "AUDIO_DIR", tmp_path / "audio")
    monkeypatch.setattr(asr_mod, "MODEL_ROOT", tmp_path / "models")
    monkeypatch.setattr(asr_mod, "available", lambda: (True, "stub"))
    return tmp_path


@pytest.fixture
def offline(monkeypatch):
    """run_one 前段的联网桩：抓字幕/简介收集不联网。"""
    monkeypatch.setattr(runner, "ensure_subtitle",
                        lambda bv, force=False, page=1: {"title": "t"})
    monkeypatch.setattr(runner, "push_desc_resources", lambda bv, info: 0)


@pytest.fixture
def counters(monkeypatch):
    """下游花钱环节的桩：被调一次计一次。都带 page 形参（兜底救回后会真的走到）。"""
    seen = {"sections": 0, "scan": 0, "digest": 0, "apply": 0, "transcribe": 0}
    monkeypatch.setattr(runner, "get_sections",
                        lambda bv, model, page=1:
                        (seen.__setitem__("sections", seen["sections"] + 1), {"sections": []})[1])
    monkeypatch.setattr(seg_filter, "scan",
                        lambda bv, thr, page=1:
                        (seen.__setitem__("scan", seen["scan"] + 1),
                         {"keep_frames": [], "n_candidates": 0})[1])
    monkeypatch.setattr(digest, "digest_one",
                        lambda bv, mode, page=1:
                        (seen.__setitem__("digest", seen["digest"] + 1),
                         {"chars": 1, "path": "x"})[1])
    monkeypatch.setattr(runner, "apply_kb", lambda bv, force=True, page=1:
                        (seen.__setitem__("apply", seen["apply"] + 1), True)[1])
    return seen


@pytest.fixture
def runs_file(tmp_path, monkeypatch):
    p = tmp_path / "_runs.jsonl"
    monkeypatch.setattr(runner, "_RUNS_JSONL", p)
    return p


def write_official_cache(tmp, bv, duration, max_end, text="词"):
    """官方轨缓存：闸门只看 max(segments[].end)/duration；标题留空跳过主题闸（不打 reranker）。"""
    p = tmp / "sub" / f"{bv}.json"
    p.write_text(json.dumps({
        "ok": True, "bvid": bv, "title": "", "duration": duration,
        "segments": [{"start": 0.0, "end": max_end, "text": text}],
    }, ensure_ascii=False), encoding="utf-8")
    return p


def write_asr_cache(tmp, bv, desc=""):
    """ASR 缓存：覆盖 50s/60s=83% 过闸，2000 字远超 200 字下限，标题空跳过主题闸。"""
    p = tmp / "sub" / f"{bv}.json"
    segs = [{"start": float(i), "end": float(i) + 1, "text": "讲解" * 20}
            for i in range(50)]
    p.write_text(json.dumps({
        "ok": True, "bvid": bv, "page": 1, "page_count": 1, "title": "",
        "desc": desc,
        "duration": 60.0, "segments": segs,
        "source": "asr:faster-whisper-medium", "no_speech": False,
    }, ensure_ascii=False), encoding="utf-8")
    return p


def stub_transcribe(monkeypatch, tmp, counter, outcome="ok"):
    """桩掉 asr.transcribe。outcome: "ok"（写 ASR 缓存并成功）/ "no_speech" / "fail" /
    callable(bv, page) -> dict。"""
    def _t(bv, page=1, *, model="medium", compute="float16", force=False):
        counter["transcribe"] = counter.get("transcribe", 0) + 1
        if callable(outcome):
            return outcome(bv, page)
        if outcome == "fail":
            return {"ok": False, "bvid": bv, "page": page, "reason": "asr_failed",
                    "error": "download: HTTP 403", "elapsed_s": 3.1}
        no_speech = outcome == "no_speech"
        if not no_speech:
            write_asr_cache(tmp, bv)
        return {"ok": True, "bvid": bv, "page": page,
                "segments": 0 if no_speech else 50, "chars": 0 if no_speech else 2000,
                "duration": 60.0, "source": "asr:faster-whisper-medium",
                "no_speech": no_speech, "elapsed_s": 12.3,
                "reason": "no_speech" if no_speech else "asr_used", "reused": False}
    monkeypatch.setattr(asr_mod, "transcribe", _t)


def read_row(runs_file) -> dict:
    return json.loads(runs_file.read_text(encoding="utf-8").strip())


# ---------------- asr.transcribe 本体的桩脚手架 ----------------

class _FakeModel:
    """替 faster_whisper.WhisperModel：transcribe 返回 (段迭代器, info)。"""

    def __init__(self, rows):
        self.rows = rows          # [(start, end, text)]
        self.calls = 0

    def transcribe(self, audio, **kw):
        self.calls += 1
        info = SimpleNamespace(duration=60.0, language="zh", language_probability=0.99)
        return (iter(SimpleNamespace(start=a, end=b, text=t) for a, b, t in self.rows),
                info)


class _FakeSampler:
    def stop(self):
        return {"peak_vram_mb": 0}


def scaffold_transcribe(monkeypatch, model: _FakeModel):
    """把 transcribe 的网络段（meta/取流/下载）和模型段全换桩，只跑真实编排与落盘。"""
    monkeypatch.setattr(asr_mod, "_get_video_meta",
                        lambda sess, bv, page:
                        {"title": "t", "cid": 1, "duration": 60, "page_count": 1})
    monkeypatch.setattr(asr_mod, "_pick_audio_stream",
                        lambda sess, bv, cid:
                        {"url": "http://upos-sz-upcdn.bilivideo.com/a.m4s", "bandwidth": 1})
    monkeypatch.setattr(asr_mod, "_download_audio",
                        lambda sess, url, dst: (dst.parent.mkdir(parents=True, exist_ok=True),
                                                dst.write_bytes(b"x" * 1024), 1024)[2])
    monkeypatch.setattr(asr_mod, "_load_model", lambda m, c: model)
    monkeypatch.setattr(asr_mod, "_VramSampler", _FakeSampler)


# ── 1. 兜底救回：官方轨离题 → ASR → 复闸通过 → 入库记账 ───────────────

def test_rescue_ingests_and_records_asr_used(env, offline, counters,
                                             runs_file, monkeypatch):
    write_official_cache(env, BV_BAD, duration=211, max_end=15.38)
    stub_transcribe(monkeypatch, env, counters, "ok")
    out = runner.run_batch([BV_BAD], no_index=True, probe_veto=False,
                           asr_fallback=True)
    r = out[0]
    assert r["status"] == "ok" and r["ok"] is True
    assert r["sub_source"] == "asr"
    assert r["kb_ok"] is True
    assert r["reason"] == "asr_used"          # 救回的按 asr_used 记，不占"ok"
    assert r["asr_model"] == "medium"
    assert r["asr_elapsed"] == 12.3
    assert "sub_gate_asr" in r                # 兜底后确实复跑过闸门
    assert counters["transcribe"] == 1
    assert counters["digest"] == 1 and counters["apply"] == 1
    row = read_row(runs_file)
    assert row["reason"] == "asr_used" and row["status"] == "ok"
    assert row["retryable"] is False
    assert row["sub_source"] == "asr" and row["asr_model"] == "medium"


# ── 2. no_speech：合法结论，不入库、不可重试 ─────────────────────────

def test_no_speech_is_conclusion_not_failure(env, offline, counters,
                                             runs_file, monkeypatch):
    write_official_cache(env, BV_MUTE, duration=211, max_end=15.38)
    stub_transcribe(monkeypatch, env, counters, "no_speech")
    out = runner.run_batch([BV_MUTE], no_index=True, probe_veto=False,
                           asr_fallback=True)
    r = out[0]
    assert r["status"] == "no_speech"
    assert r["reason"] == "no_speech"
    assert r["sub_source"] == "asr"
    # 不入库：下游一个都不碰
    assert counters == {"sections": 0, "scan": 0, "digest": 0, "apply": 0,
                        "transcribe": 1}
    assert read_row(runs_file)["retryable"] is False


# ── 3. 不误触发：字幕正常时 ASR 一次都不许碰 ─────────────────────────

def test_good_subtitle_never_touches_asr(env, offline, counters, runs_file,
                                         monkeypatch):
    write_official_cache(env, BV_GOOD, duration=572, max_end=566, text="正" * 300)
    calls = {"transcribe": 0, "load_model": 0, "available": 0}
    stub_transcribe(monkeypatch, env, calls, "ok")
    monkeypatch.setattr(asr_mod, "_load_model",
                        lambda m, c: calls.__setitem__("load_model", calls["load_model"] + 1))
    monkeypatch.setattr(asr_mod, "available",
                        lambda: calls.__setitem__("available", calls["available"] + 1)
                        or (True, "stub"))
    r = runner.run_batch([BV_GOOD], no_index=True, probe_veto=False,
                         asr_fallback=True)[0]
    assert r["status"] == "ok"
    assert r["sub_source"] == "official"
    assert calls == {"transcribe": 0, "load_model": 0, "available": 0}


# ── 4. 环境缺失降级：行为同今天，不抛、不尝试 ─────────────────────────

def test_unavailable_env_degrades(env, offline, counters, runs_file, monkeypatch):
    write_official_cache(env, BV_BAD, duration=211, max_end=15.38)
    monkeypatch.setattr(asr_mod, "available", lambda: (False, "faster_whisper 未安装"))
    stub_transcribe(monkeypatch, env, counters, "ok")
    r = runner.run_batch([BV_BAD], no_index=True, probe_veto=False,
                         asr_fallback=True)[0]
    assert r["status"] == "no_usable_subtitle"      # 与今天的跳过行为一致
    assert r["reason"] == "asr_unavailable"
    assert counters["transcribe"] == 0              # 环境不全就不许碰转写
    assert read_row(runs_file)["retryable"] is True  # 装好再跑可能成


# ── 5. 触发决策表（SPEC §2，纯函数） ────────────────────────────────

def test_should_try_asr_decision_table():
    f = runner._should_try_asr
    assert f("没有字幕缓存", 0.0, 0) is True                     # 无轨
    assert f("字幕不像这条视频的（像歌词）", 0.94, 5000) is True  # 离题
    assert f("字幕只覆盖 30%（60s/200s）", 0.30, 5000) is True   # 覆盖 <0.5
    assert f("字幕只覆盖 65%（130s/200s）", 0.65, 5000) is False  # 0.5~0.8 带内不触发
    assert f("字幕只覆盖 7%（15s/211s）", 0.07, 48) is True      # 正文太短也触发
    assert f("字幕只覆盖 99%（594s/600s）", 0.99, 100) is True   # 太短优先于覆盖判定


# ── 6. --scan off 不触发 ASR；正文<200 字记 subtitle_too_short ───────

def test_scan_off_blocks_asr(env, offline, counters, runs_file, monkeypatch):
    write_official_cache(env, BV_BAD, duration=211, max_end=15.38)
    stub_transcribe(monkeypatch, env, counters, "ok")
    r = runner.run_batch([BV_BAD], no_index=True, probe_veto=False,
                         scan_mode="off", asr_fallback=True)[0]
    assert r["status"] == "no_usable_subtitle"
    assert r["reason"] == "subtitle_too_short"     # 壳子在内容空，正是这条 reason 的用法
    assert r["sub_source"] == "none"
    assert counters["transcribe"] == 0


# ── 7. 默认关：与今天逐字节同语义，只多空字段 ────────────────────────

def test_default_off_unchanged(env, offline, counters, runs_file, monkeypatch):
    write_official_cache(env, BV_BAD, duration=211, max_end=15.38)
    stub_transcribe(monkeypatch, env, counters, "ok")
    r = runner.run_batch([BV_BAD], no_index=True, probe_veto=False)[0]  # 不传 asr 参数
    assert r["status"] == "no_usable_subtitle"
    assert r["reason"] == "subtitle_low_coverage"
    assert "sub_source" not in r
    assert counters["transcribe"] == 0
    row = read_row(runs_file)
    assert row["sub_source"] == "" and row["asr_model"] == ""
    assert row["asr_elapsed"] == 0.0


# ── 8. is_asr_cache 三态 ──────────────────────────────────────────

def test_is_asr_cache_three_states(env):
    assert asr_mod.is_asr_cache(BV_BAD) is False                    # 文件不存在
    write_official_cache(env, BV_BAD, 211, 15.38)
    assert asr_mod.is_asr_cache(BV_BAD) is False                    # 官方轨（无 source）
    write_asr_cache(env, BV_BAD)
    assert asr_mod.is_asr_cache(BV_BAD) is True                     # ASR 轨


# ── 9. 转写落盘契约：官方轨备份 / 幂等 / 原子写 / 空段照写 ─────────────

def test_transcribe_backs_up_official_cache(env, monkeypatch):
    """覆盖前官方轨改名 .official_bad（不删）；ASR 缓存写到原路径，临时音频删掉。"""
    write_official_cache(env, BV_BAD, 211, 15.38)
    scaffold_transcribe(monkeypatch, _FakeModel([(0.0, 1.0, SPEECH)]))
    r = asr_mod.transcribe(BV_BAD)
    assert r["ok"] is True and r["reused"] is False and r["reason"] == "asr_used"
    bad = env / "sub" / f"{BV_BAD}.json.official_bad"
    assert bad.exists() and json.loads(bad.read_text(encoding="utf-8"))["duration"] == 211
    d = json.loads((env / "sub" / f"{BV_BAD}.json").read_text(encoding="utf-8"))
    assert d["source"] == "asr:faster-whisper-medium"
    assert d["no_speech"] is False and len(d["segments"]) == 1
    assert (env / "audio" / f"{BV_BAD}.m4s").exists() is False


def test_transcribe_reuses_cache_without_rerun(env, monkeypatch):
    """幂等：缓存已是 ASR 产物时直接返回，模型一次都不加载。"""
    write_asr_cache(env, BV_BAD)
    model = _FakeModel([])
    scaffold_transcribe(monkeypatch, model)
    r = asr_mod.transcribe(BV_BAD)
    assert r["reused"] is True and r["ok"] is True
    assert r["segments"] == 50 and r["chars"] == 2000
    assert model.calls == 0


def test_atomic_write_leaves_no_tmp_on_failure(env, monkeypatch):
    """os.replace 那一刻炸：目标不落盘、.tmp 清掉、官方轨备份完好（SPEC §7）。"""
    write_official_cache(env, BV_BAD, 211, 15.38)
    scaffold_transcribe(monkeypatch, _FakeModel([(0.0, 1.0, SPEECH)]))
    real_replace = asr_mod.os.replace

    def exploding_replace(src, dst):
        if str(src).endswith(".tmp"):     # 只炸"缓存落盘"那一步，备份改名照常走
            raise RuntimeError("disk full")
        return real_replace(src, dst)

    monkeypatch.setattr(asr_mod.os, "replace", exploding_replace)
    r = asr_mod.transcribe(BV_BAD)
    assert r["ok"] is False and r["reason"] == "asr_failed"
    sub = env / "sub"
    assert not (sub / f"{BV_BAD}.json").exists()
    assert not (sub / f"{BV_BAD}.json.tmp").exists()
    # 官方轨已经先备份成功，坏证据保留
    assert json.loads((sub / f"{BV_BAD}.json.official_bad")
                      .read_text(encoding="utf-8"))["duration"] == 211


def test_empty_segments_writes_no_speech_cache(env, monkeypatch):
    """VAD 0 段：合法结论，缓存照写（不写下次还会重跑），返回 reason=no_speech。"""
    scaffold_transcribe(monkeypatch, _FakeModel([]))
    r = asr_mod.transcribe(BV_MUTE)
    assert r["ok"] is True and r["no_speech"] is True and r["reason"] == "no_speech"
    d = json.loads((env / "sub" / f"{BV_MUTE}.json").read_text(encoding="utf-8"))
    assert d["no_speech"] is True and d["segments"] == []


def test_punctuation_only_counts_as_no_speech(env, monkeypatch):
    """有段但全文去标点后不足 10 字（感叹词/音乐标签幻听）也归 no_speech。"""
    rows = [(float(i), float(i) + 1, "啦！") for i in range(3)]
    scaffold_transcribe(monkeypatch, _FakeModel(rows))
    r = asr_mod.transcribe(BV_MUTE)
    assert r["no_speech"] is True and r["segments"] == 3
    d = json.loads((env / "sub" / f"{BV_MUTE}.json").read_text(encoding="utf-8"))
    assert d["no_speech"] is True


# ── 10. 串行约束：批量里 transcribe 不并发（INTERFACE §9） ────────────

def test_batch_runs_transcribe_serially(env, offline, counters,
                                        runs_file, monkeypatch):
    for bv in (BV_BAD, BV_BAD2):
        write_official_cache(env, bv, duration=211, max_end=15.38)
    state = {"active": 0, "max": 0}

    def outcome(bv, page=1):
        state["active"] += 1
        state["max"] = max(state["max"], state["active"])
        write_asr_cache(env, bv)
        state["active"] -= 1
        return {"ok": True, "bvid": bv, "page": page, "segments": 50, "chars": 2000,
                "duration": 60.0, "source": "asr:faster-whisper-medium",
                "no_speech": False, "elapsed_s": 1.0, "reason": "asr_used",
                "reused": False}

    stub_transcribe(monkeypatch, env, counters, outcome)
    out = runner.run_batch([BV_BAD, BV_BAD2], no_index=True, probe_veto=False,
                           asr_fallback=True)
    assert len(out) == 2 and all(r["status"] == "ok" for r in out)
    assert state["max"] == 1        # 同一进程内不会有两个 transcribe 同时跑

# ── 11. 无官方轨也要触发 ASR（SPEC §2 条件1；2026-11-11 补） ─────────────

def _raise_no_track(bv, force=False, page=1):
    raise RuntimeError(f"抓字幕失败 {bv}: No subtitle url found（业务性失败，不重试）")


def test_no_track_triggers_asr(env, offline, counters, runs_file, monkeypatch):
    """没有任何字幕轨时 ensure_subtitle 会抛错——以前直接跳过，兜底开关打开后要能救。
    同时确认简介（desc）跟着 ASR 缓存走，资源链接还能进 backlog（A12）。"""
    pushed = {}
    monkeypatch.setattr(runner, "ensure_subtitle", _raise_no_track)
    monkeypatch.setattr(runner, "push_desc_resources",
                        lambda bv, info: pushed.update(info) or 0)

    def outcome(bv, page=1):
        write_asr_cache(env, bv, desc="资源：https://github.com/x/y")
        return {"ok": True, "bvid": bv, "page": page, "segments": 50, "chars": 2000,
                "duration": 60.0, "source": "asr:faster-whisper-medium",
                "no_speech": False, "elapsed_s": 7.0, "reason": "asr_used",
                "reused": False}

    stub_transcribe(monkeypatch, env, counters, outcome)
    r = runner.run_batch([BV_MUTE], no_index=True, probe_veto=False,
                         asr_fallback=True)[0]
    assert r["status"] == "ok" and r["sub_source"] == "asr"
    assert r["reason"] == "asr_used"
    assert counters["digest"] == 1 and counters["apply"] == 1
    assert pushed.get("desc", "").startswith("资源：https://github.com/")
    row = read_row(runs_file)
    assert row["sub_source"] == "asr" and row["status"] == "ok"


def test_no_track_without_fallback_unchanged(env, offline, counters, runs_file,
                                             monkeypatch):
    """开关关着：无轨仍按老路记 no_subtitle，一次都不碰 ASR。"""
    monkeypatch.setattr(runner, "ensure_subtitle", _raise_no_track)
    stub_transcribe(monkeypatch, env, counters, "ok")
    r = runner.run_batch([BV_MUTE], no_index=True, probe_veto=False)[0]
    assert r["status"] == "no_subtitle"
    assert r["reason"] == "subtitle_missing"
    assert counters["transcribe"] == 0

def test_no_audio_stream_records_unretryable_reason(env, monkeypatch):
    """付费/充电专属：playurl 没有 dash.audio。要单独记 asr_no_audio 且不可重试，
    免得它一直挂在"值得补抓"清单里反复烧 GPU（用户 2026-11-15 明确）。"""
    monkeypatch.setattr(asr_mod, "_get_video_meta",
                        lambda sess, bv, page:
                        {"title": "t", "cid": 1, "duration": 60, "page_count": 1, "desc": ""})

    def _no_audio(sess, bv, cid):
        raise asr_mod.NoAudioStream("playurl: 响应里没有 dash.audio（付费/充电专属或区域限制）")

    monkeypatch.setattr(asr_mod, "_pick_audio_stream", _no_audio)
    r = asr_mod.transcribe(BV_MUTE)
    assert r["ok"] is False and r["reason"] == "asr_no_audio"
    assert asr_mod.is_asr_cache(BV_MUTE) is False        # 失败不落盘
    from backend.services.bilibili.reasons import classify_reason, is_retryable
    assert is_retryable("asr_no_audio") is False
    assert classify_reason("no_usable_subtitle", "playurl: 响应里没有 dash.audio") == "asr_no_audio"
