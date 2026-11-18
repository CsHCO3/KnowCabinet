"""CCF 推荐会议/期刊分级查找表。

提供基于 venue 名称关键词的 CCF A/B/C 三级匹配。
"""
from __future__ import annotations

# ── CCF A 类 ────────────────────────────────────
CCF_A_KEYWORDS: set[str] = {
    # 人工智能 / 模式识别
    "neurips", "nips", "icml", "cvpr", "iccv", "eccv",
    "aaai", "ijcai", "acl", "emnlp", "naacl",
    "iclr",
    # 计算机视觉 / 多媒体
    "acm mm", "acm multimedia",
    # 信息检索
    "sigir", "www", "wsdm",
    # 数据挖掘
    "kdd", "icdm",
    # 计算机网络
    "sigcomm", "mobicom",
    # 操作系统 / 系统
    "osdi", "sosp", "eurosys",
    # 数据库
    "sigmod", "vldb", "icde", "pods",
    # 软件工程
    "icse", "fse", "ase",
    # 安全
    "sp", "oakland", "ccs", "ndss", "usenix security",
    # 期刊
    "tpami", "ijcv", "jmlr",
    "ai journal", "artificial intelligence",
    "tip", "tifs", "tvcg",
    "tkde", "tods", "tois",
}

# ── CCF B 类 ────────────────────────────────────
CCF_B_KEYWORDS: set[str] = {
    "coling", "icassp", "icme", "miccai",
    "aistats", "ecml", "ecai", "uai",
    "icra", "iros", "corl",
    "ismb", "eccb",
    "tomccap", "tist", "taffc",
    "tmm", "tosem", "jss",
    "cviu", "pr letters", "prl",
    "neural networks",
    "tcss", "tsc",
}

# ── CCF C 类 ────────────────────────────────────
CCF_C_KEYWORDS: set[str] = {
    "iconip", "icpr", "icb", "bibe",
    "aime", "amia", "cvmi",
    "grl",
    "neural computation",
    "ivc", "image and vision computing",
    "spl", "signal processing letters",
}

_CCF_ITEMS: list[tuple[str, set[str]]] = [
    ("A", CCF_A_KEYWORDS),
    ("B", CCF_B_KEYWORDS),
    ("C", CCF_C_KEYWORDS),
]


def get_ccf_level(venue: str) -> str:
    """返回 venue 对应的 CCF 等级 ('A', 'B', 'C')，不匹配返回 ''。

    匹配规则：venue 名称转小写，逐级检查是否包含任一关键词。
    优先级 A > B > C，命中即返回。
    """
    if not venue or not venue.strip():
        return ""
    name = venue.lower().strip()
    for level, keywords in _CCF_ITEMS:
        for kw in keywords:
            if kw in name:
                return level
    return ""


def get_ccf_score(venue: str) -> int:
    """返回 CCF 等级对应的分值（A=100, B=60, C=20, 无=0）。"""
    return {"A": 100, "B": 60, "C": 20}.get(get_ccf_level(venue), 0)
