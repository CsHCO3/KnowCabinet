# -*- coding: utf-8 -*-
"""研究：长消息（信息量大的片段）分布 + 每条消息的字数分布。只读，不改数据。"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent

GROUPS = ["AI Agent 综合交流", "27贝壳校招互帮互助群"]

for g in GROUPS:
    fp = ROOT / g / "消息.tsv"
    if not fp.is_file():
        continue
    rows = []
    for line in fp.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        rows.append((parts[0], parts[2], parts[3]))
    lens = sorted((len(t) for _, _, t in rows), reverse=True)
    n = len(lens)
    tot = sum(lens)
    print(f"■ {g}：{n} 条消息 / 正文合计 {tot} 字 / 平均 {tot / max(n,1):.0f} 字")
    buckets = [(0, 50), (50, 100), (100, 200), (200, 400), (400, 800), (800, 2000), (2000, 10**9)]
    for lo, hi in buckets:
        c = sum(1 for l in lens if lo <= l < hi)
        ch = sum(l for l in lens if lo <= l < hi)
        print(f"   {lo:>5}~{hi if hi < 10**9 else '∞':<5} 字：{c:>5} 条（{c / max(n,1) * 100:>5.1f}%）  共 {ch:>7} 字（占正文 {ch / max(tot,1) * 100:>4.1f}%）")
    print(f"   最长 5 条：{lens[:5]}")
    for thr in (200, 300, 500, 1000):
        c = sum(1 for l in lens if l >= thr)
        ch = sum(l for l in lens if l >= thr)
        print(f"   ≥{thr} 字：{c} 条 / {ch} 字（占正文 {ch / max(tot,1) * 100:.1f}%）")
    print()
