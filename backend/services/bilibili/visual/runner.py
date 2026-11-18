# -*- coding: utf-8 -*-
"""一站式编排（自沙盒 run_video.py 迁入）：字幕 → 分段 → 扫帧 → 浓缩 → 落库。

CLI 见 backend.main 的 bili-visual-scan / bili-visual-apply。
"""
from __future__ import annotations

import json
import re
import shutil
import time
import traceback
from pathlib import Path

from . import seg_filter  # noqa: F401  (必须最先导入：装 tempfile 补丁)
from .paths import SUB_DIR, VISUAL_ROOT, subtitle_cache_path, kb_id
from .seg_filter import get_sections

ROOT = VISUAL_ROOT
SUB = SUB_DIR


def ensure_subtitle(bvid: str, force: bool = False, page: int = 1) -> dict:
    """抓字幕+简介，落 data/bilibili/subtitles/<bv>.json（P2+ 为 <bv>_p<N>.json）。"""
    p = subtitle_cache_path(bvid, page)
    if p.exists() and not force:
        return json.loads(p.read_text(encoding="utf-8"))
    from backend.services.bilibili.subtitle import (
        get_bilibili_timestamped_subtitle,
        is_transient_error,
    )
    r = {}
    for i in range(3):     # 字幕轨道接口会间歇性返回空，重试几次
        r = get_bilibili_timestamped_subtitle(f"https://www.bilibili.com/video/{bvid}", page=page)
        if r.get("ok"):
            break
        err = str(r.get("error", ""))
        if not is_transient_error(err):
            # 业务性失败（视频无任何中文字幕 / cookie 失效 / 视频已删）：重试只是白等
            raise RuntimeError(f"抓字幕失败 {bvid}: {err}（业务性失败，不重试）")
        time.sleep(3 + 3 * i)
    if not r.get("ok"):
        raise RuntimeError(f"抓字幕失败 {bvid}: {r.get('error')}（已重试 3 次）")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  字幕 {len(r['segments'])} 段 / {r.get('duration')}s -> {p.name}")
    if r.get("page_count", 1) > 1:
        print(f"  注意：该视频有 {r['page_count']} 个分P（当前只处理 P{r.get('page', 1)}"
              f"「{r.get('page_title') or ''}」）。分P 指定见 bili-info \"{bvid}?p=2\"")
    return r


def subtitle_coverage(bvid: str, page: int = 1) -> float:
    p = subtitle_cache_path(bvid, page)
    if not p.exists():
        return 0.0
    d = json.loads(p.read_text(encoding="utf-8"))
    segs = d.get("segments") or []
    end = max((s["end"] for s in segs), default=0)
    dur = d.get("duration") or end
    return (end / dur) if dur else 1.0


MIN_SUBTITLE_COVERAGE = 0.15   # 覆盖率低于此值 → 判定"字幕不可用"（CLI --min-sub-cover 可临时改）
# 阈值依据：BV1Ghpy6gE5x 覆盖 7.3%（缓存里只有几段无关台词，跳过）；BV1Pkx76ZEQG 覆盖 98.9%（放行）

# reranker 相关性分高于此值就不再问 LLM（标定：真视频 +2.65~+5.96，错配 −8.42~−3.53）
TOPIC_CLEAR_LOGIT = 2.0


