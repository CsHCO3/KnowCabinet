"""
B站视频字幕提取 — MCP server（兼容 mcp 2.x MCPServer / 1.x FastMCP）。

核心函数 get_bilibili_timestamped_subtitle(url) 可直接导入使用。
作为脚本运行时启动 MCP Server：
    python -m backend.services.bilibili.subtitle
"""

import gzip
import hashlib
import io
import json
import os
import re
import time
import urllib.parse
import urllib.request
from functools import reduce
from pathlib import Path
from typing import Any

# ── 路径引导 ──
import sys
_root = Path(__file__).resolve()
for _p in _root.parents:
    if (_p / "CLAUDE.md").exists() or (_p / "README.md").exists():
        _root = _p; break
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
# ──────────────

from backend.config.settings import Settings

if sys.platform == "win32":
    # 导入期不得替换调用方的 stdio 对象（M3 P2-2）：只原地调整编码，
    # 避免 pytest capture / FastMCP 等宿主进程的 wrapper 被换掉。
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure") and (_stream.encoding or "").lower() != "utf-8":
            _stream.reconfigure(encoding="utf-8")

_mcp = None


def _import_server_class():
    """兼容 mcp 2.x（MCPServer）与 1.x（FastMCP）——照抄 `kb/mcp_server.py` 的 shim。

    M6 复核裁定（2026-10-15）：本机 mcp 2.0.0 已移除 `mcp.server.fastmcp.FastMCP`
    （改名 `mcp.server.mcpserver.MCPServer`），旧的裸导入会让
    `python -m backend.services.bilibili.subtitle` 直接 ModuleNotFoundError。
    """
    try:
        from mcp.server.mcpserver import MCPServer  # mcp >= 2.0

        return MCPServer
    except Exception:  # pragma: no cover — 旧 SDK 兜底
        from mcp.server.fastmcp import FastMCP  # type: ignore

        return FastMCP


def _get_mcp():
    global _mcp
    if _mcp is None:
        cls = _import_server_class()
        _mcp = cls("bilibili-subbatch-subtitle")
    return _mcp

# ── 从 settings 获取 cookie ──────────────────────

_settings = Settings()


def _get_bili_cookie() -> str:
    cookie = _settings.bili_cookie
    if cookie:
        return cookie
    parts = []
    if _settings.bili_cookie_sessdata:
        parts.append(f"SESSDATA={_settings.bili_cookie_sessdata}")
    if _settings.bili_cookie_bili_jct:
        parts.append(f"bili_jct={_settings.bili_cookie_bili_jct}")
    if _settings.bili_cookie_dedeuserid:
        parts.append(f"DedeUserID={_settings.bili_cookie_dedeuserid}")
    return "; ".join(parts) + ";" if parts else ""


DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

BILI_HEADERS = {
    **DEFAULT_HEADERS,
    "Referer": "https://www.bilibili.com",
    "Origin": "https://www.bilibili.com",
}

API_VIDEO_VIEW = "https://api.bilibili.com/x/web-interface/view"
API_PLAYER_WBI_V2 = "https://api.bilibili.com/x/player/wbi/v2"
API_PLAYER_V2 = "https://api.bilibili.com/x/player/v2"
API_AI_SUBTITLE_STAT = "https://api.bilibili.com/x/player/v2/ai/subtitle/search/stat"
API_NAV = "https://api.bilibili.com/x/web-interface/nav"

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]


def _log(msg: str) -> None:
    print(f"[INFO] {msg}", file=sys.stderr)


def _http_get_text(url: str, headers: dict | None = None) -> str:
    hdrs = dict(DEFAULT_HEADERS)
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = resp.read()
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        return data.decode("utf-8", errors="replace")


def _api_request(url: str, params: dict | None = None, headers: dict | None = None) -> Any:
    final_url = url
    if params:
        final_url = f"{url}?{urllib.parse.urlencode(params)}"
    try:
        return json.loads(_http_get_text(final_url, headers=headers))
    except Exception as e:
        _log(f"Request failed: {e}")
        return None


