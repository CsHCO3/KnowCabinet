"""B站收藏夹导出服务。

提供收藏夹列表查询、视频列表拉取、CSV 导出功能。
"""
from __future__ import annotations

import csv
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from backend.config.settings import Settings


def _build_cookie_header() -> str:
    s = Settings()
    cookie = s.bili_cookie
    if cookie:
        return cookie
    parts = []
    if s.bili_cookie_sessdata:
        parts.append(f"SESSDATA={s.bili_cookie_sessdata}")
    if s.bili_cookie_bili_jct:
        parts.append(f"bili_jct={s.bili_cookie_bili_jct}")
    if s.bili_cookie_dedeuserid:
        parts.append(f"DedeUserID={s.bili_cookie_dedeuserid}")
    return "; ".join(parts)


def _get_headers() -> dict[str, str]:
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Referer": "https://space.bilibili.com/",
        "Cookie": _build_cookie_header(),
    }


def _api_get(url: str, retries: int = 3) -> dict[str, Any] | None:
    """GET 收藏夹相关 API。412 是 B 站风控（请求过频触发），指数退避重试。"""
    headers = _get_headers()
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, headers=headers)
        try:
            resp = urllib.request.urlopen(req, timeout=15)
            return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 412 and attempt < retries:
                delay = 30 * attempt
                print(f"\n  [风控412] 等待 {delay}s 后重试 ({attempt}/{retries - 1})")
                time.sleep(delay)
                continue
            print(f"  [错误] HTTP {e.code}: {e.reason}")
            return None
        except Exception as e:
            print(f"  [错误] {e}")
            return None
    return None


def get_current_user() -> dict[str, Any]:
    """获取当前登录用户信息。返回 {"uname", "mid"} 或抛出异常。"""
    data = _api_get("https://api.bilibili.com/x/web-interface/nav")
    if not data or data.get("code") != 0:
        raise RuntimeError(f"Cookie 无效或已过期: {data}")
    return {
        "uname": data.get("data", {}).get("uname", "?"),
        "mid": data.get("data", {}).get("mid", 0),
    }


def list_folders() -> list[dict[str, Any]]:
    """列出当前用户的所有收藏夹。

    Returns:
        [{"id": int, "title": str, "count": int}, ...]
    """
    user = get_current_user()
    uid = user["mid"]
    folders: list[dict[str, Any]] = []
    pn = 1

    while True:
        url = f"https://api.bilibili.com/x/v3/fav/folder/created/list?up_mid={uid}&pn={pn}&ps=20"
        data = _api_get(url)
        if not data or data.get("code") != 0:
            msg = data.get("message", "unknown") if data else "no response"
            print(f"  [错误] 获取收藏夹列表失败: {msg}")
            break
        items = data.get("data", {}).get("list", [])
        if not items:
            break
        for f in items:
            folders.append({
                "id": f["id"],
                "title": f["title"],
                "count": f.get("media_count", 0),
            })
        if len(items) < 20:
            break
        pn += 1
        time.sleep(0.3)

    return folders


def get_videos(
    fav_id: int,
    max_items: int = 0,
    order: str = "mtime",
) -> list[dict[str, Any]]:
    """获取收藏夹内的视频列表。

    Args:
        fav_id: 收藏夹 ID。
        max_items: 最大数量，0 表示全部。
        order: 排序方式，mtime=收藏时间倒序，pubtime=发布时间倒序。

    Returns:
        [{"bvid": str, "title": str, "url": str, "fav_time": str, "pubdate": int}, ...]
    """
    videos: list[dict[str, Any]] = []
    ps = 20
    pn = 1
    empty_pages = 0
    max_pages = 100

    while max_items == 0 or len(videos) < max_items:
        if pn > max_pages:
            break
        url = (f"https://api.bilibili.com/x/v3/fav/resource/list?"
               f"media_id={fav_id}&pn={pn}&ps={ps}&order={order}&type=0")
        data = _api_get(url)
        if not data or data.get("code") != 0:
            msg = data.get("message", "no response") if data else "no response"
            print(f"  [错误] page {pn}: {msg}")
            break

        medias = data.get("data", {}).get("medias", [])
        if not medias:
            empty_pages += 1
            if empty_pages >= 3:
                break
        else:
            empty_pages = 0
            for item in medias:
                cnt = item.get("cnt_info") or {}
                upper = item.get("upper") or {}
                videos.append({
                    "bvid": item["bvid"],
                    "title": item["title"],
                    "url": f"https://www.bilibili.com/video/{item['bvid']}",
                    "fav_time": item.get("fav_time", ""),
                    "pubdate": item.get("pubdate", 0),
                    "duration": item.get("duration", 0),
                    "play": cnt.get("play", 0),
                    "danmaku": cnt.get("danmaku", 0),
                    "upper_name": upper.get("name", ""),
                    "upper_mid": upper.get("mid", 0),
                })
            # 达到上限后就停止，避免 overshoot 太多
            if max_items > 0 and len(videos) >= max_items:
                videos = videos[:max_items]
                break

        print(f"  第 {pn} 页, 已获取 {len(videos)} 条", end="\r")
        pn += 1
        time.sleep(0.3)

    print()
    return videos


