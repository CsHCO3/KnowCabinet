"""目录残渣块识别（M6 复核遗留 → M8-3 实现）。

**问题**：PDF→md 转换会留下"导航/目录块"，被正常切块后混进 L3，成为检索命中的证据块。
M6 现场观察：这类块中文字符占比约 0.51，正文约 0.90——但"中文占比"**不能当全局阈值**：
本库 1498 篇英文论文正文的中文占比本来就是 0.00，绝对阈值会把整个英文库判成目录
（`temp/m8_toc_profile.py` 实测 p50=0.0）。

**判据（靠数据定，见 `temp/m8_toc_heading.py` 全库 23,403 个 L3 块的形态扫描）**：

  A. 点线目录（高特异，主判据）：≥3 行且 ≥50% 的行是"标题 + 点线 + 页码"，
     如 `3.2.3 公约一般条款 .......... 20`。实测这类块 cjk 0.05–0.13、点线率 0.83–1.00。
  B. 无点线目录（兜底）：≥5 行，且"编号行 / 短行 / 行尾页码"三项同时 ≥80%。

  行数是唯一的长度护栏（A≥3 行、B≥5 行）：单行块、标题块不会命中。
  刻意**不设字符数阈值**——结构本身已经是双峰证据（`temp/m8_toc_sensitivity.py`：
  点线率阈值从 0.15 调到 0.5，全库判出的块数只在 26–27 之间，说明真实分布是两簇）。

**判据的边界（如实登记）**：判据只认"目录体"，不认"低信息量噪声"（页眉页脚、参考文献
占位等不在本判据范围内）；它对英文论文附录的编号列表（如 `3. *Capability Revocation*: ...`）
**不会**误判——那些块短行率与行尾页码率都是 0（实测见上脚本输出）。

用法（检索层）：`_vector_recall` 在 chunk→doc 折叠前调用 `partition_toc()`，
目录块不参与打分、不作为证据块返回；**每篇文档至少保留 1 块**，避免整篇被打死。
"""
from __future__ import annotations

import re

CJK = re.compile(r"[\u4e00-\u9fff]")
# 点线 + 行尾页码：标题 ........ 12
DOTTED_LEADER = re.compile(r"[.·…]{3,}\s*\d{1,4}\s*$")
# 编号行：1 / 1.2 / 1.2.3 / 第3章 / 三、
NUMBERED_HEADING = re.compile(
    r"^\s*(?:\d+(?:\.\d+)*"
    r"|第\s*(?:\d+|[一二三四五六七八九十百]+)\s*[章节]"
    r"|[一二三四五六七八九十]+、)\s*\S")
LINE_TAIL_PAGE = re.compile(r"\d{1,4}\s*$")

MIN_CHARS = 0  # 不设字符数阈值（理由见模块 docstring）；保留常量仅为报告口径对齐
DOTTED_RATIO_MIN = 0.5
DOTTED_LINES_MIN = 3
STRUCT_LINES_MIN = 5
STRUCT_RATIO_MIN = 0.8


def _lines(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def toc_score(text: str) -> dict:
    """返回判据明细，便于日志/报告解释"为什么判成目录"。"""
    lines = _lines(text)
    chars = len((text or "").strip())
    info = {
        "chars": chars, "lines": len(lines), "dotted_ratio": 0.0,
        "heading_ratio": 0.0, "short_ratio": 0.0, "numtail_ratio": 0.0,
        "rule": None, "is_toc": False,
    }
    if not lines:
        return info
    total = len(lines)
    info["dotted_ratio"] = round(
        sum(1 for line in lines if DOTTED_LEADER.search(line)) / total, 3)
    info["heading_ratio"] = round(
        sum(1 for line in lines if NUMBERED_HEADING.match(line)) / total, 3)
    info["short_ratio"] = round(sum(1 for line in lines if len(line) <= 25) / total, 3)
    info["numtail_ratio"] = round(
        sum(1 for line in lines if LINE_TAIL_PAGE.search(line)) / total, 3)
    if chars < MIN_CHARS:
        return info
    if info["dotted_ratio"] >= DOTTED_RATIO_MIN and total >= DOTTED_LINES_MIN:
        info["rule"] = "dotted_leader"
    elif (total >= STRUCT_LINES_MIN
          and info["heading_ratio"] >= STRUCT_RATIO_MIN
          and info["short_ratio"] >= STRUCT_RATIO_MIN
          and info["numtail_ratio"] >= STRUCT_RATIO_MIN):
        info["rule"] = "numbered_short_lines"
    info["is_toc"] = info["rule"] is not None
    return info


def is_toc(text: str) -> bool:
    return bool(toc_score(text)["is_toc"])


def partition_toc(chunks: list[dict]) -> tuple[list[dict], list[dict]]:
    """把同文档的候选块分成 (保留, 目录块)；**至少保留 1 块**。

    `chunks` 需已按检索分降序（调用方 `_vector_recall` 已排好）；保留块维持原顺序。
    若全部被判为目录（极端情况：整篇就是一份目录），原样返回，不打死文档。
    """
    if not chunks:
        return [], []
    flagged = [chunk for chunk in chunks if is_toc(chunk.get("text") or "")]
    if not flagged or len(flagged) == len(chunks):
        return list(chunks), []
    flagged_ids = {id(chunk) for chunk in flagged}
    kept = [chunk for chunk in chunks if id(chunk) not in flagged_ids]
    return kept, flagged