def _extract_bvid(url_or_bvid: str) -> str | None:
    if re.match(r"^BV[a-zA-Z0-9]+$", url_or_bvid):
        return url_or_bvid
    patterns = [
        r"bilibili\.com/video/(BV[a-zA-Z0-9]+)",
        r"b23\.tv/(BV[a-zA-Z0-9]+)",
        r"(BV[a-zA-Z0-9]{10})",
    ]
    for pat in patterns:
        m = re.search(pat, url_or_bvid)
        if m:
            return m.group(1)
    return None


def _parse_page(url_or_bvid: str) -> int:
    """从 URL 里取 ?p=N，没有就返回 1；非法值（p=0 / p=abc）返回 1。"""
    m = re.search(r"[?&]p=(\d+)", url_or_bvid or "")
    if not m:
        return 1
    return max(1, int(m.group(1)))


def _timeline_sane(segments: list[dict], duration: float, tolerance: float = 1.15) -> bool:
    """字幕时间轴必须落在片长内，超出 15% 视为"这条轨不是本视频的"。

    实测（2026-11-06，BV1Ghpy6gE5x，片长 211s）B站 ai-zh 接口会返回**别的视频**
    的字幕，且每次不同：三次抓取分别是 0 段 / 160 段到 298s / 806 段到 1716.5s。
    这类轨中文占比正常、句子通顺，主题判别（reranker）会漏；只有时间轴能卡住。
    缺时长（老接口/异常）时不拦，保持旧行为。
    """
    if not segments or not duration:
        return True
    end = max(float(s.get("end") or 0) for s in segments)
    return end <= float(duration) * tolerance


def _format_ts(seconds: float) -> str:
    sec = float(max(seconds or 0.0, 0.0))
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:06.3f}"
    return f"{m:02d}:{s:06.3f}"


def _normalize_url(url: str) -> str:
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if not url.startswith("http://") and not url.startswith("https://"):
        return "https://" + url
    return url


def _parse_subtitle_list(subtitles: list[dict]) -> list[dict]:
    items = []
    for sub in subtitles or []:
        sub_url = _normalize_url(sub.get("subtitle_url", ""))
        if not sub_url:
            continue
        items.append({
            "url": sub_url,
            "lang": sub.get("lan", "unknown"),
            "lang_doc": sub.get("lan_doc", "unknown"),
        })
    return items


def _get_mixin_key(orig: str) -> str:
    return reduce(lambda s, i: s + orig[i], MIXIN_KEY_ENC_TAB, "")[:32]


def _get_wbi_keys(headers: dict) -> tuple[str | None, str | None]:
    data = _api_request(API_NAV, headers=headers)
    if data and data.get("code") == 0:
        wbi_img = data.get("data", {}).get("wbi_img", {})
        img_key = wbi_img.get("img_url", "").rsplit("/", 1)[-1].split(".")[0]
        sub_key = wbi_img.get("sub_url", "").rsplit("/", 1)[-1].split(".")[0]
        if img_key and sub_key:
            return img_key, sub_key
    return None, None


def _sign_wbi(params: dict, img_key: str, sub_key: str) -> dict:
    mixin_key = _get_mixin_key(img_key + sub_key)
    params = dict(params)
    params["wts"] = int(time.time())
    params = dict(sorted(params.items()))
    params = {k: "".join(c for c in str(v) if c not in "!'()*") for k, v in params.items()}
    query = urllib.parse.urlencode(params)
    params["w_rid"] = hashlib.md5((query + mixin_key).encode()).hexdigest()
    return params


