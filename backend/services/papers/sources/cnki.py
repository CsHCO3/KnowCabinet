"""知网 CNKI 检索源（A1 纯 HTTP，带机构 cookie）。

实现依据（2026-08-12 反查）：
- 搜索走 `POST https://kns.cnki.net/kns8s/brief/grid`（application/x-www-form-urlencoded）。
- 关键参数 QueryJson（紧凑 JSON，无空格！json.dumps 默认的空格会被 urlencode 成 +，导致
  服务器报"检索模型参数错误"）。字段：Field=SU(主题)、Operator=TOPRANK、KuaKuCode=数据库码表。
- 必须带机构 cookie（SID_kns_new/SID_restapi/Ecp_* 等）。cookie 会过期：失效时响应为
  "检索模型参数错误"或跳登录，需重新登录导出 data/paper_sessions/cnki/storage_state.json。
  该文件是本地运行时数据，data/ 已被 .gitignore 排除（不要写回包目录）。
- 响应为 HTML 结果行（`class="name"` 标题链接 → kcms2/article/abstract 详情页）。
"""

from __future__ import annotations

import html
import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from backend.services.papers.http_client import ROUTE_DIRECT, http_get
from backend.services.papers.session_paths import storage_state

logger = logging.getLogger(__name__)

_GRID_URL = "https://kns.cnki.net/kns8s/brief/grid"
_SS_FILE = storage_state("cnki")
_KUAKU = "YSTT4HG0,LSTPFY1C,EMRPGLPA,JUP3MUPD,MPMFIG1A,WQ0UVIAA,BLZOG7CK,PWFIRAGL,NN3FJMUV,NLBO1Z6R"


def _cookie_header() -> str:
    if not _SS_FILE.exists():
        raise RuntimeError(f"未找到知网 cookie: {_SS_FILE}（需先登录并导出）")
    state = json.loads(_SS_FILE.read_text(encoding="utf-8"))
    pairs = []
    for c in state.get("cookies", []):
        if "cnki" not in c.get("domain", ""):
            continue
        value = c.get("value", "")
        try:
            value.encode("latin-1")
        except UnicodeEncodeError:
            continue  # last_org 等中文值，非鉴权必需，跳过
        pairs.append(f'{c["name"]}={value}')
    return "; ".join(pairs)


def _build_form(
    keyword: str, page: int = 1, page_size: int = 20,
    *, author: str = "", title: str = "",
) -> bytes:
    # 字段码（知网标准）：SU主题 TI篇名 KY关键词 AB摘要 FT全文 AU作者
    items = []
    aside = ""
    if keyword:
        items.append({"Field": "SU", "Value": keyword, "Operator": "TOPRANK", "Logic": 0, "Title": "主题"})
        aside = f"(主题：{keyword})"
    if title:
        items.append({"Field": "TI", "Value": title, "Operator": "TOPRANK", "Logic": 0, "Title": "篇名"})
        aside += f"(篇名:{title})"
    if author:
        items.append({"Field": "AU", "Value": author, "Operator": "TOPRANK", "Logic": 0, "Title": "作者"})
        aside += f"(作者:{author})"
    qj = {
        "Platform": "NZKPT", "Resource": "CROSSDB", "Classid": "WD0FTY92", "Products": "",
        "QNode": {"QGroup": [{
            "Key": "Subject", "Title": "", "Logic": 0,
            "Items": items,
            "ChildItems": [],
        }]},
        "ExScope": 1, "SearchType": 2, "Rlang": "CHINESE", "KuaKuCode": _KUAKU,
        "Expands": {}, "View": "changeDBCh", "SearchFrom": 1,
    }
    form = {
        "boolSearch": "true",
        # 紧凑 JSON：separators 去掉空格，否则被 urlencode 成 + 导致服务器参数错误
        "QueryJson": json.dumps(qj, ensure_ascii=False, separators=(",", ":")),
        "pageNum": str(page), "pageSize": str(page_size),
        "sortField": "", "sortType": "", "dstyle": "listmode", "productStr": "",
        "aside": aside, "searchFrom": "资源范围：总库",
        "subject": "", "language": "CHS", "uniplatform": "NZKPT", "CurPage": str(page),
    }
    return urlencode(form).encode("utf-8")


def _grid_headers(keyword: str, page: int = 1) -> dict[str, str]:
    return {
        "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
        "accept": "*/*",
        "x-requested-with": "XMLHttpRequest",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/123.0 Safari/537.36",
        "cookie": _cookie_header(),
        "referer": f"https://kns.cnki.net/kns8s/search?classid=WD0FTY92&kw={quote(keyword)}&korder=SU&language=CHS&uniplatform=NZKPT",
        "origin": "https://kns.cnki.net",
    }


