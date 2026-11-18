# -*- coding: utf-8 -*-
"""本地 ASR 兜底（施工单 07）：官方字幕轨拿不到 / 拿到的是别的视频的轨时，自己转写音频，
写成与官方轨同构的缓存（data/bilibili/subtitles/<bvid>[_pN].json）。写完之后
load_segments / 闸门 / 分段 / 浓缩 / 入库一行不改——ASR 是字幕层的一个数据源，不是第二条管线。

判定"要不要用 ASR"是 runner 的事；本模块只回答"能不能跑、跑出什么"（INTERFACE_ASR.md §0）。
对外只暴露 available() / transcribe() / is_asr_cache()，签名以 INTERFACE §2 为准，不许改。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from .visual.paths import subtitle_cache_path
from .visual.scan_frames import PLAY_API, VIEW_API, get_cookie, make_session

try:  # Windows 控制台默认 GBK，中文日志会炸
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

PROJECT_ROOT = Path(__file__).resolve().parents[3]
# HF 缓存根与临时音频目录；模型根可被 ASR_MODEL_ROOT 覆盖（搬机器/换盘不用改代码）
from backend.config import deployment

MODEL_ROOT = Path(os.environ.get("ASR_MODEL_ROOT") or PROJECT_ROOT / ".models" / "asr")
AUDIO_DIR = PROJECT_ROOT / "data" / "bilibili" / "audio"

# 音频流小于这个数视为下载失败（试看/区域限制时常给个几秒的空壳文件）
MIN_AUDIO_BYTES = 50_000
# 全文去标点后不足这么多字 = 只有感叹词/音乐标签幻听，按"全程无语音"记（INTERFACE §3.2）
MIN_SPEECH_CHARS = 10

# 转写参数照搬 run_asr.py 实测口径：强制 zh（中英混讲不强制会被判成 en 翻成英文，REPORT 坑2）、
# VAD 必开（静音段拖慢且产生幻听）。模型进程内单例：二次调用不再加载（INTERFACE §9）。
_TRANSCRIBE_KW = dict(language="zh", beam_size=5, vad_filter=True,
                      vad_parameters={"min_silence_duration_ms": 500})
_MODEL: dict = {"key": None, "model": None}

# 去标点计数用：\W 不含中文（Python3 的 \w 带 Unicode 字义），所以只丢标点/空白
_NOISE_RE = re.compile(r"[\W_]+", re.UNICODE)


def log(msg: str) -> None:
    print(f"[asr] {msg}", flush=True)


def available() -> tuple[bool, str]:
    """能不能跑 ASR。必须是廉价检查（不加载模型、不碰显存），可以每条视频调一次。

    检查项与顺序按 INTERFACE §2.1，任一失败即 (False, 原因)。torch 缺失/CUDA 不可用
    不算失败，只说明会落到 CPU（慢但能跑）。
    """
    try:
        import faster_whisper  # noqa: F401
        from importlib.metadata import version
        try:
            ver = version("faster-whisper")
        except Exception:
            ver = getattr(faster_whisper, "__version__", "?")
    except Exception:
        return False, "faster_whisper 未安装"
    if not shutil.which("ffmpeg"):
        return False, "ffmpeg 不在 PATH"
    # 签名不带 model 参数，这里只查"有没有下过任何 Systran 模型"；具体型号缺了由
    # WhisperModel 自己报错（transcribe 会兜成 asr_failed）
    if not any(MODEL_ROOT.glob("models--Systran--faster-whisper-*")):
        return False, f"模型未下载：{MODEL_ROOT}"
    device = "CPU 模式"
    try:
        import torch
        if torch.cuda.is_available():
            device = "CUDA 可用"
    except Exception:
        pass
    return True, f"faster-whisper {ver} / 模型已就绪 / {device}"


def is_asr_cache(bvid: str, page: int = 1) -> bool:
    """缓存是不是 ASR 产出的（区分"官方轨"和"兜底轨"）。不许拿它判断字幕能不能用——那是闸门的事。"""
    p = subtitle_cache_path(bvid, page)
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return str(d.get("source") or "").startswith("asr:")


def transcribe(bvid: str, page: int = 1, *, model: str = "medium",
               compute: str = "float16", force: bool = False) -> dict:
    """取流 → 下载音频 → 转写 → 原子写缓存。**永不抛异常**。

    返回 INTERFACE §3 三种形态之一（成功/无语音的键集合相同，失败只有 6 个键）。
    幂等：缓存已是本模块写的（source 以 asr: 开头）且 force=False 时直接返回缓存内容。
    """
    t0 = time.time()

    def fail(reason: str, error) -> dict:
        return {"ok": False, "bvid": bvid, "page": page, "reason": reason,
                "error": str(error)[:300], "elapsed_s": round(time.time() - t0, 1)}

    try:
        # 前置防御（INTERFACE §2.2）：调用方本应先问 available()，这里再判一次，
        # 环境不全时不下载不加载，直接收敛成 asr_unavailable
        ok, msg = available()
        if not ok:
            return fail("asr_unavailable", msg)

        p = subtitle_cache_path(bvid, page)
        if p.exists() and not force:
            cached = _read_cache(p)
            if cached and _is_asr_payload(cached):
                return _result_from_cache(cached, bvid, page)

        if not (MODEL_ROOT / f"models--Systran--faster-whisper-{model}").is_dir():
            return fail("asr_unavailable", f"缺少 {model} 权重，请检查 ASR_MODEL_ROOT")

        sess = make_session(get_cookie())
        meta = _get_video_meta(sess, bvid, page)
        audio_path = AUDIO_DIR / (f"{bvid}_p{page}.m4s" if page > 1 else f"{bvid}.m4s")
        if not audio_path.exists() or audio_path.stat().st_size < MIN_AUDIO_BYTES:
            stream = _pick_audio_stream(sess, bvid, meta["cid"])
            _download_audio(sess, stream["url"], audio_path)
        else:
            log(f"音频已存在，跳过下载：{audio_path.name}")

        sampler = _VramSampler()
        t1 = time.time()
        segs, audio_dur = _transcribe_audio(audio_path, model, compute)
        elapsed = time.time() - t1
        vram = sampler.stop()
        no_speech = _is_no_speech(segs)
        chars = sum(len(s["text"]) for s in segs)
        total = round(time.time() - t0, 1)   # 含下载+转写，用户看到的等待就是这两段（INTERFACE §9）

        payload = {
            "ok": True,
            "bvid": bvid, "page": page, "page_count": meta["page_count"],
            "title": meta["title"],
            "desc": meta.get("desc", ""),
            "duration": float(meta["duration"]),
            "segments": segs,      # [{start, end, text}] 已 strip、非空、start 单调
            "source": f"asr:faster-whisper-{model}",
            "no_speech": no_speech,
            "language": "zh",
            "elapsed_s": total,
            "asr_meta": {"model": model, "compute": compute, "vad": True,
                         "peak_vram_mb": vram.get("peak_vram_mb", 0),
                         "audio_real_duration_s": round(audio_dur, 1),
                         "speedup_x": round(audio_dur / max(elapsed, 0.1), 2)},
        }
        _backup_official_cache(p)      # 官方轨先改名留证，再写 ASR 缓存（永不覆盖式删除）
        if force and p.exists():       # force 重跑：旧 ASR 缓存放 .asr_old，不留半截
            os.replace(p, p.with_name(p.name + ".asr_old"))
        _atomic_write_json(p, payload)
        try:                           # 转写完立刻删临时音频（43 分钟约 40MB）
            audio_path.unlink()
        except OSError:
            pass
        log(f"转写完成：{len(segs)} 段 / {chars} 字，用时 {total}s"
            f"（倍速 {payload['asr_meta']['speedup_x']}x，显存峰值 {vram.get('peak_vram_mb', 0)}MB）")
        return {"ok": True, "bvid": bvid, "page": page,
                "segments": len(segs), "chars": chars,
                "duration": float(meta["duration"]),
                "source": payload["source"], "no_speech": no_speech,
                "elapsed_s": total,
                "reason": "no_speech" if no_speech else "asr_used",
                "reused": False}
    except NoAudioStream as e:
        # 付费/充电专属：重试没有意义，单独记一个码，别混进"可重试"里浪费排期
        return fail("asr_no_audio", e)
    except Exception as e:  # noqa: BLE001 — 其余失败收敛成 asr_failed，不往外抛
        return fail("asr_failed", e)


# ---------------- 内部实现（以下一律下划线私有） ----------------

def _read_cache(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _is_asr_payload(d: dict) -> bool:
    return str(d.get("source") or "").startswith("asr:")


def _result_from_cache(d: dict, bvid: str, page: int) -> dict:
    """命中缓存不重跑（幂等）：列表在缓存文件里，返回只给段数和字数（INTERFACE §3.1）。"""
    segs = d.get("segments") or []
    no_speech = bool(d.get("no_speech"))
    return {"ok": True, "bvid": bvid, "page": page,
            "segments": len(segs),
            "chars": sum(len(s.get("text", "")) for s in segs),
            "duration": float(d.get("duration") or 0),
            "source": d.get("source") or "",
            "no_speech": no_speech,
            "elapsed_s": float(d.get("elapsed_s") or 0),
            "reason": "no_speech" if no_speech else "asr_used",
            "reused": True}


def _atomic_write_json(path: Path, payload: dict) -> None:
    """先写 .tmp 再 os.replace：半截 JSON 会让下游报 Extra data，这个坑踩过一次。
    BaseException 兜底是为了 Ctrl-C 时也不留半截 .tmp（SPEC §7）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _backup_official_cache(p: Path) -> None:
    """官方轨永不删：转写覆盖前改名 .official_bad 留证据，以后能回查"是我们判错还是官方错"。
    已有 .official_bad 时不覆盖（保留第一次的坏证据）。"""
    if not p.exists():
        return
    d = _read_cache(p) or {}
    if _is_asr_payload(d):
        return                  # ASR 自己的缓存，走 force 的 .asr_old 分支，不占 official_bad
    bad = p.with_name(p.name + ".official_bad")
    if bad.exists():
        return
    os.replace(p, bad)
    log(f"官方轨已备份为 {bad.name}")


