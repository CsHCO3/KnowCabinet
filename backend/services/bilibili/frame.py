"""
B站视频指定时间点抽帧（单 ffmpeg 进程批量版）。

依赖:
- requests
- ffmpeg（需在 PATH 中）
- 可选: browser-cookie3
"""

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

from backend.config.settings import Settings

_settings = Settings()


class BiliFrameExtractor:
    def __init__(
        self,
        dedeuserid: Optional[str] = "103279774",
        cookie: Optional[str] = None,
        request_timeout: int = 20,
    ):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            "Referer": "https://www.bilibili.com/",
            "Origin": "https://www.bilibili.com",
        })
        self.request_timeout = request_timeout
        self.dedeuserid = dedeuserid
        self.cookie = cookie or _settings.bili_cookie or os.getenv("BILI_COOKIE", "")
        if self.cookie:
            self.session.headers["Cookie"] = self.cookie
        self._check_ffmpeg()

    def _check_ffmpeg(self) -> None:
        if shutil.which("ffmpeg") is None:
            raise RuntimeError(
                "未检测到 ffmpeg，请先安装并加入 PATH。\n"
                "Windows: https://ffmpeg.org/download.html"
            )

    @staticmethod
    def detect_dedeuserid() -> Optional[str]:
        env_value = os.getenv("BILI_DEDEUSERID", "").strip()
        if env_value:
            return env_value
        if _settings.bili_cookie_dedeuserid:
            return _settings.bili_cookie_dedeuserid
        try:
            import browser_cookie3
        except Exception:
            return None
        for loader in [browser_cookie3.chrome, browser_cookie3.edge, browser_cookie3.firefox]:
            try:
                cj = loader(domain_name=".bilibili.com")
                for cookie in cj:
                    if cookie.name == "DedeUserID" and cookie.value:
                        return str(cookie.value)
            except Exception:
                continue
        return None

    @staticmethod
    def _video_stream_score(stream: Dict) -> Tuple[int, int, int, int]:
        width = int(stream.get("width", 0) or 0)
        height = int(stream.get("height", 0) or 0)
        fps_raw = str(stream.get("frame_rate", "") or "")
        fps = 0
        if "/" in fps_raw:
            try:
                num, den = fps_raw.split("/", 1)
                den_f = float(den)
                if den_f > 0:
                    fps = int(float(num) / den_f)
            except Exception:
                fps = 0
        else:
            try:
                fps = int(float(fps_raw))
            except Exception:
                fps = 0
        bandwidth = int(stream.get("bandwidth", 0) or 0)
        quality_id = int(stream.get("id", 0) or 0)
        return width * height, fps, bandwidth, quality_id

    @staticmethod
    def _safe_json(response) -> Optional[dict]:
        try:
            return response.json()
        except Exception:
            return None

    def get_video_url(self, bvid: str) -> Dict[str, object]:
        view_url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
        view_resp = self._safe_json(self.session.get(view_url, timeout=self.request_timeout))
        if not view_resp:
            raise RuntimeError("获取视频信息失败: 非 JSON 响应")
        if view_resp.get("code") != 0:
            raise RuntimeError(f"获取视频信息失败: {view_resp.get('message', 'unknown')}")

        data = view_resp["data"]
        cid = data["cid"]
        title = data.get("title", bvid)

        qn_candidates = [127, 126, 125, 120, 116, 112, 80, 64, 32]
        best_stream = None
        best_payload = None
        last_err = ""

        for qn in qn_candidates:
            play_url = (
                "https://api.bilibili.com/x/player/playurl"
                f"?bvid={bvid}&cid={cid}&qn={qn}&fnver=0&fnval=4048&fourk=1"
            )
            play_resp = self._safe_json(self.session.get(play_url, timeout=self.request_timeout))
            if not play_resp:
                last_err = f"qn={qn} 非 JSON 响应"
                continue
            if play_resp.get("code") != 0:
                last_err = str(play_resp.get("message", "unknown"))
                continue

            payload = play_resp.get("data", {})
            dash = payload.get("dash", {})
            videos = dash.get("video", [])
            if not videos:
                continue
            candidate = max(videos, key=self._video_stream_score)
            if best_stream is None or self._video_stream_score(candidate) > self._video_stream_score(best_stream):
                best_stream = candidate
                best_payload = payload

        if best_stream is not None:
            video_url = best_stream.get("baseUrl") or best_stream.get("base_url")
            if not video_url:
                raise RuntimeError("未找到有效视频流地址")
            return {
                "title": title,
                "url": video_url,
                "quality_id": int(best_stream.get("id", 0) or 0),
                "width": int(best_stream.get("width", 0) or 0),
                "height": int(best_stream.get("height", 0) or 0),
                "codecs": str(best_stream.get("codecs", "") or ""),
                "frame_rate": str(best_stream.get("frame_rate", "") or ""),
            }

        durl = (best_payload or {}).get("durl", [])
        if durl and durl[0].get("url"):
            return {"title": title, "url": durl[0]["url"], "quality_id": 0,
                    "width": 0, "height": 0, "codecs": "", "frame_rate": ""}

        raise RuntimeError(f"未找到视频流: {last_err or 'unknown'}")

    def _build_ffmpeg_headers(self) -> str:
        parts = [
            f"Referer: {self.session.headers['Referer']}",
            f"User-Agent: {self.session.headers['User-Agent']}",
        ]
        if self.cookie:
            parts.append(f"Cookie: {self.cookie}")
        elif self.dedeuserid:
            parts.append(f"Cookie: DedeUserID={self.dedeuserid}")
        return "\r\n".join(parts) + "\r\n"

    @staticmethod
    def _parse_pts_times(stderr_text: str) -> List[float]:
        pts_times = []
        for line in stderr_text.splitlines():
            m = re.search(r"pts_time:([0-9]+(?:\.[0-9]+)?)", line)
            if m:
                pts_times.append(float(m.group(1)))
        return pts_times

    @staticmethod
    def _greedy_match(targets_abs: List[float], frames_abs: List[Tuple[str, float]], tolerance_sec: float) -> Dict[float, str]:
        matched: Dict[float, str] = {}
        used = set()
        for ts in sorted(targets_abs):
            best_i = -1
            best_delta = 1e9
            for i, (_, ft) in enumerate(frames_abs):
                if i in used:
                    continue
                delta = abs(ft - ts)
                if delta < best_delta:
                    best_delta = delta
                    best_i = i
            if best_i >= 0 and best_delta <= tolerance_sec:
                used.add(best_i)
                matched[ts] = frames_abs[best_i][0]
        return matched

    def extract_frames_batch(
        self,
        bvid: str,
        timestamps: List[float],
        output_dir: str | None = None,
        keyframe_only: bool = False,
        window_sec: float = 0.08,
        tolerance_sec: float = 0.30,
    ) -> Dict[float, str]:
        if not timestamps:
            return {}
        if output_dir is None:
            from backend.utils.paths import Paths
            output_dir = str(Paths().frames)
        os.makedirs(output_dir, exist_ok=True)

        info = self.get_video_url(bvid)
        video_url = str(info["url"])
        sorted_ts = sorted(float(x) for x in timestamps)

        start_ts = max(0.0, sorted_ts[0])
        eps = max(0.001, window_sec / 2)
        select_conditions = []
        for ts in sorted_ts:
            rel = max(0.0, ts - start_ts)
            low = max(0.0, rel - eps)
            high = rel + eps
            cond = f"between(t\\,{low:.6f}\\,{high:.6f})"
            if keyframe_only:
                cond = f"({cond})*eq(pict_type\\,I)"
            select_conditions.append(cond)

        select_expr = "+".join(select_conditions)
        vf_expr = f"select='{select_expr}',showinfo"
        temp_dir = tempfile.mkdtemp(prefix="bili_frames_")
        pattern = os.path.join(temp_dir, "frame_%06d.jpg")

        headers = self._build_ffmpeg_headers()
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "info",
            "-ss", f"{start_ts:.6f}",
            "-headers", headers,
            "-i", video_url,
            "-vf", vf_expr,
            "-fps_mode", "vfr", "-q:v", "1", "-f", "image2",
            "-start_number", "0", pattern,
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)
        if result.returncode != 0 and not os.listdir(temp_dir):
            raise RuntimeError(f"ffmpeg 执行失败: {result.stderr[:500]}")

        files = sorted([os.path.join(temp_dir, x) for x in os.listdir(temp_dir) if x.lower().endswith(".jpg")])
        pts_rel = self._parse_pts_times(result.stderr)
        n = min(len(files), len(pts_rel))
        frames_abs = [(files[i], start_ts + pts_rel[i]) for i in range(n)]
        matched = self._greedy_match(sorted_ts, frames_abs, tolerance_sec=tolerance_sec)

        output_map: Dict[float, str] = {}
        for ts in sorted_ts:
            src = matched.get(ts)
            if not src:
                continue
            dst = os.path.join(output_dir, f"frame_{ts:.3f}s.jpg")
            if os.path.exists(dst):
                os.unlink(dst)
            os.replace(src, dst)
            output_map[ts] = dst

        for name in os.listdir(temp_dir):
            try:
                os.unlink(os.path.join(temp_dir, name))
            except OSError:
                pass
        try:
            os.rmdir(temp_dir)
        except OSError:
            pass

        return output_map