def subtitle_usable(bvid: str, page: int = 1,
                    min_cover: float = MIN_SUBTITLE_COVERAGE) -> tuple[bool, str]:
    """字幕值不值得往下跑。返回 (可用?, 原因)。

    字幕是分块与浓缩的讲述骨架：覆盖率太低（比如缓存里只有几段台词）就没有骨架，
    往下跑只会烧扫帧时间和模型钱，产出的 digest 讲的也不是这条视频。只读缓存，不联网。
    """
    p = subtitle_cache_path(bvid, page)
    if not p.exists():
        return False, "没有字幕缓存"
    d = json.loads(p.read_text(encoding="utf-8"))
    dur = d.get("duration") or 0
    if not dur:
        return True, "时长未知，保守放行"
    segs = d.get("segments") or []
    end = max((s["end"] for s in segs), default=0)
    cov = end / dur
    if cov < min_cover:
        return False, f"字幕只覆盖 {cov:.0%}（{end:.0f}s/{dur}s），判定不可用"

    # 第二道闸：覆盖率够，但内容未必是这条视频的。
    # 已见两种：BV1mqso66E26 抓成歌词、BV1JGt869EEi 抓成另一条纪录片（讲珊瑚）。
    # reranker 便宜（本地 1 秒），先看；分数落在模糊带才让 LLM 复核。
    from ..quality import judge_subtitle_llm, topic_score, video_topic
    title = (d.get("title") or "").strip()
    if not title:            # 缓存里没有标题就判不了主题，只按覆盖率放行
        return True, f"字幕覆盖 {cov:.0%}"
    body = " ".join(s.get("text", "") for s in segs)
    topic = video_topic(bvid, title)
    score = topic_score(topic, body)
    if score is not None and score >= TOPIC_CLEAR_LOGIT:
        return True, f"字幕覆盖 {cov:.0%}，相关性 {score:+.1f}"
    same, why = judge_subtitle_llm(topic, body)
    if same is False:
        return False, f"字幕不像这条视频的（{why}）"
    if same is None and score is not None and score < 0:
        # 复核接口自己挂了（429/超时）：这是基础设施故障，不能让整条视频静默消失。
        # 按本地 reranker 分数决定：强负（≤-5）当错轨处理（下游会去试 ASR），弱负先放行。
        if why.startswith("判不了"):
            if score <= -5:
                return False, f"字幕像错轨（相关性 {score:+.1f}，复核接口不可用：{why[:30]}）"
            return True, f"字幕覆盖 {cov:.0%}，复核接口不可用（{why[:30]}），先放行"
        return False, f"字幕相关性 {score:+.1f}，模型又判不了（{why}）"
    return True, f"字幕覆盖 {cov:.0%}，相关性 {score if score is None else f'{score:+.1f}'}"


def push_desc_resources(bvid: str, info: dict) -> int:
    """简介里的 GitHub 等地址 → 备选调研方向。"""
    from . import resources
    items = resources.extract(info.get("desc", ""), title=info.get("title", bvid))
    if not items:
        print("  简介里没有资源地址")
        return 0
    n = resources.push(items, source="bilibili", source_ref=f"{bvid} {info.get('title','')}")
    print(f"  资源 {len(items)} 条，新增 {n}")
    return n


def apply_kb(bvid: str, page: int = 1) -> bool:
    """落库：bili.db 标注 + 写 knowledgebase md + 拷图。"""
    try:
        from . import apply_kb as AK
        return AK.apply_one(bvid, force=True, page=page)
    except Exception as e:
        print("  落库失败:", str(e)[:120])
        return False


def index_kb(python_exe: str = r"D:/Anaconda/envs/infosystem/python.exe") -> None:
    """重建目录层/分块/向量。整批跑完做一次就够，单条跑会重复扫全库。"""
    import subprocess
    for cmd in ("kb-scan", "kb-embed"):
        t0 = time.time()
        print(f"  索引: {cmd} ...")
        r = subprocess.run([python_exe, "-m", "backend.main", cmd],
                           cwd=str(Path(__file__).resolve().parents[4]))
        print(f"  {cmd} {'完成' if r.returncode == 0 else '失败'} "
              f"({time.time() - t0:.0f}s)")


def visual_budget(bvid: str, page: int = 1) -> tuple[bool, str]:
    """画面必要性诊断：分段里只要有一段被判成"整页文档展示"，才值得去扫帧。

    实测 14 条视频里只有 2 条最终真的用上了截图；按"有没有 3 分段落"卡，
    能省掉 86% 的扫帧（每条 3~10 分钟）。判断依据全部来自字幕，不需要碰视频流。
    """
    sp = ROOT / kb_id(bvid, page) / "sections.json"
    if not sp.exists():
        return True, "没有分段结果，保守起见扫一遍"
    sec = json.loads(sp.read_text(encoding="utf-8"))
    secs = sec.get("sections") or []
    n3 = sum(1 for s in secs if float(s.get("score", 0)) >= 3)
    tag = f"genre={sec.get('genre', '?')} expect={sec.get('expect', '?')}"
    if n3 == 0:
        return False, f"全片没有一处判成整页文档（{tag}）"
    return True, f"{n3}/{len(secs)} 段判成整页文档（{tag}）"