def _load_model(model_name: str, compute_type: str):
    """模型进程内单例：同 (model, compute) 只加载一次。首次约 6s（已缓存），复用 <0.1s。"""
    key = (model_name, compute_type)
    if _MODEL["key"] == key:
        return _MODEL["model"]
    from faster_whisper import WhisperModel

    cuda = False
    try:
        import torch
        cuda = torch.cuda.is_available()
    except Exception:
        pass
    # float16 只有 CUDA 支持：没卡就落 CPU+int8（慢但能跑），与 run_asr 的 --cpu 行为一致
    device = "cuda" if (cuda and compute_type != "cpu") else "cpu"
    ctype = compute_type if device == "cuda" else "int8"
    log(f"加载模型 {model_name}（{ctype} / {device}）...")
    m = WhisperModel(model_name, device=device, compute_type=ctype,
                     download_root=str(MODEL_ROOT), local_files_only=True)
    _MODEL.update(key=key, model=m)
    return m


def _transcribe_audio(audio_path: Path, model_name: str, compute_type: str) -> tuple[list, float]:
    model = _load_model(model_name, compute_type)
    raw_iter, info = model.transcribe(str(audio_path), **_TRANSCRIBE_KW)
    dur = float(getattr(info, "duration", 0) or 0)
    segs: list[dict] = []
    last_mark = -1.0
    for s in raw_iter:
        txt = (getattr(s, "text", "") or "").strip()
        if txt:
            segs.append({"start": round(float(s.start), 3),
                         "end": round(float(s.end), 3), "text": txt})
        if getattr(s, "end", 0) - last_mark >= 300:   # 长音频给个进度，43 分钟片不至于像卡死
            last_mark = s.end
            log(f"  进度 {s.end:.0f}s / {dur:.0f}s")
    return segs, dur


