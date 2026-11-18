"""ePrint (IACR Cryptology ePrint Archive) 检索源（A1 全自动，直连免认证）。

来源：https://eprint.iacr.org —— 密码学领域最重要的预印本存档，全站 PDF 开放。
检索走高级搜索页 `/search` 的 GET 表单（参数 q/title/authors/category/submittedafter/before），
返回 HTML；用正则按结果卡片 `<div class="mb-4">` 切块解析。

探路结论（2026-08-18 实测）：
- 国内直连即可，别进 Clash（与 arXiv 同）；无登录、无验证码，自动化等级 A1。
- title/author/year_from/year_to/category 落到 ePrint 原生参数（title/authors/submittedafter/before），
  与 PaperQuery 结构化条件一一对应，无需启发式映射。
- 结果卡片内部有 `</div></div>` 双重闭合，不能用它做边界（会截断摘要）；
  改为按下一条 `<div class="mb-4">` 切块，末条多余块因无 paperlink 被跳过。
- PDF 直连可下（`/YY/NNNN.pdf`），`oa_url` 直接给真实 PDF 链接。
- ePrint 无分页参数，结果全在一页，`max_results` 只做截取。
- best-effort：失败返回 [] 并告警，不阻断 pipeline。
"""
from __future__ import annotations

import html
import logging
import re
from typing import Any
from urllib.parse import urlencode

from backend.services.papers.http_client import ROUTE_DIRECT, http_get, throttle

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://eprint.iacr.org/search"

# ePrint 七类目（与 search 表单 category 取值一致）
CATEGORIES = {
    "APPLICATIONS": "Applications",
    "PROTOCOLS": "Cryptographic protocols",
    "FOUNDATIONS": "Foundations",
    "IMPLEMENTATION": "Implementation",
    "SECRETKEY": "Secret-key cryptography",
    "PUBLICKEY": "Public-key cryptography",
    "ATTACKS": "Attacks and cryptanalysis",
}

# 结果卡片：每条一个 <div class="mb-4">。注意不能以内部 `</div></div>` 做边界
# （标题区/作者区内部就有双重闭合），只以下一条 <div class="mb-4"> 切块；末条多余块无
# paperlink 会被跳过。卡片内含 paperlink(=> /YY/NNNN)、<strong>标题、fst-italic 作者、
# category-XXX badge、search-abstract 摘要、Last updated。
_ITEM_SPLIT_RE = re.compile(r'<div class="mb-4">')
_PAPERLINK_RE = re.compile(r'class="paperlink"[^>]*href="/(\d{4}/\d+)"')
_TITLE_RE = re.compile(r"<strong>(.*?)</strong>", re.S)
_AUTHORS_RE = re.compile(r'class="fst-italic">(.*?)</span>', re.S)
_CATEGORY_RE = re.compile(r"category category-([A-Z]+)")
_ABSTRACT_RE = re.compile(r'class="mb-0 mt-1 search-abstract">(.*?)</p>', re.S)
_UPDATED_RE = re.compile(r"Last updated:\s*([\d-]+)")


def _clean(text: str) -> str:
    """去 HTML 标签 + 解实体 + 压缩空白。"""
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_results(body: str) -> list[dict[str, Any]]:
    """解析 /search 结果页 HTML → 统一 dict 列表。

    最稳妥：直接在整页里按 `<div class="mb-4">` 切块。每个结果卡都是这个 div，
    卡片内部不会再有平级的 mb-4 div；最后一条之后是结果区闭合 div/表单，
    该多余块没有 paperlink 会被跳过。
    """
    chunks = _ITEM_SPLIT_RE.split(body)
    # chunks[0] 是页面开头（导航/表单），跳过；每个 chunk 对应一条 <div class="mb-4">
    out: list[dict[str, Any]] = []
    for chunk in chunks[1:]:
        m = _PAPERLINK_RE.search(chunk)
        if not m:
            continue
        paper_id = m.group(1)          # 如 "2026/1701"
        year = int(paper_id.split("/")[0])

        m = _TITLE_RE.search(chunk)
        title = _clean(m.group(1)) if m else ""

        m = _AUTHORS_RE.search(chunk)
        authors = []
        if m:
            authors = [a.strip() for a in _clean(m.group(1)).split(",") if a.strip()]

        mc = _CATEGORY_RE.search(chunk)
        category = CATEGORIES.get(mc.group(1), mc.group(1)) if mc else ""

        ma = _ABSTRACT_RE.search(chunk)
        abstract = _clean(ma.group(1)) if ma else ""

        mu = _UPDATED_RE.search(chunk)
        updated = mu.group(1) if mu else ""

        out.append({
            "paper_id": paper_id,          # ePrint 专用标识 YYYY/NNNN
            "title": title,
            "authors": authors,
            "year": year,
            "category": category,
            "abstract": abstract,
            "updated": updated,
            "pdf_url": f"https://eprint.iacr.org/{paper_id}.pdf",
            "page_url": f"https://eprint.iacr.org/{paper_id}",
            "short_url": f"https://ia.cr/{paper_id}",
        })
    return out


def search_records(
    query: str = "",
    *,
    max_results: int = 20,
    title: str = "",
    author: str = "",
    category: str = "",
    year_from: int = 0,
    year_to: int = 0,
) -> list[dict[str, Any]]:
    """ePrint /search 检索（GET，直连，best-effort）。

    title/author/year 直接落到 ePrint 原生参数 title/authors/submittedafter/before。
    返回统一 dict 列表（与 arxiv.search_records 同构），失败/空返回 []。
    """
    params: dict[str, str] = {}
    if query:
        params["q"] = query
    if title:
        params["title"] = title
    if author:
        params["authors"] = author
    if category and category.upper() in CATEGORIES:
        params["category"] = category.upper()
    if year_from:
        params["submittedafter"] = str(year_from)
    if year_to:
        params["submittedbefore"] = str(year_to)
    if query or title:
        params["relevance"] = "on"   # 有全文词时按相关度排

    url = f"{_SEARCH_URL}?{urlencode(params)}"
    throttle("eprint", 1.5)
    try:
        _, body = http_get(
            url, route=ROUTE_DIRECT, timeout=25, retries=2,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )
    except Exception as e:
        logger.warning("eprint 请求失败 %s: %s", type(e).__name__, e)
        return []

    try:
        recs = parse_results(body.decode("utf-8", errors="replace"))
    except Exception as e:
        logger.warning("eprint 解析失败: %s", e)
        return []
    return recs[:max_results]
