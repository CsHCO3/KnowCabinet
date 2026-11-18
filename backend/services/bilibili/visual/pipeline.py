"""
管线 v2：字幕 LLM 预判 → 按分数扫描 → 结构去重 → 低清OCR过滤 → 高清定点重拉 → OCR整合
====================================================================================
用户定稿链路（2026-10-29）：
  ① 先用 LLM 对字幕按 30s 块打「画面价值先验分」(0-3)，分数决定采样密度：
       0 → 地板采样 1帧/30s（盲区保险，不归零）
       1 → 平台检测（fps=2，无运动兜底）
       2 → 平台检测 + 运动兜底每 5s
       3 → 平台检测 + 运动兜底每 2s
  ② 平台尾帧 + 方向覆盖合并（结构去重）
  ③ 低清(流原生分辨率 640×360) OCR 检查内容：文字门限 + 字符对齐增量 + LLM 页判
  ④ 高清定点重拉【仅幸存帧】
  ⑤ 高清帧终 OCR → visual_content.json（供浓缩内容整合）

产物: data/bilibili/visual/<bvid>/{lo2/, hd_frames/, final_frames/, seg_scores.json,
                          windows.json, ocr_lo.json, ocr_hd.json,
                          pipeline2_log.json, visual_content.json}
用法:
  cd D:/infosystem
  python -m backend.services.bilibili.visual.pipeline BV1xxx [BV...] [--gt 63.5,78.5]
                                          [--no-llm] [--rescan] [--clean-lo]
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from .scan_frames import (  # noqa: E402
    STILL_THR, dhash_bits, ffmpeg_headers, get_cookie, get_video_meta,
    hamming, make_session, pick_stream, refetch_hd, refetch_sequential)
from .select_v2 import cell_sig, coverage_merge, run_ocr  # noqa: E402
from .align_pages import characterize, page_increment  # noqa: E402
from .llm import llm_call_raw, parse_json_loose  # noqa: E402

# ---- 参数（GT 校准后冻结，见 REPORT.md） ----
BLOCK = 30.0              # 字幕分块粒度 = LLM 打分粒度
FPS_DETECT = 2.0          # 检测窗采样帧率
FLOOR_EVERY = 30.0        # 0 分窗地板采样间隔
MOTION_BY_SCORE = {1: 0.0, 2: 5.0, 3: 2.0}   # 分数→运动兜底间隔，0=关
GAP_MIN = 2.0             # 检测窗内 ≥此长度的无平台间隙才考虑兜底
MIN_SPAN = 1.0            # 平台最短持续秒数
WINDOW_MERGE_GAP = 5.0    # 相邻同模式块间隔小于此值并窗（省 ffmpeg 进程开销）
COVER_THR = 0.80
SUBTITLE_BAND = 0.22
OCR_UPSCALE = 2.0         # 低清 OCR 前放大倍数（640→1280 补小字）
MIN_CHARS = 6             # 低清 OCR 文字门限（低于此判无文字页）
SUB_PAD = 3.0             # 对齐窗口：页前字幕余量
SUB_WIN = 15.0            # 对齐窗口：页后字幕默认长度（候选稀疏，不再取到下一候选）
JUDGE_CHUNK = 50
KEEP_INC = 0.35           # score>=3 段的先验保留增量门限
PENDING_INC = 0.5         # 字符增量达到此值但页判未过 → pending（只记日志不拉高清）

HERE = Path(__file__).resolve().parent
from .paths import VISUAL_ROOT as ROOT
from .paths import SUB_DIR
from .paths import subtitle_cache_path


def log(msg):
    print(f"[v2] {msg}", flush=True)


# ---------------- ① 字幕 LLM 预判 ----------------

PRE_PROMPT = """你是视频画面价值预判器。下面是B站视频的标题、时长，以及按30秒分块的字幕（#编号 起始时间 文本）。
给每个块打「画面信息量先验分」，用于决定视频扫描的采样密度——只依据文字推测画面内容：
- 3 = 讲架构/模型对比/流程/数据/参数，画面大概率有图示或图表
- 2 = 概念讲解、原理分析，可能出现示意图
- 1 = 软件操作演示、功能展示、口播为主（画面大概率是界面录屏或人像）
- 0 = 片头片尾、广告、寒暄、感谢观看、成果展示、纯闲聊
只输出 JSON：{{"video_type": "课程|访谈|演示|混合", "video_value": 0-10, "blocks": [{{"i": 0, "s": 3}}, ...]}}
必须覆盖输入的每一个编号，不要遗漏。