def _download_segments(subtitle_url: str, headers: dict) -> list[dict]:
    try:
        data = json.loads(_http_get_text(subtitle_url, headers=headers))
    except Exception as e:
        _log(f"Download subtitle failed: {e}")
        return []

    body = data.get("body", [])
    segs = []
    for item in body:
        text = (item.get("content") or "").strip()
        if not text:
            continue
        start = float(item.get("from", 0.0) or 0.0)
        end = float(item.get("to", start) or start)
        if end < start:
            end = start
        segs.append({
            "start": round(start, 3),
            "end": round(end, 3),
            "start_ts": _format_ts(start),
            "end_ts": _format_ts(end),
            "text": text,
        })
    # 问题C：API 偶发乱序时按开始时间重排，段落顺序始终与时间轴一致
    segs.sort(key=lambda s: s["start"])
    return segs


_TRACK_PREFERRED = ["ai-zh", "zh-CN", "zh-Hans", "zh", "en"]


def _order_tracks(subtitles: list[dict]) -> list[dict]:
    def rank(item: dict) -> int:
        lang = item.get("lang", "")
        for idx, p in enumerate(_TRACK_PREFERRED):
            if p in lang:
                return idx
        return len(_TRACK_PREFERRED)

    return sorted(subtitles, key=rank)


def _choose_track(subtitles: list[dict]) -> dict | None:
    ordered = _order_tracks(subtitles)
    return ordered[0] if ordered else None


def _chinese_ratio(text: str) -> float:
    """CJK 字符占非空白字符比例（0.0-1.0）。"""
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return 0.0
    cjk = sum(1 for c in chars if "\u4e00" <= c <= "\u9fff")
    return cjk / len(chars)


def _fetch_best_subtitle(
    tracks: list[dict], aid: int, cid: int, title: str, headers: dict, max_try: int = 3,
) -> tuple[list[dict], dict | None]:
    """按轨道优先级抓字幕并做质量选择（问题B：轨道选错的换轨抢救）。

    依次尝试前 max_try 条轨道；轨道"正文过短"、"与标题主题不符"
    （reranker 可用时）或"非中文字幕"判为选错轨，换下一条重抓。
    全部不佳时返回文本最长的一条（退化为旧单轨行为，由下游闸门隔离），
    保证不比原来更差。正常情况第一条轨道即命中，无额外请求成本。
    """
    from backend.services.bilibili.quality import subtitle_off_topic, subtitle_too_short

    fallback: tuple[list[dict], dict | None] = ([], None)
    tried = 0
    for track in _order_tracks(tracks):
        if tried >= max_try:
            break
        url = track.get("url") or ""
        if not url and str(track.get("lang", "")).startswith("ai-"):
            url = _ai_subtitle_url(aid, cid, headers)
            if url:
                track = {**track, "url": url}
        if not url:
            continue
        tried += 1
        segs = _download_segments(url, headers)
        if not segs:
            continue
        text = "\n".join(s["text"] for s in segs)
        if _chinese_ratio(text) < 0.15:
            # 外语字幕（AI 兜底轨误标 / 英文原文轨）：宁可不抓，也不进 fallback
            _log(f"轨道 {track.get('lan', '?')} 非中文字幕(中文占比{_chinese_ratio(text):.0%})，跳过")
            continue
        if len(text) > len("\n".join(s["text"] for s in fallback[0])):
            fallback = (segs, track)
        if subtitle_too_short(text):
            _log(f"轨道 {track.get('lan', '?')} 正文过短({len(text)}字)，换下一轨")
            continue
        if subtitle_off_topic(title, text) is True:
            _log(f"轨道 {track.get('lan', '?')} 与标题主题不符，换下一轨")
            continue
        return segs, track
    return fallback


def _get_subtitle_tracks(aid: int, cid: int, bvid: str, headers: dict) -> list[dict]:
    tracks: list[dict] = []
    params = {"aid": aid, "cid": cid}
    img_key, sub_key = _get_wbi_keys(headers)
    if img_key and sub_key:
        wbi_data = _api_request(API_PLAYER_WBI_V2, params=_sign_wbi(params, img_key, sub_key), headers=headers)
        if wbi_data and wbi_data.get("code") == 0:
            tracks = _parse_subtitle_list(wbi_data.get("data", {}).get("subtitle", {}).get("subtitles", []))
    if not tracks:
        legacy_data = _api_request(API_PLAYER_V2, params={"cid": cid, "bvid": bvid}, headers=headers)
        if legacy_data and legacy_data.get("code") == 0:
            tracks = _parse_subtitle_list(legacy_data.get("data", {}).get("subtitle", {}).get("subtitles", []))
    return tracks


