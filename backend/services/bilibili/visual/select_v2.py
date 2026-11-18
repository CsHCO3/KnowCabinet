"""
选择器 v2：终帧覆盖合并 + OCR 价值过滤（在 scan_frames.py 产物之上做二次选择）
====================================================================
输入: out/<bvid>/candidates.json（已有高清帧的平台尾帧候选）+ lo_frames/（低清帧）
  1. 终帧覆盖合并：相邻候选在格子级(8px格)比较变化面积占比，
     底部 20% 字幕带裁掉不参与（免疫原生字幕框）；
     变化格占比 < MERGE_FRAC => 同页渐进（小动画/元素蹦出）=> 只留终帧。
     变化大 => 新页，两帧都留（用户的覆盖规则：覆盖前帧仅在未覆盖时才留）。
  2. OCR 价值过滤：Windows WinRT OCR（ocr_win.ps1，零安装）统计每候选文字量，
     < TEXT_THR 判低价值；滚动中位数做段级低价值标注（B-roll/成果展示段）。

用法:
  python select_v2.py BV17Xp46wEPu [--merge-frac 0.12] [--gt 38,57,110,133,213,226,254,281,321,354]
  python select_v2.py BV1Nyp96pEGD --text-thr 20
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
CELL = 8          # 格子边长（lo 帧 192px 宽上）
SUBTITLE_BAND = 0.20  # 底部字幕带高度比例，不参与覆盖比较
CELL_THR = 12.0   # 格内均值差超过此值算"变化格"
TEXT_THR = 20     # OCR 字符数低于此 => 低文字量候选
ROLL = 3          # 段级滚动中位数窗口（±N 候选）


def log(msg):
    print(f"[v2] {msg}", flush=True)


def cell_sig(img_path: Path) -> np.ndarray:
    img = Image.open(img_path).convert("L")
    w, h = img.size
    img = img.crop((0, 0, w, h - int(h * SUBTITLE_BAND)))  # 去字幕带
    arr = np.asarray(img, dtype=np.float32)
    H, W = arr.shape
    return arr[:H // CELL * CELL, :W // CELL * CELL] \
        .reshape(H // CELL, CELL, W // CELL, CELL).mean(axis=(1, 3))


def changed_frac(a: np.ndarray, b: np.ndarray) -> float:
    return float((np.abs(a - b) > CELL_THR).mean())


CONTENT_THR = 18.0   # 格子与全片中位背景的差超过此值 => A 的"内容格"
COVER_THR = 0.85     # A 的内容格在 B 中存留比例 >= 此值 => B 覆盖 A（同页渐进）


def coverage_merge(sigs: list, cover_thr: float = COVER_THR,
                   content_thr: float = CONTENT_THR):
    """方向性覆盖：B 覆盖 A = A 的内容格大部分在 B 中原样存留。
    构建动画（元素追加）→ 覆盖 → 链内只留终帧；翻页 → 内容消失 → 新页。
    返回 page_id 与 is_final。"""
    n = len(sigs)
    if n == 0:                     # 一个候选帧都没有（CDN 抽风时常见），别让 np.stack 炸掉
        return [], [], []
    bg = np.median(np.stack(sigs), axis=0)
    page_id = [0] * n
    pid = 0
    covers = [None] * n
    for i in range(1, n):
        a, b = sigs[i - 1], sigs[i]
        content = np.abs(a - bg) > content_thr
        if content.sum() < 4:            # A 近乎空页：跟随前一页
            cover = 1.0
        else:
            lost = ((np.abs(a - b) > CELL_THR) & content).sum()
            cover = 1.0 - lost / content.sum()
        covers[i] = round(float(cover), 3)
        if cover >= cover_thr:
            page_id[i] = pid
        else:
            pid += 1
            page_id[i] = pid
    is_final = [i == n - 1 or page_id[i + 1] != page_id[i]
                for i in range(n)]
    return page_id, is_final, covers


def run_ocr(hd_dir: Path) -> dict:
    """对 hd_dir 里全部 jpg 跑 WinRT OCR。
    返回 {文件名: {chars: 字符数, long: >=6字符长段占比}}。
    长段占比低 = 文字碎片化（UI 菜单/按钮），是"软件操作展示"型低价值帧的信号。"""
    out = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(Path(__file__).resolve().parent / "ocr_win.ps1"), "-Dir", str(hd_dir)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=600)
    res = {}
    for line in (out.stdout or "").splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3 and parts[1].lstrip("-").isdigit():
            text = parts[2]
            toks = [x for x in re.split(r"\s+", text) if x]
            tot = sum(len(x) for x in toks) or 1
            res[parts[0]] = {
                "chars": int(parts[1]),
                "long": round(sum(len(x) for x in toks if len(x) >= 6) / tot, 3),
                "text": text,
            }
    return res


def text_rich(ocr: dict, min_chars: int, min_long: float) -> bool:
    return ocr.get("chars", -1) >= min_chars and ocr.get("long", 0) >= min_long


def contact_sheet(paths_ts, out_path, thumb_w=420, cols=4):
    from PIL import ImageDraw
    if not paths_ts:
        return
    thumbs = []
    for p, label in paths_ts:
        im = Image.open(p).convert("RGB")
        h = int(im.height * thumb_w / im.width)
        thumbs.append((im.resize((thumb_w, h)), label))
    rows = (len(thumbs) + cols - 1) // cols
    row_h = [max(th.height for th, _ in thumbs[r * cols:(r + 1) * cols])
             for r in range(rows)]
    W = cols * (thumb_w + 8) + 8
    H = sum(row_h) + rows * 26 + 8
    canvas = Image.new("RGB", (W, H), (24, 24, 24))
    draw = ImageDraw.Draw(canvas)
    y = 8
    for r in range(rows):
        for c, (im, label) in enumerate(thumbs[r * cols:(r + 1) * cols]):
            x = 8 + c * (thumb_w + 8)
            canvas.paste(im, (x, y))
            draw.text((x + 4, y + row_h[r] + 4), label, fill=(230, 230, 230))
        y += row_h[r] + 26
    canvas.save(out_path, quality=88)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bvid")
    ap.add_argument("--cover-thr", type=float, default=0.85)
    ap.add_argument("--text-thr", type=int, default=TEXT_THR)
    ap.add_argument("--min-chars", type=int, default=10,
                    help="零文字门限：只杀 B-roll/纯画面，示意图文少属正常；"
                         "碎片化与字数作为 VLM 排序信号输出，不做硬过滤")
    ap.add_argument("--min-long", type=float, default=0.0,
                    help="默认不参与硬过滤（架构图标签与 UI 垃圾不可区分）")
    ap.add_argument("--gt", type=str, default="",
                    help="逗号分隔的 ground truth 秒列表，做命中率评估")
    ap.add_argument("--no-ocr", action="store_true")
    args = ap.parse_args()

    root = HERE / "out" / args.bvid
    cand = json.loads((root / "candidates.json").read_text(encoding="utf-8"))
    finals = [c for c in cand if c.get("kind", "final") == "final"]
    finals.sort(key=lambda c: c["t"])
    times = [c["t"] for c in finals]
    lo_frames_dir = root / "lo_frames"

    # ---- 1) 终帧覆盖合并 ----
    sigs = []
    for c in finals:
        lo = Path(c["lo_frame"])
        if not lo.exists():  # lo 已清理时按时间从 lo_frames 重建
            idx = round(c["t"] * 2)
            lo = lo_frames_dir / f"lo_{idx+1:05d}.jpg"
        sigs.append(cell_sig(lo))
    page_id, is_final, covers = coverage_merge(sigs, cover_thr=args.cover_thr)
    pages = {}
    for i, c in enumerate(finals):
        pages.setdefault(page_id[i], []).append(c["t"])
    sel = [c for i, c in enumerate(finals) if is_final[i]]
    log(f"覆盖合并(cover_thr={args.cover_thr}): {len(finals)} 候选 -> "
        f"{len(pages)} 页 -> 留终帧 {len(sel)}")
    for pid in sorted(pages):
        member = ", ".join(f"{t:.1f}" for t in pages[pid])
        mark = " *" if len(pages[pid]) > 1 else ""
        log(f"  页{pid:02d}: {member}{mark}")

    # ---- 2) OCR 价值过滤 ----
    ocr_by_t = {}
    if not args.no_ocr:
        log("WinRT OCR 全部候选 ...")
        ocr_raw = run_ocr(root / "hd_frames")
        log(f"OCR 完成 {len(ocr_raw)}/{len(finals)}")
        for c in finals:
            fn2 = f"hd_{c['t']:.2f}s.jpg"
            if fn2 in ocr_raw:
                ocr_by_t[c["t"]] = ocr_raw[fn2]
        rich = [t for t in (c["t"] for c in sel)
                if text_rich(ocr_by_t.get(t, {}), args.min_chars, args.min_long)]
        kept = [c for c in sel
                if text_rich(ocr_by_t.get(c["t"], {}), args.min_chars, args.min_long)]
        log(f"终帧 {len(sel)} 个中通过文字门限(字数>={args.min_chars} 且 "
            f"长段占比>={args.min_long}): {len(kept)}")
        log(f"  保留: {[round(c['t'],1) for c in kept]}")
        cs = [ocr_by_t.get(t, {}).get("chars", -1) for t in times]
        roll_med = []
        for i in range(len(cs)):
            lo_i, hi_i = max(0, i - ROLL), min(len(cs), i + ROLL + 1)
            roll_med.append(int(np.median([x for x in cs[lo_i:hi_i] if x >= 0] or [-1])))
        low_span = [t for t, m in zip(times, roll_med) if 0 <= m < args.text_thr]

    # ---- 3) 输出 ----
    sel_times = [c["t"] for c in sel]
    if args.gt:
        gt = [float(x) for x in args.gt.split(",")]
        tol = 4.0
        matched = set()
        misses = []
        for g in gt:
            hit = next((s for s in sel_times if abs(s - g) <= tol), None)
            if hit is not None:
                matched.add(g)
            else:
                misses.append(g)
        extras = [s for s in sel_times
                  if not any(abs(s - g) <= tol for g in gt)]
        log(f"GT 评估: GT {len(gt)} 个, 命中 {len(matched)}, "
            f"漏 {misses}, 多余 {len(extras)}: {[round(x,1) for x in extras]}")

    # 选择结果拼图（带 OCR 字数标注）
    def label(c):
        o = ocr_by_t.get(c["t"])
        return f"t={c['t']:.0f}s 字数={o['chars'] if o else '?'}"

    sheet = root / "sheet_v2_selected.jpg"
    contact_sheet([(Path(c["lo_frame"]), label(c)) for c in sel], sheet)
    sheet_keep = root / "sheet_v2_kept.jpg"
    if not args.no_ocr:
        contact_sheet([(Path(c["lo_frame"]), label(c)) for c in kept], sheet_keep)
        log(f"终帧拼图: {sheet}  通过门限拼图: {sheet_keep}")
    else:
        log(f"选中拼图: {sheet}")

    result = {
        "bvid": args.bvid,
        "cover_thr": args.cover_thr,
        "n_cand": len(finals), "n_pages": len(pages),
        "pages": {str(p): v for p, v in sorted(pages.items())},
        "selected": sel_times,
        "ocr": {str(t): ocr_by_t[t] for t in sorted(ocr_by_t)},
        "kept_after_gate": [c["t"] for c in kept] if not args.no_ocr else None,
        "ocr_low_text": low_span if not args.no_ocr else None,
        "gt_eval": {
            "gt": [float(x) for x in args.gt.split(",")] if args.gt else None,
        },
    }
    (root / "select_v2.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
