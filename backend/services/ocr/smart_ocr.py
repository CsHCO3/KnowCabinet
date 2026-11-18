# -*- coding: utf-8 -*-
"""智能 OCR：本地复杂度二分类 + GLM 4.6/5 双模型 + 独立 md 输出。

用法（CLI 由 scripts/smart_ocr.py 或 backend.main ocr-smart 调用）：
    python -m backend.main ocr-smart --dir <图片文件夹> [--output 结果.md] [--dry-run]

设计：
1. 本地微信 OCR（scripts/ 下的运行时）提取文字量/文本块/行数/列数；
2. PIL 计算墨迹密度、边缘密度、文本框覆盖；
3. 综合打分，>=0.55 判定为复杂 → GLM-5V-Turbo，否则 → GLM-4.6V；
4. 实际调用时逐图调用对应 GLM 模型，结果写成一个独立 md，不嵌入原文。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Optional

from PIL import Image, ImageFilter, ImageOps

# ── 路径常量 ──────────────────────────────────────
INFOSYSTEM_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCRIPTS_DIR = INFOSYSTEM_ROOT / "scripts"
WECHAT_OCR_EXE = Path(r"C:\Users\HP\AppData\Roaming\Tencent\WeChat\XPlugin\Plugins\WeChatOCR\7079\extracted\WeChatOCR.exe")
WECHAT_DIR = Path(r"C:\Program Files\Tencent\WeChat\[3.9.12.55]")
WECHAT_DIR = WECHAT_DIR if WECHAT_DIR.exists() else Path(r"C:\Program Files\Tencent\WeChat")

SIMPLE_MODEL = "glm-4.6v_ocr"
COMPLEX_MODEL = "glm-5v-turbo_ocr"
COMPLEX_THRESHOLD = 0.55
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".webp", ".gif", ".emf", ".wmf"}


def _load_prompts() -> tuple[str, str]:
    """加载新标准提示词：system + user。"""
    from backend.pipelines._prompts import load_prompt
    try:
        return load_prompt("ocr_standard")
    except Exception:
        from backend.pipelines._prompts import load_system_prompt
        system = load_system_prompt("ocr")
        user = "请分析这张论文示意图，严格按系统提示词输出。"
        return system, user


# ── 本地微信 OCR ─────────────────────────────────
class LocalWeChatOCR:
    """封装 wechat_ocr 包，使用 scripts/ 下的运行时。"""

    def __init__(self):
        self._manager = None

    def _ensure_started(self):
        if self._manager is not None:
            return
        from wechat_ocr.ocr_manager import OcrManager
        manager = OcrManager(str(SCRIPTS_DIR))
        manager.SetExePath(str(WECHAT_OCR_EXE))
        manager.SetUsrLibDir(str(WECHAT_DIR))
        manager.StartWeChatOCR()
        self._manager = manager

    def recognize(self, image_path: str) -> Optional[dict]:
        self._ensure_started()
        result_container: dict = {}

        def callback(_img_path: str, results: dict):
            result_container["data"] = results

        self._manager.SetOcrResultCallback(callback)
        self._manager.DoOCRTask(str(image_path))
        time.sleep(1.5)
        return result_container.get("data")

    def close(self):
        if self._manager is not None:
            try:
                self._manager.KillWeChatOCR()
            except Exception:
                pass
            self._manager = None


# ── 特征提取 ─────────────────────────────────────
def extract_plain_text(ocr_result: dict) -> str:
    """按 Y 坐标把 OCR 文本块转成行文本。"""
    if not ocr_result or "ocrResult" not in ocr_result:
        return ""
    items = sorted(ocr_result["ocrResult"], key=lambda x: x["location"]["top"])
    plain_lines = []
    current_line_texts = []
    last_top = None
    for item in items:
        current_top = item["location"]["top"]
        if last_top is not None and abs(current_top - last_top) > 15:
            plain_lines.append(" ".join(current_line_texts))
            current_line_texts = []
        current_line_texts.append(item["text"])
        last_top = current_top
    if current_line_texts:
        plain_lines.append(" ".join(current_line_texts))
    return "\n".join(plain_lines)


def smart_ocr_parse(ocr_result: dict, column_threshold: float = 80.0) -> list:
    """按 X 坐标粗略聚成列，用于衡量空间复杂度。"""
    if not ocr_result or "ocrResult" not in ocr_result:
        return []
    items = list(ocr_result["ocrResult"])
    if not items:
        return []
    items = sorted(items, key=lambda x: x["location"]["left"])
    columns = [[items[0]]]
    for i in range(1, len(items)):
        gap = items[i]["location"]["left"] - items[i - 1]["location"]["left"]
        if gap > column_threshold:
            columns.append([])
        columns[-1].append(items[i])
    return columns


def image_features(path: Path) -> dict:
    img = Image.open(path).convert("RGB")
    w, h = img.size
    gray = ImageOps.grayscale(img)
    hist = gray.histogram()
    dark = sum(hist[:128])
    ink_ratio = dark / (w * h) if w * h else 0
    edge = gray.filter(ImageFilter.FIND_EDGES)
    edge_hist = edge.histogram()
    edge_pixels = sum(edge_hist[80:])
    edge_ratio = edge_pixels / (w * h) if w * h else 0
    return {
        "width": w,
        "height": h,
        "file_size": path.stat().st_size,
        "ink_ratio": round(ink_ratio, 4),
        "edge_ratio": round(edge_ratio, 4),
    }


def ocr_features(ocr_result: dict, w: int, h: int) -> dict:
    items = ocr_result.get("ocrResult", []) if ocr_result else []
    text_chars = sum(len(str(it.get("text", ""))) for it in items)
    blocks = len(items)
    lines = len(extract_plain_text(ocr_result).splitlines()) if ocr_result else 0
    columns = len(smart_ocr_parse(ocr_result)) if ocr_result else 0
    bbox_area = 0
    for it in items:
        loc = it.get("location", {})
        bw = max(0, loc.get("right", 0) - loc.get("left", 0))
        bh = max(0, loc.get("bottom", 0) - loc.get("top", 0))
        bbox_area += bw * bh
    bbox_area_ratio = bbox_area / (w * h) if w * h else 0
    return {
        "text_blocks": blocks,
        "text_chars": text_chars,
        "lines": lines,
        "columns": columns,
        "bbox_area_ratio": round(bbox_area_ratio, 4),
    }


def classify(feats: dict) -> tuple[str, float]:
    text_score = min(feats["text_chars"] / 300, 1.0)
    block_score = min(feats["text_blocks"] / 15, 1.0)
    line_score = min(feats["lines"] / 12, 1.0)
    col_score = min(feats["columns"] / 4, 1.0)
    edge_score = min(feats["edge_ratio"] / 0.25, 1.0)
    bbox_score = min(feats["bbox_area_ratio"] / 0.35, 1.0)
    score = (
        text_score * 0.30 + block_score * 0.20 + line_score * 0.15
        + col_score * 0.10 + edge_score * 0.15 + bbox_score * 0.10
    )
    label = "复杂" if score >= COMPLEX_THRESHOLD else "简单"
    return label, round(score, 3)


# ── GLM 调用 ─────────────────────────────────────
def _ocr_with_llm(llm, image_path: str, system_prompt: str, user_prompt: str) -> str:
    messages = llm.build_messages(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        input_data=image_path,
        input_type="image",
    )
    return llm.call(messages=messages, max_tokens=8192, temperature=0.3)


def _build_md(results: list[dict]) -> str:
    lines = ["# 图片 OCR 结果", ""]
    lines.append(f"> 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"> 分类阈值: {COMPLEX_THRESHOLD}（复杂 → {COMPLEX_MODEL}，简单 → {SIMPLE_MODEL}）")
    lines.append("")
    for r in results:
        lines.append(f"## {r['image']}")
        lines.append("")
        lines.append(f"- 分类: {r['label']}（score={r['score']}）")
        lines.append(f"- 模型: {r['model']}")
        lines.append(f"- 本地OCR特征: 字符 {r['text_chars']} / 文本块 {r['text_blocks']} / 行 {r['lines']} / 列 {r['columns']}")
        lines.append("")
        lines.append(r.get("ocr", "（dry-run 未调用）").strip())
        lines.append("")
    return "\n".join(lines)


# ── 主入口 ───────────────────────────────────────
def run_smart_ocr(folder: str, output: Optional[str] = None, dry_run: bool = False) -> dict:
    folder_path = Path(folder)
    if not folder_path.is_dir():
        raise NotADirectoryError(f"目录不存在: {folder_path}")

    images = sorted(
        p for p in folder_path.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    )
    if not images:
        print(f"没有找到图片: {folder_path}")
        return {"ok": False, "error": "no_images"}

    system_prompt, user_prompt = _load_prompts()
    local = LocalWeChatOCR()
    results = []
    try:
        for idx, img in enumerate(images, 1):
            print(f"[{idx}/{len(images)}] {img.name}", flush=True)
            feats = image_features(img)
            try:
                ocr = local.recognize(str(img))
            except Exception as exc:
                print(f"  本地OCR失败: {exc}", flush=True)
                ocr = None
            if ocr:
                feats.update(ocr_features(ocr, feats["width"], feats["height"]))
            else:
                feats.update({"text_blocks": 0, "text_chars": 0, "lines": 0,
                              "columns": 0, "bbox_area_ratio": 0})
            label, score = classify(feats)
            model = COMPLEX_MODEL if label == "复杂" else SIMPLE_MODEL
            item = {
                "image": img.name,
                "path": str(img),
                "label": label,
                "score": score,
                "model": model,
                **feats,
            }
            results.append(item)
            print(f"  -> {label} score={score} model={model} chars={feats['text_chars']} blocks={feats['text_blocks']} lines={feats['lines']} cols={feats['columns']} edge={feats['edge_ratio']}", flush=True)
    finally:
        local.close()

    if dry_run:
        print("\n[dry-run] 不调用 GLM API，只输出分类结果。")
        return {"ok": True, "dry_run": True, "results": results}

    from backend.config.settings import Settings
    settings = Settings()
    print("\n开始调用 GLM API...", flush=True)
    for item in results:
        print(f"  {item['image']} → {item['model']} ...", flush=True)
        llm = settings.get_llm_client(model_key=item["model"])
        text = _ocr_with_llm(llm, item["path"], system_prompt, user_prompt)
        item["ocr"] = text.strip()
        print(f"    完成 ({len(text)} 字符)", flush=True)

    out_path = Path(output) if output else folder_path / "ocr_result.md"
    if not out_path.is_absolute():
        out_path = folder_path / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(_build_md(results), encoding="utf-8")
    print(f"\n输出: {out_path}")
    return {"ok": True, "output": str(out_path), "results": results}


if __name__ == "__main__":
    # 简单调试入口：python backend/services/ocr/smart_ocr.py <dir> [--dry-run]
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--dir", required=True)
    p.add_argument("--output", default=None)
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    run_smart_ocr(a.dir, a.output, a.dry_run)

