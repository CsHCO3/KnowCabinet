"""论文搜索 PaperRecord → infosystem meta_clean 适配层（薄适配，不改两套模型）。

论文搜索的 `run_task.search_all()` 返回 `PaperRecord`（多平台合并、字段已交叉验证）。
本适配层把它转成 meta_clean 的 `sources` dict（作为高信任 'search' 源），
交给清洗引擎补 section/tags/provenance，再入库。
"""

from __future__ import annotations

from backend.services.arxiv.meta_clean import clean_paper

# 论文搜索 PaperRecord → meta_clean sources dict 的字段映射
_FIELDS = ("title", "authors", "year", "doi", "arxiv_id", "eprint_id", "venue", "venue_type",
           "citations", "is_oa", "abstract")


def to_meta_clean_sources(rec) -> dict[str, dict]:
    """论文搜索 PaperRecord → meta_clean sources dict（{"search": {...}}）。

    论文搜索的 record 已多源合并，整体作为高信任 'search' 源。
    """
    s = {}
    for k in _FIELDS:
        v = getattr(rec, k, None)
        if v not in (None, "", [], False):
            s[k] = v
    return {"search": s}


def to_meta_clean(rec, source_type: str = "paper"):
    """论文搜索 PaperRecord → meta_clean.PaperRecord（含 section/tags/provenance）。"""
    return clean_paper(to_meta_clean_sources(rec), source_type=source_type)
