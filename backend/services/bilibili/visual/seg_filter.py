# -*- coding: utf-8 -*-
"""按“先筛后抽”的流程：LLM 分段+打分 → 只扫高分段 → 结构去重 → 高清重拉。

用法:
  python -m backend.services.bilibili.visual.seg_filter sections BV1xxx [...]   # 只跑分段打分
  python -m backend.services.bilibili.visual.seg_filter scan BV1xxx --thr 3     # 只扫高分段
"""
from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path



def _mkdtemp(prefix=None, suffix=None, dir=None):
    """沙箱里 %TEMP% 下的子目录不可写（mkdir 出来的目录 PermissionError），
    统一改到工作区内。pipeline.ocr_frames 走的就是 tempfile.mkdtemp。"""
    base = Path(dir or str(PROJECT_ROOT / "temp"))
    p = base / ((prefix or "tmp_") + uuid.uuid4().hex[:8])
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


tempfile.mkdtemp = _mkdtemp

from backend.config.settings import Settings  # noqa: E402

from .paths import VISUAL_ROOT as ROOT, PROJECT_ROOT, SUB_DIR as SUB
from .paths import kb_id, subtitle_cache_path
from .llm import no_think, parse_json_loose

# "大字标题"闸门：这些类型里 OCR 去掉空格后不足 N 个字，说明画面只是把旁白放大了一遍。
# 架构图/数据图表 不受限——纯图可能一个字都没有。
TEXTY_TYPES = {"文字要点页", "提示词或文档截图", "软件界面", "标题卡"}
MIN_TEXT_CHARS = 15
# 类型稳定、keep 不稳：干脆按类型定生死，VLM 的 keep 只在未知类型时兜底。
HARD_DROP_TYPES = {"人脸/现场照片", "动画插画", "标题卡", "空镜"}
HARD_KEEP_TYPES = {"架构图/流程图", "数据图表"}
# 画面文字有这么大比例能在附近字幕里找到 → 只是把旁白放大了一遍（章节卡/标语卡）
SUB_COVER_MAX = 0.70
# 低清先判、高清后拉：省掉绝大部分高清重拉
LOWRES_FIRST = True

PROMPT = """你在做视频抽帧的前置筛选。给你标题和带时间戳的字幕。

第一步：判断这条视频的主旨与目的——它拍给谁看、想让观众得到什么。
genre 只能选一个：课程讲解 / 技术拆解 / 访谈对话 / 产品发布或宣传 / 操作教程 / Vlog·日常 / 新闻资讯
expect 只能选一个：0-2 / 3-5 / 6+，表示整条视频里大概有几张"讲义页"。

第二步：按内容单元切全片（同一件事为一段，30~180 秒，首尾相接覆盖全片）。
第三步：给每段打 0-3 分。**只判断一件事：这段画面里有没有整页的幻灯片/PDF/讲义页被展示。**

3 = 这段画面上出现了整页文档（PPT/PDF/讲义/白板整理稿）：有标题、要点列表、示意图、表格，
    且字幕没有把这些字念全——页面本身是独立的信息
2 = 出现了这类页面，但字幕基本把页面上的内容说全了，页面只是复述
1 = 这段是软件操作录屏、命令行、浏览器网页、手机界面、人像口播、动画、标题卡
    ——没有整页文档展示
0 = 片头片尾、寒暄、感谢、广告

判定要点：
- 操作演示/代码窗口/网页/手机界面，即使画面里字很多，也一律 1：那是给人照着做的，不是知识图。
- 整页文档的特征：画面是一块完整的"页"，四周有边距、黑边或版式边框，正文成行成段，开头常有标题。
- 允许整片都是 1，不要为了凑数给高分。
- 3 分不超过 40%。

只输出 JSON：
{{"genre":"课程讲解","expect":"3-5","sections":[{{"t0":0,"t1":45,"topic":"这一段在讲什么","visual":"预测画面里会出现什么","score":0}}]}}

视频标题：{title}
视频时长：{dur} 秒

字幕：
{subs}"""


def load_sub(bvid: str, page: int = 1):
    d = json.loads(subtitle_cache_path(bvid, page).read_text(encoding="utf-8"))
    segs = d["segments"]
    end = int(max(s["end"] for s in segs))
    dur = int(d.get("duration") or end)
    if dur and end < dur * 0.85:
        # AI 字幕可能只生成了前一段；别把它当成整条视频
        print(f"  ⚠ 字幕只覆盖 {end}s/{dur}s，后段没有字幕")
    return d.get("title", bvid), max(dur, end), segs


