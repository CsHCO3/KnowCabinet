"""
最终选帧器：四件套结合，产出 final_frames/（带理由命名）+ pipeline_log.json
====================================================================
① 字幕 LLM 判有效时间段（glm-flash，一视频一次，零帧成本）
② 帧间相似度（dHash 平台 + 运动兜底，scan_frames 已有）
③ 页面逐步增量关系（方向性覆盖合并，select_v2 已有）
④ OCR↔字幕字符对齐（align_pages.py，抄自 SlideSpeech WER）

决策规则（GT 视频校准后冻结，见 REPORT.md）:
  - OCR 字符 < MIN_CHARS → 无文字页，弃
  - LLM 判死段（片头广告/成果展示/纯演示）→ 弃
  - increment >= INC_THRESHOLD 或落在 LLM 有效段 → 保留

用法:
  python finalize.py BV1pzjy6GEkC [BV...] [--scan] [--no-llm] [--thr 0.35]
产物: out/<bvid>/final_frames/*.jpg, out/<bvid>/pipeline_log.json
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

from PIL import Image

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from .scan_frames import (  # noqa: E402
    STILL_THR, dhash_bits, get_cookie, get_video_meta, hamming,
    make_session, pick_stream, refetch_hd, refetch_sequential, scan_lowres)
from .select_v2 import cell_sig, coverage_merge, run_ocr  # noqa: E402
from .align_pages import page_increment  # noqa: E402

FPS = 2.0
GAP_MIN = 2.0
MOTION_EVERY = 2.0
SUBTITLE_BAND = 0.22
MIN_CHARS = 10
from .paths import SUB_DIR


def log(msg):
    print(f"[fin] {msg}", flush=True)


SEM_GAP_THR = 0.45  # 页面↔字幕窗口语义差阈值（嵌入余弦），GT 视频校准后冻结
EMB_URL = "https://open.bigmodel.cn/api/paas/v4/embeddings"
EMB_MODEL = "embedding-3"


def embed_texts(texts: list[str], batch: int = 16) -> dict:
    """批量嵌入，返回 {原文: 向量}。GLM 免费档不含 embedding（1113 余额不足），
    熔断后仅靠字符对齐。"""
    import requests
    import time as _time
    from backend.config.settings import Settings
    key = Settings().glm_api_key
    out: dict = {}
    for i in range(0, len(texts), batch):
        chunk = texts[i:i + batch]
        for attempt in range(3):
            try:
                r = requests.post(EMB_URL, headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                }, json={"model": EMB_MODEL, "input": chunk}, timeout=60)
                if r.status_code == 429 and "1113" in r.text:
                    log("  嵌入余额不足，语义层停用（仅字符对齐）")
                    return out
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code}: {r.text[:120]}")
                data = r.json()["data"]
                for item, text in zip(data, chunk):
                    out[text] = item["embedding"]
                break
            except Exception as e:
                log(f"  嵌入异常({attempt+1}/3): {str(e)[:100]}")
                _time.sleep(2 * (attempt + 1))
        _time.sleep(0.2)
    return out


# ---------- ① 字幕 LLM 判段 ----------

LLM_PROMPT = """你是视频画面提取助手。下面是一个B站视频的标题、时长和带时间戳的字幕。
请判断：哪些时间段画面里可能出现【字幕之外的增量信息】（架构图/流程图/示意图/关键界面/重要数字），
哪些时间段是【死段】（片头片尾广告、AI生成成果展示、纯软件操作演示、无画面价值的过场）。

规则：
- 字幕在念关键词而画面可能只是把话放大显示 → 死段
- 字幕说"如图/这个架构/看这个流程/它包括这些模块" → 有效段
- 纯讲述无图示迹象 → 中性段（不算死段也不算有效）
只输出 JSON：{{"video_value": 0-10, "video_reason": "...", "valuable": [["ss", "ee", "理由"], ...], "dead": [["ss", "ee", "理由"], ...]}}
时间用秒(数字)。宁可少标，不确定就不标。