标题: {title}
时长: {dur}秒
分块字幕:
{blocks}"""


def load_segments(bvid: str, page: int = 1) -> list[dict]:
    p = subtitle_cache_path(bvid, page)   # P1 仍是 SUB_DIR/<bvid>.json，路径字符串不变
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8")).get("segments") or []


def make_blocks(segments: list[dict], dur: float, block: float = BLOCK):
    n = max(1, int(dur // block) + (1 if dur % block else 0))
    texts: list[list[str]] = [[] for _ in range(n)]
    for s in segments:
        i = int(s["start"] // block)
        if 0 <= i < n:
            texts[i].append(str(s["text"]))
    out = []
    for i in range(n):
        t1 = min((i + 1) * block, dur)
        if t1 <= 0:
            continue
        out.append({"i": i, "t0": i * block, "t1": t1,
                    "text": " ".join(texts[i])[:300]})
    return out


def llm_block_scores(title: str, dur: int, blocks: list[dict]) -> dict | None:
    """按 100 块/次调 LLM 打分。返回 {video_type, video_value, scores[]} 或 None。"""
    scores: dict[int, int] = {}
    meta = {"video_type": "未知", "video_value": 5}
    for c0 in range(0, len(blocks), 100):
        chunk = blocks[c0:c0 + 100]
        lines = []
        for b in chunk:
            t0 = int(b["t0"])
            lines.append(f"#{b['i']} [{t0//60:02d}:{t0%60:02d}] {b['text']}")
        prompt = PRE_PROMPT.format(title=title, dur=dur, blocks="\n".join(lines))
        resp = llm_call_raw(prompt, max_tokens=8000)
        r = parse_json_loose(resp or "")
        if not isinstance(r, dict) or not isinstance(r.get("blocks"), list):
            log(f"  预判块 {c0}~{c0+len(chunk)} 无 JSON，该段默认分 2")
            continue
        meta = {"video_type": str(r.get("video_type", "未知")),
                "video_value": r.get("video_value", 5)}
        for x in r["blocks"]:
            try:
                i, s = int(x["i"]), int(x["s"])
            except (KeyError, ValueError, TypeError):
                continue
            if 0 <= s <= 3:
                scores[i] = s
    if not scores:
        return None
    return {**meta, "scores": [scores.get(b["i"], 2) for b in blocks]}


def windows_from(blocks: list[dict], scores: list[int], dur: float):
    """全部块走检测扫：0 分块 1fps 慢扫（片头片尾的静止页照样成候选，
    由页判杀——实测地板采样 30s 一帧会漏掉低分块里的 GT 帧），
    1 分以上 2fps 全速。相邻同速块并窗省 ffmpeg 进程开销。"""
    wins = []
    for b, s in zip(blocks, scores):
        mode = "slow" if s == 0 else "detect"
        if wins and wins[-1]["mode"] == mode and \
                b["t0"] - wins[-1]["t1"] < WINDOW_MERGE_GAP:
            wins[-1]["t1"] = b["t1"]
        else:
            wins.append({"t0": b["t0"], "t1": b["t1"], "mode": mode})
    if wins:
        wins[0]["t0"] = 0.0
        wins[-1]["t1"] = dur
    for w in wins:
        w["fps"] = 1.0 if w["mode"] == "slow" else FPS_DETECT
    return wins


# ---------------- ② 按窗扫描 ----------------

def scan_windows(url: str, cookie: str, out_dir: Path, wins: list[dict]):
    """每窗一个 ffmpeg：-ss/-t 定界（必配对，select 不停止读取的坑），
    原生分辨率出图（640×360 供 OCR），帧名 w{窗}_{序号}.jpg。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    for k, w in enumerate(wins):
        fps = w["fps"]
        dur = max(w["t1"] - w["t0"], 0.5)
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-ss", f"{w['t0']:.3f}", "-t", f"{dur:.3f}",
            "-headers", ffmpeg_headers(cookie), "-i", url,
            "-vf", f"fps={fps}", "-q:v", "4", "-f", "image2",
            "-start_number", "0", str(out_dir / f"w{k:02d}_%05d.jpg"),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=900)
        n = len(list(out_dir.glob(f"w{k:02d}_*.jpg")))
        if n == 0:
            log(f"  窗{k} {w['mode']} [{w['t0']:.0f},{w['t1']:.0f}] 无帧! "
                f"{(r.stderr or '')[:120]}")
        else:
            log(f"  窗{k} {w['mode']:6s} [{w['t0']:6.1f},{w['t1']:6.1f}] "
                f"{n} 帧 @{fps:.4g}fps")