def _strip_html(t: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", t)).strip()


def _parse_grid(body: str) -> list[dict[str, Any]]:
    """解析 brief/grid 的 HTML 结果行（<tr> 行，标题在 td.name 的 fz14 链接）。"""
    results: list[dict[str, Any]] = []
    for row in re.findall(r"<tr>(.*?)</tr>", body, re.S):
        if "kcms2/article" not in row:
            continue
        tm = re.search(
            r'<a[^>]*class="fz14"[^>]*href="([^"]*kcms2/article/[^"]*)"[^>]*>(.*?)</a>',
            row, re.S,
        )
        if not tm:
            continue
        title = _strip_html(tm.group(2))
        link = html.unescape(tm.group(1))

        authors: list[str] = []
        am = re.search(r"<td class=['\"]author['\"]>(.*?)</td>", row, re.S)
        if am:
            auth_cell = am.group(1)
            anchors = re.findall(r"<a[^>]*>(.*?)</a>", auth_cell, re.S)
            if anchors:
                authors = [a.strip() for a in (_strip_html(x) for x in anchors) if a.strip()][:10]
            else:
                authors = [a.strip() for a in re.split(r"[;；]", _strip_html(auth_cell)) if a.strip()][:10]

        journal = ""
        jm = re.search(r"<td class=['\"]source['\"][^>]*>(.*?)</td>", row, re.S)
        if jm:
            journal = _strip_html(jm.group(1))

        year: int | None = None
        # 年份在行内，取第一个合理的 4 位年份
        for m in re.finditer(r"\b(19\d{2}|20\d{2})\b", row):
            y = int(m.group(1))
            if 1980 <= y <= 2030:
                year = y
                break

        results.append({
            "title": title,
            "authors": authors,
            "year": year,
            "venue": journal,
            "doi": "",
            "link": link,
            "source": "cnki",
        })
    return results


def search_records(
    keyword: str, *, rows: int = 20,
    author: str = "", title: str = "",
) -> list[dict[str, Any]]:
    """按关键词搜知网（A1 纯 HTTP）。返回统一 dict 列表。cookie 失效抛异常/告警。

    高级选项：author（作者限定 AU）、title（篇名限定 TI）——QueryJson 多字段 Items。
    """
    data = _build_form(keyword, page=1, page_size=rows, author=author, title=title)
    try:
        from urllib.request import Request, urlopen
        req = Request(_GRID_URL, data=data, headers=_grid_headers(keyword), method="POST")
        with urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        logger.warning("知网搜索失败: %s", e)
        return []
    if "检索模型参数错误" in body or "登录" in body[:500] and "location" in body[:500]:
        logger.warning("知网返回参数错误/登录跳转（cookie 失效？需重新登录导出）")
        return []
    return _parse_grid(body)


def probe() -> dict:
    """检测知网会话状态：cookie 文件、字段、真实搜索探测。

    Returns:
        {"ok": bool, "file": str, "configured": bool, "cookie_keys": list[str],
         "results": int|None, "error": str|None}
    """
    info = {"file": str(_SS_FILE), "configured": False, "cookie_keys": []}
    if not _SS_FILE.exists():
        return {**info, "ok": False, "error": "未找到 cookie 文件，需先登录并导出（cnki_agent.js）"}
    try:
        state = json.loads(_SS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        return {**info, "ok": False, "error": f"cookie 文件解析失败: {e}"}
    keys = sorted({c.get("name", "") for c in state.get("cookies", []) if "cnki" in c.get("domain", "")})
    info["cookie_keys"] = keys
    info["configured"] = bool(keys)
    if not keys:
        return {**info, "ok": False, "error": "storage_state 中无 cnki 域 cookie，需重新导出"}
    # 真实探测：最小一次 brief/grid 搜索（page_size=1，验证 cookie 是否仍有效）
    try:
        data = _build_form("test", page=1, page_size=1)
        from urllib.request import Request, urlopen
        req = Request(_GRID_URL, data=data, headers=_grid_headers("test"), method="POST")
        with urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
        if "检索模型参数错误" in body or ("登录" in body[:600] and "location" in body[:600]):
            return {**info, "ok": False, "error": "cookie 已失效（返回登录跳转），需重新登录导出"}
        return {**info, "ok": True, "results": len(_parse_grid(body))}
    except Exception as e:
        return {**info, "ok": False, "error": f"网络/请求失败: {e}"}