def _is_no_speech(segs: list[dict]) -> bool:
    """VAD 0 段、或全文去标点后不足 10 字（只有感叹词/音乐标签）都算"全程无语音"。
    这是合法结论不是失败（INTERFACE §3.2），主流程要尊重，别当错误重试。"""
    if not segs:
        return True
    body = "".join(s["text"] for s in segs)
    return len(_NOISE_RE.sub("", body)) < MIN_SPEECH_CHARS


def _get_video_meta(sess, bvid: str, page: int) -> dict:
    """view 接口拿 cid/title/duration/page_count（分P 用 pages[page-1]，照搬 run_asr）。"""
    r = sess.get(f"{VIEW_API}?bvid={bvid}", timeout=20)
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"view: {data.get('message')}")
    d = data["data"]
    pages = d.get("pages") or [{"cid": d["cid"], "duration": d.get("duration", 0)}]
    if page > len(pages):
        raise RuntimeError(f"view: 该视频只有 {len(pages)} 个分P，没有 P{page}")
    pg = pages[page - 1]
    return {"title": d.get("title", bvid), "cid": pg["cid"],
            # desc 顺带存进缓存：无官方轨的视频救回后，简介里的资源链接还指望它进 backlog（A12）
            "desc": d.get("desc", "") or "",
            "duration": int(pg.get("duration") or d.get("duration", 0)),
            "page_count": len(pages)}