def get_sections(bvid: str, model: str = "v4flash", page: int = 1) -> dict:
    title, dur, segs = load_sub(bvid, page)
    subs = "\n".join(f"[{int(s['start'])}] {s['text']}" for s in segs)[:26000]
    prompt = PROMPT.format(title=title, dur=dur, subs=subs)
    client = Settings().get_llm_client(model)
    t0 = time.time()
    resp = client.call([{"role": "user", "content": prompt}],
                       max_tokens=6000, temperature=0.2,
                       extra_body=no_think(model))
    m = re.search(r"\{.*\}", resp or "", re.S)
    try:
        obj = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        # 模型在 JSON 前后又写了说明/连吐两个对象时，贪婪匹配会拼成非法串
        obj = parse_json_loose(resp) or {}
    secs = normalize_sections(obj.get("sections", []), dur)
    out = {"bvid": bvid, "title": title, "dur": dur, "model": model,
           "genre": obj.get("genre", ""), "expect": obj.get("expect", ""),
           "elapsed": round(time.time() - t0, 1), "sections": secs}
    d = ROOT / kb_id(bvid, page)
    d.mkdir(parents=True, exist_ok=True)
    (d / "sections.json").write_text(json.dumps(out, ensure_ascii=False, indent=1),
                                     encoding="utf-8")
    lines = [f"{bvid}  {title}  {dur}s  单元 {len(secs)}  {model} {out['elapsed']}s",
             f"主旨类型={out['genre']}  预计有价值画面={out['expect']}"]
    dist = {}
    for s in secs:
        k = int(s.get("score", -1)); dist[k] = dist.get(k, 0) + 1
        lines.append(f"[{s.get('t0')}-{s.get('t1')}] {s.get('score')} "
                     f"{s.get('visual_type','')} ｜{s.get('topic','')}｜{s.get('visual','')}")
    lines.append(f"分布 {dist}")
    (d / "sections.txt").write_text("\n".join(lines), encoding="utf-8")
    return out


def normalize_sections(secs: list[dict], dur: float) -> list[dict]:
    """补齐/纠正分段的时间字段。

    glm-flash 实测会漏写 `t0`（12 段里 11 段没有），扫描阶段直接 KeyError('t0') 崩掉。
    这里按"首尾相接"的约定用上一段的 t1 补 t0，顺带丢掉时间无效的段。
    """
    out, prev = [], 0.0
    for s in secs or []:
        if not isinstance(s, dict):
            continue
        try:
            t1 = float(s.get("t1"))
        except (TypeError, ValueError):
            continue
        try:
            t0 = float(s.get("t0"))
        except (TypeError, ValueError):
            t0 = prev
        t0 = max(0.0, min(t0, t1))
        t1 = min(max(t1, 0.0), float(dur or t1))
        if t1 - t0 <= 0:
            t1 = min(float(dur or t0 + 1), t0 + 1.0)
        out.append({**s, "t0": t0, "t1": t1})
        prev = t1
    return out


def windows_from_sections(secs: list[dict], thr: float, dur: float, pad: float = 2.0):
    spans = []
    for s in secs:
        if float(s.get("score", 0)) < thr:
            continue
        # 有页的段落 2fps，其余 1fps（平台检测按窗记帧率，仍可用）
        fps = 2.0 if float(s.get("score", 0)) >= 2 else 1.0
        spans.append([max(0.0, float(s["t0"]) - pad),
                      min(dur, float(s["t1"]) + pad), fps])
    spans.sort()
    merged = []
    for a, b, fps in spans:
        if (merged and a <= merged[-1][1] + 0.5
                and abs(merged[-1][2] - fps) < 1e-6):
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b, fps])
    return [{"t0": round(a, 2), "t1": round(b, 2),
             "mode": "detect" if fps >= 2 else "slow", "fps": fps}
            for a, b, fps in merged if b - a >= 1.5]