标题: {title}
时长: {dur}秒
字幕:
{subs}"""


_LLM_DEAD: set[str] = set()  # 熔断：撞限额(1308)/持续挂的模型键，本进程内不再尝试


def llm_call_raw(prompt: str, max_tokens: int = 4000) -> str | None:
    """glm-flash 优先、deepseek-v4-flash 兜底的健壮调用；返回文本或 None。
    429/1308（5小时限额）这类非瞬态错误直接熔断该键，避免每次白等重试。"""
    import time as _time
    from backend.config.settings import Settings
    s = Settings()
    for key in ("glm-flash", "v4flash"):
        if key in _LLM_DEAD:
            continue
        try:
            client = s.get_llm_client(key)
        except Exception:
            continue
        for attempt in range(3):
            try:
                resp = client.call([{"role": "user", "content": prompt}],
                                   max_tokens=max_tokens, temperature=0.2)
            except Exception as e:
                msg = str(e)
                if "1308" in msg:  # 限额窗口未到，重试无意义
                    _LLM_DEAD.add(key)
                    log(f"  LLM[{key}] 限额熔断，转兜底")
                    break
                log(f"  LLM[{key}] 异常({attempt+1}/3): {msg[:100]}")
                _time.sleep(2 * (attempt + 1))
                continue
            if (resp or "").strip():
                return resp
            log(f"  LLM[{key}] 空正文，重试({attempt+1}/3)")
            _time.sleep(3 * (attempt + 1))
    return None


def parse_json_loose(resp: str):
    m = re.search(r"\{.*\}", resp, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    m = re.search(r"\[.*\]", resp, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None


def llm_segments(title: str, dur: int, segments: list[dict]) -> dict | None:
    lines = []
    for s in segments:
        t = int(s["start"])
        lines.append(f"[{t//60:02d}:{t%60:02d}] {s['text']}")
    subs = "\n".join(lines)
    if len(subs) > 28000:
        subs = subs[:28000] + "\n...[截断]"
    prompt = LLM_PROMPT.format(title=title, dur=dur, subs=subs)
    resp = llm_call_raw(prompt)
    if not resp:
        return None
    r = parse_json_loose(resp)
    if isinstance(r, dict):
        return r
    log(f"  判段无 JSON: {resp[:120]!r}")
    return None


def in_ranges(t: float, ranges: list) -> tuple[bool, str]:
    for r in ranges:
        try:
            s, e = float(r[0]), float(r[1])
            why = r[2] if len(r) > 2 else ""
        except (ValueError, TypeError):
            continue
        if s - 2 <= t <= e + 2:
            return True, why
    return False, ""


PAGE_JUDGE_PROMPT = """你是视频画面提取助手。下面是一段讲解视频的完整字幕，以及从画面中OCR出的各页文字（带编号和时间）。
逐页判断该页画面相对字幕是否提供了【增量信息】：
- incremental=1：架构图/流程图/示意图/关键界面/数据图表/具体模型或工具名——字幕没展示或没细说的东西
- incremental=0：只是把字幕原话/关键词放大显示、纯装饰、乱码、或与字幕完全同义
只输出 JSON 数组：[{{"i": 页编号, "v": 0或1, "why": "一词理由"}}]，每页一条，不要遗漏。

字幕:
{subs}

