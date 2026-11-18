"""统一论文记录 + 去重合并 + 字段补齐。

探路结论驱动的设计：
- DOI 是主键但非绝对：归一化 DOI（去前缀+小写）优先；无 DOI 时用
  归一化标题 + 首作者姓氏 + 年份 兜底。
- 同一篇论文的 arXiv id 与 DOI 是两套标识 → 通过 arxiv_id / locations 交叉链接。
- 字段补齐：谁发现的论文自带部分元信息，只补还缺的（见 pipeline.enrich）。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any


# ── 归一化 ───────────────────────────────────────

def normalize_doi(doi: str | None) -> str:
    """归一化 DOI：去 https://doi.org/ 前缀、去空白、小写。"""
    if not doi:
        return ""
    d = doi.strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if d.startswith(prefix):
            d = d[len(prefix):]
            break
    return d


def normalize_title(title: str | None) -> str:
    """标题归一化：NFC + 小写 + 去标点空白，用于模糊匹配。"""
    if not title:
        return ""
    t = unicodedata.normalize("NFC", title).lower()
    t = re.sub(r"[^a-z0-9一-鿿]+", "", t)
    return t


def _first_author_last(authors: list[str]) -> str:
    if not authors:
        return ""
    parts = authors[0].strip().lower().split()
    return parts[-1] if parts else ""


# ── 记录模型 ─────────────────────────────────────

@dataclass
class PaperRecord:
    title: str = ""
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    doi: str = ""
    arxiv_id: str = ""
    eprint_id: str = ""            # ePrint 预印本 id（如 "2023/708"），与 arxiv_id 并列
    venue: str = ""
    venue_type: str = ""          # journal / conference / repository / ...
    citations: int = 0
    is_oa: bool = False
    oa_url: str = ""
    abstract: str = ""
    tldr: str = ""                 # Semantic Scholar 一句话摘要
    sources: list[str] = field(default_factory=list)   # 命中来源
    links: dict[str, str] = field(default_factory=dict)  # 官方/PDF/ee 链接
    confidence: float = 0.0        # 发现阶段的匹配置信度 0~100
    rank_score: float = 0.0        # 后续排序注入

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "authors": self.authors,
            "year": self.year,
            "doi": self.doi,
            "arxiv_id": self.arxiv_id,
            "eprint_id": self.eprint_id,
            "venue": self.venue,
            "venue_type": self.venue_type,
            "citations": self.citations,
            "is_oa": self.is_oa,
            "oa_url": self.oa_url,
            "abstract": self.abstract,
            "tldr": self.tldr,
            "sources": self.sources,
            "links": self.links,
            "confidence": self.confidence,
            "rank_score": self.rank_score,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PaperRecord":
        return cls(
            title=d.get("title", ""),
            authors=list(d.get("authors") or []),
            year=d.get("year"),
            doi=d.get("doi", ""),
            arxiv_id=d.get("arxiv_id", ""),
            eprint_id=d.get("eprint_id", ""),
            venue=d.get("venue", ""),
            venue_type=d.get("venue_type", ""),
            citations=int(d.get("citations") or 0),
            is_oa=bool(d.get("is_oa")),
            oa_url=d.get("oa_url", ""),
            abstract=d.get("abstract", ""),
            tldr=d.get("tldr", ""),
            sources=list(d.get("sources") or []),
            links=dict(d.get("links") or {}),
            confidence=float(d.get("confidence") or 0.0),
            rank_score=float(d.get("rank_score") or 0.0),
        )


# ── 去重键 ───────────────────────────────────────

def _dedup_key(rec: PaperRecord) -> tuple[str, ...]:
    """优先 (doi, norm_doi)，否则 (title, norm_title, 首作者姓氏, 年份)。"""
    if rec.doi:
        return ("doi", normalize_doi(rec.doi))
    return (
        "title",
        normalize_title(rec.title),
        _first_author_last(rec.authors),
        str(rec.year or ""),
    )


# ── 合并 ─────────────────────────────────────────

def _pick_existing(current: Any, incoming: Any, *, prefer_incoming: bool = False) -> Any:
    """保留已有值（非空），否则取新值；若 prefer_incoming 且两者都非空则取新值。"""
    if prefer_incoming:
        return incoming if incoming not in (None, "", []) else current
    return current if current not in (None, "", []) else incoming


def merge_into(target: PaperRecord, src: PaperRecord) -> None:
    """把 src 的缺失字段补进 target，双向保留更多信息。"""
    t, s = target, src
    t.title = _pick_existing(t.title, s.title)
    if not t.authors:
        t.authors = list(s.authors)
    t.year = t.year if t.year is not None else s.year
    t.doi = _pick_existing(t.doi, s.doi)
    t.arxiv_id = _pick_existing(t.arxiv_id, s.arxiv_id)
    t.venue = _pick_existing(t.venue, s.venue)
    t.venue_type = _pick_existing(t.venue_type, s.venue_type)
    t.citations = max(t.citations, s.citations)
    t.is_oa = t.is_oa or s.is_oa
    t.oa_url = _pick_existing(t.oa_url, s.oa_url)
    t.abstract = _pick_existing(t.abstract, s.abstract)
    t.tldr = _pick_existing(t.tldr, s.tldr)
    for src_name in s.sources:
        if src_name not in t.sources:
            t.sources.append(src_name)
    for k, v in s.links.items():
        t.links.setdefault(k, v)
    t.confidence = max(t.confidence, s.confidence)


def merge_records(records: list[PaperRecord]) -> list[PaperRecord]:
    """两级去重合并：
    1) 有 DOI 的按归一化 DOI 分组；
    2) 无 DOI 的按 (标题, 首作者, 年份) 匹配进已有组或自成一组的兜底。
    返回合并后的记录列表（组内 merge_into）。
    """
    groups: list[PaperRecord] = []

    def find_group(rec: PaperRecord) -> PaperRecord | None:
        if rec.doi:
            nd = normalize_doi(rec.doi)
            for g in groups:
                if g.doi and normalize_doi(g.doi) == nd:
                    return g
        key = _dedup_key(rec)
        if key[0] == "title":
            for g in groups:
                if g.doi or g.arxiv_id:  # 已有强标识的组不与无标识记录强行合并
                    continue
                if _dedup_key(g) == key:
                    return g
        return None

    for rec in records:
        g = find_group(rec)
        if g is None:
            groups.append(rec)
        else:
            merge_into(g, rec)
    return groups
