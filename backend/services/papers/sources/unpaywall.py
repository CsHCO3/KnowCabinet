"""Unpaywall 按 DOI 找开放副本（免费 API，需邮箱参数）。

注意：Unpaywall 基于 Crossref，只覆盖 Crossref 注册的 DOI。
万方/知网中文论文的 DOI 多为 ISTIC 注册，Unpaywall 通常查不到（422）。
"""

from __future__ import annotations

import logging
from typing import Any

from backend.services.papers.http_client import ROUTE_DIRECT, http_get_json, throttle

logger = logging.getLogger(__name__)

API = "https://api.unpaywall.org/v2"
EMAIL = "research@example.com"  # Unpaywall 要求一个邮箱参数


def find_oa(doi: str) -> dict[str, Any] | None:
    """按 DOI 查开放副本。返回 {is_oa, oa_status, url, url_for_pdf} 或 None。"""
    from backend.services.papers.record import normalize_doi

    nd = normalize_doi(doi)
    if not nd:
        return None
    throttle("unpaywall", 0.5)
    try:
        d = http_get_json(f"{API}/{nd}?email={EMAIL}", route=ROUTE_DIRECT, retries=1)
    except Exception as e:
        logger.debug("Unpaywall %s 失败: %s", nd, e)
        return None
    if not d or not d.get("is_oa"):
        return None
    loc = d.get("best_oa_location") or {}
    return {
        "is_oa": bool(d.get("is_oa")),
        "oa_status": d.get("oa_status", ""),
        "url": loc.get("url", ""),
        "url_for_pdf": loc.get("url_for_pdf", ""),
    }