def _ai_subtitle_url(aid: int, cid: int, headers: dict) -> str:
    data = _api_request(API_AI_SUBTITLE_STAT, params={"aid": aid, "cid": cid}, headers=headers)
    if not (data and data.get("code") == 0 and data.get("data")):
        return ""
    return _normalize_url((data.get("data") or {}).get("subtitle_url", ""))


# ── 公开 API ─────────────────────────────────────

def get_bilibili_timestamped_subtitle(url: str, page: int | None = None) -> dict:
    """获取 B站视频时间轴字幕。

    page 缺省时从 URL 里的 ?p=N 解析（无则 P1）；显式传入则优先。
    多P 视频按所选分P 取 cid/时长（旧实现固定取 P1，时长还是各P 之和）。
    """
    bvid = _extract_bvid(url)
    if not bvid:
        return {"ok": False, "error": "Invalid Bilibili URL or BV id", "url": url}
    page = _parse_page(url) if page is None else page

    headers = dict(BILI_HEADERS)
    bili_cookie = _get_bili_cookie()
    if not bili_cookie:
        return {"ok": False, "error": "BILI_COOKIE not configured", "url": url}
    headers["Cookie"] = bili_cookie

    view = _api_request(API_VIDEO_VIEW, params={"bvid": bvid}, headers=headers)
    if not (view and view.get("code") == 0 and view.get("data")):
        return {"ok": False, "error": "view API failed", "url": url}

    data = view["data"]
    desc = data.get("desc", "")
    aid = data.get("aid")
    title = data.get("title", "")
    owner = data.get("owner", {})
    stat = data.get("stat", {})
    pic = data.get("pic", "")
    pubdate = data.get("pubdate", 0)
    videos = data.get("videos", 1)
    pages = data.get("pages", [])

    page_count = len(pages) or 1
    if page < 1 or page > page_count:
        return {"ok": False, "error": f"page out of range: {page} (total {page_count})",
                "url": url, "title": title, "aid": aid}
    if pages:
        pg = pages[page - 1]
        cid = pg.get("cid") or data.get("cid")
        duration = pg.get("duration") or data.get("duration", 0)
        page_title = pg.get("part", "")
    else:  # 老接口/异常没有 pages：退回现状（P1 语义）
        cid = data.get("cid")
        duration = data.get("duration", 0)
        page_title = ""

    if not aid or not cid:
        return {"ok": False, "error": "Missing aid/cid", "url": url, "title": title, "aid": aid, "cid": cid}

    tracks = _parse_subtitle_list(data.get("subtitle", {}).get("list", []))
    if page > 1:
        # view 接口的 subtitle.list 挂在 P1 的 cid 上；P2+ 必须按该分P 的 cid 重新取轨
        tracks = _get_subtitle_tracks(aid, cid, bvid, headers)
    elif not tracks:
        tracks = _get_subtitle_tracks(aid, cid, bvid, headers)
    # 非中文 AI 翻译轨（ai-es/ai-ko 等）宁可不抓：那是外语视频的机器字幕，
    # 抓下来必然被 off-topic 闸门隔离，白费 reranker 算力还污染隔离名单原因
    foreign = [t for t in tracks if str(t.get("lang", "")).startswith("ai-")
               and not str(t.get("lang", "")).startswith("ai-zh")]
    tracks = [t for t in tracks if t not in foreign]
    if not tracks:
        if foreign:
            return {"ok": False,
                    "error": "No Chinese subtitle track (only foreign/AI-translation)",
                    "url": url, "title": title, "aid": aid, "cid": cid,
                    "foreign_langs": [t.get("lang") for t in foreign]}
        # 轨道列表全空时兜底尝试 AI 字幕接口
        ai_url = _ai_subtitle_url(aid, cid, headers)
        if ai_url:
            tracks = [{"url": ai_url, "lang": "ai-zh", "lang_doc": "自动生成字幕"}]

    segments, selected = _fetch_best_subtitle(tracks, aid, cid, title, headers)

    if not selected or not selected.get("url"):
        return {"ok": False, "error": "No subtitle url found", "url": url, "title": title, "aid": aid, "cid": cid}

    if not segments:
        return {
            "ok": False, "error": "Subtitle file is empty or unsupported",
            "url": url, "title": title, "aid": aid, "cid": cid,
            "subtitle_url": selected["url"],
        }

    if not _timeline_sane(segments, duration):
        end = max(float(s.get("end") or 0) for s in segments)
        return {
            "ok": False,
            "error": (f"subtitle timeline exceeds video duration "
                      f"({end:.0f}s > {duration}s): track likely belongs to another video"),
            "url": url, "title": title, "aid": aid, "cid": cid,
            "duration": duration, "subtitle_end": round(end, 1),
            "page": page, "page_count": page_count,
            "subtitle_url": selected["url"],
        }

    return {
        "ok": True,
        "platform": "bilibili",
        "source": "subbatch_api_chain",
        "url": url,
        "bvid": bvid,
        "title": title,
        "desc": desc,
        "owner": owner,
        "stat": stat,
        "duration": duration,
        "pic": pic,
        "pubdate": pubdate,
        "videos": videos,
        "pages": pages,
        "page": page,
        "page_count": page_count,
        "page_title": page_title,
        "aid": aid,
        "cid": cid,
        "language": selected.get("lang", "unknown"),
        "language_doc": selected.get("lang_doc", "unknown"),
        "subtitle_url": selected["url"],
        "segments": segments,
        "text": "\n".join(seg["text"] for seg in segments),
    }


