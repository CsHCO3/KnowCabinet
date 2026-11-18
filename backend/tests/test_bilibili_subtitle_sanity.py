"""字幕轨时间轴闸（2026-11-06）：字幕必须落在片长内，否则判为"别的视频的轨"。

背景：B站 ai-zh 接口会返回**别的视频**的字幕（BV1Ghpy6gE5x 片长 211s，
三次抓取分别是 0 段 / 160 段到 298s / 806 段到 1716.5s），中文占比与主题判别
都拦不住，只能靠时间轴。纯 mock，不联网。
"""
from __future__ import annotations

import copy

import pytest

from backend.services.bilibili import subtitle

BVID = "BV1Pkx76ZEQG"
TRACK = {"url": "https://sub.example.com/x.json", "lang": "ai-zh", "lang_doc": "AI字幕"}


def _seg(end: float, text: str = "你好") -> dict:
    return {"start": 0.0, "end": end, "start_ts": "00:00.000",
            "end_ts": "00:00.000", "text": text}


def _view(duration: int, pages: list | None = None) -> dict:
    return {"code": 0, "data": {
        "bvid": BVID, "aid": 1, "title": "测试视频", "desc": "",
        "cid": 111, "duration": duration,
        "owner": {"name": "up", "mid": 1}, "stat": {"view": 1},
        "pages": pages or [], "subtitle": {"list": []}, "pic": "", "pubdate": 0,
    }}


@pytest.fixture
def patched(monkeypatch):
    """给定 view 与字幕段，返回 (跑一次的函数)。"""
    def _run(view: dict, segs: list[dict], page: int | None = None):
        monkeypatch.setattr(subtitle, "_get_bili_cookie", lambda: "SESSDATA=t;")
        monkeypatch.setattr(subtitle, "_api_request", lambda *a, **k: copy.deepcopy(view))
        monkeypatch.setattr(subtitle, "_get_subtitle_tracks", lambda *a: [dict(TRACK)])
        monkeypatch.setattr(subtitle, "_fetch_best_subtitle",
                            lambda tracks, aid, cid, title, headers, max_try=3: (segs, dict(TRACK)))
        return subtitle.get_bilibili_timestamped_subtitle(BVID, page=page)
    return _run


# ── 单测：_timeline_sane ─────────────────────────────────────

@pytest.mark.parametrize("end,duration,expect", [
    (565.8, 572, True),      # 正常：P1 覆盖 99%
    (211, 211, True),
    (242.0, 211, True),      # 1.147 倍，仍在 15% 容差内
    (243.0, 211, False),     # 1.152 倍，超容差
    (298.0, 211, False),     # 实测：别的视频的轨
    (1716.5, 211, False),    # 实测：另一条更离谱的
    (0, 211, True),          # 空时间轴不拦（交给"空字幕"那条分支）
])
def test_timeline_sane_thresholds(end, duration, expect):
    segs = [_seg(end)] if end else []
    assert subtitle._timeline_sane(segs, duration) is expect


def test_timeline_sane_without_duration_is_permissive():
    """老接口给不出 duration 时不拦，保持旧行为。"""
    assert subtitle._timeline_sane([_seg(999)], 0) is True
    assert subtitle._timeline_sane([_seg(999)], None) is True


# ── 端到端（mock 网络）：坏轨必须判失败，好轨照常通过 ─────────

def test_cross_video_track_is_rejected(patched):
    r = patched(_view(211), [_seg(150.0), _seg(298.0)])
    assert r["ok"] is False
    assert "timeline exceeds video duration" in r["error"]
    assert r["subtitle_end"] == 298.0
    assert r["duration"] == 211


def test_normal_track_still_passes(patched):
    r = patched(_view(572), [_seg(1.0), _seg(565.8)])
    assert r["ok"] is True
    assert r["duration"] == 572
    assert len(r["segments"]) == 2


def test_rejection_uses_selected_page_duration(patched):
    """多P：容差按所选分P 的时长算，不按各P 之和。"""
    pages = [{"cid": 111, "duration": 572, "part": "普通话"},
             {"cid": 222, "duration": 549, "part": "粤语"}]
    r = patched(_view(1121, pages), [_seg(565.8)], page=1)
    assert r["ok"] is True          # 565.8 < 572*1.15，且远小于 1121
    assert r["duration"] == 572
