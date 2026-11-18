"""收藏视频终态对账：每条收藏视频现在"处理到哪一步"，一条命令看清，免得重复跑或误判漏跑。

只读文件（收藏 CSV / _runs.jsonl / dense / quarantine.json），不联网、不跑模型，
所以跑批期间也能查，不会影响 GPU。

为什么需要：批跑最容易出的错不是"跑失败"，而是"以为没跑过又跑一遍"或者
"以为跑过了其实没跑"。入库的真相在 dense 目录，尝试的真相在 _runs.jsonl，
两者都会漏记一些历史路径（老 video-import 只写 dense 不写 _runs），所以要合起来看。
"""
from __future__ import annotations

import csv
import json
import time
from pathlib import Path

from .visual.paths import DENSE_DIR, PROJECT_ROOT

RUNS = PROJECT_ROOT / "data" / "bilibili" / "visual" / "_runs.jsonl"
QUAR = PROJECT_ROOT / "data" / "bilibili" / "quarantine.json"
ABANDONED = PROJECT_ROOT / "data" / "bilibili" / "abandoned.json"
FAV_CSV = PROJECT_ROOT / "data" / "bili_servey" / "videos" / "favorites_51087974_latest200.csv"

CN = {"ok": "入库", "no_subtitle": "无字幕轨", "no_usable_subtitle": "字幕不可用",
      # 原因码比状态码更具体（同样是 no_usable_subtitle，付费和转写故障处理方式完全不同）
      "asr_no_audio": "付费/无音频流", "no_speech": "无语音（纯 BGM）",
      "subtitle_missing": "无字幕轨", "subtitle_off_topic": "字幕抓错",
      "subtitle_low_coverage": "字幕覆盖低", "digest_empty": "浓缩后无内容",
      "page_unsupported": "分P不支持", "scan_failed": "扫帧失败",
      "digest_failed": "浓缩失败", "apply_failed": "落库失败",
      "error": "报错"}

LONG_MIN = 30          # ≥30 分钟按 A16 不自动跑，要人工逐次批


def latest_runs() -> dict:
    last = {}
    if not RUNS.exists():
        return last
    for line in RUNS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("bvid"):
            last[d["bvid"]] = d
    return last


def dense_ids() -> set[str]:
    """已入库的 bvid（P2+ 的 dense 文件名带 _pN，归并回主 bvid）。"""
    if not DENSE_DIR.exists():
        return set()
    return {p.stem.split("_p")[0] for p in DENSE_DIR.glob("*.md")}


def abandoned() -> dict:
    """人已判定放弃的（视频失效/误收藏/重复/无内容）。有它才能把"没跑"和"决定不跑"分开。"""
    try:
        return {e["bvid"]: e for e in json.loads(ABANDONED.read_text(encoding="utf-8"))["entries"]}
    except (OSError, json.JSONDecodeError, KeyError):
        return {}


def quarantined() -> dict:
    try:
        return {e["bvid"]: e for e in json.loads(QUAR.read_text(encoding="utf-8"))["entries"]}
    except (OSError, json.JSONDecodeError, KeyError):
        return {}


def favorites(csv_path: Path = FAV_CSV) -> list[dict]:
    """收藏清单。Excel 存过一次会变 GBK，按 utf-8-sig → gbk 兜底读。"""
    if not csv_path.exists():
        return []
    raw = csv_path.read_bytes()
    text = None
    for enc in ("utf-8-sig", "gbk"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise SystemExit(f"收藏清单编码不认识：{csv_path}")
    rows = []
    for r in csv.DictReader(text.splitlines()):
        bv = (r.get("BV号") or "").strip()
        if not bv.startswith("BV"):
            continue
        try:
            mins = float(r.get("时长分") or 0)
        except ValueError:
            mins = 0.0
        rows.append({"bvid": bv, "title": (r.get("标题") or "").strip(), "min": mins})
    return rows


def classify(rows: list[dict], last: dict, dense: set, quar: dict,
             include_long: bool = False, aban: dict | None = None) -> list[dict]:
    """给每条收藏打一个终态标签。判定顺序：已入库 > 已放弃 > 隔离 > 有跑动 > 没跑动。

    "已放弃"必须排在"隔离"前面：人已经判过死刑的片子，不该继续挂在待验清单里。
    """
    aban = aban or {}
    out = []
    for r in rows:
        bv, mins = r["bvid"], r["min"]
        if bv in dense:
            bucket = "已入库"
        elif bv in aban:
            bucket = "已放弃"
        elif bv in quar:
            bucket = "隔离待验"
        elif bv in last and (last[bv].get("status") or "") != "ok":
            st = last[bv].get("status") or ""
            rs = last[bv].get("reason") or ""
            bucket = f"未入库：{CN.get(rs) or CN.get(st, st)}"
        elif bv in last:
            # _runs 说 ok 但 dense 里没有：多半被隔离移走了
            bucket = "隔离待验"
        elif mins >= LONG_MIN and not include_long:
            bucket = f"长视频(≥{LONG_MIN}min，按 A16 未跑)"
        else:
            bucket = "从未处理"
        out.append({**r, "bucket": bucket})
    return out


def format_md(items: list[dict]) -> str:
    order = ["已入库", "已放弃", "隔离待验", "从未处理"]
    order += sorted({i["bucket"] for i in items
                     if i["bucket"].startswith("未入库：")})
    order += [i["bucket"] for i in items if i["bucket"].startswith("长视频")]
    lines = ["# 收藏视频终态对账", "",
             f"生成时间：{time.strftime('%Y-%m-%d %H:%M')}",
             f"收藏有效条目 {len(items)} 条", ""]
    for k in order:
        group = [i for i in items if i["bucket"] == k]
        if not group:
            continue
        lines += [f"## {k}（{len(group)} 条）", "",
                  "| bvid | 分钟 | 标题 |", "|---|---|---|"]
        for d in sorted(group, key=lambda x: x["min"]):
            title = d["title"].replace("|", "／")[:44]
            lines.append(f"| {d['bvid']} | {d['min']:.0f} | {title} |")
        lines.append("")
    return "\n".join(lines)


def build(csv_path: Path = FAV_CSV, out_path: Path | None = None,
          include_long: bool = False) -> dict:
    """算一遍并落一份 md；返回 {bucket: count} 摘要。"""
    items = classify(favorites(csv_path), latest_runs(), dense_ids(),
                     quarantined(), include_long, abandoned())
    summary: dict[str, int] = {}
    for i in items:
        summary[i["bucket"]] = summary.get(i["bucket"], 0) + 1
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(format_md(items), encoding="utf-8")
    return {"total": len(items), "buckets": summary, "items": items}
