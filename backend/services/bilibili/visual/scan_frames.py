"""
实验沙盒：B站视频「低清流式全扫 + dHash 平台检测选帧 → 高清定点重拉」
====================================================================
验证「视频≈PPT：最少画面集合覆盖画面信息量」路线的第一半（选帧）。
全程不下载视频文件、不落盘视频字节：ffmpeg 直接吃 DASH URL 流式解码，
磁盘上只有抽出的低清小图（临时）和最终选中的 JPG。

带宽实测：ffmpeg 不报告输入流量，故起一个本地字节计数代理
（透传 Range 请求，逐 chunk 累加），ffmpeg 的流 URL 全部经代理。

选帧语义:
  - 静止平台(span>=MIN_SPAN)取尾帧 = PPT 页动画播完的最终态
  - 相邻候选 dHash 距离 <= MERGE_THR => 同页渐进动画，链式合并留最终态
  - 合并链保险丝：链跨度 > chain_max_span 时（滚动/长演示，无法区分
    "PPT高亮"还是"网页滚动"），链内每 periodic_every 秒额外出周期快照
    （kind=periodic，与 kind=final 分开去重；VLM 阶段可按需丢弃）

用法:
  cd D:/infosystem
  python -m backend.services.bilibili.visual.scan_frames BV1fkxu6REPx [选项]
    --fps 2                扫描采样帧率
    --chain-max-span 60    链跨度保险丝（秒），0=关闭（纯 PPT 语义）
    --periodic-every 25    保险丝触发后周期快照间隔（秒）
    --skip-hd              只跑扫描+检测，不重拉高清
    --keep-lo              保留低清扫描小图（默认跑完删除）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Tuple
from urllib.parse import parse_qs, quote, urlparse

import numpy as np
import requests
from PIL import Image, ImageDraw

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36")
VIEW_API = "https://api.bilibili.com/x/web-interface/view"
PLAY_API = "https://api.bilibili.com/x/player/playurl"

# ---- 可调参数（报告里看曲线再调） ----
STILL_THR = 3      # 相邻帧 dHash 汉明距离 <= 此值 => 静止对
MIN_SPAN = 1.0     # 平台最短持续秒数
MERGE_THR = 10     # 相邻候选尾帧距离 <= 此值 => 同页渐进动画，留最终态
HD_WINDOW = 0.15   # 高清重拉时间窗半宽（秒）
HD_TOLERANCE = 0.30
HD_SEEK_BACK = 1.5  # 逐候选重拉时从目标前多少秒开始定位（跨 GOP 对齐）


def log(msg: str) -> None:
    print(f"[exp] {msg}", flush=True)


# ---------------- B站接口 ----------------

def get_cookie() -> str:
    """cookie 来源优先级：环境变量 BILI_COOKIE → Settings.bili_cookie →
    用 .env 里的三段（SESSDATA / bili_jct / DedeUserID）拼。"""
    env = os.getenv("BILI_COOKIE", "").strip()
    if env:
        return env
    try:  # 只读复用 backend 配置，不写任何东西
        from backend.config.settings import Settings  # noqa: E402
        st = Settings()
        whole = (st.bili_cookie or "").strip()
        if whole:
            return whole
        parts = []
        if (st.bili_cookie_sessdata or "").strip():
            parts.append("SESSDATA=" + st.bili_cookie_sessdata.strip())
        if (st.bili_cookie_bili_jct or "").strip():
            parts.append("bili_jct=" + st.bili_cookie_bili_jct.strip())
        if (st.bili_cookie_dedeuserid or "").strip():
            parts.append("DedeUserID=" + st.bili_cookie_dedeuserid.strip())
        return "; ".join(parts)
    except Exception:
        return ""


def make_session(cookie: str) -> requests.Session:
    s = requests.Session()
    s.trust_env = False  # B站国内直连，绕开系统代理（Clash 掐 urllib 的坑）
    s.headers.update({
        "User-Agent": UA,
        "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
    })
    if cookie:
        s.headers["Cookie"] = cookie
    return s


def get_video_meta(sess: requests.Session, bvid: str, page: int = 1) -> Dict:
    r = sess.get(f"{VIEW_API}?bvid={bvid}", timeout=20)
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"view 接口失败: {data.get('message')}")
    d = data["data"]
    if page > 1:                      # 多P：按集取 cid/时长（view 顶层的 cid 是 P1 的）
        pages = d.get("pages") or []
        if len(pages) < page:
            raise RuntimeError(f"该视频只有 {len(pages)} 个分P，没有 P{page}")
        pg = pages[page - 1]
        return {"bvid": bvid, "title": d.get("title", bvid), "cid": pg["cid"],
                "duration": int(pg.get("duration") or d.get("duration", 0))}
    return {"bvid": bvid, "title": d.get("title", bvid), "cid": d["cid"],
            "duration": int(d.get("duration", 0))}


def pick_stream(sess: requests.Session, bvid: str, cid: int,
                mode: str) -> Dict[str, object]:
    """mode='low': 带宽最小的流（游客 360p 可达）; mode='hd': 分数最高的流。"""
    last_err = ""
    for qn in ([16, 32] if mode == "low" else [64, 80, 32, 16]):
        url = (f"{PLAY_API}?bvid={bvid}&cid={cid}&qn={qn}"
               f"&fnver=0&fnval=4048&fourk=1")
        try:
            data = sess.get(url, timeout=20).json()
        except Exception as e:
            last_err = str(e)
            continue
        if data.get("code") != 0:
            last_err = f"qn={qn}: {data.get('message')}"
            continue
        videos = (data.get("data", {}).get("dash", {}) or {}).get("video", []) or []
        if not videos:
            last_err = f"qn={qn}: dash 为空"
            continue

        def score(v: Dict) -> Tuple[int, int, int]:
            w, h = int(v.get("width", 0) or 0), int(v.get("height", 0) or 0)
            bw = int(v.get("bandwidth", 0) or 0)
            return (w * h, bw, int(v.get("id", 0) or 0))

        stream = (min if mode == "low" else max)(videos, key=score)
        vurl = stream.get("baseUrl") or stream.get("base_url")
        # mcdn 是 PCDN 边缘节点，时常连不上（ffmpeg 报 Connection failed / -138），
        # 有 upos 之类的主站备份地址就优先用备份。
        cand = [str(u) for u in
                ([vurl] + list(stream.get("backupUrl") or stream.get("backup_url") or []))
                if u]
        if cand:
            vurl = next((u for u in cand if "mcdn" not in u), cand[0])
        if vurl:
            log(f"stream[{mode}] qn={stream.get('id')} {stream.get('width')}x"
                f"{stream.get('height')} bandwidth={stream.get('bandwidth')}")
            return {"url": str(vurl), "id": int(stream.get("id", 0) or 0),
                    "bandwidth": int(stream.get("bandwidth", 0) or 0)}
    raise RuntimeError(f"未拿到可用视频流: {last_err}")


def ffmpeg_headers(cookie: str) -> str:
    parts = ["Referer: https://www.bilibili.com/", f"User-Agent: {UA}"]
    if cookie:
        parts.append(f"Cookie: {cookie}")
    return "\r\n".join(parts) + "\r\n"


# ---------------- 字节计数代理 ----------------

class CountingProxy:
    """本地 HTTP 代理：把 /?url=<真实流地址> 转发到 B站 CDN，透传 Range，
    逐 chunk 统计下行字节。用于实测 ffmpeg 到底拉了多少。"""

    def __init__(self, cookie: str):
        self.cookie = cookie
        self.bytes_served = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                target = parse_qs(urlparse(self.path).query).get("url", [""])[0]
                if not target:
                    self.send_error(400)
                    return
                fwd = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}
                if outer.cookie:
                    fwd["Cookie"] = outer.cookie
                rng = self.headers.get("Range")
                if rng:
                    fwd["Range"] = rng
                try:
                    up = requests.get(target, headers=fwd, stream=True,
                                      timeout=(10, 60))
                except Exception as e:
                    self.send_error(502, str(e))
                    return
                with up:
                    self.send_response(up.status_code)
                    for k in ("Content-Length", "Content-Range",
                              "Accept-Ranges", "Content-Type"):
                        if k in up.headers:
                            self.send_header(k, up.headers[k])
                    self.end_headers()
                    try:
                        for chunk in up.iter_content(65536):
                            outer.bytes_served += len(chunk)
                            self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError,
                            ConnectionAbortedError):
                        return  # ffmpeg 到达 -t 主动断开属正常

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def wrap(self, url: str) -> str:
        return f"http://127.0.0.1:{self.port}/?url={quote(url, safe='')}"

    def take_bytes(self) -> int:
        b = self.bytes_served
        self.bytes_served = 0
        return b

    def stop(self) -> None:
        self.server.shutdown()


# ---------------- 低清扫描 ----------------

def scan_lowres(url: str, cookie: str, out_dir: Path, fps: float,
                timeout: int = 1800) -> Dict:
    """流式全扫：抽低清小图。返回 {n_frames, wall_s}（字节量由代理统计）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(out_dir / "lo_%05d.jpg")
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats",
        "-headers", ffmpeg_headers(cookie),
        "-i", url,
        "-vf", f"fps={fps},scale=192:-2",
        "-q:v", "7", "-f", "image2", pattern,
        "-progress", "pipe:1",
    ]
    t0 = time.perf_counter()
    last_report = 0.0
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL,
                            text=True, encoding="utf-8", errors="replace")
    assert proc.stdout is not None
    for line in proc.stdout:
        k, _, v = line.strip().partition("=")
        if k != "out_time_us":
            continue
        try:
            t = int(v) / 1e6
        except ValueError:
            continue
        if t - last_report >= 30:
            last_report = t
            log(f"  扫描进度 t={t:7.1f}s")
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise RuntimeError("ffmpeg 扫描超时")
    wall = time.perf_counter() - t0
    n = len(list(out_dir.glob("lo_*.jpg")))
    if n == 0:
        raise RuntimeError("ffmpeg 扫描未产出任何帧")
    return {"n_frames": n, "wall_s": round(wall, 1)}


