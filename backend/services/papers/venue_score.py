"""期刊/venue 评分：CCF 等级 + OpenAlex 期刊指标 + 中文核心标志。

探路结论：
- CCF 是本地专家表（venue 关键词 → A/B/C），不联网。
- OpenAlex /sources 免费提供期刊指标（2yr_mean_citedness≈影响因子类、
  h_index、i10_index、works_count），可批量预取后缓存本地。
- 中文核心（北大核心/CSCD/科技核心）名单以一次性维护进 _CN_CORE 表为准。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from backend.services.papers.http_client import ROUTE_DIRECT, http_get_json, throttle

CCF_SCORES = {"A": 100, "B": 60, "C": 20}

# ── CCF 关键词表（数据来自 infosystem backend/services/arxiv/ccf.py）──

CCF_A_KEYWORDS: set[str] = {
    "neurips", "nips", "icml", "cvpr", "iccv", "eccv", "aaai", "ijcai",
    "acl", "emnlp", "naacl", "iclr", "acm mm", "acm multimedia", "sigir",
    "www", "wsdm", "kdd", "icdm", "sigcomm", "mobicom", "osdi", "sosp",
    "eurosys", "sigmod", "vldb", "icde", "pods", "icse", "fse", "ase",
    "sp", "oakland", "ccs", "ndss", "usenix security", "tpami", "ijcv",
    "jmlr", "ai journal", "artificial intelligence", "tip", "tifs",
    "tvcg", "tkde", "tods", "tois",
    # 常见全称（OpenAlex/Crossref 返回全称）
    "neural information processing systems", "international conference on machine learning",
    "computer vision and pattern recognition", "international conference on computer vision",
    "european conference on computer vision", "empirical methods in natural language processing",
    "international conference on learning representations", "advances in neural information",
    "association for the advancement of artificial intelligence",
    "international joint conference on artificial intelligence",
}
CCF_B_KEYWORDS: set[str] = {
    "coling", "icassp", "icme", "miccai", "aistats", "ecml", "ecai", "uai",
    "icra", "iros", "corl", "ismb", "eccb", "tomccap", "tist", "taffc",
    "tmm", "tosem", "jss", "cviu", "pr letters", "prl", "neural networks",
    "tcss", "tsc",
}
CCF_C_KEYWORDS: set[str] = {
    "iconip", "icpr", "icb", "bibe", "aime", "amia", "cvmi", "grl",
    "neural computation", "ivc", "image and vision computing", "spl",
    "signal processing letters",
}

_CCF_ITEMS: list[tuple[str, set[str]]] = [
    ("A", CCF_A_KEYWORDS), ("B", CCF_B_KEYWORDS), ("C", CCF_C_KEYWORDS),
]


def get_ccf_level(venue: str) -> str:
    """返回 venue 的 CCF 等级（A/B/C），无匹配返回 ''。A>B>C 优先。"""
    if not venue or not venue.strip():
        return ""
    name = venue.lower().strip()
    for level, keywords in _CCF_ITEMS:
        for kw in keywords:
            if kw in name:
                return level
    return ""


def get_ccf_score(venue: str) -> int:
    return CCF_SCORES.get(get_ccf_level(venue), 0)


# ── 中文核心标志（人工维护一次）───────────────────
# 结构：{venue小写: "北大核心"|"CSCD"|"科技核心"|"CSSCI"|"中科院1区"|...}
_CN_CORE: dict[str, str] = {
    # 示例占位，真实名单需人工补充
}


def get_cn_core(venue: str) -> str:
    if not venue:
        return ""
    return _CN_CORE.get(venue.strip().lower(), "")


# ── OpenAlex 期刊指标（免费 API，预取后缓存本地）──

_CACHE_FILE = Path(__file__).parent / "venue_cache.json"
_OPENALEX_CACHE: dict[str, dict[str, Any]] = {}


def _load_cache() -> None:
    global _OPENALEX_CACHE
    if _OPENALEX_CACHE:
        return
    if _CACHE_FILE.exists():
        try:
            _OPENALEX_CACHE = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            _OPENALEX_CACHE = {}


def _save_cache() -> None:
    try:
        _CACHE_FILE.write_text(
            json.dumps(_OPENALEX_CACHE, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


def fetch_openalex_metrics(venue: str, *, cache: bool = True) -> dict[str, Any]:
    """按期刊名查 OpenAlex /sources，返回 {2yr_mean_citedness, h_index, i10_index, works_count}。"""
    if not venue or not venue.strip():
        return {}
    _load_cache()
    key = venue.strip().lower()
    if cache and key in _OPENALEX_CACHE:
        return dict(_OPENALEX_CACHE[key])

    url = (
        "https://api.openalex.org/sources"
        f"?search={urllib_quote(venue)}&per-page=1&select=id,display_name,summary_stats,works_count,cited_by_count,is_oa"
    )
    throttle("openalex", 0.1)
    try:
        data = http_get_json(url, route=ROUTE_DIRECT, mailto="")
    except Exception:
        return {}
    results = data.get("results") or []
    if not results:
        return {}
    best = results[0]
    ss = best.get("summary_stats") or {}
    metrics = {
        "2yr_mean_citedness": ss.get("2yr_mean_citedness"),
        "h_index": ss.get("h_index"),
        "i10_index": ss.get("i10_index"),
        "works_count": best.get("works_count"),
        "openalex_source_id": best.get("id"),
        "display_name": best.get("display_name"),
    }
    if cache:
        _OPENALEX_CACHE[key] = metrics
        _save_cache()
    return metrics


def urllib_quote(text: str) -> str:
    from urllib.parse import quote
    return quote(text)


# ── 综合评分 ─────────────────────────────────────

def score_venue(venue: str, *, metrics: dict[str, Any] | None = None) -> dict[str, Any]:
    """返回 venue 评分结构：ccf_rank/ccf_score/cn_core/openalex_metrics/综合分。

    综合分 = ccf(0-100) 为主，openalex 2yr_mean_citedness 归一化加成，中文核心加分。
    权重可后续调。
    """
    ccf_rank = get_ccf_level(venue)
    ccf_score = get_ccf_score(venue)
    cn_core = get_cn_core(venue)
    metrics = metrics or fetch_openalex_metrics(venue)

    oa_part = 0.0
    if metrics and isinstance(metrics.get("2yr_mean_citedness"), (int, float)):
        # 2yr_mean_citedness 常见 0~20，映射到 0~40 分
        oa_part = min(40.0, float(metrics["2yr_mean_citedness"]) * 2.0)
    cn_part = 30.0 if cn_core else 0.0

    total = round(min(100.0, ccf_score * 0.6 + oa_part + cn_part), 1)
    return {
        "ccf_rank": ccf_rank,
        "ccf_score": ccf_score,
        "cn_core": cn_core,
        "openalex_metrics": metrics,
        "score": total,
    }