# ---------------- ③ 结构终帧 ----------------

def structural(lo_dir: Path, wins: list[dict], scores: list[int]):
    """每窗独立做平台检测（帧率按窗记录）；窗内无平台间隙按局部分数决定
    兜底密度；最后全局方向覆盖合并。"""
    def score_at(t: float) -> int:
        i = int(t // BLOCK)
        return scores[i] if 0 <= i < len(scores) else 2

    cands: list[tuple[float, Path]] = []
    stats = {"platforms": 0, "fill": 0, "slow": 0}
    for k, w in enumerate(wins):
        fps_w = w["fps"]
        files = sorted(lo_dir.glob(f"w{k:02d}_*.jpg"))
        if not files:
            continue
        hashes = [dhash_bits(p) for p in files]
        dists = [hamming(hashes[i], hashes[i + 1]) for i in range(len(hashes) - 1)]
        spans: list[tuple[int, int]] = []
        i = 0
        while i < len(dists):
            if dists[i] <= STILL_THR:
                j = i
                while j + 1 < len(dists) and dists[j + 1] <= STILL_THR:
                    j += 1
                if (j + 1 - i) / fps_w >= MIN_SPAN:
                    spans.append((i, j + 1))
                i = j + 1
            else:
                i += 1
        stats["platforms"] += len(spans)
        pts = sorted({b for _, b in spans})
        prev = 0
        gaps: list[tuple[float, float]] = []
        for a, b in spans:
            if (a - prev) / fps_w >= GAP_MIN:
                gaps.append((w["t0"] + prev / fps_w,
                             w["t0"] + a / fps_w))
            prev = b
        if (len(files) - prev) / fps_w >= GAP_MIN:
            gaps.append((w["t0"] + prev / fps_w,
                         w["t0"] + len(files) / fps_w))
        for g0, g1 in gaps:
            every = MOTION_BY_SCORE.get(score_at((g0 + g1) / 2), 0.0)
            if every <= 0:
                continue
            t = g0
            while t < g1:
                fi = min(int(round((t - w["t0"]) * fps_w)), len(files) - 1)
                if fi not in pts:
                    pts.append(fi)
                    stats["fill"] += 1
                t += every
        pts.sort()
        cands.extend((round(w["t0"] + p / fps_w, 2), files[p]) for p in pts)

    # 时间去重（窗边界可能重叠 <1s）
    cands.sort(key=lambda x: x[0])
    dedup: list[tuple[float, Path]] = []
    for t, f in cands:
        if not dedup or t - dedup[-1][0] > 0.5:
            dedup.append((t, f))
    sigs = [cell_sig(f) for _, f in dedup]
    page_id, is_final, _ = coverage_merge(sigs, cover_thr=COVER_THR)
    finals = [dedup[i] for i in range(len(dedup)) if is_final[i]]
    stats["after_coverage"] = len(finals)
    return finals, stats


# ---------------- ④ 低清 OCR ----------------

def ocr_frames(entries: list[tuple[str, Path]], cache_path: Path,
               crop_band: bool = True) -> dict:
    """entries: [(key, path)]。裁字幕带+放大后 WinRT OCR，JSON 缓存。"""
    old = json.loads(cache_path.read_text(encoding="utf-8")) \
        if cache_path.exists() else {}
    missing = [(k, p) for k, p in entries if k not in old and p.exists()]
    if missing:
        tmp = Path(tempfile.mkdtemp(prefix="ocr2_"))
        try:
            for k, p in missing:
                im = Image.open(p).convert("RGB")
                w, h = im.size
                if crop_band:
                    im = im.crop((0, 0, w, h - int(h * SUBTITLE_BAND)))
                im = im.resize((int(im.width * OCR_UPSCALE),
                                int(im.height * OCR_UPSCALE)), Image.LANCZOS)
                im.save(tmp / f"{k}.jpg", quality=92)
            raw = run_ocr(tmp)
            for k, _ in missing:
                if f"{k}.jpg" in raw:
                    old[k] = raw[f"{k}.jpg"]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(old, ensure_ascii=False),
                              encoding="utf-8")
    return old