# ---------------- 平台检测 ----------------

def dhash_bits(img_path: Path) -> np.ndarray:
    img = Image.open(img_path).convert("L").resize((9, 8), Image.LANCZOS)
    arr = np.asarray(img, dtype=np.int16)
    return (arr[:, 1:] > arr[:, :-1]).reshape(-1)  # 64 bool


def hamming(a: np.ndarray, b: np.ndarray) -> int:
    return int(np.count_nonzero(a != b))


def detect_platforms(hashes: List[np.ndarray], fps: float,
                     chain_max_span: float = 0.0,
                     periodic_every: float = 0.0) -> Dict:
    """静止平台检测 + 尾帧候选 + 增量链合并 + 链跨度保险丝。

    返回 candidates: [{idx, kind}], kind ∈ final|periodic。
    final 之间做全局去重（切走又切回来的同页留先出现）；
    periodic 只在彼此之间去重（同一页的周期快照与该页 final 距离很近，
    会被距离去重误杀，故分开处理）。
    """
    n = len(hashes)
    dists = [hamming(hashes[i], hashes[i + 1]) for i in range(n - 1)]

    platforms: List[Tuple[int, int]] = []  # (首帧idx, 尾帧idx)
    i = 0
    while i < len(dists):
        if dists[i] <= STILL_THR:
            j = i
            while j + 1 < len(dists) and dists[j + 1] <= STILL_THR:
                j += 1
            span = (j + 1 - i) / fps  # 帧 i..j+1 构成平台
            if span >= MIN_SPAN:
                platforms.append((i, j + 1))
            i = j + 1
        else:
            i += 1

    # 相邻链式合并（同页渐进动画）
    groups: List[List[int]] = []
    for _, tail in platforms:
        if groups and hamming(hashes[groups[-1][-1]], hashes[tail]) <= MERGE_THR:
            groups[-1].append(tail)
        else:
            groups.append([tail])

    finals: List[int] = []
    periodics: List[int] = []
    n_fused = 0
    for g in groups:
        span = (g[-1] - g[0]) / fps
        if chain_max_span > 0 and periodic_every > 0 and span > chain_max_span:
            step = max(1, int(periodic_every * fps))
            picks = list(range(0, len(g), step))
            if picks[-1] != len(g) - 1:
                picks.append(len(g) - 1)
            periodics.extend(g[k] for k in picks[:-1])
            n_fused += 1
        finals.append(g[-1])

    # final 全局去重：留先出现
    cands: List[Dict] = []
    dup_groups: List[List[int]] = []
    for t in finals:
        for g in dup_groups:
            if hamming(hashes[g[0]], hashes[t]) <= MERGE_THR:
                g.append(t)
                break
        else:
            cands.append({"idx": t, "kind": "final"})
            dup_groups.append([t])
    # periodic 只在彼此之间去重
    seen_p: List[int] = []
    for t in periodics:
        if any(hamming(hashes[s], hashes[t]) <= MERGE_THR for s in seen_p):
            continue
        seen_p.append(t)
        cands.append({"idx": t, "kind": "periodic"})
    cands.sort(key=lambda c: c["idx"])

    return {"dists": dists, "platforms": platforms, "candidates": cands,
            "n_groups": len(groups), "n_fused_chains": n_fused,
            "dup_groups": dup_groups}