def _mcp_get_subtitle(url: str) -> dict:
    """MCP 工具注册 — 获取B站视频字幕。"""
    return get_bilibili_timestamped_subtitle(url)


# 瞬时性错误（网络抖动 / 服务端抖动 / 坏轨）：值得重试。
# 业务性结论（URL 无效 / 没 cookie / 视频确实无中文字幕）重试无意义，立即返回。
_TRANSIENT_ERRORS = (
    "view API failed",
    "Subtitle file is empty",
    "Request failed",
    "Download subtitle failed",
    "No subtitle url found",                      # 轨道接口间歇性返回空
    "subtitle timeline exceeds video duration",   # 抓到了别的视频的轨，重取通常能换回来
)


def is_transient_error(err: str) -> bool:
    """错误是否瞬时（值得重试）。两条字幕线共用这一个判定，别各写一套。"""
    return any(k in str(err or "") for k in _TRANSIENT_ERRORS)


def get_bilibili_subtitle_with_retry(url: str, attempts: int = 3, backoff: float = 5.0) -> dict:
    """P-10：字幕抓取的瞬时失败自动重试（退避递增）。

    实测失败过的视频事后重抓都能成功，属瞬时网络问题，不该直接判死。
    业务性失败（URL 无效 / 未配置 cookie / 视频确实无字幕）立即返回。
    """
    result: dict = {"ok": False}
    for attempt in range(1, attempts + 1):
        result = get_bilibili_timestamped_subtitle(url)
        if result.get("ok"):
            return result
        err = str(result.get("error", ""))
        if attempt == attempts or not is_transient_error(err):
            return result
        wait = backoff * attempt
        _log(f"瞬时失败({err})，{wait:.0f}s 后重试 {attempt + 1}/{attempts}")
        time.sleep(wait)
    return result


if __name__ == "__main__":
    mcp = _get_mcp()
    mcp.tool()(_mcp_get_subtitle)
    mcp.run()
