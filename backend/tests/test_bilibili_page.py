"""分P 支持（施工单 A）：?p= 解析 / 按 P 选 cid 与时长 / 缓存命名 / 越界拒绝。

纯 mock，不联网：monkeypatch subtitle._api_request 返回固定 view JSON。
"""
from __future__ import annotations

import copy

import pytest

from backend.services.bilibili import subtitle
from backend.services.bilibili.visual.paths import subtitle_cache_path

BVID = "BV1Pkx76ZEQG"
CID_P1, DUR_P1, PART_P1 = 43126816927, 572, "普通话"
CID_P2, DUR_P2, PART_P2 = 43126820711, 549, "粤语"

TRACK = {"url": "https://sub.example.com/x.json", "lang": "ai-zh", "lang_doc": "AI字幕"}
SEG = {"start": 1.0, "end": 2.0, "start_ts": "00:01.000", "end_ts": "00:02.000", "text": "你好"}

# 多P 样本：data.duration=1121 是各P 之和（view 接口对多P 的既有行为）
VIEW_MULTI = {
    "code": 0,
    "data": {
        "bvid": BVID, "aid": 1120045176, "title": "测试多P视频", "desc": "d",
        "cid": CID_P1, "duration": DUR_P1 + DUR_P2,
        "owner": {"name": "up", "mid": 1}, "stat": {"view": 10},
        "pages": [
            {"cid": CID_P1, "duration": DUR_P1, "part": PART_P1},
            {"cid": CID_P2, "duration": DUR_P2, "part": PART_P2},
        ],
        "subtitle": {"list": []},
    },
}

# 单P 样本：无 pages（老接口形态），cid/duration 挂在顶层
VIEW_SINGLE = {
    "code": 0,
    "data": {
        "bvid": "BV1Ghpy6gE5x", "aid": 1, "title": "单P视频", "desc": "",
        "cid": 999, "duration": 211,
        "owner": {"name": "up", "mid": 1}, "stat": {"view": 1},
        "pages": [],
        "subtitle": {"list": []},
    },
}


@pytest.fixture
def mock_view(monkeypatch):
    """固定 view JSON + 假字幕轨 + 假字幕内容；captured 记录下游拿到的 cid。"""
    captured: dict = {}
    monkeypatch.setattr(subtitle, "_get_bili_cookie", lambda: "SESSDATA=test;")
    monkeypatch.setattr(subtitle, "_api_request",
                        lambda *a, **k: copy.deepcopy(VIEW_MULTI))

    def fake_tracks(aid, cid, bvid, headers):
        captured["track_cid"] = cid
        return [dict(TRACK)]

    def fake_fetch(tracks, aid, cid, title, headers, max_try=3):
        captured["fetch_cid"] = cid
        return [dict(SEG)], tracks[0]

    monkeypatch.setattr(subtitle, "_get_subtitle_tracks", fake_tracks)
    monkeypatch.setattr(subtitle, "_fetch_best_subtitle", fake_fetch)
    return captured


# ── A1: ?p= 解析 ─────────────────────────────────────────────

@pytest.mark.parametrize("url,expect", [
    ("https://www.bilibili.com/video/BVxxx?p=3", 3),
    ("https://www.bilibili.com/video/BVxxx?spm_id_from=x&p=3", 3),
    ("BVxxx?p=12", 12),
    ("BVxxx", 1),
    ("https://www.bilibili.com/video/BVxxx", 1),
    ("https://www.bilibili.com/video/BVxxx?p=0", 1),
    ("https://www.bilibili.com/video/BVxxx?p=abc", 1),
    ("https://www.bilibili.com/video/BVxxx?p=", 1),
    ("", 1),
    ("BVxxx?pp=3", 1),      # 别的参数不误伤
])
def test_parse_page_variants(url, expect):
    assert subtitle._parse_page(url) == expect


# ── A2/A3: 按 P 选 cid 与时长 ────────────────────────────────

def test_default_page_is_p1_and_uses_page_duration(mock_view):
    """无 ?p= 默认 P1；时长取 P1 自己的 572，不再是各P 之和 1121。"""
    r = subtitle.get_bilibili_timestamped_subtitle(BVID)
    assert r["ok"] is True
    assert (r["page"], r["page_count"], r["page_title"]) == (1, 2, PART_P1)
    assert r["cid"] == CID_P1
    assert r["duration"] == DUR_P1
    assert mock_view["track_cid"] == CID_P1


def test_url_p_parameter_selects_page(mock_view):
    url = f"https://www.bilibili.com/video/{BVID}?p=2"
    r = subtitle.get_bilibili_timestamped_subtitle(url)
    assert r["ok"] is True
    assert (r["page"], r["page_count"], r["page_title"]) == (2, 2, PART_P2)
    assert r["cid"] == CID_P2
    assert r["duration"] == DUR_P2
    # 取字幕轨与字幕内容用的都是所选分P 的 cid
    assert mock_view["track_cid"] == CID_P2
    assert mock_view["fetch_cid"] == CID_P2


def test_explicit_page_wins_over_url(mock_view):
    url = f"https://www.bilibili.com/video/{BVID}?p=2"
    r = subtitle.get_bilibili_timestamped_subtitle(url, page=1)
    assert r["ok"] is True
    assert r["cid"] == CID_P1 and r["duration"] == DUR_P1


# ── A3: 越界报错 ─────────────────────────────────────────────

@pytest.mark.parametrize("page", [3, 0, -1])
def test_page_out_of_range_rejected(mock_view, page):
    r = subtitle.get_bilibili_timestamped_subtitle(BVID, page=page)
    assert r["ok"] is False
    assert "page out of range" in r["error"]
    assert "2" in r["error"]


# ── A2: pages 为空（老接口/异常）退回现状 ────────────────────

def test_no_pages_falls_back_to_legacy(monkeypatch):
    view = copy.deepcopy(VIEW_SINGLE)
    monkeypatch.setattr(subtitle, "_get_bili_cookie", lambda: "SESSDATA=test;")
    monkeypatch.setattr(subtitle, "_api_request", lambda *a, **k: view)
    monkeypatch.setattr(subtitle, "_get_subtitle_tracks", lambda *a: [dict(TRACK)])
    monkeypatch.setattr(
        subtitle, "_fetch_best_subtitle",
        lambda tracks, aid, cid, title, headers, max_try=3: ([dict(SEG)], tracks[0]))
    r = subtitle.get_bilibili_timestamped_subtitle("BV1Ghpy6gE5x")
    assert r["ok"] is True
    assert (r["page"], r["page_count"], r["page_title"]) == (1, 1, "")
    assert r["cid"] == 999 and r["duration"] == 211


# ── A5: 缓存命名 ─────────────────────────────────────────────

def test_subtitle_cache_path_p1_keeps_legacy_name():
    assert subtitle_cache_path("BVx", 1).name == "BVx.json"
    assert subtitle_cache_path("BVx").name == "BVx.json"          # 缺省即 P1


def test_subtitle_cache_path_p2_and_beyond():
    assert subtitle_cache_path("BVx", 2).name == "BVx_p2.json"
    assert subtitle_cache_path("BVx", 3).name == "BVx_p3.json"
    assert subtitle_cache_path("BVx", 2).parent == subtitle_cache_path("BVx", 1).parent