def local_subs(segments: list[dict], lo: float, hi: float) -> str:
    return " ".join(s["text"] for s in segments
                    if s["start"] < hi and s["end"] > lo)


# ---------------- LLM 页判（窗口化字幕版） ----------------

JUDGE_PROMPT = """你是视频画面提取助手。下面每个候选画面页给出：附近字幕（前后各约30秒）与页面OCR文字。
逐页判断画面相对字幕是否提供【增量信息】：
- v=1：架构图/流程图/示意图/关键界面/数据图表/具体模型或工具名——字幕没写或没细说的东西
- v=0：只是字幕原话或关键词放大显示、纯装饰、界面常规元素、乱码
只输出 JSON 数组：[{{"i": 0, "v": 1, "why": "一词理由"}}]，每页一条，不要遗漏。

页面:
{pages}"""

NO_SUB_JUDGE_PROMPT = """你是视频画面提取助手。视频标题：《{title}》。该视频无可用字幕，下面是各候选画面页的OCR文字。
逐页判断画面是否值得保留为知识帧。默认怀疑：讲解类视频里大部分画面是软件演示或口播，真正值得留的每视频往往只有几张。
- v=1：架构图/流程图/示意图/对比表/难点标注/方法总结——画面本身在讲解知识，跳过会丢信息
- v=0：软件界面录屏/IDE代码编辑器/终端命令输出/文件目录树/网页浏览/对话框/设置面板/文字碎片化(UI按钮)/纯装饰/乱码
判断线索：OCR 里出现代码语法、文件路径、命令行、菜单项、按钮文字 → v=0；
OCR 呈现完整句子/要点列表/对比结构/数字结论 → 可能 v=1。
只输出 JSON 数组：[{{"i": 0, "v": 1, "why": "一词理由"}}]，每页一条，不要遗漏。

页面OCR:
{pages}"""


def judge_pages(segments: list[dict], page_texts: list[tuple[int, float, str]],
                title: str = ""):
    """page_texts: [(行号, t, ocr文本)] → {行号: {v, why}}，50 页/次。
    segments 为空（无字幕对齐）时用纯 OCR 版提示词（带标题校准视频类型）。"""
    out: dict[int, dict] = {}
    no_sub = not segments
    for c0 in range(0, len(page_texts), JUDGE_CHUNK):
        chunk = page_texts[c0:c0 + JUDGE_CHUNK]
        plines = []
        for k, t, text in chunk:
            if no_sub:
                plines.append(f"#{k} t={t:.0f}s\n页面OCR: {text[:250]}")
            else:
                sub = local_subs(segments, t - 30, t + 30)
                plines.append(f"#{k} t={t:.0f}s\n附近字幕: {sub[:400]}"
                              f"\n页面OCR: {text[:200]}")
        tmpl = NO_SUB_JUDGE_PROMPT if no_sub else JUDGE_PROMPT
        prompt = tmpl.format(title=title, pages="\n\n".join(plines))
        resp = llm_call_raw(prompt, max_tokens=8000)
        r = parse_json_loose(resp or "")
        if isinstance(r, list):
            for x in r:
                try:
                    out[int(x["i"])] = {"v": int(x["v"]),
                                        "why": str(x.get("why", ""))}
                except (KeyError, ValueError, TypeError):
                    continue
        else:
            log(f"  页判批次 {c0} 无 JSON 数组")
    return out


# ---------------- 主流程 ----------------

