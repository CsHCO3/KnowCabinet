"""无 AI 打分：纯确定性、可复现、可解释。

score = w1*citation_norm + w2*venue_score + w3*recency_score
      + w4*keyword_hit_score + w5*source_trust_score
"""

from __future__ import annotations

import math

DEFAULT_WEIGHTS = {
    "citation": 0.40,
    "venue": 0.30,
    "recency": 0.15,
    "keyword": 0.10,
    "source": 0.05,
}

# 粗粒度 venue 等级分（确定性；不做实时查询）
_VENUE_HINTS = {
    "nature": 1.0, "science": 1.0, "cell": 1.0,
    "acl": 0.9, "emnlp": 0.9, "naacl": 0.9, "neurips": 0.9, "icml": 0.9,
    "iclr": 0.9, "cvpr": 0.9, "iccv": 0.9, "eccv": 0.9,
    "tacl": 0.85, "jmlr": 0.85, "tpami": 0.85,
    "arxiv": 0.5, "ssrn": 0.4, "preprint": 0.4,
}

_SOURCE_TRUST = {
    "openalex": 0.9, "crossref": 0.9, "arxiv": 0.9, "semantic_scholar": 0.85,
    "google_scholar": 0.7, "wanfang": 0.7, "cnki": 0.7,
}


def _norm_citation(citations: int | None, max_citations: int = 1000) -> float:
    if not citations or citations <= 0:
        return 0.0
    return min(1.0, math.log10(1 + citations) / math.log10(1 + max_citations))


def _venue_score(venue: str) -> float:
    v = (venue or "").lower()
    if not v:
        return 0.5
    for key, score in _VENUE_HINTS.items():
        if key in v:
            return score
    return 0.5


def _recency_score(year: int | None, current_year: int | None = None) -> float:
    if not year:
        return 0.5
    current_year = current_year or 2026
    age = max(0, current_year - int(year))
    return max(0.0, min(1.0, 1.0 - age / 20.0))


def _keyword_hit_score(title: str, abstract: str, keywords: list[str]) -> float:
    text = f"{title or ''} {abstract or ''}".lower()
    if not keywords:
        return 0.5
    hits = sum(1 for k in keywords if k and k.lower() in text)
    if not hits:
        return 0.0
    return min(1.0, hits / len(keywords))


def _source_trust_score(sources: list[str] | None) -> float:
    if not sources:
        return 0.6
    return max((_SOURCE_TRUST.get(s.lower(), 0.6) for s in sources), default=0.6)


def score_paper(record: dict, keywords: list[str] | None = None,
                weights: dict | None = None) -> dict:
    """对单篇论文记录打分。record 使用 paper-search 结果常用字段。"""
    w = dict(DEFAULT_WEIGHTS)
    if weights:
        w.update(weights)
    keywords = keywords or []

    citations = record.get("citations") or 0
    venue = record.get("venue") or record.get("venue_type") or ""
    year = record.get("year") or record.get("publication_year")
    title = record.get("title") or ""
    abstract = record.get("abstract") or ""
    sources = record.get("sources") or []
    if isinstance(sources, dict):
        sources = list(sources.keys())

    parts = {
        "citation": _norm_citation(citations),
        "venue": _venue_score(venue),
        "recency": _recency_score(int(year) if str(year).isdigit() else None),
        "keyword": _keyword_hit_score(title, abstract, keywords),
        "source": _source_trust_score(sources),
    }
    score = sum(w[k] * parts[k] for k in parts)
    return {
        "score": round(score, 4),
        "score_breakdown": {k: round(v, 4) for k, v in parts.items()},
        "weights": w,
    }