# ---------------- 高清定点重拉（逐候选定位） ----------------

def refetch_hd(sess: requests.Session, bvid: str, cid: int, cookie: str,
               timestamps: List[float], out_dir: Path,
               proxy_wrap=None, quality: str = "hd") -> Dict[float, str]:
    """每个候选单独起 ffmpeg，-ss 从目标前 HD_SEEK_BACK 秒定位，
    字节量 ∝ 候选数×几个GOP，而不是视频时长。quality="low" 时抓 360p 做抽样判断。"""
    if not timestamps:
        return {}
    out_dir.mkdir(parents=True, exist_ok=True)
    info = pick_stream(sess, bvid, cid, quality)
    vurl = info["url"] if proxy_wrap is None else proxy_wrap(info["url"])
    hdr = ffmpeg_headers(cookie)
    pat = re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")
    result: Dict[float, str] = {}

    for k, t in enumerate(sorted(timestamps)):
        seek = max(0.0, t - HD_SEEK_BACK)
        lo_w, hi_w = t - seek - HD_WINDOW, t - seek + HD_WINDOW
        vf = f"select='between(t\\,{lo_w:.6f}\\,{hi_w:.6f})',showinfo"
        tmp = tempfile.mkdtemp(prefix="bili_hd_")
        # -t 截止必须有：select 只丢帧不停止读取，没有它每个进程都会解码到 EOF
        dur = (t - seek) + HD_WINDOW + 0.2
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "info",
            "-ss", f"{seek:.6f}", "-t", f"{dur:.6f}",
            "-headers", hdr, "-i", vurl,
            "-vf", vf, "-fps_mode", "vfr", "-q:v", "2", "-f", "image2",
            "-start_number", "0", os.path.join(tmp, "f_%06d.jpg"),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120)
        files = sorted(Path(tmp).glob("f_*.jpg"))
        pts = [float(m.group(1)) for line in (r.stderr or "").splitlines()
               for m in [pat.search(line)] if m]
        if files:
            best_i = min(range(min(len(files), len(pts))),
                         key=lambda i: abs(pts[i] - (t - seek)),
                         default=None)
            if best_i is not None and abs(pts[best_i] - (t - seek)) <= HD_TOLERANCE:
                dst = out_dir / f"hd_{t:.2f}s.jpg"
                dst.write_bytes(files[best_i].read_bytes())
                result[t] = str(dst)
        for f in Path(tmp).glob("f_*.jpg"):
            f.unlink(missing_ok=True)
        try:
            os.rmdir(tmp)
        except OSError:
            pass
        if (k + 1) % 10 == 0:
            log(f"  重拉进度 {k+1}/{len(timestamps)}")
    return result