def process(bvid: str, use_llm: bool = True, rescan: bool = False,
            clean_lo: bool = False, gt: list[float] | None = None) -> dict:
    tm: dict[str, float] = {}
    T0 = time.perf_counter()
    root = ROOT / bvid
    root.mkdir(parents=True, exist_ok=True)
    cookie = get_cookie()
    sess = make_session(cookie)
    meta = get_video_meta(sess, bvid)
    segments = load_segments(bvid)
    # 字幕覆盖率 <30% 视为无字幕：对齐无意义（inc 恒 1），预判与页判换无字幕变体
    sub_cov = sum(s["end"] - s["start"] for s in segments) / max(meta["duration"], 1)
    if sub_cov < 0.3:
        segments = []
        log(f"  字幕覆盖率 {sub_cov:.0%} <30%，按无字幕模式跑")
    log(f"{bvid}: {meta['title'][:40]} {meta['duration']}s "
        f"字幕{len(segments)}条")

    # ① LLM 预判（缓存）
    t0 = time.perf_counter()
    seg_cache = root / "seg_scores.json"
    if seg_cache.exists() and not rescan and not segments:
        seg = {"video_type": "无字幕", "video_value": 5,
               "scores": [2] * len(make_blocks(segments, meta["duration"]))}
    elif seg_cache.exists() and not rescan:
        seg = json.loads(seg_cache.read_text(encoding="utf-8"))
        log(f"  预判(缓存): {seg['video_type']} value={seg['video_value']}")
    elif not use_llm or not segments:
        seg = None
    else:
        blocks0 = make_blocks(segments, meta["duration"])
        seg = llm_block_scores(meta["title"], meta["duration"], blocks0)
        if seg:
            log(f"  预判: {seg['video_type']} value={seg['video_value']} "
                f"分数分布 " +
                "/".join(f"{s}:{seg['scores'].count(s)}" for s in range(4)))
            seg_cache.write_text(json.dumps(seg, ensure_ascii=False),
                                 encoding="utf-8")
    blocks = make_blocks(segments, meta["duration"])
    if seg is None:
        seg = {"video_type": "无字幕" if not segments else "LLM失败",
               "video_value": 5, "scores": [2] * len(blocks)}
    scores = seg["scores"]
    wins = windows_from(blocks, scores, meta["duration"])
    n_det = sum(w["t1"] - w["t0"] for w in wins if w["mode"] == "detect")
    log(f"  窗口 {len(wins)} 个: 检测 {n_det:.0f}s / 地板 "
        f"{meta['duration']-n_det:.0f}s")
    tm["prescore_s"] = round(time.perf_counter() - t0, 1)

    # ② 扫描
    t0 = time.perf_counter()
    lo_dir = root / "lo2"
    wins_file = root / "windows.json"
    cached = (wins_file.exists() and lo_dir.exists()
              and any(lo_dir.glob("w*_*.jpg"))
              and json.loads(wins_file.read_text(encoding="utf-8")) == wins)
    if cached and not rescan:
        log("  扫描(缓存)")
    else:
        low = pick_stream(sess, bvid, meta["cid"], "low")
        scan_windows(low["url"], cookie, lo_dir, wins)
        wins_file.write_text(json.dumps(wins, ensure_ascii=False),
                             encoding="utf-8")
    tm["scan_s"] = round(time.perf_counter() - t0, 1)

    # ③ 结构终帧
    t0 = time.perf_counter()
    finals, st = structural(lo_dir, wins, scores)
    log(f"  结构: 平台{st['platforms']} 兜底{st['fill']}"
        f" -> 覆盖合并后 {st['after_coverage']} 候选")
    tm["structural_s"] = round(time.perf_counter() - t0, 1)

    # ④ 低清 OCR + 对齐 + 页判 + 决策
    t0 = time.perf_counter()
    ocr = ocr_frames([(f"{t:.2f}s", f) for t, f in finals],
                     root / "ocr_lo.json", crop_band=bool(segments))
    rows = []
    for k, (t, f) in enumerate(finals):
        o = ocr.get(f"{t:.2f}s") or {}
        ptext = o.get("text", "")
        chars = o.get("chars", 0)
        lo_w, hi_w = max(0.0, t - SUB_PAD), t + SUB_WIN
        sub_text = local_subs(segments, lo_w, hi_w)
        if sub_text and ptext:
            for _ in range(3):
                if len(characterize(sub_text)) >= 0.5 * len(characterize(ptext)):
                    break
                lo_w = max(0.0, lo_w - 5.0)
                hi_w += 5.0
                sub_text = local_subs(segments, lo_w, hi_w)
        rows.append({"t": t, "frame": str(f), "ocr_chars": chars,
                     "ptext": ptext, "sub_text": sub_text,
                     "seg": scores[min(int(t // BLOCK), len(scores) - 1)]})
    tm["ocr_lo_s"] = round(time.perf_counter() - t0, 1)

    t0 = time.perf_counter()
    judged: dict[int, dict] = {}
    page_texts = [(k, r["t"], r["ptext"]) for k, r in enumerate(rows)
                  if r["ocr_chars"] >= MIN_CHARS and r["ptext"]]
    if use_llm and page_texts:  # 无字幕模式也页判（NO_SUB 提示词）
        judged = judge_pages(segments, page_texts, title=meta["title"])
        log(f"  页判 {len(judged)}/{len(page_texts)} 页")
    tm["judge_s"] = round(time.perf_counter() - t0, 1)

    pages = []
    for k, r in enumerate(rows):
        t = r["t"]
        if r["ocr_chars"] < MIN_CHARS:
            pages.append({**r, "inc": 0.0, "judge": 0, "judge_why": "",
                          "decision": "drop", "why": "无文字页"})
            continue
        pa = page_increment(r["ptext"], r["sub_text"]) if segments else None
        inc = pa.increment if pa else 0.0
        jv = judged.get(k, {}).get("v", 0)
        jwhy = judged.get(k, {}).get("why", "")
        if not segments:  # 无字幕：全凭页判，无 pending（字符对齐失效无复核意义）
            if jv == 1:
                d, dwhy = "keep", f"页判[{jwhy[:16]}]"
            else:
                d, dwhy = "drop", f"页判不通过[{jwhy[:16]}]"
            pages.append({**r, "inc": 0.0, "hyp_only": "",
                          "judge": jv, "judge_why": jwhy,
                          "decision": d, "why": dwhy})
            continue
        if jv == 1:
            decision, why = "keep", f"页判增量[{jwhy[:16]}]"
        elif r["seg"] >= 3 and inc >= KEEP_INC:
            decision, why = "keep", f"高分段先验 inc={inc:.2f}"
        elif inc >= PENDING_INC:
            decision, why = "pending", f"字符增量待复核 inc={inc:.2f}"
        else:
            decision, why = "drop", f"与字幕同义 inc={inc:.2f}"
        pages.append({**r, "inc": inc, "hyp_only": pa.hyp_only_text,
                      "judge": jv, "judge_why": jwhy,
                      "decision": decision, "why": why})

    kept = [p for p in pages if p["decision"] == "keep"]
    kept.sort(key=lambda p: -p["inc"])
    pending = [p for p in pages if p["decision"] == "pending"]
    log(f"  决策: 保留 {len(kept)} / 待复核 {len(pending)} / 共 {len(pages)}")

    # ⑤ 高清定点重拉（仅保留帧）
    t0 = time.perf_counter()
    hd_dir = root / "hd_frames"
    need = [p["t"] for p in kept]
    have = {float(m.group(1)) for p_ in hd_dir.glob("hd_*.jpg")
            if (m := re.match(r"hd_([\d.]+)s\.jpg", p_.name))}
    need = [t for t in need if not any(abs(t - h) < 0.05 for h in have)]
    if need:
        if len(need) > 60:
            got = refetch_sequential(sess, bvid, meta["cid"], cookie,
                                     need, hd_dir)
        else:
            got = refetch_hd(sess, bvid, meta["cid"], cookie, need, hd_dir)
        log(f"  高清重拉 {len(got)}/{len(need)}")
    tm["hd_s"] = round(time.perf_counter() - t0, 1)

    # ⑥ 高清终 OCR → 整合产物
    t0 = time.perf_counter()
    hd_entries = [(f"{p['t']:.2f}s", hd_dir / f"hd_{p['t']:.2f}s.jpg")
                  for p in kept]
    ocr_hd = ocr_frames(hd_entries, root / "ocr_hd.json",
                        crop_band=bool(segments))
    fin_dir = root / "final_frames"
    if fin_dir.exists():
        shutil.rmtree(fin_dir)
    fin_dir.mkdir()
    frames_out = []
    for rank, p in enumerate(kept, 1):
        src = hd_dir / f"hd_{p['t']:.2f}s.jpg"
        name = (f"{rank:02d}_t{p['t']:.0f}s_inc{p['inc']:.2f}"
                f"{'_P' if p['seg'] >= 3 else ''}.jpg")
        if src.exists():
            shutil.copy2(src, fin_dir / name)
        htext = (ocr_hd.get(f"{p['t']:.2f}s") or {}).get("text", "")
        frames_out.append({"rank": rank, "t": p["t"], "file": name,
                           "ocr_text": htext, "increment": p["inc"],
                           "judge_why": p["judge_why"], "seg_score": p["seg"],
                           "why_kept": p["why"]})
    (root / "visual_content.json").write_text(json.dumps(
        {"bvid": bvid, "title": meta["title"], "duration": meta["duration"],
         "video_type": seg["video_type"], "video_value": seg["video_value"],
         "frames": frames_out}, ensure_ascii=False, indent=1), encoding="utf-8")
    tm["final_ocr_s"] = round(time.perf_counter() - t0, 1)
    tm["total_s"] = round(time.perf_counter() - T0, 1)

    # GT 自检
    gt_eval = None
    if gt:
        kept_ts = [p["t"] for p in kept]
        pend_ts = [p["t"] for p in pending]
        hit, miss = [], []
        for g in gt:
            m_ = next((x for x in kept_ts if abs(x - g) <= 4.0), None)
            if m_ is not None:
                hit.append(g)
            elif any(abs(x - g) <= 4.0 for x in pend_ts):
                miss.append(f"{g}(在pending)")
            else:
                miss.append(g)
        extras = [x for x in kept_ts
                  if not any(abs(x - g) <= 4.0 for g in gt)]
        gt_eval = {"gt": gt, "hit": hit, "miss": miss, "extras": extras}
        log(f"  GT: 命中 {len(hit)}/{len(gt)} 漏 {miss} "
            f"多余 {len(extras)}: {[round(x,1) for x in extras]}")

    logj = {"bvid": bvid, "title": meta["title"],
            "duration": meta["duration"], "video_type": seg["video_type"],
            "video_value": seg["video_value"], "scores": scores,
            "windows": wins, "stats": st, "timings": tm,
            "n_candidates": len(finals), "n_pages": len(pages),
            "kept": [p["t"] for p in kept],
            "pending": [p["t"] for p in pending], "pages": pages,
            "gt_eval": gt_eval}
    (root / "pipeline2_log.json").write_text(
        json.dumps(logj, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"  完成 {tm['total_s']}s "
        f"(预判{tm['prescore_s']} 扫描{tm['scan_s']} 结构{tm['structural_s']} "
        f"低OCR{tm['ocr_lo_s']} 页判{tm['judge_s']} 高清{tm['hd_s']} "
        f"终OCR{tm['final_ocr_s']})")

    if clean_lo:
        shutil.rmtree(lo_dir, ignore_errors=True)
    return logj


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("bvids", nargs="+")
    ap.add_argument("--gt", type=str, default="")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--rescan", action="store_true")
    ap.add_argument("--clean-lo", action="store_true")
    args = ap.parse_args()
    gt = [float(x) for x in args.gt.split(",")] if args.gt else None
    summary = []
    for bv in args.bvids:
        try:
            r = process(bv, use_llm=not args.no_llm, rescan=args.rescan,
                        clean_lo=args.clean_lo,
                        gt=gt if len(args.bvids) == 1 else None)
            summary.append({"bvid": bv, "kept": r["kept"],
                            "pending": r["pending"],
                            "total_s": r["timings"]["total_s"]})
        except Exception as e:
            summary.append({"bvid": bv, "error": str(e)[:200]})
    print("RESULT", json.dumps(summary, ensure_ascii=False))
