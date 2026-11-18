"""B站视频收集服务 — 合集关联 + 作者空间批量查询。

提供两种"找相关视频"的能力：
  1. 合集关联: 从某视频查出它所属的合集，再拉取合集中全部视频
  2. 作者空间: 从作者 mid (space.bilibili.com/mid) 查询其全部视频，
     支持按最新发布 / 最多播放 / 最多收藏排序

依赖 Settings 中的 bili_cookie_* 配置。
适合配合字幕提取 pipeline 做"发现→筛选→提取"链路。
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.parse
import urllib.request
from functools import reduce
from pathlib import Path
from typing import Any

from backend.config.settings import Settings

# ── WBI 签名表 ──
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]


# ── 内部工具 ────────────────────────────────────

def _build_headers() -> dict[str, str]:
    s = Settings()
    cookie = s.bili_cookie
    if not cookie:
        parts = []
        if s.bili_cookie_sessdata:
            parts.append(f"SESSDATA={s.bili_cookie_sessdata}")
        if s.bili_cookie_bili_jct:
            parts.append(f"bili_jct={s.bili_cookie_bili_jct}")
        if s.bili_cookie_dedeuserid:
            parts.append(f"DedeUserID={s.bili_cookie_dedeuserid}")
        cookie = "; ".join(parts)
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Referer": "https://www.bilibili.com",
        "Cookie": cookie,
    }


def _api_get(url: str) -> dict[str, Any] | None:
    headers = _build_headers()
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"  [bili_collect] API error: {e}")
        return None


def _get_mixin_key(orig: str) -> str:
    return reduce(lambda s, i: s + orig[i], MIXIN_KEY_ENC_TAB, "")[:32]


def _sign_wbi(params: dict, img_key: str, sub_key: str) -> dict:
    """对参数字典进行 WBI 签名，返回带 wts / w_rid 的新字典。"""
    mixin_key = _get_mixin_key(img_key + sub_key)
    p = dict(params)
    p["wts"] = int(time.time())
    p = dict(sorted(p.items()))
    p = {k: "".join(c for c in str(v) if c not in "!'()*") for k, v in p.items()}
    query = urllib.parse.urlencode(p)
    p["w_rid"] = hashlib.md5((query + mixin_key).encode()).hexdigest()
    return p


def _fetch_wbi_keys() -> tuple[str | None, str | None]:
    """从导航接口获取 WBI img_key / sub_key。"""
    data = _api_get("https://api.bilibili.com/x/web-interface/nav")
    if data and data.get("code") == 0:
        wbi_img = data.get("data", {}).get("wbi_img", {})
        img_key = wbi_img.get("img_url", "").rsplit("/", 1)[-1].split(".")[0]
        sub_key = wbi_img.get("sub_url", "").rsplit("/", 1)[-1].split(".")[0]
        if img_key and sub_key:
            return img_key, sub_key
    return None, None


# ── 视频 URL → BVID 提取 ──────────────────────

def extract_bvid(url_or_bvid: str) -> str | None:
    """从 URL 或纯字符串中提取 BVID。"""
    import re
    if re.match(r"^BV[a-zA-Z0-9]+$", url_or_bvid):
        return url_or_bvid
    for pat in [
        r"bilibili\.com/video/(BV[a-zA-Z0-9]+)",
        r"b23\.tv/(BV[a-zA-Z0-9]+)",
        r"(BV[a-zA-Z0-9]{10})",
    ]:
        m = re.search(pat, url_or_bvid)
        if m:
            return m.group(1)
    return None


def extract_mid(space_url_or_mid: str | int) -> int | None:
    """从 space.bilibili.com/xxx 或纯数字提取 mid。"""
    import re
    if isinstance(space_url_or_mid, int) or space_url_or_mid.isdigit():
        return int(space_url_or_mid)
    m = re.search(r"space\.bilibili\.com/(\d+)", space_url_or_mid)
    return int(m.group(1)) if m else None


def get_video_view(bvid: str) -> dict[str, Any] | None:
    """查询单个视频的 view API 完整数据。

    对 enrich 阶段（Stage 2）批量补全视频信息很有用。
    返回 data 字段（解析后的 dict），或 None（视频不存在/API 失败）。
    """
    data = _api_get(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}")
    if data and data.get("code") == 0 and data.get("data"):
        return data["data"]
    return None


# ═══════════════════════════════════════════════
# 能力 1：合集关联
# ═══════════════════════════════════════════════

def get_video_collection(url_or_bvid: str) -> dict[str, Any] | None:
    """查询视频所属合集 (ugc_season)。

    返回示例:
        {
            "ok": True,
            "season_id": 7217205,
            "title": "基础设施与企业架构",
            "mid": 413210446,
            "cover": "https://...",
            "intro": "",
            "count": 12,
            "videos": [
                {
                    "aid": ...,
                    "bvid": "BVxxx",
                    "title": "...",
                    "duration": 913,
                    "pubdate": 1765603788,
                    "pic": "https://...",
                    "view": 1633,
                    "like": 28,
                    "coin": 4,
                    "fav": 23,
                    "share": 9,
                    "danmaku": 0,
                    "reply": 8,
                },
                ...
            ],
        }

    如果视频不属于任何合集，返回 {"ok": False, "reason": "not_in_season"}。
    """
    bvid = extract_bvid(url_or_bvid)
    if not bvid:
        return {"ok": False, "error": "invalid_bvid"}

    data = _api_get(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}")
    if not data or data.get("code") != 0:
        return {"ok": False, "error": "view_api_failed", "detail": data}

    ugc = data.get("data", {}).get("ugc_season")
    if not ugc:
        return {"ok": False, "reason": "not_in_season"}

    season_id = ugc.get("id")
    videos: list[dict] = []
    for sec in ugc.get("sections", []):
        for ep in sec.get("episodes", []):
            arc = ep.get("arc", {})
            stat = arc.get("stat", {})
            author = arc.get("author", {})
            videos.append({
                "aid": ep.get("aid"),
                "cid": ep.get("cid"),
                "bvid": arc.get("bvid", ""),
                "title": arc.get("title", ""),
                "duration": arc.get("duration", 0),
                "pubdate": arc.get("pubdate", 0),
                "pic": arc.get("pic", ""),
                "view": stat.get("view", 0),
                "like": stat.get("like", 0),
                "coin": stat.get("coin", 0),
                "fav": stat.get("fav", 0),
                "share": stat.get("share", 0),
                "danmaku": stat.get("danmaku", 0),
                "reply": stat.get("reply", 0),
                "owner_name": author.get("name", ""),
                "owner_mid": author.get("mid", 0),
            })

    return {
        "ok": True,
        "season_id": season_id,
        "title": ugc.get("title", ""),
        "mid": ugc.get("mid"),
        "cover": ugc.get("cover", ""),
        "intro": ugc.get("intro", ""),
        "count": len(videos),
        "videos": videos,
    }


# ═══════════════════════════════════════════════
# 能力 2：作者空间查询
# ═══════════════════════════════════════════════

def get_author_info(mid: int) -> dict[str, Any]:
    """获取作者基本信息 (name, sign, level, face, ...)。"""
    data = _api_get(f"https://api.bilibili.com/x/space/acc/info?mid={mid}")
    if data and data.get("code") == 0:
        d = data["data"]
        return {
            "mid": mid,
            "name": d.get("name", ""),
            "sign": d.get("sign", ""),
            "level": d.get("level", 0),
            "face": d.get("face", ""),
            "birthday": d.get("birthday", ""),
            "gender": d.get("gender", 0),
        }
    return {"mid": mid, "name": "", "error": data.get("message", "unknown") if data else "no_response"}


def get_author_stat(mid: int) -> dict[str, Any]:
    """获取作者统计数据 (following, follower, video_count, likes)。"""
    data = _api_get(f"https://api.bilibili.com/x/relation/stat?vmid={mid}")
    if data and data.get("code") == 0:
        d = data["data"]
        return {
            "following": d.get("following", 0),
            "follower": d.get("follower", 0),
            "video_count": d.get("video", 0),
            "article_count": d.get("article", 0),
            "likes": d.get("likes", 0),
        }
    return {}


def get_author_videos(
    mid: int,
    order: str = "pubdate",
    page: int = 1,
    page_size: int = 30,
) -> dict[str, Any]:
    """获取作者上传的视频列表（需要 WBI 签名）。

    Args:
        mid: 作者用户 ID。
        order: 排序方式。
            "pubdate" — 最新发布
            "click"   — 最多播放
            "stow"    — 最多收藏
        page: 页码，从 1 开始。
        page_size: 每页条数（最大 30）。

    Returns:
        {
            "ok": True,
            "mid": ...,
            "order": ...,
            "page": {"pn": 1, "ps": 30, "count": 48},
            "videos": [
                {
                    "bvid": "BVxxx",
                    "title": "...",
                    "play": 1234,
                    "comment": 56,
                    "favorite": 78,
                    "length": "10:46",
                    "pubdate": 1765603788,
                    "pic": "https://...",
                },
            ],
        }
    """
    img_key, sub_key = _fetch_wbi_keys()
    if not img_key or not sub_key:
        return {"ok": False, "error": "wbi_key_fetch_failed"}

    params = _sign_wbi(
        {"mid": mid, "pn": page, "ps": min(page_size, 30), "order": order},
        img_key,
        sub_key,
    )
    url = f"https://api.bilibili.com/x/space/wbi/arc/search?{urllib.parse.urlencode(params)}"
    data = _api_get(url)
    if not data or data.get("code") != 0:
        return {
            "ok": False,
            "error": data.get("message", "api_failed") if data else "no_response",
            "code": data.get("code") if data else -1,
        }

    d = data.get("data", {})
    page_info = d.get("page", {})
    raw_list = d.get("list", {}).get("vlist", [])

    videos = []
    for v in raw_list:
        videos.append({
            "bvid": v.get("bvid", ""),
            "aid": v.get("aid", 0),
            "title": v.get("title", ""),
            "play": v.get("play", 0),
            "comment": v.get("comment", 0),
            "favorite": v.get("favorites", 0),
            "length": str(v.get("length", "?")),
            "pubdate": v.get("pubdate", 0),
            "pic": v.get("pic", ""),
            "description": v.get("description", ""),
        })

    return {
        "ok": True,
        "mid": mid,
        "order": order,
        "page": {
            "pn": page_info.get("pn", page),
            "ps": page_info.get("ps", page_size),
            "count": page_info.get("count", 0),
        },
        "videos": videos,
    }


def get_all_author_videos(
    mid: int,
    order: str = "pubdate",
    max_pages: int = 10,
) -> list[dict[str, Any]]:
    """遍历分页获取作者全部视频。

    每页 30 条，自动翻页直到取完或达到 max_pages。
    """
    all_videos: list[dict] = []
    for pn in range(1, max_pages + 1):
        result = get_author_videos(mid, order=order, page=pn, page_size=30)
        if not result.get("ok"):
            break
        page_videos = result.get("videos", [])
        if not page_videos:
            break
        all_videos.extend(page_videos)
        total = result.get("page", {}).get("count", 0)
        if len(all_videos) >= total:
            break
        time.sleep(0.3)  # 礼貌性延迟
    return all_videos


# ═══════════════════════════════════════════════
# 便利函数
# ═══════════════════════════════════════════════

def resolve_space_url(space_url: str) -> dict[str, Any]:
    """从 space.bilibili.com/xxx 一次性获取作者信息和视频列表。

    返回:
        {
            "info": {...},   # get_author_info
            "stat": {...},   # get_author_stat
            "videos_latest": [...],   # order=pubdate
            "videos_top": [...],      # order=click
        }
    """
    mid = extract_mid(space_url)
    if not mid:
        return {"ok": False, "error": "invalid_space_url", "url": space_url}

    info = get_author_info(mid)
    stat = get_author_stat(mid)
    latest = get_author_videos(mid, order="pubdate")
    top = get_author_videos(mid, order="click")

    return {
        "ok": True,
        "mid": mid,
        "info": info,
        "stat": stat,
        "videos_latest": latest.get("videos", []),
        "videos_top": top.get("videos", []),
    }


# ═══════════════════════════════════════════════
# 评论获取
# ═══════════════════════════════════════════════

def get_comments(
    aid: int,
    page: int = 1,
    page_size: int = 20,
    sort: int = 2,
) -> dict[str, Any]:
    """获取视频的评论区内容。

    Args:
        aid: 视频 AV 号（从 view API 获取）。
        page: 页码，从 1 开始。
        page_size: 每页条数（最大 50）。
        sort: 排序，1=按时间, 2=按热度。

    Returns:
        {
            "ok": True,
            "aid": ...,
            "total": 355,
            "page": 1,
            "comments": [
                {
                    "rpid": ...,
                    "mid": ...,
                    "username": "...",
                    "content": "...",
                    "like": 123,
                    "ctime": 1782152785,
                    "reply_count": 6,
                    "replies": [...]   # 子评论平铺
                }
            ]
        }
    """
    data = _api_get(
        f"https://api.bilibili.com/x/v2/reply?"
        f"type=1&oid={aid}&pn={page}&ps={min(page_size, 50)}&sort={sort}"
    )
    if not data or data.get("code") != 0:
        return {
            "ok": False,
            "error": data.get("message", "api_failed") if data else "no_response",
        }

    d = data.get("data", {})
    page_info = d.get("page", {})
    replies = d.get("replies", []) or []

    comments = []
    for r in replies:
        member = r.get("member", {})
        content = r.get("content", {})

        # 子回复
        sub_replies = []
        for sr in (r.get("replies") or []):
            sm = sr.get("member", {})
            sc = sr.get("content", {})
            sub_replies.append({
                "rpid": sr.get("rpid"),
                "mid": sr.get("mid"),
                "username": sm.get("uname", ""),
                "content": sc.get("message", ""),
                "like": sr.get("like", 0),
                "ctime": sr.get("ctime", 0),
            })

        comments.append({
            "rpid": r.get("rpid"),
            "mid": r.get("mid"),
            "username": member.get("uname", ""),
            "user_level": member.get("level_info", {}).get("current_level", 0),
            "content": content.get("message", ""),
            "like": r.get("like", 0),
            "ctime": r.get("ctime", 0),
            "reply_count": r.get("rcount", 0),
            "replies": sub_replies,
        })

    return {
        "ok": True,
        "aid": aid,
        "total": page_info.get("total", 0),
        "page": page_info.get("num", page),
        "page_size": page_info.get("size", page_size),
        "comments": comments,
    }


def get_video_tags(bvid: str) -> list[str]:
    """视频官方标签（UP主投稿时手打）。免 WBI；失效视频/无标签返回空列表。"""
    data = _api_get(f"https://api.bilibili.com/x/tag/archive/tags?bvid={bvid}")
    if not data or data.get("code") != 0 or not isinstance(data.get("data"), list):
        return []
    return [str(t.get("tag_name", "")).strip() for t in data["data"]
            if t.get("tag_name")]
