"""从 markdown 中提取截图标记 [SHOT:秒数] 并回填图片。"""

import re
from pathlib import Path
from typing import Dict, List, Tuple

from backend.services.bilibili.frame import BiliFrameExtractor


def _safe_name(raw: str, fallback: str = "untitled") -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|]+", "_", raw).strip().strip(".")
    return cleaned or fallback


def _extract_shot_markers(markdown_text: str) -> List[Tuple[float, str]]:
    pattern = re.compile(r"\[SHOT:(\d+(?:\.\d+)?)(?:\|([^\]]+))?\]")
    items: List[Tuple[float, str]] = []
    for m in pattern.finditer(markdown_text):
        ts = float(m.group(1))
        reason = (m.group(2) or "").strip()
        items.append((ts, reason))
    return items


def apply_screenshot_hints_to_markdown(
    md_path: Path,
    bvid: str,
    title: str,
    task_root: Path,
    max_shots: int = 8,
) -> Dict[str, int]:
    text = md_path.read_text(encoding="utf-8")
    markers = _extract_shot_markers(text)
    if not markers:
        return {"found_markers": 0, "generated_images": 0}

    dedup_times = sorted({ts for ts, _ in markers})[:max_shots]
    if not dedup_times:
        return {"found_markers": len(markers), "generated_images": 0}

    short_dir_name = _safe_name(title[:15], fallback="video_frames")
    frame_dir = task_root / short_dir_name
    frame_dir.mkdir(parents=True, exist_ok=True)

    extractor = BiliFrameExtractor(dedeuserid=BiliFrameExtractor.detect_dedeuserid())
    frame_map = extractor.extract_frames_batch(
        bvid=bvid, timestamps=dedup_times, output_dir=str(frame_dir), keyframe_only=False,
    )

    pattern = re.compile(r"\[SHOT:(\d+(?:\.\d+)?)(?:\|([^\]]+))?\]")

    def repl(match: re.Match) -> str:
        ts = float(match.group(1))
        reason = (match.group(2) or "").strip()
        if ts not in frame_map:
            return f"[截图失败 t={ts:.3f}s]"
        img_name = Path(frame_map[ts]).name
        rel = f"./{short_dir_name}/{img_name}"
        alt = f"{title} @ {ts:.3f}s"
        return f"{reason}\n\n![{alt}]({rel})" if reason else f"![{alt}]({rel})"

    replaced = pattern.sub(repl, text)
    md_path.write_text(replaced, encoding="utf-8")

    return {"found_markers": len(markers), "generated_images": len(frame_map)}
