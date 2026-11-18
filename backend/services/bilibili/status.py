"""视频处理进度总表：一条命令看清"线索库 → 已入库 / 跳过 / 待处理"。

数据源（全是真相文件，不另建状态库）：
- `data/bilibili/visual/_runs.jsonl`  每次处理的记录（含失败原因、耗时、图数）
- `knowledgebase/community/bilibili/dense/*.md`  真正进库的条目
- `knowledgebase/community/bilibili/meta/*.json`  L0 线索卡
- `data/bilibili/quarantine.json`  被隔离、待人工验收的
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

from .reasons import (REASON_LABEL, VISUAL_VERDICT_LABEL, classify_reason,
                      is_retryable)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
RUNS = PROJECT_ROOT / "data" / "bilibili" / "visual" / "_runs.jsonl"
DENSE = PROJECT_ROOT / "knowledgebase" / "community" / "bilibili" / "dense"
META = PROJECT_ROOT / "knowledgebase" / "community" / "bilibili" / "meta"
QUARANTINE = PROJECT_ROOT / "data" / "bilibili" / "quarantine.json"
ABANDONED = PROJECT_ROOT / "data" / "bilibili" / "abandoned.json"

_FM = re.compile(r"^---\n(.*?)\n---\n", re.S)

# 历史上手写过的测试号，不是真视频：别让它进统计（会污染"值得补抓"清单）
_FAKE_BVIDS = {"BV1zzzzzzzzz"}


def _dense_index() -> dict[str, dict]:
    """已入库条目：id → {title, images, sections, visual, multipart, chars}"""
    out: dict[str, dict] = {}
    if not DENSE.exists():
        return out
    for p in DENSE.glob("*.md"):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = _FM.match(text)
        head = m.group(1) if m else ""

        def pick(key: str) -> str:
            mm = re.search(rf"^{key}:\s*(.*)$", head, re.M)
            return mm.group(1).strip() if mm else ""

        out[p.stem] = {
            "title": pick("title"),
            "images": int(pick("images") or 0),
            "sections": int(pick("sections") or 0),
            "visual": pick("visual"),
            "multipart": pick("multipart"),
            "chars": len(text),
        }
    return out


def _runs_index() -> dict[tuple[str, int], dict]:
    """每个 (bvid, page) 取最后一次记录。"""
    last: dict[tuple[str, int], dict] = {}
    if not RUNS.exists():
        return last
    for line in RUNS.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        bv = d.get("bvid") or ""
        if not bv or bv in _FAKE_BVIDS:
            continue
        last[(bv, int(d.get("page") or 1))] = d
    return last


def _quarantine() -> list[dict]:
    try:
        payload = json.loads(QUARANTINE.read_text(encoding="utf-8"))
        return [e for e in payload.get("entries", []) if isinstance(e, dict)]
    except (OSError, json.JSONDecodeError):
        return []


def _abandoned() -> set[str]:
    """人已判定放弃的：不算"没进库"，也不用再补抓。"""
    try:
        return {e["bvid"] for e in json.loads(ABANDONED.read_text(encoding="utf-8"))["entries"]}
    except (OSError, json.JSONDecodeError, KeyError):
        return set()


def _derive_verdict(d: dict) -> str:
    """老记录没有 visual_verdict：从 gate 文本和 n_keep 反推画面侧到底发生了什么。"""
    v = d.get("visual_verdict")
    if v:
        return v
    gate = d.get("gate") or ""
    if (d.get("n_keep") or 0) > 0:
        return "kept"
    if (d.get("status") or "") == "scan_failed":
        return "failed"
    if "scan off" in gate or "扫帧开关关闭" in gate:
        return "off"
    if "不做画面分析" in gate or "没有一处判成整页文档" in gate:
        return "no_pages"
    if "扫出" in gate or "候选" in gate:
        return "scanned_none"
    return ""


def build_report(limit: int = 12, pending_limit: int = 15) -> dict:
    dense = _dense_index()
    runs = _runs_index()
    quar = _quarantine()
    metas = list(META.glob("*.json")) if META.exists() else []

    abandoned = _abandoned()
    # 一条视频可能多 P：按 bvid 归并，只要有一页进了库就算"已入库"
    in_kb: set[str] = {bid.split("_p")[0] for bid in dense}
    tried: set[str] = {bv for bv, _ in runs if bv not in abandoned}
    meta_ids = {p.stem for p in metas}

    status_count = Counter()
    reason_count = Counter()
    verdict_count = Counter()
    sub_source_count = Counter()
    asr_rescued = 0
    skipped: list[dict] = []
    ok_runs: list[dict] = []
    retryable: list[dict] = []
    for (bv, page), d in runs.items():
        if bv in abandoned:
            continue          # 已放弃的不进任何统计，免得每月都冒出来问一遍
        st = d.get("status") or ("ok" if d.get("ok") else "error")
        status_count[st] += 1
        reason = d.get("reason") or classify_reason(st, d.get("error", ""),
                                                    d.get("gate", ""))
        reason_count[reason] += 1
        verdict_count[_derive_verdict(d)] += 1
        # 字幕来源（official/asr/none）。老记录没这个字段：入库的都走的官方轨，
        # 没入库的就没有可用字幕源——按现状反推，沿用 classify_reason 的兼容写法
        sub = d.get("sub_source") or ("official" if st == "ok" else "none")
        sub_source_count[sub] += 1
        if sub == "asr" and st == "ok":     # 救回口径：sub_source=asr 且最终入库（§6.2）
            asr_rescued += 1
        one = {"bvid": bv, "page": page, "page_count": d.get("page_count"),
               "status": st, "reason": reason, "ts": d.get("ts", ""),
               "elapsed": d.get("elapsed") or 0,
               "n_keep": d.get("n_keep") or 0, "chars": d.get("chars") or 0,
               "verdict": _derive_verdict(d),
               "error": (d.get("error") or "")[:80]}
        if st == "ok":
            ok_runs.append(one)
        else:
            skipped.append(one)
            if is_retryable(reason):
                retryable.append(one)

    ok_runs.sort(key=lambda x: x["ts"], reverse=True)
    skipped.sort(key=lambda x: x["ts"], reverse=True)

    pending = sorted(
        (bid for bid in meta_ids if bid not in in_kb and bid not in tried),
        key=lambda x: x)
    # 试过但没进库：以 _runs 为准（有些视频没有 L0 卡，不能只看 meta 交集）
    under_tried = sorted(bid for bid in tried if bid not in in_kb)

    img_entries = [k for k, v in dense.items() if v["images"] > 0]
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "totals": {
            "clues_l0": len(meta_ids),
            "in_kb": len(in_kb),
            "in_kb_entries": len(dense),
            "with_images": len(img_entries),
            "images": sum(dense[k]["images"] for k in img_entries),
            "tried_not_in_kb": len(under_tried),
            "never_touched": len(pending),
            "quarantined": len(quar),
            "abandoned": len(abandoned),
        },
        "status": dict(status_count),
        "reason": dict(reason_count),
        "visual_verdict": dict(verdict_count),
        "sub_source": dict(sub_source_count),
        "asr_rescued": asr_rescued,
        "retryable_total": len(retryable),
        "retryable": retryable[:limit],
        "recent_ok": ok_runs[:limit],
        "recent_skipped": skipped[:limit],
        "pending": pending[:pending_limit],
        "pending_total": len(pending),
        "quarantine": quar[:limit],
    }


_CN = {
    "ok": "入库", "no_subtitle": "无字幕轨", "no_usable_subtitle": "字幕不可用",
    "no_speech": "无语音", "page_unsupported": "分P不支持", "scan_failed": "扫帧失败",
    "digest_failed": "浓缩失败", "apply_failed": "落库失败", "error": "报错",
}


def format_report(r: dict) -> str:
    t = r["totals"]
    lines = [f"视频处理进度（{r['generated_at']}）", ""]
    lines.append(f"线索库 {t['clues_l0']} 条 L0   ｜   已入库 {t['in_kb']} 条"
                 f"（条目 {t['in_kb_entries']}，其中带图 {t['with_images']} 条 / {t['images']} 张）")
    lines.append(f"试过但没进库 {t['tried_not_in_kb']} 条   ｜   从未处理 {t['never_touched']} 条"
                 f"   ｜   隔离待验 {t['quarantined']} 条   ｜   已放弃 {t.get('abandoned', 0)} 条")

    if r.get("reason"):
        lines += ["", "没进库的原因（分开记，别混成一锅）"]
        for k, v in sorted(r["reason"].items(), key=lambda kv: -kv[1]):
            if k in ("ok", "asr_used"):
                # asr_used 是入库成功的另一种记法（ASR 救回），不算"没进库"，
                # 它的条数看报告尾部的"字幕来源"行
                continue
            flag = "可重试" if is_retryable(k) else "别重试"
            lines.append(f"  {REASON_LABEL.get(k, k):<24} {v:>4} 条   {flag}")
        if r.get("retryable_total"):
            lines.append(f"  → 其中 {r['retryable_total']} 条值得补抓（字幕可能补全/画面抓取失败）")

    if r.get("visual_verdict"):
        vd = "  ".join(f"{VISUAL_VERDICT_LABEL.get(k, k)} {v}"
                       for k, v in sorted(r["visual_verdict"].items(), key=lambda kv: -kv[1]))
        lines.append(f"画面侧结果：{vd}")

    if r["recent_ok"]:
        lines += ["", "最近入库"]
        for x in r["recent_ok"]:
            pg = f" P{x['page']}/{x['page_count']}" if (x.get("page_count") or 1) > 1 else ""
            lines.append(f"  {x['ts']}  {x['bvid']}{pg}  {x['elapsed']:.0f}s  "
                         f"{x['n_keep']}张图  {x['chars']}字")

    if r["recent_skipped"]:
        lines += ["", "最近跳过/失败"]
        for x in r["recent_skipped"]:
            why = _CN.get(x["status"], x["status"])
            detail = x["error"] or ""
            lines.append(f"  {x['ts']}  {x['bvid']}  {why}  {detail}")

    if r["quarantine"]:
        lines += ["", "隔离待验（需要人看一眼）"]
        for e in r["quarantine"][:6]:
            lines.append(f"  {e.get('bvid')}  {e.get('reason')}  {(e.get('note') or '')[:40]}")

    lines += ["", f"从未处理 {r['pending_total']} 条，例如："]
    lines += [f"  {b}" for b in r["pending"]]
    if r.get("sub_source"):
        ss = r["sub_source"]
        lines += ["", f"字幕来源：official {ss.get('official', 0)} / asr {ss.get('asr', 0)}"
                     f" / none {ss.get('none', 0)}（ASR 救回 {r.get('asr_rescued', 0)} 条）"]
    return "\n".join(lines)
