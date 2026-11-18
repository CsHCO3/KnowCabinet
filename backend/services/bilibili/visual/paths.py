"""visual 子包统一路径（沙盒时代的 out2/out3 已收敛至此）。"""
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[4]
SUB_DIR = PROJECT_ROOT / "data" / "bilibili" / "subtitles"
VISUAL_ROOT = PROJECT_ROOT / "data" / "bilibili" / "visual"
DENSE_DIR = PROJECT_ROOT / "knowledgebase" / "community" / "bilibili" / "dense"
HERE = Path(__file__).resolve().parent          # ocr_win.ps1 等资产所在


def subtitle_cache_path(bvid: str, page: int = 1) -> Path:
    """P1 沿用旧的 <bvid>.json；P2 起叫 <bvid>_p<N>.json，避免互相覆盖。"""
    return SUB_DIR / (f"{bvid}.json" if page <= 1 else f"{bvid}_p{page}.json")


_KB_ID_RE = re.compile(r"^(BV[0-9A-Za-z]{10})_p(\d+)$")   # BV 号字符表里没有下划线


def kb_id(bvid: str, page: int = 1) -> str:
    """一个分P 在全链路的统一标识：P1 就是 bvid（历史行为零改动），P2+ 加后缀 _p<N>。

    visual 目录名、digest frontmatter id、dense md 文件名、_frames 子目录、
    字幕缓存文件名（subtitle_cache_path）都等于它。
    """
    return bvid if page <= 1 else f"{bvid}_p{page}"


def split_kb_id(kid: str) -> tuple[str, int]:
    """"BVxxx_p2" → ("BVxxx", 2)；普通 bvid → (自身, 1)。"""
    m = _KB_ID_RE.match(kid or "")
    return (m.group(1), int(m.group(2))) if m else (kid, 1)


def visual_dir(bvid: str, page: int = 1) -> Path:
    """这一集的过程产物目录：P1 用旧目录名，P2+ 用 <bvid>_p<N>。"""
    return VISUAL_ROOT / kb_id(bvid, page)