def scan(bvid: str, thr: float, page: int = 1):
    from . import pipeline as P
    from .scan_frames import (get_cookie, get_video_meta, make_session,
                             refetch_hd, refetch_sequential)

    kid = kb_id(bvid, page)
    info = json.loads((ROOT / kid / "sections.json").read_text(encoding="utf-8"))
    dur = info["dur"]
    secs = normalize_sections(info["sections"], dur)
    segs = P.load_segments(bvid, page)
    wins = windows_from_sections(secs, thr, dur)
    covered = sum(w["t1"] - w["t0"] for w in wins)
    print(f"{bvid}: thr={thr} 选中 {len(wins)} 段 / {covered:.0f}s / {dur}s")
    if not wins:   # 没有够分的段落，别去开 ffmpeg
        report = {"bvid": bvid, "title": info["title"], "dur": dur, "thr": thr,
                  "windows": [], "covered_s": 0, "n_candidates": 0, "n_frames": 0,
                  "frames": [], "sections": secs}
        (ROOT / kid / f"report_thr{thr:g}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        return report

    cookie = get_cookie()
    sess = make_session(cookie)
    meta = get_video_meta(sess, bvid, page)

    lo_dir = ROOT / kid / f"lo_thr{thr:g}"
    # 扫描窗口一变，旧的 lo 帧就作废（窗口数/边界不同，帧名对不上，缓存会静默给出错结果）
    stamp = lo_dir / "wins.json"
    cur = json.dumps(wins, sort_keys=True)
    old = stamp.read_text(encoding="utf-8") if stamp.exists() else None
    if old != cur and lo_dir.exists():   # 没指纹的老目录也算失效
        print("  窗口变了，清掉旧缓存")
        for name in (f"lo_thr{thr:g}", f"frames_thr{thr:g}",
                     f"frames_final_thr{thr:g}", f"frames_keep_thr{thr:g}"):
            shutil.rmtree(ROOT / kid / name, ignore_errors=True)
    if not (lo_dir.exists() and any(lo_dir.glob("w*_*.jpg"))):
        P.scan_windows(P.pick_stream(sess, bvid, meta["cid"], "low")["url"],
                       cookie, lo_dir, wins)
        stamp.write_text(cur, encoding="utf-8")
    else:
        print("  扫描(缓存)")
    if not stamp.exists():
        stamp.write_text(cur, encoding="utf-8")

    # 30s 块分数：块中心落在哪个单元就用哪个分数
    n = max(1, math.ceil(dur / P.BLOCK))

    def score_at(t):
        for s in secs:
            if float(s["t0"]) <= t < float(s["t1"]):
                return int(s.get("score", 0))
        return 0

    scores = [score_at(i * P.BLOCK + 1) for i in range(n)]
    finals, st = P.structural(lo_dir, wins, scores)
    print(f"  结构：平台{st['platforms']} 兜底{st['fill']} -> {len(finals)} 候选")

    # 低清预筛：先在 360p 上判"是不是整页文档"，只把活下来的拉高清。
    # 高清重拉是整条流水线最慢的一步（每帧 ~10s），预筛能把候选砍到 1/5。
    if LOWRES_FIRST and len(finals) > 4:
        try:
            from .type_filter import classify_slide
            from . import pipeline as _P
            # 低清帧名是 w00_00042.jpg，判类器按 t<秒>s.jpg 取时间戳，先改名拷一份
            td = Path(tempfile.mkdtemp(prefix="lopre_"))
            renamed, back = [], {}
            for t, p in finals:
                dst = td / f"t{t:.1f}s.jpg"
                shutil.copy2(p, dst)
                renamed.append(dst)
                back[f"{t:.1f}"] = (t, p)
            labs = classify_slide(
                sorted(renamed, key=lambda x: float(x.stem[1:-1])),
                lambda t: _P.local_subs(segs, t - 4, t + 10)[:80])
            kept = [back[f"{t:.1f}"] for t, lab in labs.items()
                    if lab.get("slide") == 1 and f"{t:.1f}" in back]
            shutil.rmtree(td, ignore_errors=True)
            print(f"  低清预筛 {len(finals)} -> {len(kept)}")
            # 分数高的段落本该有页：低清全判 0 可能是不准，退回全量
            if kept or max(scores or [0]) < 2:
                finals = kept
            else:
                print("  低清预筛全灭但分段说有页，按全量重拉")
        except Exception as e:
            print("  低清预筛跳过:", str(e)[:100])

    hd_dir = ROOT / kid / f"frames_thr{thr:g}"
    hd_dir.mkdir(parents=True, exist_ok=True)
    ts = [t for t, _ in finals]
    got = {}
    if cookie:
        got = (refetch_sequential if len(ts) > 60 else refetch_hd)(
            sess, bvid, meta["cid"], cookie, ts, hd_dir)
    frames = []
    if got:
        for t, src in sorted(got.items()):
            dst = hd_dir / f"t{t:.1f}s.jpg"
            Path(src).replace(dst)
            frames.append({"t": t, "file": dst.name})
        # 帧换了分辨率：OCR 缓存按时间戳存，旧的低清文本会顶掉高清结果
        for f in ROOT.glob(f"{bvid}/ocr_thr*.json"):
            f.unlink()
    else:  # 无 cookie 拿不到 480p，退化用 360p 候选帧
        print("  无 cookie，高清重拉不可用，改用 360p 帧")
        for t, src in finals:
            dst = hd_dir / f"t{t:.1f}s.jpg"
            dst.write_bytes(Path(src).read_bytes())
            frames.append({"t": t, "file": dst.name})
    report = {"bvid": bvid, "title": info["title"], "dur": dur, "thr": thr,
              "windows": wins, "covered_s": round(covered, 1),
              "n_candidates": len(finals), "n_frames": len(frames),
              "frames": frames, "sections": secs}

    # 信息点去重：同一张图的不同状态合成一张（图形学优先，OCR 补充）
    try:
        from .dedup_pages import dedup, pick_rep
        ocr_path = ROOT / kid / f"ocr_thr{thr:g}.json"
        try:
            ocr = P.ocr_frames([(f"{f['t']:.2f}s", hd_dir / f["file"])
                                for f in frames], ocr_path)
        except Exception as e:
            print("  OCR 跳过:", str(e)[:100])
            ocr = {}

        def ocr_text(t):
            return (ocr.get(f"{t:.2f}s") or {}).get("text", "")

        items = [{"t": f["t"], "path": str(hd_dir / f["file"]),
                  "text": ocr_text(f["t"])} for f in frames]
        clusters = dedup(items)
        reps = sorted(pick_rep(c, items) for c in clusters)
        final_dir = ROOT / kid / f"frames_final_thr{thr:g}"
        final_dir.mkdir(parents=True, exist_ok=True)
        for f in final_dir.glob("*.jpg"):
            f.unlink()
        final = []
        for i in reps:
            src = Path(items[i]["path"])
            dst = final_dir / f"t{items[i]['t']:.1f}s.jpg"
            dst.write_bytes(src.read_bytes())
            final.append({"t": items[i]["t"], "file": dst.name})
        report["n_dedup"] = len(final)
        report["dedup_frames"] = final
        report["clusters"] = [[round(items[i]["t"], 1) for i in c] for c in clusters]
        print(f"  去重 {len(frames)} → {len(final)} 帧 -> {final_dir}")
    except Exception as e:
        print("  去重跳过:", str(e)[:120])

    # 幻灯片判定：只要"整页 PPT/PDF/讲义"，操作录屏一律不要
    if "final" in locals() and final:
        try:
            from .type_filter import classify_slide
            from .dedup_pages import text_sim
            from .dedup_pages import ui_repeats as merge_ui_repeats
            labels = classify_slide([final_dir / f["file"] for f in final],
                                    lambda t: P.local_subs(segs, t - 4, t + 10)[:80])
            if labels:
                keep_dir = ROOT / kid / f"frames_keep_thr{thr:g}"
                keep_dir.mkdir(parents=True, exist_ok=True)
                for f in keep_dir.glob("*.jpg"):
                    f.unlink()
                pool = []
                for f in final:
                    lab = labels.get(f["t"], {})
                    if int(lab.get("slide", 0)) != 1:
                        continue                       # 不是整页文档
                    txt = ocr_text(f["t"]).replace(" ", "")
                    if len(txt) < MIN_TEXT_CHARS:
                        continue                       # 标题卡/空页
                    sub = P.local_subs(segs, f["t"] - 8, f["t"] + 12)
                    if text_sim(sub, txt) >= SUB_COVER_MAX:
                        continue                       # 页面文字就是这句旁白
                    pool.append({**f, "path": str(final_dir / f["file"]),
                                 "type": "幻灯片",
                                 "text": ocr_text(f["t"]),
                                 "why": lab.get("why", "")})
                # 界面类重复收敛：同一界面反复截图只留一张
                keep, merged_ui = merge_ui_repeats(pool, labels)
                kept = []
                for f in keep:
                    dst = keep_dir / f["file"]
                    dst.write_bytes(Path(f["path"]).read_bytes())
                    kept.append({k: f[k] for k in ("t", "file", "type", "why")})
                report["type_labels"] = {str(k): v for k, v in labels.items()}
                report["n_keep"] = len(kept)
                report["keep_frames"] = kept
                report["ui_merged"] = merged_ui
                print(f"  类型过滤 {len(final)} → {len(pool)} → 去重界面 {len(kept)} 帧"
                      f"（并掉 {sum(len(m) - 1 for m in merged_ui)}）-> {keep_dir}")
        except Exception as e:
            print("  类型过滤跳过:", str(e)[:120])

    (ROOT / kid / f"report_thr{thr:g}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  高清 {len(frames)} 张 -> {hd_dir}")
    return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["sections", "scan"])
    ap.add_argument("bvids", nargs="+")
    ap.add_argument("--thr", type=float, default=3)
    ap.add_argument("--model", default="v4flash")
    a = ap.parse_args()
    for bv in a.bvids:
        if a.cmd == "sections":
            r = get_sections(bv, a.model)
            print("sections", bv, len(r["sections"]))
        else:
            scan(bv, a.thr)