def refetch_sequential(sess: requests.Session, bvid: str, cid: int, cookie: str,
                       timestamps: List[float], out_dir: Path,
                       proxy_wrap=None) -> Dict[float, str]:
    """顺序单遍拉取：一个 ffmpeg 从首个候选读到末尾，select 一次性抓全部窗口。
    候选多时远快于逐候选定位（时间只跟视频时长走），字节量≈480p 码率×跨度。"""
    if not timestamps:
        return {}
    out_dir.mkdir(parents=True, exist_ok=True)
    info = pick_stream(sess, bvid, cid, "hd")
    vurl = info["url"] if proxy_wrap is None else proxy_wrap(info["url"])
    ts_sorted = sorted(timestamps)
    start = max(0.0, ts_sorted[0] - HD_SEEK_BACK)
    end = ts_sorted[-1] + HD_WINDOW + 0.5
    conds = [f"between(t\\,{max(0.0, t - start - HD_WINDOW):.6f}\\,"
             f"{t - start + HD_WINDOW:.6f})" for t in ts_sorted]
    vf = f"select='{' + '.join(conds)}',showinfo"
    tmp = tempfile.mkdtemp(prefix="bili_seq_")
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "info",
        "-ss", f"{start:.6f}", "-t", f"{end - start:.6f}",
        "-headers", ffmpeg_headers(cookie), "-i", vurl,
        "-vf", vf, "-fps_mode", "vfr", "-q:v", "2", "-f", "image2",
        "-start_number", "0", os.path.join(tmp, "f_%06d.jpg"),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=1800)
    files = sorted(Path(tmp).glob("f_*.jpg"))
    pat = re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")
    pts = [float(m.group(1)) for line in (r.stderr or "").splitlines()
           for m in [pat.search(line)] if m]
    result: Dict[float, str] = {}
    used = set()
    for t in ts_sorted:
        best, bd = None, 1e9
        for i, ft in enumerate(pts):
            if i in used:
                continue
            d = abs(ft - (t - start))
            if d < bd:
                bd, best = d, i
        if best is not None and bd <= HD_TOLERANCE and best < len(files):
            used.add(best)
            dst = out_dir / f"hd_{t:.2f}s.jpg"
            dst.write_bytes(files[best].read_bytes())
            result[t] = str(dst)
    for f in Path(tmp).glob("f_*.jpg"):
        f.unlink(missing_ok=True)
    try:
        os.rmdir(tmp)
    except OSError:
        pass
    return result