def export_csv(
    fav_id: int,
    output_path: str | Path,
    max_items: int = 0,
) -> str:
    """导出收藏夹视频列表到 CSV。

    Returns:
        CSV 文件路径。
    """
    videos = get_videos(fav_id, max_items=max_items)
    if not videos:
        raise ValueError("收藏夹没有视频")

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["序号", "标题", "链接", "BV号", "收藏时间"])
        for i, v in enumerate(videos, 1):
            writer.writerow([i, v["title"], v["url"], v["bvid"], v.get("fav_time", "")])

    return str(output_path)


def load_videos_from_csv(csv_path: str) -> list[dict[str, Any]]:
    """从之前导出的 CSV 加载视频列表。

    CSV 格式必须与 export_csv 输出的格式一致。
    """
    videos: list[dict[str, Any]] = []
    with open(csv_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            videos.append({
                "序号": int(row.get("序号", 0)),
                "标题": row.get("标题", ""),
                "链接": row.get("链接", ""),
                "bvid": row.get("BV号", ""),
                "收藏时间": row.get("收藏时间", ""),
            })
    return videos


# ── 取消收藏（写操作） ────────────────────────────────

FAV_DEL_API = "https://api.bilibili.com/x/v3/fav/resource/batch-del"
AID_API = "https://api.bilibili.com/x/web-interface/view"


def _post_form(url: str, data: dict[str, str], retries: int = 2) -> dict[str, Any] | None:
    """带 Cookie 的 POST 表单（收藏夹写接口都要求 csrf=bili_jct）。"""
    headers = _get_headers()
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    body = urllib.parse.urlencode(data).encode()
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=20)
            return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 412 and attempt < retries:
                time.sleep(20 * attempt)
                continue
            print(f"  [错误] HTTP {e.code}: {e.reason}")
            return None
        except Exception as e:
            print(f"  [错误] {e}")
            time.sleep(2 * attempt)
    return None


def _aid_of(bvid: str) -> int:
    data = _api_get(f"{AID_API}?bvid={bvid}")
    if not data or data.get("code") != 0:
        return 0
    return int((data.get("data") or {}).get("aid") or 0)


def remove_videos(fav_id: int, bvids: list[str], dry_run: bool = True,
                  sleep_s: float = 1.5) -> list[dict[str, Any]]:
    """把视频从收藏夹里删掉（取消收藏）。**写操作**，默认 dry_run。

    返回 [{bvid, aid, ok, msg}]。删不存在的条目时 B 站返回 code!=0，按失败记。
    """
    s = Settings()
    jct = (s.bili_cookie_bili_jct or "").strip()
    if not jct and not dry_run:
        raise RuntimeError("缺少 bili_jct（csrf），无法执行写操作")
    if not _build_cookie_header() and not dry_run:
        raise RuntimeError("没有 B 站 Cookie，无法执行写操作")

    out: list[dict[str, Any]] = []
    for bv in bvids:
        aid = _aid_of(bv)
        if not aid:
            out.append({"bvid": bv, "aid": 0, "ok": False, "msg": "拿不到 aid"})
            continue
        if dry_run:
            out.append({"bvid": bv, "aid": aid, "ok": True, "msg": "dry-run"})
            continue
        data = _post_form(FAV_DEL_API, {
            "media_id": str(fav_id),
            "resources": f"{aid}:2",
            "csrf": jct,
        })
        ok = bool(data) and data.get("code") == 0
        msg = (data or {}).get("message", "no response")
        out.append({"bvid": bv, "aid": aid, "ok": ok, "msg": str(msg)})
        time.sleep(sleep_s)       # 批量删除别贴着风控阈值打
    return out
