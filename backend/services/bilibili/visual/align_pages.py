"""
页面↔字幕字符对齐器（抄自 SlideSpeech cal_recall_demo/compute_wer_details.py，
ICASSP 2024，字符级 CJK 对齐 + numpy 行进 DP 加速）。
====================================================================
原版输出 match/sub/ins/del 四类计数；本项目用法：
  ref = 页面时间段内的字幕文本
  hyp = 该页终帧（裁掉字幕带后）的 OCR 文本
  - hyp 的 insertion = 页面有、字幕没有的字符 = 页面增量（架构节点名/模型名/数字）
  - increment = inserted / len(hyp_chars)   （页面增量占比）
  - matched  = 页面内容与字幕重合（放大卡片型 → 高重合）
零部署、纯文本。
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass

import numpy as np

# 抄自原版：中文标点表（对齐时忽略）
PUNCTS = set("!,:;?!、。！，；？：「」︰『』《》（）()[]【】·…—～~-\"'")


def characterize(s: str) -> list[str]:
    """抄自原版 characterize：CJK 逐字，英文按词，标点丢弃。"""
    res: list[str] = []
    buf = ""
    for ch in s:
        if ch in PUNCTS or ch.isspace():
            if buf:
                res.append(buf)
                buf = ""
            continue
        cat = unicodedata.category(ch)
        if cat.startswith("Lo"):  # CJK 表意文字
            if buf:
                res.append(buf)
                buf = ""
            res.append(ch)
        else:
            buf += ch
    if buf:
        res.append(buf)
    return res


def align(hyp: list[str], ref: list[str]) -> dict:
    """Levenshtein 对齐（原版同为编辑距离家族），返回分解计数与 hyp 独有 token。
    numpy 行进 DP：O(len(hyp)*len(ref)) 内存 O(min)。"""
    n, m = len(hyp), len(ref)
    if n == 0:
        return {"matched": 0, "sub": 0, "ins": 0, "del": m,
                "hyp_only": [], "matched_tokens": []}
    if m == 0:
        return {"matched": 0, "sub": 0, "ins": n, "del": 0,
                "hyp_only": list(hyp), "matched_tokens": []}

    # DP：d[j] = hyp[:i] 与 ref[:j] 的最小编辑距离；回溯用方向矩阵
    prev = np.arange(m + 1, dtype=np.int32)
    dirs = np.zeros((n, m + 1), dtype=np.uint8)  # 1=match/sub(对角) 2=del(hyp消费,上) 3=ins(ref消费,左)
    for i in range(1, n + 1):
        cur = np.empty(m + 1, dtype=np.int32)
        cur[0] = i
        dirs[i - 1, 0] = 2
        h = hyp[i - 1]
        eq = np.fromiter((h == ref[j - 1] for j in range(1, m + 1)),
                         count=m, dtype=bool)
        diag = prev[:-1] + (~eq)          # match or substitution
        up = prev[1:] + 1                 # deletion (hyp 有 ref 无 → insertion 相对 ref)
        left = cur[:-1] + 1               # insertion (ref 有 hyp 无)
        cur[1:] = np.minimum(np.minimum(diag, up), left)
        # 方向：对角优先于 up 优先于 left（与原版一致优先 match）
        dirs[i - 1, 1:] = np.where(cur[1:] == diag, 1,
                                   np.where(cur[1:] == up, 2, 3))
        prev = cur

    # 回溯
    i, j = n, m
    matched, sub, ins, del_ = 0, 0, 0, 0
    hyp_only: list[str] = []
    matched_tokens: list[str] = []
    while i > 0 or j > 0:
        if i > 0 and j > 0 and dirs[i - 1, j] == 1:
            if hyp[i - 1] == ref[j - 1]:
                matched += 1
                matched_tokens.append(hyp[i - 1])
            else:
                sub += 1
                hyp_only.append(hyp[i - 1])
            i -= 1
            j -= 1
        elif i > 0 and (j == 0 or dirs[i - 1, j] == 2):
            ins += 1
            hyp_only.append(hyp[i - 1])
            i -= 1
        else:
            del_ += 1
            j -= 1
    # hyp_only 是逆序回溯的，恢复顺序
    return {"matched": matched, "sub": sub, "ins": ins, "del": del_,
            "hyp_only": hyp_only[::-1], "matched_tokens": matched_tokens[::-1]}


@dataclass
class PageAlign:
    page_chars: int
    matched: int
    sub: int
    ins: int          # 页面有、字幕没有 → 页面增量
    increment: float  # (ins + sub*0.5) / page_chars，替换算半分（页面换了说法也算增量）
    hyp_only_text: str = ""  # 页面独有字词（可解释性）

    @property
    def summary(self) -> str:
        return (f"页{self.page_chars}字 重合{self.matched} 换述{self.sub} "
                f"独有{self.ins} 增量={self.increment:.2f}")


def page_increment(page_text: str, subtitle_text: str) -> PageAlign:
    hyp = characterize(page_text)
    ref = characterize(subtitle_text)
    r = align(hyp, ref)
    inc = (r["ins"] + 0.5 * r["sub"]) / max(len(hyp), 1)
    return PageAlign(page_chars=len(hyp), matched=r["matched"], sub=r["sub"],
                     ins=r["ins"], increment=round(float(inc), 3),
                     hyp_only_text=" ".join(r["hyp_only"])[:120])