def probe_page_count(bvid: str, n: int = 8, quality: str = "hd",
                     page: int = 1) -> tuple[int, str]:
    """便宜探测：全片等距抽 n 帧，数其中几张是"整页文档"。返回 (命中数, 说明)。

    两个用途，方向相反：
    - 分段说没页时用来救回漏判（BV1Kppn6tEem 有 42 张 PPT，分段却全判 1 分）；
    - 分段说有页时用来否决（实测 8 条：6 条判对，2 条误报成"有页"，0 条漏判，
      见下方 veto 规则）。

    360p 抽样不可靠（早期两个样本都判反），所以直接拉 1080p。
    """
    try:
        import tempfile as _tf
        from . import pipeline as P
        from .scan_frames import (get_cookie, get_video_meta, make_session,
                                  refetch_hd)
        from .seg_filter import HARD_KEEP_TYPES
        from .type_filter import classify

        segs = P.load_segments(bvid, page)
        dur = max(s["end"] for s in segs)
        if dur < 30:
            return n, f"视频只有 {dur:.0f}s，直接扫"
        ts = [dur * (i + 1) / (n + 1) for i in range(n)]
        cookie = get_cookie()
        sess = make_session(cookie)
        meta = get_video_meta(sess, bvid, page)
        tmp = Path(_tf.mkdtemp(prefix="probe_"))
        got = refetch_hd(sess, bvid, meta["cid"], cookie, ts, tmp, quality=quality)
        if not got:
            return n, "抽样没抓到帧，保守起见扫一遍"
        named = tmp / "named"
        named.mkdir(exist_ok=True)
        frames = []
        for t, src in sorted(got.items()):
            dst = named / f"t{float(t):.1f}s.jpg"
            shutil.copy2(src, dst)
            frames.append(dst)
        # 用与正式扫描同一套类型判定：整页文档、架构图/流程图、数据图表都算"有画面"。
        # 早先只数 slide==1（纯整页文档），结果把"画架构图但没有整页 PPT"的视频全否决了
        # （BV1kr4h6fExw 实测：抽样 0 命中 → 否决；强制扫描留下 8 张图）。
        labs = classify(frames, lambda t: P.local_subs(segs, t - 4, t + 10)[:80])
        if not labs:
            return n, "抽样分类失败，保守起见扫一遍"
        hits = sum(1 for v in labs.values()
                   if int(v.get("keep") or 0) == 1 or v.get("type") in HARD_KEEP_TYPES)
        shutil.rmtree(tmp, ignore_errors=True)
        if hits:
            return hits, f"抽样 {len(frames)} 帧里 {hits} 帧有可读文字/结构图"
        return 0, f"抽样 {len(frames)} 帧都没有可读文字/结构图"
    except Exception as e:
        return n, f"抽样失败（{str(e)[:40]}），保守起见扫一遍"


def probe_pages(bvid: str, n: int = 8, quality: str = "hd",
                page: int = 1) -> tuple[bool, str]:
    """probe_page_count 的布尔版本（有页/没页）。"""
    hits, why = probe_page_count(bvid, n, quality, page=page)
    return bool(hits), why


def cleanup(bvid: str, keep_temps: bool = False, page: int = 1) -> None:
    """销毁过程图：只留 frames_keep_*，其余帧目录整棵删掉。"""
    if keep_temps:
        print("  --keep-temps：中间帧保留")
        return
    root = ROOT / kb_id(bvid, page)
    freed = 0
    if root.exists():
        for p in list(root.iterdir()):
            if p.is_dir() and p.name.startswith(("lo_thr", "frames_thr", "frames_final_thr")):
                freed += sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
                shutil.rmtree(p, ignore_errors=True)
        for p in list(root.glob("ocr_thr*.json")):
            freed += p.stat().st_size
            p.unlink()
    print(f"  清理中间帧 {freed / 1e6:.1f} MB，保留 frames_keep_*")


def _cache_page_count(bvid: str, page: int = 1) -> int:
    """从字幕缓存读 page_count（只读盘不联网）。--from digest 这类跳过抓取段的
    入口没人写这个账，先按缓存补一版；fetch 路径随后会用新抓的 info 覆写。"""
    p = subtitle_cache_path(bvid, page)
    if not p.exists():
        return 1
    try:
        return int(json.loads(p.read_text(encoding="utf-8")).get("page_count") or 1)
    except Exception:
        return 1


