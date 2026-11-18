"""Google Scholar 检索源（best-effort，走 Clash 代理，严格限速）。

探路结论（2026-08-11 实测）：
- GS 在中国需走 Clash 代理；轻量 HTTP GET（带浏览器 UA + Referer）比完整 Playwright
  浏览器更不容易触发 reCAPTCHA（浏览器新会话常被 429 转到 google.com/sorry 拦截页）。
- 高频请求会封 IP（历史上 60+ 次/分钟就拉黑）。安全速率 ≤10/分钟，本模块节流 6s/次。
- 解析：结果块 `div.gs_r`，标题 `h3.gs_rt`，元信息 `div.gs_a`（作者-期刊,年份-来源），
  [PDF] 链接在 `span.gs_ctg2` 的父 `<a>`。
- 429/被拦时降权（返回空列表 + 告警），不阻断 pipeline。
"""

from __future__ import annotations

import html
import logging
import re
from typing import Any

from backend.services.papers.http_client import ROUTE_PROXY, http_get, throttle

logger = logging.getLogger(__name__)

_BASE = "https://scholar.google.com/scholar"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://scholar.google.com/",
    "User-Agent": _UA,
}
_TITLE_RE = re.compile(r'<h3 class="gs_rt"[^>]*>(.*?)</h3>', re.S)
_A_RE = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_META_RE = re.compile(r'<div class="gs_a">(.*?)</div>', re.S)
_PDF_RE = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>(?:(?!</a>).)*?\[PDF\]', re.S)
_SNIPPET_RE = re.compile(r'<div class="gs_rs"[^>]*>(.*?)</div>', re.S)


def _clean(text: str) -> str:
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text))).strip()


def _parse_meta(meta_text: str) -> dict[str, str]:
    """gs_a 行：`作者 - 期刊/会议, 年份 - 来源` → {authors, venue, year, source_host}"""
    parts = [p.strip() for p in meta_text.split(" - ")]
    out = {"authors": "", "venue": "", "year": "", "source_host": ""}
    if parts:
        out["authors"] = _clean(parts[0])
    if len(parts) >= 2:
        venue_year = _clean(parts[1])
        m = re.search(r"(.*?),\s*(\d{4})$", venue_year)
        if m:
            out["venue"], out["year"] = m.group(1), m.group(2)
        else:
            out["venue"] = venue_year
    if len(parts) >= 3:
        out["source_host"] = _clean(parts[2])
    return out


def _parse_page(body: str) -> list[dict[str, Any]]:
    """按"标题位置作结果边界"解析：每个结果 = 本篇标题 → 下一篇标题 之间的 HTML。"""
    titles = list(_TITLE_RE.finditer(body))
    results: list[dict[str, Any]] = []
    for i, title_m in enumerate(titles):
        start = title_m.end()
        end = titles[i + 1].start() if i + 1 < len(titles) else len(body)
        window = body[start:end]

        title = _clean(title_m.group(1)).replace("[PDF]", "").strip()

        # 主链接（标题内的链接，或窗口内第一个外部 http 链接）
        link = ""
        a_m = _A_RE.search(title_m.group(1))
        if a_m and "scholar.google" not in a_m.group(1):
            link = html.unescape(a_m.group(1))
        if not link:
            for lm in _A_RE.findall(window):
                href = html.unescape(lm[0])
                if "scholar.google" not in href and href.startswith("http"):
                    link = href
                    break

        meta_m = _META_RE.search(window)
        meta = _parse_meta(_clean(meta_m.group(1))) if meta_m else {}

        pdf_m = _PDF_RE.search(window)
        pdf_url = html.unescape(pdf_m.group(1)) if pdf_m else ""

        snip_m = _SNIPPET_RE.search(window)
        snippet = _clean(snip_m.group(1)) if snip_m else ""

        results.append({
            "title": title,
            "authors": meta.get("authors", ""),
            "venue": meta.get("venue", ""),
            "year": int(meta["year"]) if meta.get("year", "").isdigit() else None,
            "source_host": meta.get("source_host", ""),
            "link": link,
            "pdf_url": pdf_url,
            "snippet": snippet,
        })
    return results


def search_records(
    query: str,
    *,
    rows: int = 10,
    start: int = 0,
    author: str = "",
    title: str = "",
    year_from: int | None = None,
    year_to: int | None = None,
    exact: bool = False,
) -> list[dict[str, Any]]:
    """按关键词搜 Google Scholar（best-effort）。返回统一 dict 列表。

    高级选项（对应网页）：
        author: 作者限定 → `author:"..."` 操作符
        title:  标题限定 → `intitle:"..."` 操作符
        year_from / year_to: 年份区间 → as_ylo/as_yhi 参数
        exact: 精确短语 → 关键词加引号
    """
    from urllib.parse import urlencode

    q_parts = []
    if author:
        q_parts.append(f'author:"{author}"')
    if title:
        q_parts.append(f'intitle:"{title}"')
    q_parts.append(f'"{query}"' if exact else query)
    q = " ".join(q_parts)

    params: dict[str, Any] = {"hl": "en", "q": q, "start": start}
    if year_from:
        params["as_ylo"] = year_from
    if year_to:
        params["as_yhi"] = year_to
    url = f"{_BASE}?{urlencode(params)}"
    throttle("gs", 6.0)  # ≤10/min，防封 IP
    try:
        status, body = http_get(url, route=ROUTE_PROXY, timeout=25, headers=_HEADERS, retries=2, backoff=6.0)
    except Exception as e:
        logger.warning("GS 请求失败（可能被拦/封）: %s", e)
        return []

    text = body.decode("utf-8", errors="ignore")
    if "sorry" in text or "gs_r" not in text:
        logger.warning("GS 返回拦截页（sorry/验证码），降权跳过")
        return []
    return _parse_page(text)[:rows]


def probe() -> dict:
    """GS 连通性/封禁检测：轻量 GET 一次，看是否被拦截（sorry）或代理不可达。

    注意：与 search_records 共用 GS 节流槽（6s/次）。
    """
    from urllib.parse import urlencode

    url = f"{_BASE}?{urlencode({'hl': 'en', 'q': 'test'})}"
    throttle("gs", 6.0)
    try:
        _, body = http_get(url, route=ROUTE_PROXY, timeout=15, headers=_HEADERS, retries=1, backoff=6.0)
    except Exception as e:
        return {"ok": False, "error": f"GS 请求失败（代理未开/网络）: {type(e).__name__}"}
    text = body.decode("utf-8", errors="ignore")
    if "sorry" in text or "gs_r" not in text:
        return {"ok": False, "error": "返回拦截页（sorry/验证码），IP 可能已被标记，换 Clash 节点或稍后再试"}
    return {"ok": True}
