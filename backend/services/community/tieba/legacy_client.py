#!/usr/bin/env python3
"""
百度贴吧 · 内测读取工具

功能：
  1. 读取指定帖子的内容和全部回复
  2. 读取指定贴吧的帖子列表
  3. 在指定贴吧内搜索关键词
  4. 读取指定贴吧的精华帖

用法：
  python tieba_test.py <命令> [参数]

示例：
  python tieba_test.py thread 10744245451
  python tieba_test.py thread 7487460366 --json
  python tieba_test.py forum sillytavern --page 1
  python tieba_test.py search sillytavern "记忆" --page 1
  python tieba_test.py featured sillytavern --page 1
"""

import argparse
import glob
import hashlib
import io
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
import time
import urllib.parse

# ── 终端编码兼容 ──────────────────────────────────────────────
# 强制 stdout/stderr 使用 UTF-8（兼容 Windows GBK 终端）
if sys.stdout.encoding and sys.stdout.encoding.upper() != "UTF-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.upper() != "UTF-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import requests
from bs4 import BeautifulSoup


# ── 路径 & 持久化 ──────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "..", "..", ".."))
DATA_DIR = os.environ.get(
    "TIEBA_DATA_DIR",
    os.path.join(PROJECT_ROOT, "data", "community", "tieba"),
)
CONFIG_DIR = os.path.join(DATA_DIR, "config")
COOKIE_FILE = os.path.join(CONFIG_DIR, "cookies.json")
ENV_FILE = os.path.join(PROJECT_ROOT, ".envs")
BRIDGE_JS = os.path.join(SCRIPT_DIR, "tieba_bridge.js")

os.makedirs(CONFIG_DIR, exist_ok=True)


def _load_env() -> dict:
    from dotenv import dotenv_values

    return {k: os.getenv(k, value or "") for k, value in
            dotenv_values(ENV_FILE, interpolate=False).items()}


