"""确定性元数据清洗引擎 — 满足三条性质：幂等 / 单调 / 可审计。

模型对齐 `论文搜索/record.py` 的 PaperRecord（填空 + max 合并 + 来源合并），扩展：
- 字段级来源信任（T1 权威/已验证  >  T2 聚合/派生  >  T3 尽力推断）
- provenance（每字段记录来源）+ manual_fields（用户人工保留，永不自动覆盖）
- section：固定粗分类表 + 前缀兜底 + "未分类"桶（任何输入都有去处，不抛异常）
- tags：三路合并（arXiv 分类 ∪ OpenAlex topics ∪ 关键词行）+ 确定性（排序/去重/归一）

清洗 = 确定性转换函数：
    (原始源 dict + 已有 metadata + manual_fields)  →  PaperRecord（含 provenance/conflicts）

验收：
- 幂等：clean 两次 → 零 diff（测试保证）
- 单调：只"填空 + 低T→高T 升级"，绝不"高→低 覆盖"
- 可审计：每个字段有 provenance；冲突记录在 conflicts
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


# ═══════════════════════════════════════════════════════
# 1. PaperRecord（对齐 论文搜索/record.py，+provenance/manual）
# ═══════════════════════════════════════════════════════

@dataclass
class PaperRecord:
    title: str = ""
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    doi: str = ""
    arxiv_id: str = ""
    eprint_id: str = ""            # ePrint 预印本 id（如 "2023/708"）
    venue: str = ""
    venue_type: str = ""
    citations: int = 0
    is_oa: bool = False
    abstract: str = ""
    primary_category: str = ""
    section: str = ""
    tags: list[str] = field(default_factory=list)
    source_type: str = "paper"
    # 清洗信息
    provenance: dict[str, str] = field(default_factory=dict)   # {字段: 来源}
    manual_fields: list[str] = field(default_factory=list)      # 用户改过的字段
    conflicts: list[str] = field(default_factory=list)          # 同信任冲突日志

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title, "authors": self.authors, "year": self.year,
            "doi": self.doi, "arxiv_id": self.arxiv_id, "eprint_id": self.eprint_id,
            "venue": self.venue, "venue_type": self.venue_type,
            "citations": self.citations, "is_oa": self.is_oa, "abstract": self.abstract,
            "primary_category": self.primary_category,
            "section": self.section, "tags": self.tags, "source_type": self.source_type,
            "provenance": self.provenance, "manual_fields": self.manual_fields,
            "conflicts": self.conflicts,
        }

    def frontmatter_entries(self) -> dict[str, Any]:
        """8 字段 frontmatter（非空值）。"""
        out: dict[str, Any] = {}
        if self.title:
            out["title"] = self.title
        if self.doi:
            out["source"] = f"https://doi.org/{self.doi}"
        elif self.arxiv_id:
            out["source"] = f"https://arxiv.org/abs/{self.arxiv_id}"
        elif self.eprint_id:
            out["source"] = f"https://eprint.iacr.org/{self.eprint_id}"
        if self.authors:
            out["authors"] = ", ".join(self.authors)
        if self.year is not None:
            out["year"] = str(self.year)
        if self.section:
            out["section"] = self.section
        if self.tags:
            out["tags"] = "[%s]" % ", ".join(self.tags)
        out["source_type"] = self.source_type
        return out


# ═══════════════════════════════════════════════════════
# 2. section：固定粗分类表 + 前缀兜底 + 未分类桶
# ═══════════════════════════════════════════════════════

# arXiv primary_category → 中文主题（约 50 条，一次人工维护，可版本化增长）
ARXIV_SECTION: dict[str, str] = {
    "cs.AI": "人工智能", "cs.AR": "计算机体系结构", "cs.CC": "计算复杂性",
    "cs.CE": "计算工程", "cs.CG": "计算几何", "cs.CL": "计算语言学",
    "cs.CR": "计算机安全", "cs.CV": "计算机视觉", "cs.CY": "计算机与社会",
    "cs.DB": "数据库", "cs.DC": "分布式计算", "cs.DL": "数字图书馆",
    "cs.DM": "离散数学", "cs.DS": "数据结构与算法", "cs.ET": "新兴技术",
    "cs.FL": "形式语言与自动机", "cs.GL": "计算机科学总论", "cs.GR": "计算机图形学",
    "cs.GT": "计算博弈论", "cs.HC": "人机交互", "cs.IR": "信息检索",
    "cs.IT": "信息论", "cs.LG": "机器学习", "cs.LO": "逻辑计算",
    "cs.MA": "多智能体系统", "cs.MM": "多媒体", "cs.MS": "数学软件",
    "cs.NA": "数值分析", "cs.NE": "神经与进化计算", "cs.NI": "计算机网络",
    "cs.OH": "计算机其他", "cs.OS": "操作系统", "cs.PF": "性能分析",
    "cs.PL": "编程语言", "cs.RO": "机器人", "cs.SC": "符号计算",
    "cs.SD": "声音处理", "cs.SE": "软件工程", "cs.SI": "社会信息学",
    "cs.SY": "系统与控制",
    # 非 CS 大类（前缀兜底覆盖子类）
    "math": "数学", "stat": "统计学", "eess": "电气与系统工程",
    "q-bio": "定量生物学", "q-fin": "量化金融", "econ": "经济学",
    "physics": "物理学", "cond-mat": "凝聚态物理", "astro-ph": "天体物理",
    "hep-th": "高能物理", "hep-ph": "高能物理", "hep-ex": "高能物理",
    "nucl-th": "核物理", "nucl-ex": "核物理", "quant-ph": "量子物理",
    "gr-qc": "引力与相对论",
}

# 前缀兜底：cs.* 未命中细分 → 顶级大类
_ARXIV_PREFIX: list[tuple[str, str]] = [
    ("cs.", "计算机科学"), ("math.", "数学"), ("stat.", "统计学"),
    ("eess.", "电气与系统工程"), ("q-bio.", "定量生物学"), ("q-fin.", "量化金融"),
    ("econ.", "经济学"), ("physics.", "物理学"), ("cond-mat.", "凝聚态物理"),
    ("hep-", "高能物理"), ("nucl-", "核物理"),
]

# OpenAlex field/subfield（英文）→ 中文主题（小映射，用于无 arXiv 的论文）
OPENALEX_SECTION: dict[str, str] = {
    "computer science": "计算机科学", "machine learning": "机器学习",
    "artificial intelligence": "人工智能", "computer networks": "计算机网络",
    "distributed computing": "分布式计算", "computer security": "计算机安全",
    "database": "数据库", "information retrieval": "信息检索",
    "computer vision": "计算机视觉", "software engineering": "软件工程",
    "programming language": "编程语言", "computer architecture": "计算机体系结构",
    "human-computer interaction": "人机交互", "cryptography": "密码学",
    "blockchain": "区块链", "mathematics": "数学", "statistics": "统计学",
    "physics": "物理学", "economics": "经济学",
}

_UNCLASSIFIED = "未分类"


def _clean_section_from_arxiv(category: str) -> str:
    """arXiv primary_category → 中文主题。细分命中 > 前缀兜底 > 未分类。"""
    cat = (category or "").strip()
    if not cat:
        return _UNCLASSIFIED
    if cat in ARXIV_SECTION:
        return ARXIV_SECTION[cat]
    for prefix, topic in _ARXIV_PREFIX:
        if cat.startswith(prefix):
            return topic
    return _UNCLASSIFIED


def _clean_section_from_openalex(topics: list[str]) -> str:
    """OpenAlex topics（domain/field/subfield/topic 英文）→ 中文主题。

    倒序取最具体的已知映射；都不命中 → 返回英文 field（不瞎造中文）。
    """
    if not topics:
        return _UNCLASSIFIED
    for t in reversed(topics):  # 越靠后越具体
        low = t.lower().strip()
        if low in OPENALEX_SECTION:
            return OPENALEX_SECTION[low]
    # 都不命中 → 取 field（第 2 级）英文原文
    return topics[1] if len(topics) >= 2 else (topics[0] or _UNCLASSIFIED)


def clean_section(primary_category: str = "", openalex_topics: list[str] | None = None) -> str:
    """section 汇总：arXiv 优先，OpenAlex 兜底，最后未分类桶。确定性。"""
    if primary_category:
        s = _clean_section_from_arxiv(primary_category)
        if s != _UNCLASSIFIED:
            return s
    if openalex_topics:
        return _clean_section_from_openalex(openalex_topics)
    return _UNCLASSIFIED


# ═══════════════════════════════════════════════════════
# 3. tags：三路合并 + 归一化 + 确定性
# ═══════════════════════════════════════════════════════

_KEYWORD_RE = re.compile(r"关键词\s*[:：]\s*(.+)")
_SPLIT_RE = re.compile(r"[，,;；、/|\s]+")

# 英文停用词（abstract 关键词提取用）
_STOPWORDS = set(
    "the a an and or of to in for on with is are was were be been being this that these those "
    "it its as at by from can could would should may might also such their there which not but "
    "has have had do does did shall will while over under between after before during through "
    "into out up down off about against etc e.g i.e use using based on via new paper study our "
    "we they he she you".split()
)


def _extract_abstract_keywords(text: str | None, n: int = 8) -> list[str]:
    """从摘要/正文提取高频关键词（英/中文，去停用词，频率+词长排序）。"""
    if not text:
        return []
    words = re.findall(r"[A-Za-z][A-Za-z0-9\-]{2,}|[一-鿿]{2,}", text.lower())
    cnt: dict[str, int] = {}
    for w in words:
        if w not in _STOPWORDS:
            cnt[w] = cnt.get(w, 0) + 1
    items = sorted(cnt.items(), key=lambda x: (-x[1], -len(x[0])))[:n]
    return [w for w, _ in items]


def parse_keywords(text: str | None) -> list[str]:
    """容错解析"关键词:"行（有无/中英冒号、分隔符集）。找不到返回 []。"""
    if not text:
        return []
    for line in text.splitlines():
        m = _KEYWORD_RE.search(line)
        if m:
            parts = _SPLIT_RE.split(m.group(1))
            return [p.strip().strip("*`").strip() for p in parts if p.strip()]
    return []


def clean_tags(primary_category: str = "", openalex_keywords: list[str] | None = None,
               keyword_text: str | None = None,
               manual_tags: list[str] | None = None) -> list[str]:
    """tags = 人工(优先,不变) ∪ arXiv分类 ∪ OpenAlex topics ∪ 关键词行。确定性排序去重。"""
    auto: list[str] = []
    if primary_category:
        auto.append(primary_category)
    for k in (openalex_keywords or [])[:6]:
        k = k.strip()
        if k and k not in auto:
            auto.append(k)
    parsed = parse_keywords(keyword_text)
    if not parsed and keyword_text:  # 无"关键词:"行 → 从摘要提取高频词兜底
        parsed = _extract_abstract_keywords(keyword_text)
    for k in parsed:
        if k not in auto:
            auto.append(k)
    auto = sorted(dict.fromkeys(auto))
    manual = list(dict.fromkeys(t.strip() for t in (manual_tags or []) if t.strip()))
    return manual + auto  # 人工在前，不被自动覆盖


# ═══════════════════════════════════════════════════════
# 4. 字段级来源信任 + 确定性合并
# ═══════════════════════════════════════════════════════

# 信任级：1=权威/已验证，2=聚合/派生，3=尽力推断
SOURCE_TRUST: dict[str, int] = {
    "search": 1,       # 多平台检索合并结果（论文搜索系统，字段已多源交叉验证）
    "arxiv": 1,        # arXiv API（作者/年份/分类/doi）
    "doi": 1,          # Crossref/DOI 记录（OpenAlex works?doi）
    "file": 1,         # 用户手写 frontmatter（manual 时最高）
    "openalex": 2,     # OpenAlex 按标题丰富（引用/venue/is_oa）
    "surveys": 2,      # surveys JSON 丰富字段
    "tasks": 3,        # tasks.json 原始（精简、兜底）
    "infer": 3,        # 从文件名/正文推断
}

# 每字段候选来源（按信任降序；同信任按此顺序 = 优先）
FIELD_SOURCES: dict[str, list[str]] = {
    "title": ["search", "arxiv", "doi", "file", "surveys", "tasks", "infer"],
    "authors": ["search", "arxiv", "doi", "file", "surveys", "tasks"],
    "year": ["search", "arxiv", "doi", "file", "openalex", "surveys", "tasks"],
    "doi": ["search", "arxiv", "doi", "file", "openalex"],
    "arxiv_id": ["search", "arxiv", "doi", "tasks"],
    "eprint_id": ["search"],
    "venue": ["search", "doi", "file", "openalex", "surveys"],
    "venue_type": ["search", "openalex", "surveys"],
    "citations": ["search", "doi", "openalex", "surveys", "tasks"],
    "ccf_rank": ["openalex", "surveys"],
    "is_oa": ["search", "openalex", "surveys"],
    "abstract": ["search", "arxiv", "doi", "openalex", "surveys", "tasks"],
    "primary_category": ["arxiv", "tasks"],
}


def _as_int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def merge_metadata(sources: dict[str, dict[str, Any]],
                   existing: dict[str, Any] | None = None,
                   manual_fields: list[str] | None = None) -> tuple[PaperRecord, list[str]]:
    """核心确定性合并（单调）。

    Args:
        sources: {来源名: {字段: 值}}，来源名 ∈ SOURCE_TRUST（如 {"arxiv": {...}, "openalex": {...}}）
        existing: 已有 metadata（文件 frontmatter 读出 / 上次清洗结果）
        manual_fields: 用户人工改过的字段（永不自动覆盖）

    Returns:
        (PaperRecord, conflicts)
    """
    manual = list(dict.fromkeys(manual_fields or []))
    prev = existing or {}
    conflicts: list[str] = []

    rec = PaperRecord()
    rec.manual_fields = manual

    for fname in ("title", "authors", "year", "doi", "arxiv_id", "eprint_id", "venue", "venue_type",
                  "citations", "ccf_rank", "is_oa", "abstract", "primary_category"):
        order = FIELD_SOURCES.get(fname, [])
        # 已有值（可能是人工写的）——file 来源在 order 里视为用户已有
        cur_val = prev.get(fname)
        cur_src = prev.get(f"_{fname}_src") or ("file" if cur_val is not None else None)
        cur_trust = SOURCE_TRUST.get(cur_src, 0)

        # 人工字段：永不覆盖
        if fname in manual and cur_val not in (None, "", [], False):
            rec.__setattr__(fname, cur_val)
            rec.provenance[fname] = f"manual:{cur_src}"
            continue

        # 收集候选（按 order 的信任降序）
        best_val, best_src, best_trust = None, None, 0
        for src in order:
            val = sources.get(src, {}).get(fname)
            if val in (None, "", [], False):
                continue
            trust = SOURCE_TRUST[src]
            # 首次有值
            if best_val is None:
                best_val, best_src, best_trust = val, src, trust
            elif trust > best_trust:  # 更高信任 → 升级
                if best_val not in (None, "", [], False):
                    conflicts.append(f"{fname}: {best_src}(T{best_trust}) 被 {src}(T{trust}) 覆盖")
                best_val, best_src, best_trust = val, src, trust
            elif trust == best_trust and src != best_src:
                conflicts.append(f"{fname}: {src}(T{trust}) 与 {best_src}(T{trust}) 冲突，保留 {best_src}")

        # 与已有值合并（单调：已有更高/相同信任则保留已有）
        if cur_val not in (None, "", [], False) and best_val is not None:
            if cur_trust >= best_trust:
                best_val, best_src, best_trust = cur_val, cur_src, cur_trust
            else:
                conflicts.append(f"{fname}: 已有 {cur_src}(T{cur_trust}) 被 {best_src}(T{best_trust}) 升级")

        if best_val is None:
            best_val = cur_val
            best_src = cur_src

        if best_val not in (None, "", [], False):
            # 类型归一
            if fname == "year":
                best_val = _as_int(best_val) or None
            elif fname == "citations":
                best_val = _as_int(best_val)
            elif fname == "is_oa":
                best_val = bool(best_val)
            elif fname == "authors":
                best_val = [a.strip() for a in best_val if a.strip()] if isinstance(best_val, list) \
                    else [a.strip() for a in str(best_val).split(",") if a.strip()]
            rec.__setattr__(fname, best_val)
            if best_src:
                rec.provenance[fname] = best_src

    return rec, conflicts


def clean_paper(sources: dict[str, dict[str, Any]],
                existing: dict[str, Any] | None = None,
                manual_fields: list[str] | None = None,
                source_type: str = "paper") -> PaperRecord:
    """对外主入口：合并字段 → 派生 section/tags → 完整记录。"""
    rec, conflicts = merge_metadata(sources, existing, manual_fields)
    rec.source_type = source_type
    rec.conflicts = conflicts
    # 派生 section/tags
    rec.section = clean_section(
        rec.primary_category or "",
        (sources.get("openalex", {}).get("topics") or
         sources.get("doi", {}).get("topics") or []),
    )
    # 人工 tags：仅当 "tags" 在 manual_fields 里，才把已有 tags 视为人工保留
    existing_tags = (existing or {}).get("tags") or []
    manual_tags = existing_tags if "tags" in set(manual_fields or []) else []
    # 关键词来源：无"关键词:"行时用摘要提取（abstract 或 title 兜底）
    keyword_text = sources.get("infer", {}).get("keywords") or ""
    if not keyword_text:
        keyword_text = rec.abstract or rec.title or ""
    rec.tags = clean_tags(
        primary_category=rec.primary_category or "",
        openalex_keywords=(
            sources.get("openalex", {}).get("topics") or
            sources.get("doi", {}).get("topics") or []
        ),
        keyword_text=keyword_text,
        manual_tags=manual_tags,
    )
    rec.provenance["section"] = "derive:arxiv" if rec.primary_category else "derive:openalex"
    rec.provenance["tags"] = "derive:merge"
    return rec