class NoAudioStream(RuntimeError):
    """拿不到音频流：付费/充电专属、区域限制、试看片段。

    单独一个异常类型是为了和"下载失败/解码失败"分开记账——这条重试一百次也还是拿不到，
    不该进"值得补抓"的清单（用户 2026-11-15：付费视频别再浪费时间去跑）。
    """


def _pick_audio_stream(sess, bvid: str, cid: int) -> dict:
    """playurl 的 dash.audio[] 按 bandwidth 挑最大；mcdn(PCDN) 域名实测 -138 连不上，跳过。"""
    url = f"{PLAY_API}?bvid={bvid}&cid={cid}&qn=16&fnver=0&fnval=4048&fourk=1"
    data = sess.get(url, timeout=20).json()
    if data.get("code") != 0:
        raise NoAudioStream(f"playurl: {data.get('message')}")
    audios = (data.get("data", {}).get("dash", {}) or {}).get("audio", []) or []
    if not audios:
        raise NoAudioStream("playurl: 响应里没有 dash.audio（付费/充电专属或区域限制）")
    a = max(audios, key=lambda x: int(x.get("bandwidth", 0) or 0))
    cand = [str(u) for u in
            ([a.get("baseUrl") or a.get("base_url")]
             + list(a.get("backupUrl") or a.get("backup_url") or [])) if u]
    aurl = next((u for u in cand if "mcdn" not in u), cand[0])
    return {"url": aurl, "bandwidth": int(a.get("bandwidth", 0) or 0)}


def _download_audio(sess, aurl: str, dst: Path) -> int:
    """流式下载 .m4s（DASH audio-only）。小于 50KB 视为失败（试看壳子）。"""
    n = 0
    try:
        with sess.get(aurl, stream=True, timeout=(10, 60)) as r:
            r.raise_for_status()
            dst.parent.mkdir(parents=True, exist_ok=True)
            with open(dst, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
                    n += len(chunk)
    except Exception as e:
        raise RuntimeError(f"download: {e}") from e
    if n < MIN_AUDIO_BYTES:
        raise RuntimeError(f"download: 音频只有 {n} 字节（<{MIN_AUDIO_BYTES}，视为失败）")
    log(f"音频下载完成 {n / 1e6:.1f} MB")
    return n


class _VramSampler:
    """后台线程采 nvidia-smi 显存峰值（asr_meta 排期用）。nvidia-smi 不在就给 0，不致命。"""

    def __init__(self, interval: float = 2.0):
        self.interval = interval
        self.peak = self._query()
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    @staticmethod
    def _query() -> int:
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10)
            return int(out.stdout.strip().splitlines()[0])
        except Exception:
            return 0

    def _loop(self) -> None:
        while not self._stop.is_set():
            v = self._query()
            if v > self.peak:
                self.peak = v
            self._stop.wait(self.interval)

    def stop(self) -> dict:
        self._stop.set()
        self._t.join(timeout=5)
        return {"peak_vram_mb": self.peak}