def _get_stored_cookies() -> dict:
    """从 cookie 文件或 .envs 读取 BDUSS/BAIDUID。"""
    # 优先 cookie 文件
    if os.path.exists(COOKIE_FILE):
        try:
            with open(COOKIE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    # 回退 .envs
    env = _load_env()
    return {
        "BDUSS": env.get("BDUSS", ""),
        "BAIDUID": env.get("BAIDUID", ""),
        "STOKEN": env.get("STOKEN", ""),
    }


# ── 官方客户端 JSON API ────────────────────────────────────────
TIEBA_CLIENT_SECRET = "tiebaclient!!!"
CST = timezone(timedelta(hours=8))
TID_URL_RE = re.compile(r"(?:tieba\.baidu\.com)?/p/(\d{5,})", re.IGNORECASE)


def _client_sign(params: dict) -> str:
    """贴吧客户端接口签名：按 key 排序拼接后 MD5。"""
    raw = "".join(f"{k}={params[k]}" for k in sorted(params))
    raw += TIEBA_CLIENT_SECRET
    return hashlib.md5(raw.encode("utf-8")).hexdigest().upper()


def _request_client_api(path: str, biz_params: dict, timeout: int = 30) -> dict:
    """
    请求贴吧 Android 客户端 JSON API。

    这是公开主题/回复最稳定的入口；BDUSS 有效时会带上，用于访问
    登录后可见内容以及楼中楼。不会执行任何写操作。
    """
    cookies = _get_stored_cookies()
    params = {
        "_client_id": f"wappc_{int(time.time() * 1000)}_{int(time.time() % 100000)}",
        "_client_type": "2",
        "_client_version": "9.7.8.0",
        "_phone_imei": "000000000000000",
        **biz_params,
    }
    params["sign"] = _client_sign(params)

    headers = {
        "User-Agent": "bdtb for Android 9.7.8.0",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    if cookies.get("BDUSS"):
        headers["Cookie"] = f"BDUSS={cookies['BDUSS']}"

    last_error: Exception | None = None
    data = None
    for attempt in range(2):
        try:
            resp = requests.post(
                f"https://c.tieba.baidu.com{path}",
                data=urllib.parse.urlencode(params),
                headers=headers,
                timeout=timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            break
        except (requests.RequestException, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(1.0)
                params["_client_id"] = f"wappc_{int(time.time() * 1000)}_{int(time.time() % 100000)}"
                params["sign"] = _client_sign(params)
    if data is None:
        raise RuntimeError(f"客户端接口请求失败: {last_error}")

    # 部分接口用 error_code，部分移动网页接口用 no
    code = int(data.get("error_code", data.get("no", 0)) or 0)
    msg = data.get("error_msg") or data.get("error") or "unknown error"
    if code != 0:
        raise RuntimeError(f"贴吧接口错误 {code}: {msg}")
    return data


def check_stored_cookie() -> dict:
    """只读校验 BDUSS 是否仍能登录贴吧客户端接口。"""
    cookies = _get_stored_cookies()
    bduss = cookies.get("BDUSS", "")
    if not bduss:
        return {"logged_in": False, "reason": "未配置 BDUSS"}
    try:
        data = _request_client_api(
            "/c/s/initNickname",
            {"BDUSS": bduss, "_client_version": "9.7.8.0"},
        )
        user = data.get("user_info") or {}
        return {
            "logged_in": True,
            "user_name": user.get("user_name") or user.get("name_show") or "",
            "nickname": user.get("name_show") or "",
            "tieba_uid": user.get("tieba_uid", ""),
        }
    except Exception as e:
        return {"logged_in": False, "reason": str(e)}


def read_account_favorites(max_pages: int = 0) -> dict:
    """
    读取百度贴吧 App/移动端的“我的收藏”。

    接口：GET https://c.tieba.baidu.com/mg/o/threadstore
    每页固定 10 条；max_pages=0 表示自动翻完。
    """
    cookies = _get_stored_cookies()
    bduss = cookies.get("BDUSS", "")
    if not bduss:
        raise RuntimeError("读取账号收藏需要 BDUSS；请先运行 login")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 13; SM-S9080) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/112.0.0.0 Mobile Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://c.tieba.baidu.com/index/tbwise/collect?source=a0-bindex-c-d-e0#/",
        "Cookie": f"BDUSS={bduss}",
    }

    favorites: dict[str, dict] = {}
    page = 1
    max_pages = max(0, int(max_pages))
    while True:
        resp = requests.get(
            "https://c.tieba.baidu.com/mg/o/threadstore",
            params={"pn": str(page), "rn": "10"},
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        if int(payload.get("errno", 0)) != 0:
            raise RuntimeError(f"读取收藏失败: {payload.get('errmsg') or payload}")
        data = payload.get("data") or {}
        for raw in data.get("store_thread") or []:
            tid = str(raw.get("tid") or "")
            if not tid or tid in favorites:
                continue
            author = raw.get("author") or {}
            media = (raw.get("media") or [{}])[0] if raw.get("media") else {}
            favorites[tid] = {
                "tid": tid,
                "title": raw.get("title", ""),
                "forum_name": raw.get("forum_name", ""),
                "author": author.get("show_nickname")
                          or author.get("name_show")
                          or author.get("name") or "",
                "last_reply_time": TiebaClient._client_time(raw.get("last_time")),
                "is_deleted": bool(int(raw.get("is_deleted") or 0)),
                "cover_image": media.get("cover_img", ""),
                "url": f"https://tieba.baidu.com/p/{tid}",
            }

        has_more = str(data.get("has_more") or "0") == "1"
        if not has_more or (max_pages and page >= max_pages):
            break
        page += 1
        time.sleep(0.35)

    return {
        "source": "account",
        "pages_fetched": page,
        "count": len(favorites),
        "favorites": list(favorites.values()),
    }


def extract_tids(text: str) -> list[str]:
    """从文本 / HTML / 导出的书签 JSON 中提取贴吧 TID。"""
    seen = set()
    result = []
    for m in TID_URL_RE.finditer(text):
        tid = m.group(1)
        if tid not in seen:
            seen.add(tid)
            result.append(tid)
    for raw in text.splitlines():
        tid = raw.strip()
        if re.fullmatch(r"\d{5,}", tid) and tid not in seen:
            seen.add(tid)
            result.append(tid)
    return result


def load_source_tids(source: str) -> list[str]:
    """支持单个 TID/URL，或 .txt/.json/.html URL 清单。"""
    if re.fullmatch(r"\d{5,}", source or ""):
        return [source]

    path = os.path.expanduser(source)
    if not os.path.isfile(path):
        # 也允许用户直接粘贴一个 URL 作为参数
        tids = extract_tids(source)
        if tids:
            return tids
        raise RuntimeError(f"找不到输入文件: {path}")

    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        return extract_tids(f.read())


def browser_bookmark_paths(browser: str) -> list[str]:
    """返回本机 Edge / Chrome / Brave 的书签数据库路径。"""
    base = os.environ.get("LOCALAPPDATA", "")
    roots = {
        "edge": r"Microsoft\Edge\User Data",
        "chrome": r"Google\Chrome\User Data",
        "brave": r"BraveSoftware\Brave-Browser\User Data",
    }
    browser = (browser or "edge").lower()
    selected = list(roots) if browser == "all" else [browser]
    paths = []
    for name in selected:
        root = roots.get(name)
        if root and base:
            paths.extend(glob.glob(os.path.join(base, root, "*", "Bookmarks")))
    return paths


def collect_tieba_bookmarks(path: str, profile: str = "") -> list[dict]:
    """从浏览器 Bookmarks JSON 提取贴吧帖子和标题。"""
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        data = json.load(f)

    wanted_profile = os.path.basename(os.path.dirname(path))
    if profile and wanted_profile.lower() != profile.lower():
        return []

    items: list[dict] = []

    def walk(node):
        if not isinstance(node, dict):
            return
        if node.get("type") == "url":
            url = node.get("url", "")
            m = re.search(r"tieba\.baidu\.com/p/(\d+)", url, re.I)
            if m:
                items.append({
                    "tid": m.group(1),
                    "title": node.get("name", ""),
                    "url": url,
                    "browser_profile": wanted_profile,
                })
        for child in node.get("children") or []:
            walk(child)

    for root in (data.get("roots") or {}).values():
        walk(root)
    return items


# ── 浏览器桥接 (Edge + puppeteer-core) ──────────────────────
def _fetch_with_bridge(url: str) -> str:
    """
    调用 Node.js 桥接脚本，用真实 Edge 浏览器加载页面。
    返回最终渲染后的 HTML 字符串。
    """
    cookies = _get_stored_cookies()
    env = os.environ.copy()
    if cookies.get("BDUSS"):
        env["BDUSS"] = cookies["BDUSS"]
    if cookies.get("BAIDUID"):
        env["BAIDUID"] = cookies["BAIDUID"]

    if not os.path.exists(BRIDGE_JS):
        raise RuntimeError(f"桥接脚本不存在: {BRIDGE_JS}")

    print(f"  [Browser] 启动 Edge 加载页面...")
    # 检测帖子 URL 以告知桥接需要验证页重试
    page_type = "thread" if "/p/" in url else ""
    result = subprocess.run(
        ["node", BRIDGE_JS, "html", url, page_type],
        capture_output=True,
        timeout=60,
        env=env,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"桥接失败: {stderr or 'unknown error'}")

    # 二进制输出 → UTF-8 解码
    html = result.stdout.decode("utf-8", errors="replace")
    if not html or len(html) < 500:
        raise RuntimeError(f"桥接返回的 HTML 太短 ({len(html)} 字节)")
    return html


def _fetch_json(url: str, page_type: str = "forum") -> list | dict:
    """
    调用 Node.js 桥接脚本的 JSON 模式，直接返回结构化数据。
    page_type: "forum" | "search" | "thread"
    """
    cookies = _get_stored_cookies()
    env = os.environ.copy()
    if cookies.get("BDUSS"):
        env["BDUSS"] = cookies["BDUSS"]
    if cookies.get("BAIDUID"):
        env["BAIDUID"] = cookies["BAIDUID"]

    if not os.path.exists(BRIDGE_JS):
        raise RuntimeError(f"桥接脚本不存在: {BRIDGE_JS}")

    print(f"  [Browser] 启动 Edge 加载页面...")
    result = subprocess.run(
        ["node", BRIDGE_JS, "json", url, page_type],
        capture_output=True,
        timeout=60,
        env=env,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"桥接失败: {stderr or 'unknown error'}")

    output = result.stdout.decode("utf-8", errors="replace").strip()
    if output:
        try:
            data = json.loads(output)
            return data
        except json.JSONDecodeError:
            raise RuntimeError(f"桥接返回非 JSON: {output[:200]}")
    raise RuntimeError("桥接返回空")


# ── 客户端 ──────────────────────────────────────────────────────
class TiebaClient:
    """贴吧客户端 — requests 直连 + 浏览器桥接双引擎。"""

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;"
            "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://tieba.baidu.com/",
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "DNT": "1",
    }

    def __init__(self, bduss: str = "", stoken: str = "", baiduid: str = ""):
        self.session = requests.Session()
        self.session.headers.update(self.HEADERS)

        domain = ".baidu.com"
        if bduss:
            self.session.cookies.set("BDUSS", bduss, domain=domain)
        if stoken:
            self.session.cookies.set("STOKEN", stoken, domain=domain)
        if baiduid:
            self.session.cookies.set("BAIDUID", baiduid, domain=domain)

        self._logged_in = bool(bduss)
        self._cookies_set = bool(bduss or baiduid)

    @property
    def is_logged_in(self) -> bool:
        return self._logged_in

    # ── 双引擎请求 ────────────────────────────────────────────
    def _get_html(self, url: str) -> str:
        """
        获取页面 HTML。
        先用 requests 直连，403/WAF 时自动降级到浏览器桥接。
        """
        for attempt in range(2):
            try:
                resp = self.session.get(url, timeout=20)
                if resp.status_code == 403:
                    print(f"  [!] requests 被 WAF 拦截 (403)，切换浏览器引擎...")
                    time.sleep(1)
                    return _fetch_with_bridge(url)
                resp.raise_for_status()
                return resp.text
            except (requests.ConnectionError, requests.Timeout):
                if attempt == 0:
                    time.sleep(1)
                    continue
                # 最后尝试浏览器
                print(f"  [!] requests 网络错误，切换浏览器引擎...")
                return _fetch_with_bridge(url)

        return _fetch_with_bridge(url)

    # ── 1. 读取帖子 ─────────────────────────────────────────────
    def read_thread(self, tid: str | int, page: int | None = None) -> dict:
        """
        读取帖子全部楼层。

        page=None：自动翻完所有页（默认，保证拿到所有楼层）。
        page=N  ：只读第 N 页，保留调试/增量能力。
        """
        return self.read_thread_client(tid, page=page)

    def read_thread_client(self, tid: str | int, page: int | None = None) -> dict:
        """
        通过 c.tieba 客户端 JSON API 读取帖子。

        相比旧版移动网页 API，这个入口不依赖 WAF，返回的是结构化 JSON，
        不再需要解析 HTML。每层楼的所有文字片段、图片、视频、链接都会保留。
        """
        tid = str(tid)

        def fetch_one(pn: int) -> dict:
            return _request_client_api(
                "/c/f/pb/page",
                {
                    "kz": tid,
                    "pn": str(pn),
                    "rn": "30",
                    "r": "0",       # 正序
                    "lz": "0",      # 不只看楼主
                    "st": "0",
                    "z": "0",
                    # 登录 cookie 有效时，贴吧会下发楼中楼；未登录时为空
                    "with_floor": "1",
                    "floor_sort_type": "0",
                    "floor_rn": "10",
                },
            )

        first = fetch_one(page or 1)
        raw_page = first.get("page") or {}
        total_pages = max(1, int(raw_page.get("total_page") or raw_page.get("new_total_page") or 1))
        if page is not None:
            target_pages = [page]
        else:
            target_pages = list(range(1, total_pages + 1))

        raw_posts: dict[int, dict] = {}
        users: dict[int, dict] = {}

        def absorb(payload: dict):
            for u in payload.get("user_list") or []:
                if u.get("id") is not None:
                    users[int(u["id"])] = u
            for p in payload.get("post_list") or []:
                try:
                    floor = int(p.get("floor") or 0)
                except (TypeError, ValueError):
                    continue
                # 以楼层为唯一键；同楼层出现时保留 PID 更大/内容更多的一条
                if floor not in raw_posts or len(str(p)) > len(str(raw_posts[floor])):
                    raw_posts[floor] = p

        absorb(first)
        for pn in target_pages[1:]:
            time.sleep(0.45)   # 稳定翻页，避免给贴吧制造突发压力
            try:
                absorb(fetch_one(pn))
                if len(target_pages) > 5 and pn % 5 == 0:
                    print(f"  [i] 已抓取 {pn}/{total_pages} 页")
            except Exception as e:
                print(f"  [!] 第 {pn} 页抓取失败: {e}")

        if not raw_posts:
            raise RuntimeError("贴吧接口成功但没有返回任何楼层")

        # 楼中楼：默认也抓全。/c/f/pb/page 的内嵌 sub_post_list
        # 通常为空或不完整；/c/f/pb/floor 才是完整楼中楼分页接口。
        comment_posts = 0
        for floor in sorted(raw_posts):
            p = raw_posts[floor]
            try:
                sub_num = int(p.get("sub_post_number") or 0)
            except (TypeError, ValueError):
                sub_num = 0
            pid = str(p.get("id") or "")
            if not sub_num or not pid:
                continue
            try:
                p["_fetched_comments"] = self._fetch_floor_comments(tid, pid)
                comment_posts += 1
                if comment_posts % 5 == 0:
                    print(f"  [i] 已抓取 {comment_posts} 个楼中楼")
            except Exception as e:
                print(f"  [!] 楼中楼 {pid} 抓取失败: {e}")

        thread = first.get("thread") or {}
        forum = first.get("forum") or {}
        author_id = thread.get("author_id") or first.get("post_list", [{}])[0].get("author_id")
        author = self._client_user_display(users, author_id)
        floor1 = raw_posts.get(1)
        main_post = self._client_post_to_result(
            floor1 or {},
            users=users,
            tid=tid,
            title=thread.get("title", ""),
            total_pages=total_pages,
            current_page=page if page is not None else total_pages,
            fetch_all=page is None,
            pages_fetched=target_pages,
            forum_name=forum.get("name", ""),
        )

        replies = []
        for floor in sorted(raw_posts):
            if floor == 1:
                continue
            replies.append(
                self._client_post_to_result(
                    raw_posts[floor],
                    users=users,
                    tid=tid,
                    title=thread.get("title", ""),
                    total_pages=total_pages,
                    current_page=page if page is not None else total_pages,
                    fetch_all=page is None,
                    pages_fetched=target_pages,
                    forum_name=forum.get("name", ""),
                )
            )
        main_post["replies"] = replies
        main_post["reply_count"] = len(replies)
        main_post["thread_reply_num"] = thread.get("reply_num", 0)
        main_post["total_post_count"] = int(raw_page.get("total_num") or thread.get("reply_num") or 0)
        return main_post

    @staticmethod
    def _client_user_display(users: dict, author_id) -> str:
        try:
            u = users.get(int(author_id or 0)) or {}
        except (TypeError, ValueError):
            u = {}
        return u.get("name_show") or u.get("name") or ""

    @staticmethod
    def _client_content_parts(content_items) -> tuple[str, list[dict], list[str]]:
        """把客户端 content 片段转换为纯文本、结构化片段、图片 URL。"""
        if isinstance(content_items, str):
            try:
                parsed = json.loads(content_items)
                content_items = parsed if isinstance(parsed, list) else []
            except json.JSONDecodeError:
                content_items = []
        text_parts: list[str] = []
        structured: list[dict] = []
        images: list[str] = []

        for raw in content_items or []:
            if not isinstance(raw, dict):
                continue
            try:
                typ = int(raw.get("type") or 0)
            except (TypeError, ValueError):
                typ = 0

            item: dict = {"type": typ}
            text = raw.get("text")
            link = raw.get("link") or raw.get("url")
            title = raw.get("title")
            desc = raw.get("desc") or raw.get("c")
            img = raw.get("cdn_src") or raw.get("big_cdn_src") or raw.get("origin_src") or raw.get("src")
            video = raw.get("src") or raw.get("link")
            cover = raw.get("vcover")

            # 表情接口会把 image_emoticon3 这类内部 ID 放在 text，
            # 真正可读的文字在 desc/c；不要把内部 ID 混进正文。
            if text and not (typ == 2 and str(text).startswith("image_emoticon")):
                item["text"] = text
                text_parts.append(str(text))
            if title:
                item["title"] = title
            if desc and desc != text:
                item["desc"] = desc
                if typ == 2:      # 表情：描述也是文字信息
                    text_parts.append(f"[表情:{desc}]")
            if link:
                item["link"] = link
                if typ != 4:
                    text_parts.append(str(link))
            if typ == 3 and img:
                item["image"] = img
                images.append(img)
            elif typ == 4 and video:
                item["video"] = video
                if cover:
                    item["cover"] = cover

            structured.append(item)

        # 段落/换行已由 text 自带；客户端片段通常不需要额外连接符
        return "\n".join(x for x in text_parts if x).strip(), structured, images

    @staticmethod
    def _client_time(value) -> str:
        try:
            return datetime.fromtimestamp(int(value), tz=CST).strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError, OSError, OverflowError):
            return ""

    @staticmethod
    def _client_json_list(value) -> list:
        """部分字段会序列化成 JSON 字符串，这里统一兜底。"""
        if isinstance(value, list):
            return value
        if isinstance(value, str) and value.strip():
            try:
                parsed = json.loads(value)
                return parsed if isinstance(parsed, list) else []
            except json.JSONDecodeError:
                return []
        return []

    def _fetch_floor_comments(self, tid: str, pid: str) -> list[dict]:
        """抓取某一楼下的楼中楼，自动翻页。"""
        comments: dict[str, dict] = {}
        page = 1
        total_pages = 1

        while page <= total_pages:
            payload = _request_client_api(
                "/c/f/pb/floor",
                {
                    "kz": tid,
                    "pid": str(pid),
                    "pn": str(page),
                    "sort": "0",
                },
            )
            page_info = payload.get("page") or {}
            try:
                total_pages = max(1, int(page_info.get("total_page") or 1))
            except (TypeError, ValueError):
                total_pages = 1

            for sp in payload.get("subpost_list") or []:
                if not isinstance(sp, dict):
                    continue
                spid = str(sp.get("id") or "")
                if not spid or spid in comments:
                    continue
                sp_text, sp_content, sp_images = self._client_content_parts(sp.get("content"))
                author = sp.get("author") or {}
                comments[spid] = {
                    "pid": spid,
                    "author": author.get("name_show") or author.get("name") or "",
                    "author_id": author.get("id", ""),
                    "time": self._client_time(sp.get("time")),
                    "content_text": sp_text,
                    "content": sp_content,
                    "images": sp_images,
                    "agree": (sp.get("agree") or {}).get("agree_num", 0),
                }

            if page < total_pages:
                time.sleep(0.35)
            page += 1

        return [comments[k] for k in sorted(comments, key=lambda x: int(x) if x.isdigit() else 0)]

    def _client_post_to_result(
        self,
        p: dict,
        *,
        users: dict,
        tid: str,
        title: str,
        total_pages: int,
        current_page: int,
        fetch_all: bool,
        pages_fetched: list[int],
        forum_name: str = "",
    ) -> dict:
        try:
            floor = int(p.get("floor") or 0)
        except (TypeError, ValueError):
            floor = 0

        content_text, content, images = self._client_content_parts(p.get("content"))
        author = self._client_user_display(users, p.get("author_id"))
        pid = str(p.get("id") or "")

        fetched = list(p.get("_fetched_comments") or [])
        inline = self._client_json_list(p.get("sub_post_list"))
        seen_pids = {str(x.get("pid")) for x in fetched if isinstance(x, dict)}

        for sp in inline:
            if not isinstance(sp, dict):
                continue
            spid = str(sp.get("id") or "")
            if spid and spid in seen_pids:
                continue
            sp_text, sp_content, sp_images = self._client_content_parts(sp.get("content"))
            author = sp.get("author") or {}
            fetched.append({
                "pid": spid,
                "author": author.get("name_show") or author.get("name")
                          or self._client_user_display(users, sp.get("author_id")),
                "author_id": sp.get("author_id", author.get("id", "")),
                "time": self._client_time(sp.get("time")),
                "content_text": sp_text,
                "content": sp_content,
                "images": sp_images,
            })

        comments = []
        for sp in fetched:
            comments.append({
                "pid": str(sp.get("pid") or sp.get("id") or ""),
                "author": sp.get("author", ""),
                "author_id": sp.get("author_id", ""),
                "time": sp.get("time", ""),
                "content_text": sp.get("content_text", ""),
                "content": sp.get("content", []),
                "images": sp.get("images", []),
                "agree": sp.get("agree", 0),
            })

        return {
            "tid": tid,
            "forum_name": forum_name,
            "title": title,
            "pid": pid,
            "floor": floor,
            "author": author,
            "author_id": p.get("author_id", ""),
            "pub_time": self._client_time(p.get("time")),
            "content_text": content_text,
            "content": content,
            "images": images,
            "agree": p.get("agree_num", p.get("agree", 0)),
            "sub_post_number": int(p.get("sub_post_number") or 0),
            "comments": comments,
            "total_pages": total_pages,
            "current_page": current_page,
            "fetch_all": fetch_all,
            "pages_fetched": pages_fetched,
            "replies": [],       # 仅 1 楼使用；其他楼层置空避免旧格式误读
        }

    def read_thread_legacy(self, tid: str | int, page: int = 1) -> dict:
        """旧版移动网页 API；保留用于排查，不再是主路径。"""
        url = f"https://tieba.baidu.com/mo/q/thread"
        params = {"kz": str(tid), "pn": str(page)}

        try:
            resp = self._session_mobile().get(url, params=params, timeout=15)
            resp.raise_for_status()
            return self._parse_thread_mobile(resp.text, str(tid), page)
        except Exception as e:
            print(f"  [!] 移动端 API 失败 ({e})，尝试浏览器桥接...")
            desktop_url = f"https://tieba.baidu.com/p/{tid}?pn={page}"
            html = _fetch_with_bridge(desktop_url)
            return self._parse_thread_html(html, str(tid), page)

    def _session_mobile(self):
        """创建移动端专用 session（轻量 headers，不触发 WAF）。"""
        s = requests.Session()
        s.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 13; SM-S9080) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/112.0.0.0 Mobile Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        cookies = _get_stored_cookies()
        if cookies.get("BDUSS"):
            s.cookies.set("BDUSS", cookies["BDUSS"], domain=".baidu.com")
        if cookies.get("BAIDUID"):
            s.cookies.set("BAIDUID", cookies["BAIDUID"], domain=".baidu.com")
        return s

    def _parse_thread_mobile(self, html: str, tid: str, page: int) -> dict:
        """解析移动端 API 返回的帖子 HTML。"""
        soup = BeautifulSoup(html, "html.parser")

        title_tag = soup.select_one(".post_title_embed")
        title = title_tag.text.strip() if title_tag else ""

        # 总页数
        total_pages = 1
        page_links = soup.select("a[href*='pn=']")
        for a in page_links:
            m = re.search(r"pn=(\d+)", a.get("href", ""))
            if m:
                pn = int(m.group(1))
                if pn > total_pages:
                    total_pages = pn

        # 遍历每个 post/reply 的 li
        posts = soup.select("li.list_item.post_list_item")
        main_post = None
        replies = []

        for li in posts:
            # 从 data-info 提取元数据
            data_info = li.get("data-info", "{}")
            try:
                meta = json.loads(data_info)
            except json.JSONDecodeError:
                meta = {}

            floor = int(meta.get("floor_num", 0))
            author = meta.get("name_show", meta.get("un", ""))
            pid = str(meta.get("pid", ""))

            # 内容
            content_div = li.select_one(".content")
            content_text = content_div.get_text("\n", strip=True) if content_div else ""

            # 时间
            time_div = li.select_one(".post-tail, .ti_time, .time")
            pub_time = time_div.text.strip() if time_div else ""
            if not pub_time:
                # 尝试从别的地方提取
                for el in li.select("div, span"):
                    t = el.text.strip()
                    if re.match(r"\d{1,2}-\d{1,2}( \d{1,2}:\d{2})?$", t):
                        pub_time = t
                        break

            # 图片
            images = []
            for img in li.select("img"):
                src = img.get("src", "")
                if src and "avatar" not in src and not src.startswith("data:"):
                    images.append(src)

            if floor == 1:
                main_post = {
                    "tid": tid,
                    "title": title,
                    "author": author,
                    "author_id": meta.get("un", ""),
                    "pub_time": pub_time,
                    "content_text": content_text,
                    "images": images,
                    "total_pages": total_pages,
                    "current_page": page,
                    "replies": [],
                }
            else:
                replies.append({
                    "floor": floor,
                    "author": author,
                    "pub_time": pub_time,
                    "content_text": content_text,
                    "images": images,
                })

        if not main_post:
            main_post = {
                "tid": tid,
                "title": title,
                "author": "",
                "author_id": "",
                "pub_time": "",
                "content_text": "",
                "images": [],
                "total_pages": total_pages,
                "current_page": page,
                "replies": [],
            }

        main_post["replies"] = replies
        return main_post

    def _parse_thread_html(self, html: str, tid: str, page: int) -> dict:
        """兜底: 解析桌面版 HTML（浏览器桥接获取）。"""
        soup = BeautifulSoup(html, "html.parser")

        title_tag = soup.find("title")
        title = title_tag.text.strip() if title_tag else ""
        title = re.sub(r"[-_]百度贴吧$", "", title).strip()

        author = ""
        pub_time = ""
        content_text = ""
        images = []

        content_container = soup.select_one(".content-container")
        if content_container:
            cc_text = content_container.get_text("\n", strip=True)
            lines = [l.strip() for l in cc_text.split("\n") if l.strip()]
            if lines:
                author = lines[0]
            pb_wrap = soup.select_one(".pb-content-wrap")
            if pb_wrap:
                content_text = pb_wrap.get_text("\n", strip=True)
            for img in soup.select(".pb-content-wrap img, .content-container img"):
                src = img.get("src") or img.get("data-original") or ""
                if src and not src.startswith("data:"):
                    images.append(src)

        if not content_text:
            for sel in [".d_post_content", "#j_p_post_content", ".p_content"]:
                c_tag = soup.select_one(sel)
                if c_tag:
                    content_text = c_tag.get_text(strip=True)
                    break

        if not author:
            for sel in [".p_author_name", ".d_name a", ".poster_name"]:
                a_tag = soup.select_one(sel)
                if a_tag:
                    author = a_tag.text.strip()
                    break

        total_pages = 1
        for a in soup.select("a"):
            text = a.text.strip()
            if text.isdigit() and 1 < int(text) <= 999:
                total_pages = max(total_pages, int(text))
        for span in soup.select(".page_count, .total_page, .pb-page-count"):
            m = re.search(r"/(\d+)", span.text)
            if m:
                total_pages = max(total_pages, int(m.group(1)))
        inp = soup.select_one("input[type='text'][max]")
        if inp:
            try:
                total_pages = max(total_pages, int(inp["max"]))
            except (ValueError, KeyError):
                pass

        replies = []
        seen_floors = set()

        for comment in soup.select(".comment-content, .l_post, .p_post"):
            comment_text = comment.get_text("\n", strip=True)
            floor_num = 0
            m = re.search(r"第(\d+)楼", comment_text)
            if m:
                floor_num = int(m.group(1))
            else:
                m = re.search(r"#(\d+)", comment_text)
                if m:
                    floor_num = int(m.group(1))
            if not floor_num:
                continue
            if floor_num in seen_floors:
                continue
            seen_floors.add(floor_num)

            r_author = ""
            a_tag = comment.select_one(
                ".p_author_name, .d_name a, .poster_name, .ti_author"
            )
            if a_tag:
                r_author = a_tag.text.strip()

            r_time = ""
            t_tag = comment.select_one(
                ".p_date, .post-tail-wrap .p_date, .ti_time"
            )
            if t_tag:
                r_time = t_tag.text.strip()

            r_text = ""
            c_tag = comment.select_one(
                ".d_post_content, .p_content, .post_content, .pb-content-item, .content"
            )
            if c_tag:
                r_text = c_tag.get_text(strip=True)

            r_images = []
            for img in (c_tag or comment).select("img"):
                src = img.get("src") or img.get("data-original") or ""
                if src and not src.startswith("data:"):
                    r_images.append(src)

            replies.append({
                "floor": floor_num,
                "author": r_author,
                "pub_time": r_time,
                "content_text": r_text,
                "images": r_images,
            })

        return {
            "tid": tid,
            "title": title,
            "author": author,
            "author_id": "",
            "pub_time": pub_time,
            "content_text": content_text,
            "images": images,
            "total_pages": total_pages,
            "current_page": page,
            "replies": replies,
        }

    @staticmethod
    def _extract_images(tag) -> list:
        if tag is None:
            return []
        urls = []
        for img in tag.select("img"):
            src = img.get("src") or img.get("data-original") or ""
            if src and not src.startswith("data:"):
                urls.append(src)
        return urls

    # ── 2. 读取贴吧帖子列表 ────────────────────────────────────
    def read_forum(self, forum_name: str, page: int = 1) -> dict:
        """读取贴吧帖子列表（客户端 JSON API）。"""
        return self._read_forum_client(forum_name, page=page, is_good=False)

    def _parse_forum_mobile(self, html: str, forum_name: str, page: int) -> dict:
        """解析移动端 API 的贴吧列表 HTML。"""
        soup = BeautifulSoup(html, "html.parser")
        threads = []

        for li in soup.select("li.tl_shadow, li.tl_top"):
            a_tag = li.select_one("a[href*='/p/']")
            if not a_tag:
                continue
            href = a_tag.get("href", "")
            m = re.search(r"/p/(\d+)", href)
            tid = m.group(1) if m else ""
            if not tid:
                continue

            title_div = li.select_one(".ti_title")
            title = title_div.text.strip() if title_div else ""

            author_div = li.select_one(".ti_author")
            author = author_div.text.strip() if author_div else ""

            reply_span = li.select_one(".ti_zan_reply")
            reply_count = 0
            if reply_span:
                m2 = re.search(r"(\d+)", reply_span.text.strip())
                if m2:
                    reply_count = int(m2.group(1))

            threads.append({
                "tid": tid, "title": title, "author": author,
                "reply_count": reply_count, "last_reply_time": "",
            })

        total_pages = self._parse_mobile_pages(soup)
        return {
            "forum_name": forum_name, "current_page": page,
            "total_pages": total_pages, "threads": threads,
        }

    @staticmethod
    def _parse_mobile_pages(soup: BeautifulSoup) -> int:
        """移动端分页解析。"""
        pages = []
        for a in soup.select("a[href*='pn=']"):
            m = re.search(r"pn=(\d+)", a.get("href", ""))
            if m:
                pages.append(int(m.group(1)))
        for span in soup.select(".page_info, .page_count"):
            m = re.search(r"/(\d+)", span.text)
            if m:
                pages.append(int(m.group(1)))
        return max(pages) if pages else 1

    def read_forum_old(self, forum_name: str, page: int = 1) -> dict:
        """旧版: 桥接 JSON 模式（保留备用）。"""
        url = (
            f"https://tieba.baidu.com/f?kw={urllib.parse.quote(forum_name)}"
            f"&ie=utf-8&pn={page}"
        )
        data = _fetch_json(url, "forum")
        if isinstance(data, list):
            return {"forum_name": forum_name, "current_page": page,
                    "total_pages": 1, "threads": data}
        return {"forum_name": forum_name, "current_page": page,
                "total_pages": 1, "threads": []}

    def _parse_thread_list(self, soup) -> list:
        """HTML 解析帖子列表（多策略，适应新旧版设计）。"""
        threads = []
        seen_tids = set()

        # 策略1: 通过 thread-title + a[href*=p/] 关联
        # 找所有 thread-title 元素
        title_divs = soup.select(".thread-title, .top-thread-card .thread-title")
        for div in title_divs:
            title = div.text.strip()
            if not title or len(title) < 2:
                continue
            # 在父级范围内找 TID
            parent = div.parent
            tid = ""
            for _ in range(4):
                if not parent:
                    break
                a_tag = parent.select_one("a[href*='/p/']")
                if a_tag:
                    m = re.search(r"/p/(\d+)", a_tag.get("href", ""))
                    if m:
                        tid = m.group(1)
                    break
                parent = parent.parent if parent else None
            if not tid:
                continue
            if tid in seen_tids:
                continue
            seen_tids.add(tid)
            threads.append({
                "tid": tid, "title": title,
                "author": "", "reply_count": 0, "last_reply_time": "",
            })

        # 策略2: 从 a.thread-content-link 提取
        if not threads:
            for a_tag in soup.select("a.thread-content-link, a.top-thread-card-item"):
                href = a_tag.get("href", "")
                m = re.search(r"/p/(\d+)", href)
                if not m:
                    continue
                tid = m.group(1)
                if tid in seen_tids:
                    continue
                seen_tids.add(tid)
                # 优先取内部的 .thread-title
                title_el = a_tag.select_one(".thread-title")
                title = title_el.text.strip() if title_el else a_tag.text.strip()
                threads.append({
                    "tid": tid, "title": title,
                    "author": "", "reply_count": 0, "last_reply_time": "",
                })

        # 策略3: 从所有 a[href*=p/] 兜底
        if not threads:
            for a_tag in soup.select("a[href*='/p/']"):
                href = a_tag.get("href", "")
                title = a_tag.text.strip()
                if len(title) < 4:
                    continue
                m = re.search(r"/p/(\d+)", href)
                if not m:
                    continue
                tid = m.group(1)
                if tid in seen_tids:
                    continue
                seen_tids.add(tid)
                # 同级找 .thread-title
                parent = a_tag.parent
                title_el = parent.select_one(".thread-title") if parent else None
                clean_title = title_el.text.strip() if title_el else title
                threads.append({
                    "tid": tid, "title": clean_title,
                    "author": "", "reply_count": 0, "last_reply_time": "",
                })

        # 策略4: 旧版桌面选择器
        if not threads:
            for li in soup.select(
                "li.j_thread_list, .tl_thread_list_wrap, .thread_item"
            ):
                a_tag = li.select_one("a.j_th_tit, a.bluelink, a.thread_title")
                if not a_tag:
                    continue
                title = a_tag.text.strip()
                href = a_tag.get("href", "")
                m = re.search(r"/p/(\d+)", href)
                tid = m.group(1) if m else ""
                if tid in seen_tids:
                    continue
                seen_tids.add(tid)
                author_tag = li.select_one(
                    ".tb_icon_author, .threadlist_author a, .thread_item_author"
                )
                author = author_tag.text.strip() if author_tag else ""
                reply_tag = li.select_one(
                    ".threadlist_rep_num, .threadlist_rep, .reply_count"
                )
                reply_count = 0
                if reply_tag:
                    m2 = re.search(r"\d+", reply_tag.text.strip())
                    if m2:
                        reply_count = int(m2.group(0))
                time_tag = li.select_one(
                    ".threadlist_reply_date, .last_reply_time, .pull_right"
                )
                last_reply_time = time_tag.text.strip() if time_tag else ""
                threads.append({
                    "tid": tid, "title": title,
                    "author": author, "reply_count": reply_count,
                    "last_reply_time": last_reply_time,
                })

        return threads

    # ── 3. 搜索 ─────────────────────────────────────────────────
    @staticmethod
    def _clean_html(value: str) -> str:
        if not value:
            return ""
        return BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True)

    def search(self, forum_name: str, keyword: str, page: int = 1, max_pages: int = 1) -> dict:
        """在指定贴吧内搜索关键词（客户端 JSON API，可翻多页）。"""
        max_pages = max(1, int(max_pages))
        all_results: dict[str, dict] = {}
        pages_fetched = []
        total_pages = 1

        for pn in range(page, page + max_pages):
            payload = _request_client_api(
                "/c/s/searchpost",
                {
                    "kw": forum_name,
                    "word": keyword,
                    "pn": str(pn),
                    "rn": "20",
                    "sm": "0",
                    "only_thread": "0",
                    "_client_version": "9.7.8.0",
                },
            )
            page_info = payload.get("page") or {}
            try:
                total_pages = max(total_pages, int(page_info.get("total_page") or 1))
            except (TypeError, ValueError):
                pass

            for raw in payload.get("post_list") or []:
                tid = str(raw.get("tid") or "")
                pid = str(raw.get("pid") or "")
                key = pid or f"{tid}:{len(all_results)}"
                if key in all_results:
                    continue
                author = raw.get("author") or {}
                all_results[key] = {
                    "tid": tid,
                    "pid": pid,
                    "title": self._clean_html(raw.get("title", "")),
                    "brief": self._clean_html(raw.get("content", "")),
                    "author": author.get("name_show") or author.get("name") or "",
                    "pub_time": self._client_time(raw.get("time")),
                    "is_floor_reply": bool(int(raw.get("is_floor") or 0)),
                    "url": f"https://tieba.baidu.com/p/{tid}",
                    "forum_name": raw.get("fname", forum_name),
                }

            pages_fetched.append(pn)
            if str(page_info.get("has_more") or "0") != "1":
                break
            time.sleep(0.35)

        return {
            "forum_name": forum_name,
            "keyword": keyword,
            "current_page": page,
            "pages_fetched": pages_fetched,
            "total_pages": total_pages,
            "results": list(all_results.values()),
        }

    # ── 4. 精华帖 ──────────────────────────────────────────────
    def read_featured(self, forum_name: str, page: int = 1) -> dict:
        """读取精华帖列表（客户端 JSON API）。"""
        return self._read_forum_client(forum_name, page=page, is_good=True)

    def _read_forum_client(self, forum_name: str, page: int, is_good: bool) -> dict:
        """按吧名读取普通/精华列表。"""
        payload = _request_client_api(
            "/c/f/frs/page",
            {
                "kw": forum_name,
                "pn": str(page),
                "rn": "30",
                "sort_type": "0",
                "is_good": "1" if is_good else "0",
                "_client_version": "9.7.8.0",
            },
        )

        users = {}
        for u in payload.get("user_list") or []:
            if u.get("id") is not None:
                users[int(u["id"])] = u

        threads = []
        seen = set()
        for raw in payload.get("thread_list") or []:
            tid = str(raw.get("tid") or raw.get("id") or "")
            if not tid or tid in seen:
                continue
            seen.add(tid)
            author_id = raw.get("author_id")
            user = users.get(int(author_id or 0)) or {}
            abstract_parts = []
            for item in raw.get("abstract") or []:
                if item.get("text"):
                    abstract_parts.append(str(item["text"]))
            threads.append({
                "tid": tid,
                "title": raw.get("title", ""),
                "author": user.get("name_show") or user.get("name") or "",
                "reply_count": int(raw.get("reply_num") or 0),
                "agree_count": int((raw.get("agree") or {}).get("agree_num") or 0),
                "last_reply_time": raw.get("last_time", ""),
                "pub_time": self._client_time(raw.get("create_time")),
                "brief": "\n".join(abstract_parts).strip(),
                "is_excellent": bool(raw.get("is_excellent_thread")),
                "url": f"https://tieba.baidu.com/p/{tid}",
            })

        page_info = payload.get("page") or {}
        try:
            total_pages = max(1, int(page_info.get("total_page") or 1))
        except (TypeError, ValueError):
            total_pages = 1
        forum = payload.get("forum") or {}
        return {
            "forum_name": forum.get("name", forum_name),
            "current_page": page,
            "total_pages": total_pages,
            "threads": threads,
        }

    # ── 辅助 ────────────────────────────────────────────────────
    @staticmethod
    def _parse_total_pages(soup: BeautifulSoup) -> int:
        nums = []
        for a in soup.select("a"):
            text = a.text.strip()
            if text.isdigit():
                nums.append(int(text))
        inp = soup.select_one("input[type='text'][max]")
        if inp:
            try:
                nums.append(int(inp["max"]))
            except (ValueError, KeyError):
                pass
        # 也检查分页容器
        for span in soup.select(".page_count, .total_page"):
            m = re.search(r"(\d+)", span.text)
            if m:
                nums.append(int(m.group(1)))
        return max(nums) if nums else 1

    # ── 持久化 ──────────────────────────────────────────────────
    def save_cookies(self, bduss: str, stoken: str = "", baiduid: str = ""):
        data = {"BDUSS": bduss, "STOKEN": stoken, "BAIDUID": baiduid}
        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"[*] Cookie 已保存到 {COOKIE_FILE}")
        domain = ".baidu.com"
        self.session.cookies.set("BDUSS", bduss, domain=domain)
        if stoken:
            self.session.cookies.set("STOKEN", stoken, domain=domain)
        if baiduid:
            self.session.cookies.set("BAIDUID", baiduid, domain=domain)
        self._logged_in = True
        self._cookies_set = True

    def load_cookies(self) -> bool:
        if not os.path.exists(COOKIE_FILE):
            return False
        try:
            with open(COOKIE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._apply_cookies(data)
            return True
        except (json.JSONDecodeError, KeyError):
            return False

    def _apply_cookies(self, data: dict):
        bduss = data.get("BDUSS", "")
        stoken = data.get("STOKEN", "")
        baiduid = data.get("BAIDUID", "")
        domain = ".baidu.com"
        if bduss:
            self.session.cookies.set("BDUSS", bduss, domain=domain)
        if stoken:
            self.session.cookies.set("STOKEN", stoken, domain=domain)
        if baiduid:
            self.session.cookies.set("BAIDUID", baiduid, domain=domain)
        self._logged_in = bool(bduss)
        self._cookies_set = bool(bduss or baiduid)

    def _cookies_for_bridge(self) -> dict:
        """返回给桥接脚本使用的 cookie dict。"""
        cookies = {}
        for c in self.session.cookies:
            if c.name in ("BDUSS", "BAIDUID", "STOKEN"):
                cookies[c.name] = c.value
        return cookies


# ── 输出格式化 ──────────────────────────────────────────────────
def fmt_thread(result: dict):
    print(f"\n{'=' * 60}")
    print(f"  [{result['tid']}] {result['title']}")
    print(f"{'=' * 60}")
    print(f"  楼主: {result['author']}  |  时间: {result['pub_time']}")
    if result.get("fetch_all"):
        print(f"  页码: 全部 {result['total_pages']} 页 (已抓取 {len(result.get('pages_fetched', []))} 页)")
    else:
        print(f"  页码: {result['current_page']}/{result['total_pages']}")
    print(f"{'-' * 60}")
    if result["images"]:
        print(f"  [图片 {len(result['images'])} 张]")
    print(f"  {result['content_text']}")
    if result["replies"]:
        print(f"\n  -- 回复 (共 {len(result['replies'])} 条) --")
        for r in result["replies"]:
            flag = f"#{r['floor']}"
            img_flag = f" [图{len(r['images'])}]" if r["images"] else ""
            print(f"  {flag} {r['author']} {r['pub_time']}{img_flag}")
            text = r["content_text"]
            if text:
                for line in text.split("\n"):
                    print(f"     {line}")
            for c in r.get("comments") or []:
                cimg = f" [图{len(c.get('images', []))}]" if c.get("images") else ""
                print(f"     ↳ {c.get('author','')} {c.get('time','')}{cimg}")
                if c.get("content_text"):
                    print(f"       {c['content_text']}")
            print()
    else:
        print("  (本页无回复)")


def fmt_thread_list(result: dict, label: str = "帖子"):
    print(f"\n{'=' * 60}")
    print(f"  {result.get('forum_name','?')}吧 -- {label}")
    print(f"  (第 {result['current_page']}/{result['total_pages']} 页)")
    print(f"{'=' * 60}")
    items = result.get("threads", result.get("results", []))
    for i, t in enumerate(items, 1):
        reply_info = ""
        if "reply_count" in t and t.get("reply_count"):
            reply_info = f" 回复:{t['reply_count']}"
        if "brief" in t and t["brief"]:
            print(f"  {i:>3}. {t['title']}")
            print(f"       | {t['brief'][:120]}")
        else:
            print(f"  {i:>3}. {t['title']}{reply_info}")
        author = t.get("author", "")
        if author:
            print(f"      作者: {author}")
        if "last_reply_time" in t and t["last_reply_time"]:
            print(f"      最后回复: {t['last_reply_time']}")
        print()


# ── 命令行入口 ──────────────────────────────────────────────────
def cmd_login(args):
    print("\n" + "=" * 60)
    print("  贴吧 Cookie 录入")
    print("=" * 60)
    print()
    print("  如何获取:")
    print("  1. 浏览器登录 https://tieba.baidu.com")
    print("  2. F12 -> Application -> Cookies -> .baidu.com")
    print("  3. 复制 BDUSS (必填)、STOKEN (可选)、BAIDUID (可选)")
    print()
    bduss = input("  BDUSS: ").strip()
    if not bduss:
        print("[!] BDUSS 不能为空")
        sys.exit(1)
    stoken = input("  STOKEN (可选): ").strip()
    baiduid = input("  BAIDUID (可选): ").strip()
    client = TiebaClient()
    client.save_cookies(bduss, stoken, baiduid)
    print("[OK] 保存成功")


def cmd_cookie_check(args):
    status = check_stored_cookie()
    if status.get("logged_in"):
        print("[OK] BDUSS 有效")
        for k in ("user_name", "nickname", "tieba_uid"):
            if status.get(k):
                print(f"  {k}: {status[k]}")
    else:
        print("[ERR] BDUSS 无效或未配置")
        print(f"  原因: {status.get('reason','')}")
        sys.exit(2)


def cmd_favorites(args):
    """导出贴吧账号收藏；也可选浏览器收藏夹模式。"""
    items: list[dict] = []


    if not args.bookmarks and not args.file:
        try:
            result = read_account_favorites(max_pages=args.max_pages)
        except RuntimeError as e:
            print(f"[ERR] {e}")
            sys.exit(2)
        if args.json:
            text = json.dumps(result, ensure_ascii=False, indent=2)
            if args.out:
                with open(args.out, "w", encoding="utf-8") as f:
                    f.write(text)
                print(f"[OK] 已写入 {args.out}")
            else:
                print(text)
        else:
            for item in result["favorites"]:
                if getattr(args, "urls", False):
                    print(item["url"])
                    continue
                title = f"    # [{item['forum_name']}] {item['title']}" if item.get("title") else ""
                print(item["url"] + title)
            if not result["favorites"]:
                print("[i] 账号收藏为空")
        return

    # 以下保留为浏览器 Bookmarks / 导出 HTML 模式
    if args.file:
        path = os.path.abspath(os.path.expanduser(args.file))
        if not os.path.isfile(path):
            print(f"[ERR] 找不到书签文件: {path}")
            sys.exit(2)
        if args.html:
            with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
                for tid in extract_tids(f.read()):
                    items.append({
                        "tid": tid, "title": "", "url": f"https://tieba.baidu.com/p/{tid}",
                        "browser_profile": os.path.basename(path),
                    })
        else:
            items.extend(collect_tieba_bookmarks(path, args.profile))
    else:
        for path in browser_bookmark_paths(args.browser):
            items.extend(collect_tieba_bookmarks(path, args.profile))

    # 同一帖子的不同书签/不同 profile 去重
    dedup: dict[str, dict] = {}
    for item in items:
        old = dedup.get(item["tid"])
        if not old or (item.get("title") and not old.get("title")):
            dedup[item["tid"]] = item
    result = {
        "source": "bookmarks",
        "browser": args.browser,
        "profile": args.profile or "all",
        "count": len(dedup),
        "favorites": list(dedup.values()),
    }

    if args.json:
        text = json.dumps(result, ensure_ascii=False, indent=2)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(text)
            print(f"[OK] 已写入 {args.out}")
        else:
            print(text)
        return

    for item in result["favorites"]:
        title = f"    # {item['title']}" if item.get("title") else ""
        print(item["url"] + title)
    if not result["favorites"]:
        print("[i] 没有在收藏夹中找到 tieba.baidu.com/p/... 链接")


def cmd_batch(args):
    """批量抓取 URL/TID 清单，每个帖子输出一个完整 JSON。"""
    tids: list[str] = []
    seen = set()
    for source in args.sources:
        for tid in load_source_tids(source):
            if tid not in seen:
                seen.add(tid)
                tids.append(tid)
    if not tids:
        print("[ERR] 没有从输入中提取到 TID")
        sys.exit(2)
    if args.limit > 0:
        tids = tids[:args.limit]

    out_dir = os.path.abspath(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)
    client = _get_client()
    summary = {"output_dir": out_dir, "total": len(tids), "success": 0, "failures": []}

    for idx, tid in enumerate(tids, 1):
        print(f"\n[{idx}/{len(tids)}] 抓取 {tid}")
        try:
            result = client.read_thread(tid)
            output_path = os.path.join(out_dir, f"thread_{tid}.json")
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            summary["success"] += 1
            print(f"[OK] {result.get('title','')}  楼层={result.get('floor')} 回复={result.get('reply_count')}")
            print(f"     {output_path}")
        except Exception as e:
            summary["failures"].append({"tid": tid, "error": str(e)})
            print(f"[ERR] {e}")
        if idx < len(tids):
            time.sleep(args.delay)

    summary_path = os.path.join(out_dir, "_summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"\n[OK] 完成 {summary['success']}/{summary['total']}，摘要: {summary_path}")


def cmd_thread(args):
    client = _get_client()
    try:
        result = client.read_thread(args.tid, page=args.page)
    except RuntimeError as e:
        print(f"[ERR] {e}")
        sys.exit(2)
    if args.json:
        text = json.dumps(result, ensure_ascii=False, indent=2)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(text)
            print(f"[OK] 已写入 {args.out}")
        else:
            print(text)
    else:
        fmt_thread(result)


def cmd_forum(args):
    client = _get_client()
    result = client.read_forum(args.forum, page=args.page)
    _emit_list(args, result, "帖子列表")


def cmd_search(args):
    client = _get_client()
    try:
        result = client.search(args.forum, args.keyword, page=args.page, max_pages=args.max_pages)
    except RuntimeError as e:
        print(f"[ERR] {e}")
        sys.exit(2)
    _emit_list(args, result, f'搜索结果: "{args.keyword}"')


def cmd_featured(args):
    client = _get_client()
    result = client.read_featured(args.forum, page=args.page)
    _emit_list(args, result, "精华帖")


def _emit_list(args, result: dict, label: str):
    if getattr(args, "json", False):
        text = json.dumps(result, ensure_ascii=False, indent=2)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(text)
            print(f"[OK] 已写入 {args.out}")
        else:
            print(text)
        return

    if getattr(args, "urls", False):
        items = result.get("threads", result.get("results", []))
        for t in items:
            if t.get("url"):
                print(t["url"])
        return

    fmt_thread_list(result, label=label)


def _get_client() -> TiebaClient:
    client = TiebaClient()
    if client.load_cookies():
        print("[i] 已加载本地 cookie")
    else:
        env = _load_env()
        bduss = env.get("BDUSS", "")
        baiduid = env.get("BAIDUID", "")
        if bduss:
            client = TiebaClient(bduss=bduss, baiduid=baiduid)
            print("[i] 已从 .envs 加载 cookie")
        else:
            print("[i] 未登录 -- 仅尝试公开读取")
    return client


def main():
    parser = argparse.ArgumentParser(
        description="百度贴吧 · 内测读取工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_login = sub.add_parser("login", help="录入 BDUSS/STOKEN/BAIDUID")
    p_login.set_defaults(func=cmd_login)

    p_ck = sub.add_parser("cookie-check", aliases=["whoami"], help="校验本地 BDUSS 登录态")
    p_ck.set_defaults(func=cmd_cookie_check)

    p_thread = sub.add_parser("thread", help="读取帖子内容和回复")
    p_thread.add_argument("tid", help="帖子 TID (URL 中的数字)")
    p_thread.add_argument(
        "--page", "-p", type=int, default=None,
        help="只读指定页；不传时默认自动翻完所有页"
    )
    p_thread.add_argument("--json", action="store_true", help="输出完整结构化 JSON")
    p_thread.add_argument("--out", help="配合 --json 写入文件")
    p_thread.set_defaults(func=cmd_thread)

    p_forum = sub.add_parser("forum", help="读取贴吧帖子列表")
    p_forum.add_argument("forum", help="贴吧名称")
    p_forum.add_argument("--page", "-p", type=int, default=1, help="页码 (默认 1)")
    p_forum.add_argument("--json", action="store_true", help="输出完整结构化 JSON")
    p_forum.add_argument("--out", help="配合 --json 写入文件")
    p_forum.add_argument("--urls", action="store_true", help="只输出帖子 URL 列表")
    p_forum.set_defaults(func=cmd_forum)

    p_search = sub.add_parser("search", help="在贴吧内搜索关键词")
    p_search.add_argument("forum", help="贴吧名称")
    p_search.add_argument("keyword", help="搜索关键词")
    p_search.add_argument("--page", "-p", type=int, default=1, help="页码 (默认 1)")
    p_search.add_argument("--max-pages", type=int, default=1, help="最多抓取搜索结果页数 (默认 1)")
    p_search.add_argument("--json", action="store_true", help="输出完整结构化 JSON")
    p_search.add_argument("--out", help="配合 --json 写入文件")
    p_search.add_argument("--urls", action="store_true", help="只输出帖子 URL 列表")
    p_search.set_defaults(func=cmd_search)

    p_feat = sub.add_parser("featured", aliases=["good"], help="读取精华帖")
    p_feat.add_argument("forum", help="贴吧名称")
    p_feat.add_argument("--page", "-p", type=int, default=1, help="页码 (默认 1)")
    p_feat.add_argument("--json", action="store_true", help="输出完整结构化 JSON")
    p_feat.add_argument("--out", help="配合 --json 写入文件")
    p_feat.add_argument("--urls", action="store_true", help="只输出帖子 URL 列表")

    p_fav = sub.add_parser(
        "favorites", aliases=["fav"],
        help="导出贴吧账号收藏，或浏览器收藏夹中的贴吧帖子 URL"
    )
    p_fav.add_argument(
        "--bookmarks", action="store_true",
        help="改为扫描本地浏览器收藏夹，而不是贴吧账号收藏"
    )
    p_fav.add_argument(
        "--max-pages", type=int, default=0,
        help="账号收藏最多抓几页；0 表示自动翻完（每页 10 条）"
    )
    p_fav.add_argument(
        "--browser", default="edge", choices=["edge", "chrome", "brave", "all"],
        help="要扫描的浏览器 (默认 edge)"
    )
    p_fav.add_argument("--profile", default="", help="只扫描指定 profile 目录，例如 Default 或 Profile 1")
    p_fav.add_argument("--file", help="使用指定 Bookmarks JSON / 收藏夹 HTML 文件")
    p_fav.add_argument("--html", action="store_true", help="--file 是浏览器导出的 HTML 收藏夹")
    p_fav.add_argument("--json", action="store_true", help="输出含标题/来源的 JSON")
    p_fav.add_argument("--out", help="配合 --json 写入文件")
    p_fav.add_argument("--urls", action="store_true", help="只输出帖子 URL，不输出标题注释")
    p_fav.set_defaults(func=cmd_favorites)

    p_batch = sub.add_parser(
        "batch",
        help="批量抓取 TID/URL 或清单文件中的所有帖子"
    )
    p_batch.add_argument("sources", nargs="+", help="TID、帖子 URL，或 .txt/.json/.html 清单文件")
    p_batch.add_argument("--out-dir", default="output\\tieba_batch", help="输出目录")
    p_batch.add_argument("--limit", type=int, default=0, help="最多抓取前 N 个帖子；0 表示全部")
    p_batch.add_argument("--delay", type=float, default=0.8, help="两个帖子之间等待秒数")
    p_batch.set_defaults(func=cmd_batch)
    p_feat.set_defaults(func=cmd_featured)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