# ---------------- 拼图 ----------------

def contact_sheet(paths_ts: List[Tuple[str, str]], out_path: Path,
                  thumb_w: int = 480, cols: int = 4) -> None:
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


# ---------------- 主流程 ----------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("bvid")
    ap.add_argument("--fps", type=float, default=2.0)
    ap.add_argument("--chain-max-span", type=float, default=0.0,
                    help="合并链跨度保险丝（秒），0=关闭（纯 PPT 语义）")
    ap.add_argument("--periodic-every", type=float, default=25.0)
    ap.add_argument("--skip-hd", action="store_true")
    ap.add_argument("--keep-lo", action="store_true",
                    help="保留低清扫描小图（默认跑完删除）")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent / "out" / args.bvid
    lo_dir = root / "lo_frames"
    hd_dir = root / "hd_frames"
    root.mkdir(parents=True, exist_ok=True)

    cookie = get_cookie()
    log(f"cookie: {'有' if cookie else '无(游客)'}")
    sess = make_session(cookie)
    meta = get_video_meta(sess, args.bvid)
    log(f"视频: {meta['title']}  时长 {meta['duration']}s")

    proxy = CountingProxy(cookie)

    # ---- 1) 低清流式全扫 ----
    low = pick_stream(sess, args.bvid, meta["cid"], "low")
    log(f"开始低清扫描 fps={args.fps} ...")
    scan = scan_lowres(proxy.wrap(low["url"]), cookie, lo_dir, args.fps)
    scan_bytes = proxy.take_bytes()
    est_full = scan_bytes / max(meta["duration"], 1)
    log(f"扫描完成: {scan['n_frames']} 帧, 网络拉取 {scan_bytes/1e6:.2f} MB, "
        f"耗时 {scan['wall_s']}s (实测折算码率 {est_full*8/1e6:.2f} Mbps)")

    # ---- 2) 平台检测 ----
    lo_files = sorted(lo_dir.glob("lo_*.jpg"))
    t0 = time.perf_counter()
    hashes = [dhash_bits(p) for p in lo_files]
    log(f"dHash {len(hashes)} 帧耗时 {time.perf_counter()-t0:.1f}s")
    det = detect_platforms(hashes, args.fps,
                           chain_max_span=args.chain_max_span,
                           periodic_every=args.periodic_every)
    dists = det["dists"]
    still_ratio = sum(1 for d in dists if d <= STILL_THR) / max(len(dists), 1)
    log(f"变化率曲线: 中位 {int(np.median(dists))}, p90 {int(np.percentile(dists,90))}, "
        f"静止对占比 {still_ratio:.0%}")
    n_final = sum(1 for c in det["candidates"] if c["kind"] == "final")
    n_periodic = sum(1 for c in det["candidates"] if c["kind"] == "periodic")
    log(f"平台 {len(det['platforms'])} 个 -> 合并链 {det['n_groups']} 条"
        f"(保险丝触发 {det['n_fused_chains']} 条) -> "
        f"final {n_final} + periodic {n_periodic}")

    cands = det["candidates"]
    cand_rec = [{"t": round(c["idx"] / args.fps, 2), "kind": c["kind"],
                 "lo_frame": str(lo_files[c["idx"]])} for c in cands]
    (root / "candidates.json").write_text(json.dumps(
        cand_rec, ensure_ascii=False, indent=1), encoding="utf-8")
    (root / "change_curve.csv").write_text(
        "frame,t,dist\n" + "\n".join(
            f"{k+1},{(k+1)/args.fps:.2f},{d}"
            for k, d in enumerate(dists)), encoding="ascii")

    # ---- 3) 高清定点重拉 ----
    hd: Dict[float, str] = {}
    hd_bytes = 0
    if not args.skip_hd:
        ts = [c["t"] for c in cand_rec]
        log(f"定点重拉 {len(ts)} 个候选 (逐候选 -ss 定位) ...")
        hd = refetch_hd(sess, args.bvid, meta["cid"], cookie, ts, hd_dir,
                        proxy_wrap=proxy.wrap)
        hd_bytes = proxy.take_bytes()
        log(f"重拉命中 {len(hd)}/{len(ts)}, 网络拉取 {hd_bytes/1e6:.2f} MB")
    proxy.stop()

    # ---- 4) 拼图（无高清时用低清尾帧代替） ----
    sheet = root / "contact_sheet.jpg"
    if hd:
        kind_by_t = {c["t"]: c["kind"] for c in cand_rec}
        contact_sheet([(hd[t], f"{'F' if kind_by_t[t]=='final' else 'P'} t={t:.1f}s")
                       for t in sorted(hd)], sheet)
    else:
        contact_sheet([(c["lo_frame"],
                        f"{'F' if c['kind']=='final' else 'P'} t={c['t']:.1f}s")
                       for c in cand_rec], sheet)

    # ---- 5) 清理 + 报告 ----
    if not args.keep_lo:
        for p in lo_dir.glob("lo_*.jpg"):
            p.unlink()
        try:
            lo_dir.rmdir()
        except OSError:
            pass
    report = {
        "bvid": args.bvid, "title": meta["title"], "duration_s": meta["duration"],
        "scan": {"net_MB": round(scan_bytes / 1e6, 2),
                 "wall_s": scan["wall_s"], "n_frames": scan["n_frames"],
                 "fps": args.fps, "quality_id": low["id"],
                 "declared_bandwidth_kbps": int(low["bandwidth"] / 1000),
                 "measured_bitrate_Mbps": round(est_full * 8 / 1e6, 2)},
        "hd_refetch": {"net_MB": round(hd_bytes / 1e6, 2),
                       "requested": len(cand_rec), "hit": len(hd),
                       "window_s": HD_WINDOW, "seek_back_s": HD_SEEK_BACK,
                       "skipped": args.skip_hd},
        "detect": {"still_thr": STILL_THR, "min_span_s": MIN_SPAN,
                   "merge_thr": MERGE_THR, "still_pair_ratio": round(still_ratio, 3),
                   "platforms": len(det["platforms"]), "merge_chains": det["n_groups"],
                   "chain_max_span_s": args.chain_max_span,
                   "periodic_every_s": args.periodic_every,
                   "fused_chains": det["n_fused_chains"],
                   "final_candidates": n_final, "periodic_candidates": n_periodic,
                   "dup_groups": [len(g) for g in det["dup_groups"] if len(g) > 1]},
        "outputs": {"candidates": str(root / "candidates.json"),
                    "curve": str(root / "change_curve.csv"),
                    "sheet": str(sheet), "hd_dir": str(hd_dir)},
    }
    (root / "report.json").write_text(json.dumps(report, ensure_ascii=False,
                                                 indent=1), encoding="utf-8")
    log("==== 报告 ====")
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