def _subtitle_body_chars(bvid: str, page: int = 1) -> int:
    """字幕正文字数。口径与 quality.subtitle_too_short 相同：全部 segments 拼起来算长度
    （SPEC §2 条件3）。只读盘不联网。"""
    p = subtitle_cache_path(bvid, page)
    if not p.exists():
        return 0
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    return len(" ".join(s.get("text", "") for s in d.get("segments") or []))


def _should_try_asr(why: str, sub_cover: float, body_chars: int) -> bool:
    """SPEC §2 唯一决策表（只在闸门判不可用之后来问）。

    无轨 / 离题 / 正文<200字 / 覆盖<0.5 → 触发；覆盖 0.5~0.8 不触发——骨架还在，
    全片扫帧正好补画面，为补后段花小时级 GPU 不划算（SPEC §2 第4/5条的取舍）。
    """
    from ..quality import MIN_SUBTITLE_CHARS
    if body_chars < MIN_SUBTITLE_CHARS:
        return True    # 壳子在、内容空，浓缩只会出空话
    code = subtitle_reason(why)
    if code in ("subtitle_missing", "subtitle_off_topic"):
        return True
    return code == "subtitle_low_coverage" and sub_cover < 0.5


def _try_asr_fallback(res: dict, bvid: str, page: int, why: str,
                      min_sub_cover: float, asr_model: str, asr_compute: str,
                      scan_mode: str) -> str:
    """ASR 兜底的执行段。返回 "rescued"（字幕已换成 ASR 产物且过闸，继续往下跑）
    或 "stop"（res 已按 SPEC §4/§7 记好账，调用方直接 finish）。

    每个出口都写 res["sub_source"]（official/asr/none），救回的把 asr_model/asr_elapsed
    也带上，_runs.jsonl 靠这三个字段统计"ASR 救回多少条"。
    """
    from .. import asr
    from ..quality import MIN_SUBTITLE_CHARS

    res["sub_source"] = "none"     # 下面任何出口默认 none；真用上 ASR 会覆盖成 asr
    body_chars = _subtitle_body_chars(bvid, page)
    cover = subtitle_coverage(bvid, page)
    has_cache = subtitle_cache_path(bvid, page).exists()

    if scan_mode == "off":
        # --scan off 是"纯字幕"语义：不扫帧也不调 ASR（SPEC §2 明确不触发），缺字幕就缺着
        print(f"  字幕不可用：{why} → 跳过（--scan off 不触发 ASR）")
        res["status"] = "no_usable_subtitle"
        # 只有"有缓存但内容空"才叫太短；根本没轨的时候是 subtitle_missing，别混
        res["reason"] = "subtitle_too_short" if (has_cache and body_chars < MIN_SUBTITLE_CHARS) \
            else _subtitle_reason(why)
        res["error"] = why
        return "stop"
    if not _should_try_asr(why, cover, body_chars):
        print(f"  字幕不可用：{why} → 跳过（覆盖 {cover:.0%}，0.5~0.8 带内不触发 ASR）")
        res["status"] = "no_usable_subtitle"
        res["reason"] = _subtitle_reason(why)
        res["error"] = why
        return "stop"

    ok, msg = asr.available()
    if not ok:
        # 环境不全：退回现状（跳过视频），装好依赖再跑即可（retryable）
        print(f"  ASR 不可用：{msg} → 按现状跳过")
        res["status"] = "no_usable_subtitle"
        res["reason"] = "asr_unavailable"
        res["error"] = msg
        return "stop"

    print(f"  字幕不可用：{why} → 试 ASR 兜底（{asr_model}/{asr_compute}，"
          f"正文 {body_chars} 字 / 覆盖 {cover:.0%}）")
    r = asr.transcribe(bvid, page, model=asr_model, compute=asr_compute)
    res["asr_model"] = asr_model
    res["asr_elapsed"] = r.get("elapsed_s", 0.0)
    res["sub_source"] = "asr" if r.get("ok") else "none"

    if not r.get("ok"):
        # 转写自己分好了原因：asr_no_audio（付费/充电，重试无用）vs asr_failed（真故障，可重试）
        why_fail = r.get("reason") or "asr_failed"
        print(f"  ASR 转写失败（{why_fail}）：{r.get('error')} → 按现状跳过")
        res["status"] = "no_usable_subtitle"
        res["reason"] = why_fail
        res["error"] = r.get("error", "")
        return "stop"
    if r.get("no_speech"):
        # 无语音是合法结论不是故障（纯 BGM / UI 演示）：不入库、不重试（retryable=false）
        print("  ASR 判定全程无语音 → 不入库（no_speech，结论而非故障）")
        res["status"] = "no_speech"
        res["reason"] = "no_speech"
        res["error"] = "ASR 判定全程无语音（纯 BGM/UI 演示类）"
        return "stop"

    # 兜底不许硬塞：ASR 文本也要过一遍同一道闸，不过就按闸门结论记（SPEC §7）
    usable2, why2 = subtitle_usable(bvid, page, min_sub_cover)
    res["sub_gate_asr"] = why2
    if not usable2:
        print(f"  ASR 文本仍不可用：{why2} → 跳过（ASR 缓存保留供人工核）")
        res["status"] = "no_usable_subtitle"
        res["reason"] = _subtitle_reason(why2)
        res["error"] = why2
        return "stop"

    res["sub_source"] = "asr"
    print(f"  ASR 兜底救回：{r.get('segments')} 段 / {r.get('chars')} 字"
          f"（用时 {r.get('elapsed_s', 0):.0f}s）")
    return "rescued"


