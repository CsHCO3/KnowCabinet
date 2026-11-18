"""arXiv 论文多因子排序引擎。

排序因子（权重可调）：
  - 引用量 (ln 归一化)
  - CCF 等级分值
  - 时间衰减（5 年线性归零）
"""
from __future__ import annotations

import math
from typing import Any

CURRENT_YEAR = 2026


def rank_papers(
    papers: list[dict[str, Any]],
    *,
    cit_weight: float = 0.5,
    ccf_weight: float = 0.3,
    time_weight: float = 0.2,
    current_year: int = CURRENT_YEAR,
) -> list[dict[str, Any]]:
    """多因子加权排序。

    Args:
        papers: 每篇论文的 enrich 结果字典（需含 cited_by_count, ccf_level, publication_year）。
        各权重之和不必为 1，内部会做归一化。
        current_year: 计算时效的基准年。

    Returns:
        在每篇论文中注入 rank_score 和 rank_components 后，按 rank_score 降序排列。
    """
    if not papers:
        return []

    max_cit = max((p.get("cited_by_count") or 0) for p in papers)
    max_cit = max(max_cit, 1)

    total_weight = cit_weight + ccf_weight + time_weight
    if total_weight <= 0:
        total_weight = 1.0

    scored: list[dict[str, Any]] = []
    for p in papers:
        cit = p.get("cited_by_count") or 0
        ccf_level = p.get("ccf_level") or ""
        year = p.get("publication_year")

        cit_score = (math.log1p(cit) / math.log1p(max_cit)) * 100 if max_cit > 0 else 0

        ccf_scores = {"A": 100, "B": 60, "C": 20}
        ccf_score = ccf_scores.get(ccf_level, 0)

        age = current_year - (year or current_year)
        time_score = max(0.0, 1.0 - age / 5.0) * 100

        components = {
            "citation": round(cit_score, 1),
            "ccf": ccf_score,
            "time": round(time_score, 1),
        }
        rank_score = (cit_score * cit_weight + ccf_score * ccf_weight + time_score * time_weight) / total_weight
        rank_score = round(rank_score, 1)

        entry = dict(p)
        entry["rank_score"] = rank_score
        entry["rank_components"] = components
        scored.append(entry)

    scored.sort(key=lambda x: -x["rank_score"])

    for i, entry in enumerate(scored):
        entry["rank"] = i + 1

    return scored
