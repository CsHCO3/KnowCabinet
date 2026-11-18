"""PDF 开放版下载器。

策略：
- 取开放 URL 优先级：Semantic Scholar openAccessPdf → OpenAlex oa_url。
- 下载先直连；失败（超时/403/反爬）自动走 Clash 代理兜底。
- 校验文件头 `%PDF`，失败则删除残文件并标记失败。
- 输出三态：downloaded（已下载） / oa_link_only（有链接无文件） / paywall（无开放链接）。
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from backend.services.papers.http_client import ROUTE_DIRECT, ROUTE_PROXY, http_get

logger = logging.getLogger(__name__)

PDF_MAGIC = b"%PDF"


def pick_oa_url(record: Any) -> str:
    """从记录里挑最好的开放 URL：links['s2_open_pdf'] 优先，其次 oa_url。"""
    if getattr(record, "links", None):
        s2 = record.links.get("s2_open_pdf")
        if s2:
            return s2
    return getattr(record, "oa_url", "") or ""


def _safe_filename(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._一-鿿-]+", "_", text).strip("_")
    return cleaned[:80] or "paper"


def _try_download(url: str, path: str, *, route: str, timeout: float = 40) -> bool:
    try:
        status, body = http_get(url, route=route, timeout=timeout, retries=2, backoff=3.0)
    except Exception as e:
        logger.debug("下载失败 %s (%s): %s", url, route, type(e).__name__)
        return False
    if status != 200:
        logger.debug("下载非200 %s (%s): %s", url, route, status)
        return False
    if not body.startswith(PDF_MAGIC):
        logger.debug("非 PDF 内容: %s (%s) head=%r", url, route, body[:8])
        return False
    with open(path, "wb") as f:
        f.write(body)
    return True


def download_paper_pdf(record: Any, output_dir: str) -> tuple[str, str]:
    """下载单篇开放 PDF。返回 (状态, 路径或 URL)。

    状态: 'downloaded' | 'oa_link_only' | 'paywall'
    """
    os.makedirs(output_dir, exist_ok=True)
    url = pick_oa_url(record)
    if not url:
        return "paywall", ""

    filename = _safe_filename(record.title or record.doi or "paper") + ".pdf"
    path = os.path.join(output_dir, filename)

    # 已有同名文件则跳过（幂等）
    if os.path.exists(path):
        return "downloaded", path

    # 先直连，失败走 Clash
    if _try_download(url, path, route=ROUTE_DIRECT):
        return "downloaded", path
    if _try_download(url, path, route=ROUTE_PROXY):
        return "downloaded", path
    return "oa_link_only", url


def resolve_oa_pdf_url(doi: str) -> str:
    """DOI → OpenAlex best_oa_location.pdf_url（无开放版返回 ''）。"""
    import urllib.parse
    try:
        import requests
        r = requests.get(
            f"https://api.openalex.org/works/doi:{urllib.parse.quote(doi.strip(), safe='')}?select=best_oa_location,open_access",
            timeout=30,
        )
        if r.status_code != 200:
            return ""
        w = r.json()
        loc = w.get("best_oa_location") or {}
        return loc.get("pdf_url") or loc.get("landing_page_url") or ""
    except Exception:
        return ""


def download_pdf(url: str, target_path: str, *, doi: str = "") -> bool:
    """下载 PDF 到 target_path，校验 %PDF 文件头；直连失败走 Clash。

    只有 URL 走不通时（开放版缺失/被拦），再尝试**订阅库机构通道**（elib 反向代理，
    目前覆盖 IEEE / ACM，需要浏览器过 WAF，见 elib.py）。
    """
    if os.path.exists(target_path):
        return True
    os.makedirs(os.path.dirname(target_path) or ".", exist_ok=True)
    for route in (ROUTE_DIRECT, ROUTE_PROXY):
        if _try_download(url, target_path, route=route):
            return True
    # 机构通道兜底（IEEE/ACM）：需要 DOI 才知道走哪个源
    if doi:
        try:
            from backend.services.papers.elib import download_via_institution
            status, info = download_via_institution(doi, target_path)
            if status == "downloaded":
                logger.info("机构通道下载成功 %s -> %s", doi, info)
                return True
            logger.debug("机构通道未成功 %s: %s", doi, info)
        except Exception as e:
            logger.debug("机构通道异常 %s: %s", doi, e)
    if os.path.exists(target_path):
        os.remove(target_path)
    return False


def download_by_id(arxiv_id: str = "", doi: str = "", target_path: str = "") -> tuple[str, str]:
    """按 arxiv_id 或 DOI 下载单篇 PDF 到 target_path。

    返回 (状态, 详情)：'downloaded' | 'no_url' | 'failed'。
    arxiv 走 arxiv.org 直连；DOI 走 OpenAlex best_oa_location，
    开放版拿不到时回落到**订阅库机构通道**（elib 反向代理，IEEE/ACM 实测可用）。
    """
    if arxiv_id:
        base = _ARXIV_ID_BASE.search(arxiv_id)
        url = f"https://arxiv.org/pdf/{base.group(1) if base else arxiv_id}.pdf"
        return ("downloaded", target_path) if download_pdf(url, target_path) else ("failed", url)
    if doi:
        url = resolve_oa_pdf_url(doi)
        if url:
            if download_pdf(url, target_path, doi=doi):
                return ("downloaded", target_path)
            return ("failed", url)
        # 无开放版：先看是不是订阅库（IEEE/ACM），是则走机构通道
        from backend.services.papers.elib import inst_source_for_doi, download_via_institution

        if inst_source_for_doi(doi):
            status, info = download_via_institution(doi, target_path)
            if status == "downloaded":
                return ("downloaded", info)
            return ("failed", info or "机构通道下载失败")
        return ("no_url", "")
    return ("no_url", "")


_ARXIV_ID_BASE = re.compile(r"(\d{4}\.\d{4,5})")


def download_batch(records: list[Any], output_dir: str, *, verbose: bool = True) -> dict[str, int]:
    """批量下载。返回各状态计数。"""
    counts = {"downloaded": 0, "oa_link_only": 0, "paywall": 0}
    for rec in records:
        status, info = download_paper_pdf(rec, output_dir)
        counts[status] += 1
        if verbose:
            label = "下载" if status == "downloaded" else ("仅链接" if status == "oa_link_only" else "付费墙")
            print(f"  [{label}] {rec.title[:48]}")
            if status == "downloaded":
                print(f"      -> {info}")
    return counts