def run_one(bvid: str, thr: float, model: str, start: str = "fetch",
            keep_temps: bool = False, min_useful: int = 2,
            force_scan: bool = False, use_probe: bool = False, page: int = 1,
            min_sub_cover: float = MIN_SUBTITLE_COVERAGE,
            scan_mode: str = "auto", probe_veto: bool = True,
            asr_fallback: bool = False, asr_model: str = "medium",
            asr_compute: str = "float16") -> dict | None:
    t_all = time.time()
    print(f"\n==== {bvid}" + (f"（P{page}）" if page > 1 else ""))
    res = {"bvid": bvid, "page": page, "ok": False}
    res["page_count"] = _cache_page_count(bvid, page)
    if res["page_count"] > 1 and page == 1:
        res["partial"] = True
        if start not in ("fetch", "all"):   # fetch 路径有自己的提示与覆写
            print(f"  注意：多P 视频本次只处理 P1（共 {res['page_count']} 集），按「不完整」记账")

    def finish() -> dict:
        """所有出口共用的收尾：清中间帧 + 记时。"""
        cleanup(bvid, keep_temps, page=page)
        res["elapsed"] = round(time.time() - t_all, 1)
        print(f"  {bvid} 用时 {res['elapsed']:.0f}s ok={res['ok']}")
        return res

    if start in ("fetch", "all"):
        try:
            info = ensure_subtitle(bvid, page=page)
        except Exception as e:
            # 抛错=压根没抓到字幕（无轨/只有外语轨/业务性失败），与"抓到但覆盖低"分开记。
            # 无轨也是 SPEC §2 的触发条件之一：没有官方轨时更该自己转写，所以开关打开时先试兜底。
            if not asr_fallback:
                print(f"  字幕抓取失败：{str(e)[:120]} → 跳过（不扫帧、不浓缩、不入库）")
                res["status"] = "no_subtitle"
                res["reason"] = "subtitle_missing"
                res["error"] = str(e)[:200]
                return finish()
            print(f"  字幕抓取失败：{str(e)[:100]}")
            why_missing = f"没有字幕缓存（{str(e)[:80]}）"
            if _try_asr_fallback(res, bvid, page, why_missing, min_sub_cover,
                                 asr_model, asr_compute, scan_mode) != "rescued":
                return finish()
            # 救回来了：后续几行要 info（标题/分P/简介链接），从 ASR 缓存里取
            try:
                info = json.loads(subtitle_cache_path(bvid, page).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                res["status"] = "no_usable_subtitle"
                res["reason"] = "asr_failed"
                res["error"] = "ASR 缓存读不出来"
                return finish()
        res["title"] = info.get("title", "")
        pc = int(info.get("page_count") or 1)
        res["page_count"] = pc
        if pc > 1 and page == 1:
            # 多P 只处理了 P1：本条按「不完整」记账（partial）。
            # page>1 的跑动不标 partial——那条记录本身就是"第 N 集"。
            res["partial"] = True
            print(f"  注意：该视频有 {pc} 个分P，本次只处理 P1「{info.get('page_title') or ''}」"
                  " —— 多P 未完整解决，本条按「不完整」记账")
        elif pc > 1:
            print(f"  多P 视频第 {page}/{pc} 集「{info.get('page_title') or ''}」")
        # 简介里的 GitHub 链接和字幕质量无关，保持在闸门之前
        push_desc_resources(bvid, info)

    if start in ("fetch", "sections", "all"):
        # 字幕可用性闸：字幕烂就没有讲述骨架，往下跑只会烧钱产垃圾。
        # --force-scan 不救这里：它救的是"画面诊断漏判"，救不了"字幕烂"。
        usable, why = subtitle_usable(bvid, page, min_sub_cover)
        res["sub_gate"] = why
        if not usable:
            if asr_fallback:
                # 兜底开关开着才进来；救回就继续往下走，否则 helper 已把账记好
                if _try_asr_fallback(res, bvid, page, why, min_sub_cover,
                                     asr_model, asr_compute, scan_mode) != "rescued":
                    return finish()
            else:
                # 开关关（默认）：与历史行为逐字节一致，连 sub_source 都不写（留空）
                print(f"  字幕不可用：{why} → 跳过（不扫帧、不浓缩、不入库）")
                res["status"] = "no_usable_subtitle"
                res["reason"] = _subtitle_reason(why)
                res["error"] = why
                return finish()
        elif asr_fallback:
            # 闸门放行时确认这条字幕是谁写的（重跑时可能已经是上次的 ASR 缓存）
            from .. import asr
            res["sub_source"] = "asr" if asr.is_asr_cache(bvid, page) else "official"
        sec = get_sections(bvid, model, page)
        res["sections"] = len(sec.get("sections", []))

    eff = thr
    if start in ("fetch", "sections", "scan", "all"):
        if scan_mode == "off":
            # 总开关：关掉就一条都不扫（连诊断都不做），全走纯字幕浓缩。
            # 这是给"这批视频不用画面"的场景留的，不是逃生阀，--force-scan 也覆盖不了它。
            print("  扫帧开关关闭（--scan off）→ 不做画面分析，直接只读字幕浓缩")
            eff = 0.0
            res["n_keep"] = 0
            res["visual_gate"] = "scan off"
            res["visual_verdict"] = "off"
        else:
            try:
                need, why = visual_budget(bvid, page)
                if not need and use_probe and not force_scan:
                    ok, pwhy = probe_pages(bvid, page=page)     # 可选：抽 8 帧复核（实测不可靠，默认关）
                    need = ok
                    why = f"{why}；抽样复核：{pwhy}"
                elif need and probe_veto and not force_scan:
                    # 反向用法：分段说有页，但 8 帧一张都不是 → 否决，省下 8~10 分钟扫帧。
                    # 实测 8 条：6 条判对、2 条误报"有页"、0 条漏判。所以只在
                    # "一张都没有"时否决（1 张也放行），宁可多扫不可漏扫。
                    hits, pwhy = probe_page_count(bvid, page=page)
                    res["probe_hits"] = hits
                    if hits == 0:
                        print(f"  抽样否决：{pwhy} → 不做画面分析")
                        need = False
                        why = f"{why}；抽样否决：{pwhy}"
                    else:
                        why = f"{why}；{pwhy}"
                res["visual_gate"] = why
                if not need and not force_scan:
                    print(f"  画面诊断：{why} → 不做画面分析，直接只读字幕浓缩")
                    eff = 0.0
                    res["n_keep"] = 0
                    res["visual_verdict"] = "no_pages"
                else:
                    print(f"  画面诊断：{why}")
                    cov = subtitle_coverage(bvid, page)
                    if cov < 0.8:   # 字幕只覆盖前半段时，分段打分看不到后段，直接全片扫
                        print(f"  字幕只覆盖 {cov:.0%}，分段不可信 → 全片扫描")
                        eff = 0.0
                    from .seg_filter import scan as seg_scan
                    rep = seg_scan(bvid, eff, page=page)
                    kept = rep.get("keep_frames") or []
                    n_cand = int(rep.get("n_candidates") or 0)
                    if not kept and eff:
                        if n_cand == 0:
                            print(f"  thr={eff:g} 一个候选帧都没有，回退全片扫描 thr=0")
                            eff = 0.0
                            rep = seg_scan(bvid, 0)
                        else:
                            # 候选是有的，只是全被内容判定否掉（手机屏摄/人头/动效卡）。
                            # 全片重扫只会再扫出同一类画面，白花一遍时间。
                            print(f"  thr={eff:g} 扫出 {n_cand} 张候选但全被判成非幻灯片 → 不回退")
                    res["n_keep"] = len(rep.get("keep_frames") or [])
                    res["visual_verdict"] = "kept" if res["n_keep"] else "scanned_none"
            except Exception as e:
                print(f"  扫帧段失败：{str(e)[:120]} → 跳过（scan_failed）")
                res["status"] = "scan_failed"
                res["reason"] = "visual_capture_failed"
                res["visual_verdict"] = "failed"
                res["error"] = str(e)[:200]
                return finish()

    if start in ("fetch", "sections", "scan", "digest", "all"):
        from . import digest
        if not subtitle_cache_path(bvid, page).exists():
            # 没字幕就没有讲述顺序，分块无从谈起（画面驱动分块是另一件事，见待定）
            print("  没有字幕缓存，跳过浓缩")
            res["error"] = "no_subtitle"
            res["status"] = "no_subtitle"
        else:
            thr_use = f"{eff:g}" if res.get("n_keep") else None
            if thr_use is None:
                # --from digest 会跳过扫帧段，此时 n_keep 是 None。若磁盘上已经有扫帧结果
                # （report_thr*.json），必须沿用那个 thr —— 否则会把带图的条目重写成
                # 纯字幕版（visual: none），再 apply 就把知识库里的图版覆盖掉了。
                best = None
                for rp in sorted((ROOT / kb_id(bvid, page)).glob("report_thr*.json")):
                    try:
                        n = len(json.loads(rp.read_text(encoding="utf-8")).get("keep_frames") or [])
                    except Exception:
                        continue
                    if n and (best is None or n > best[0]):
                        best = (n, rp.stem.replace("report_thr", ""))
                if best:
                    thr_use = best[1]
                    print(f"  沿用已有扫帧结果 thr={thr_use}（{best[0]} 帧），不降级成纯字幕")
            d = digest.digest_one(bvid, thr_use, page=page) if thr_use else None
            if not d:
                if res.get("n_keep"):
                    # 画面判定为无用也照样记一条：标明 visual: none，只读字幕
                    print("  画面判定为无用，改用纯字幕浓缩（visual: none）")
                d = digest.digest_one(bvid, "none", page=page)
            if d:
                res["ok"] = True
                res["reason"] = "ok"
                res["chars"] = d["chars"]
                res["digest"] = str(d["path"])
                if d.get("resources"):
                    from . import resources
                    items = [{"title": x["title"], "url": x["url"], "note": x["note"],
                              "kind": "project", "tags": ["项目", "备选调研"]}
                             for x in d["resources"]]
                    resources.push(items, source="bilibili", source_ref=bvid)
                kb_ok = apply_kb(bvid, page=page)
                res["kb_ok"] = bool(kb_ok)
                if not kb_ok:
                    # digest 写出来了但没进知识库：ok 仍是"浓缩成功"，落库可后补
                    # （bili-visual-apply <bv>），但不能当它成功了
                    print("  注意：落库失败，digest 已保留，可稍后 bili-visual-apply 重试")
                    res["error"] = res.get("error") or "apply_failed"
                    res["status"] = "apply_failed"
                    res["reason"] = "apply_failed"
                else:
                    res["status"] = "ok"
                    if res.get("sub_source") == "asr":
                        # 兜底救回的按 asr_used 记 reason（不覆盖 ok 的成功口径），
                        # bili-status 靠它统计"ASR 救回多少条"（INTERFACE §5.3）
                        res["reason"] = "asr_used"
            else:
                res.setdefault("error", "digest_failed")
                res["status"] = "digest_failed"
                res["reason"] = "digest_empty" if "没有产出" in str(res.get("error")) else "digest_failed"

    return finish()


_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")

from ..reasons import (  # noqa: E402  原因词表见 reasons.py（查账时不必拉起本模块）
    REASON_LABEL, REASON_RETRYABLE, classify_reason, subtitle_reason)


def _subtitle_reason(why: str) -> str:
    return subtitle_reason(why)


def parse_bvid_entry(x: str) -> tuple[str, int] | None:
    """"BVxxx" / "BVxxx?p=2" / URL → (bvid, page)；抠不出 BV 号返回 None。"""
    m = _BV_RE.search(x or "")
    if not m:
        return None
    pm = re.search(r"[?&]p=(\d+)", x or "")
    page = max(1, int(pm.group(1))) if pm else 1
    return m.group(1), page


def read_bvids_from_stdin() -> list[tuple[str, int]]:
    """从标准输入读 BV 号（一行一个，可带 ?p=N），配合 bili-find --bvid-only 用。"""
    import sys as _sys
    out: list[tuple[str, int]] = []
    for raw in _sys.stdin:
        line = raw.strip()
        if not line:
            continue
        ent = parse_bvid_entry(line)
        if ent and ent not in out:
            out.append(ent)
    return out


_RUNS_JSONL = ROOT / "_runs.jsonl"   # data/bilibili/visual/_runs.jsonl：每条视频的结果明细


def _record_run(res: dict) -> None:
    """一条结果追加一行。每条跑完立刻写（在 run_batch 里逐条调），Ctrl-C 不丢已完成的；
    写盘失败只警告一行，不能连累整批。"""
    row = {
        "ts": time.strftime("%Y-%m-%d %H:%M"),
        "bvid": res.get("bvid", ""),
        "page": res.get("page", 1),
        "page_count": res.get("page_count", 1),
        "partial": bool(res.get("partial", False)),
        "status": res.get("status") or ("ok" if res.get("ok") else "error"),
        "reason": res.get("reason") or classify_reason(
            res.get("status") or ("ok" if res.get("ok") else "error"),
            res.get("error", ""), res.get("visual_gate") or res.get("sub_gate", "")),
        "retryable": bool(REASON_RETRYABLE.get(
            res.get("reason") or classify_reason(
                res.get("status") or ("ok" if res.get("ok") else "error"),
                res.get("error", ""), ""), False)),
        "error": res.get("error", ""),
        "gate": res.get("visual_gate") or res.get("sub_gate", ""),
        "visual_verdict": res.get("visual_verdict", ""),
        "n_keep": res.get("n_keep", 0),
        "chars": res.get("chars", 0),
        "kb_ok": bool(res.get("kb_ok", False)),
        "elapsed": res.get("elapsed", 0.0),
        # ASR 兜底记账（只增不改）：开关没开时是空串/0，老行读法不受影响
        "sub_source": res.get("sub_source", ""),
        "asr_model": res.get("asr_model", ""),
        "asr_elapsed": res.get("asr_elapsed", 0.0),
    }
    try:
        with open(_RUNS_JSONL, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"  警告：_runs.jsonl 写盘失败（{str(e)[:80]}），本条没记上账，继续跑")


def run_batch(bvids: list, thr: float = 3, model: str = "v4flash",
              start: str = "fetch", keep_temps: bool = False,
              force_scan: bool = False, use_probe: bool = False,
              no_index: bool = False,
              backend_python: str = r"D:/Anaconda/envs/infosystem/python.exe",
              min_sub_cover: float = MIN_SUBTITLE_COVERAGE,
              scan_mode: str = "auto", probe_veto: bool = True,
              asr_fallback: bool = False, asr_model: str = "medium",
              asr_compute: str = "float16") -> list[dict]:
    """bvids 接受 "BVxxx" / "BVxxx?p=N" 字符串或 (bvid, page) 元组。

    ASR 兜底参数默认关（asr_fallback=False），逐层透传；串行执行，
    天然满足"不许并发跑 ASR"（6GB 卡上 whisper+reranker 会 OOM）。
    """
    entries: list[tuple[str, int]] = []
    for x in bvids:
        ent = parse_bvid_entry(x) if isinstance(x, str) else x
        if ent and ent not in entries:
            entries.append(ent)
    out = []
    for bv, page in entries:
        t0 = time.time()
        try:
            r = run_one(bv, thr, model, start, keep_temps, 2, force_scan,
                        use_probe, page=page, min_sub_cover=min_sub_cover,
                        scan_mode=scan_mode, probe_veto=probe_veto,
                        asr_fallback=asr_fallback, asr_model=asr_model,
                        asr_compute=asr_compute)
        except Exception as e:
            # 到这里的是预想之外的异常：抓字幕/扫帧/digest 的失败都已在 run_one 内记了 status
            r = {"bvid": bv, "page": page, "ok": False, "status": "error",
                 "error": str(e)[:200], "elapsed": round(time.time() - t0, 1)}
            print("  失败:", str(e)[:200])
            traceback.print_exc()
        if r:
            _record_run(r)
        out.append(r)
    if not no_index and any(r.get("ok") for r in out):
        index_kb(backend_python)
    return out