页面OCR:
{pages}"""


def llm_page_judge(title: str, segments: list[dict],
                   page_texts: list[tuple[int, float, str]]) -> dict[int, dict]:
    """批量页面语义判定（glm-flash→deepseek 兜底）。返回 {页序号: {v, why}}。"""
    sub_lines = []
    for s in segments:
        t = int(s["start"])
        sub_lines.append(f"[{t//60:02d}:{t%60:02d}] {s['text']}")
    subs = "\n".join(sub_lines)
    if len(subs) > 20000:
        subs = subs[:20000] + "\n...[截断]"
    plines = []
    for idx, t, text in page_texts:
        plines.append(f"#{idx} t={t:.0f}s: {text[:150]}")
    prompt = PAGE_JUDGE_PROMPT.format(subs=subs, pages="\n".join(plines))
    resp = llm_call_raw(prompt, max_tokens=8000)
    if not resp:
        return {}
    r = parse_json_loose(resp)
    if isinstance(r, list):
        try:
            return {int(x["i"]): x for x in r if "i" in x}
        except (KeyError, ValueError):
            return {}
    log(f"  页判无 JSON 数组: {resp[:120]!r}")
    return {}


# ---------- ②③ 结构终帧（平台 + 运动兜底 + 覆盖合并） ----------

def structural_finals(root: Path) -> list[dict]:
    lo_files = sorted((root / "lo_frames").glob("lo_*.jpg"))
    if not lo_files:
        raise RuntimeError(f"{root}: 无 lo_frames，先跑 scan_frames.py --keep-lo")
    hashes = [dhash_bits(p) for p in lo_files]
    dists = [hamming(hashes[i], hashes[i + 1]) for i in range(len(hashes) - 1)]
    spans = []
    i = 0
    while i < len(dists):
        if dists[i] <= STILL_THR:
            j = i
            while j + 1 < len(dists) and dists[j + 1] <= STILL_THR:
                j += 1
            if (j + 1 - i) / FPS >= 1.0:
                spans.append((i, j + 1))
            i = j + 1
        else:
            i += 1
    pts = [b for _, b in spans]
    prev = 0
    gaps = []
    for a, b in spans:
        if a - prev >= GAP_MIN * FPS:
            gaps.append((prev, a))
        prev = b + 1
    if len(lo_files) - prev >= GAP_MIN * FPS:
        gaps.append((prev, len(lo_files)))
    for g0, g1 in gaps:
        k = 0
        while g0 / FPS + k * MOTION_EVERY < g1 / FPS:
            fi = min(int(round((g0 / FPS + k * MOTION_EVERY) * FPS)),
                     len(lo_files) - 1)
            pts.append(fi)
            k += 1
    pts = sorted(set(pts))
    sigs = [cell_sig(lo_files[i]) for i in pts]
    page_id, is_final, _ = coverage_merge(sigs, cover_thr=0.80)
    return [{"t": round(pts[i] / FPS, 2), "kind": "final"}
            for i in range(len(pts)) if is_final[i]]


# ---------- OCR（裁字幕带，缓存） ----------

def ocr_pages(hd_dir: Path, times: list[float], cache: Path) -> dict:
    old = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else {}
    missing = [t for t in times if f"hd_{t:.2f}s.jpg" not in old]
    if not missing:
        return old
    tmp = Path(tempfile.mkdtemp(prefix="ocr_crop_"))
    try:
        for t in missing:
            p = hd_dir / f"hd_{t:.2f}s.jpg"
            if not p.exists():
                continue
            im = Image.open(p)
            w, h = im.size
            im.crop((0, 0, w, h - int(h * SUBTITLE_BAND))).save(tmp / p.name,
                                                                quality=90)
        raw = run_ocr(tmp)
    finally:
        for f in tmp.glob("*.jpg"):
            f.unlink()
        if tmp.exists():
            tmp.rmdir()
    old.update({k: v for k, v in raw.items() if v.get("chars", -1) >= 0})
    cache.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    return old


# ---------- 主流程 ----------

def process(bvid: str, do_scan: bool, use_llm: bool, thr: float) -> dict:
    root = Path(__file__).resolve().parent / "out" / bvid
    hd_dir = root / "hd_frames"
    fin_dir = root / "final_frames"
    sess = make_session(get_cookie())
    meta = get_video_meta(sess, bvid)
    log(f"{bvid}: {meta['title'][:36]} {meta['duration']}s")

    # ② 扫描（无 lo_frames 时现场扫）
    lo_dir = root / "lo_frames"
    if not lo_dir.exists() or not list(lo_dir.glob("lo_*.jpg")):
        if not do_scan:
            raise RuntimeError("无 lo_frames 且未加 --scan")
        low = pick_stream(sess, bvid, meta["cid"], "low")
        scan_lowres(str(low["url"]), get_cookie(), lo_dir, FPS)
        log(f"  扫描 {len(list(lo_dir.glob('lo_*.jpg')))} 帧完成")

    # ③ 结构终帧
    finals = structural_finals(root)
    log(f"  结构终帧 {len(finals)}")

    # 高清补拉（已有跳过）
    have = {float(m.group(1)) for p in hd_dir.glob("hd_*.jpg")
            if (m := re.match(r"hd_([\d.]+)s\.jpg", p.name))}
    need = [c["t"] for c in finals
            if not any(abs(c["t"] - h) < 0.05 for h in have)]
    if need:
        # 交叉点：候选 > 60 时顺序单遍拉远快于逐候选定位
        if len(need) > 60:
            log(f"  顺序单遍补拉 {len(need)} 帧（跨度 "
                f"{need[-1]-need[0]:.0f}s）...")
            got = refetch_sequential(sess, bvid, meta["cid"], get_cookie(),
                                     need, hd_dir)
        else:
            log(f"  定点补拉 {len(need)} 帧 ...")
            got = refetch_hd(sess, bvid, meta["cid"], get_cookie(), need,
                             hd_dir)
        log(f"  补拉命中 {len(got)}/{len(need)}")

    # OCR（裁字幕带，带缓存）
    ocr = ocr_pages(hd_dir, [c["t"] for c in finals], root / "ocr_pages.json")

    # 字幕
    sub_path = SUB_DIR / f"{bvid}.json"
    segments = []
    if sub_path.exists():
        segments = json.loads(sub_path.read_text(encoding="utf-8")).get("segments") or []
    else:
        log("  ⚠ 无字幕文件，对齐退化为纯 LLM 先验")

    # ① LLM 判段
    llm = None
    if use_llm and segments:
        log("  LLM 判段 ...")
        llm = llm_segments(meta["title"], meta["duration"], segments)
        if llm:
            log(f"  LLM: value={llm.get('video_value')} "
                f"有效段{len(llm.get('valuable') or [])} 死段{len(llm.get('dead') or [])}")

    # ④ 对齐 + 打分 + 决策
    # 第一遍：收集每页的页面文本与对齐窗口字幕文本
    SUB_PAD = 3.0
    ts = [c["t"] for c in finals]
    rows = []
    for k, c in enumerate(finals):
        t = c["t"]
        span_end = ts[k + 1] if k + 1 < len(ts) else float(meta["duration"])
        o = ocr.get(f"hd_{t:.2f}s.jpg") or {}
        ptext = o.get("text", "")
        chars = o.get("chars", 0)
        lo = max(0.0, t - SUB_PAD)
        hi = span_end + SUB_PAD
        sub_text = " ".join(s["text"] for s in segments
                            if s["start"] < hi and s["end"] > lo)
        # 窗口膨胀：字幕字数至少达到页面 OCR 字数的一半，否则外扩（上限+15s）
        if sub_text and ptext:
            from .align_pages import characterize
            for _ in range(3):
                if len(characterize(sub_text)) >= 0.5 * len(characterize(ptext)):
                    break
                lo = max(0.0, lo - 5.0)
                hi = hi + 5.0
                sub_text = " ".join(s["text"] for s in segments
                                    if s["start"] < hi and s["end"] > lo)
        rows.append({"t": t, "span": [t, round(span_end, 2)],
                     "ocr_chars": chars, "ptext": ptext, "sub_text": sub_text})

    # 第二遍：页面语义判定（glm-flash 批量，替代无余额的 embedding；
    # 字符对齐抓字面复用，LLM 判定抓跨语言同义与架构增量）
    judge: dict[int, dict] = {}
    page_texts = [(k, r["t"], r["ptext"]) for k, r in enumerate(rows)
                  if r["ocr_chars"] >= MIN_CHARS and r["ptext"]]
    if use_llm and segments and page_texts:
        CHUNK = 50
        for ci in range(0, len(page_texts), CHUNK):
            log(f"  页面判定 {ci+1}~{min(ci+CHUNK, len(page_texts))}"
                f"/{len(page_texts)} ...")
            judge.update(llm_page_judge(meta["title"], segments,
                                        page_texts[ci:ci + CHUNK]))

    # 第三遍：决策
    pages = []
    for k, r in enumerate(rows):
        t = r["t"]
        valuable, v_why = (False, "")
        dead, d_why = (False, "")
        if llm:
            valuable, v_why = in_ranges(t, llm.get("valuable") or [])
            dead, d_why = in_ranges(t, llm.get("dead") or [])
        jv = judge.get(k, {}).get("v", 0)
        jwhy = judge.get(k, {}).get("why", "")
        if r["ocr_chars"] < MIN_CHARS:
            inc = 0.0
            decision, why = "drop", "无文字页"
        else:
            pa = page_increment(r["ptext"], r["sub_text"])
            inc = pa.increment
            if dead:
                decision, why = "drop", f"LLM死段:{d_why[:24]}"
            elif valuable:
                decision = "keep"
                why = f"先验[{v_why[:20]}] inc={inc}"
            elif jv == 1:
                decision = "keep"
                why = f"页判增量[{jwhy[:16]}] inc={inc}"
            elif inc >= 0.5:
                decision = "pending"
                why = f"字符增量待复核 inc={inc} 独有[{pa.hyp_only_text[:20]}]"
            else:
                decision = "drop"
                why = f"与字幕同义 inc={inc}"
        pages.append({"t": t, "span": r["span"], "ocr_chars": r["ocr_chars"],
                      "increment": inc, "judge": jv, "judge_why": jwhy,
                      "hyp_only": pa.hyp_only_text if r["ocr_chars"] >= MIN_CHARS else "",
                      "valuable": valuable, "dead": dead,
                      "decision": decision, "why": why})

    # 产出 final_frames/
    if fin_dir.exists():
        shutil.rmtree(fin_dir)
    fin_dir.mkdir()
    kept = sorted([p for p in pages if p["decision"] == "keep"],
                  key=lambda p: -p["increment"])
    for rank, p in enumerate(kept, 1):
        src = hd_dir / f"hd_{p['t']:.2f}s.jpg"
        if src.exists():
            shutil.copy2(src, fin_dir /
                         f"{rank:02d}_t{p['t']:.0f}s_inc{p['increment']:.2f}"
                         f"{'_P' if p['valuable'] else ''}.jpg")
    log(f"  保留 {len(kept)}/{len(pages)} → final_frames/")

    logj = {"bvid": bvid, "title": meta["title"], "duration": meta["duration"],
            "thr": thr, "llm": llm, "pages": pages,
            "kept": [p["t"] for p in kept]}
    (root / "pipeline_log.json").write_text(
        json.dumps(logj, ensure_ascii=False, indent=1), encoding="utf-8")
    return logj


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("bvids", nargs="+")
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--thr", type=float, default=0.35)
    args = ap.parse_args()
    for bv in args.bvids:
        try:
            r = process(bv, do_scan=args.scan, use_llm=not args.no_llm,
                        thr=args.thr)
            print("RESULT", json.dumps({"bvid": bv, "kept": r["kept"]},
                                       ensure_ascii=False))
        except Exception as e:
            print("RESULT", json.dumps({"bvid": bv, "error": str(e)[:200]},
                                       ensure_ascii=False))
