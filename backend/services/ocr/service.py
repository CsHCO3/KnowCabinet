"""本地 OCR 服务 — 对 Markdown 文档中的图片进行文字识别。

工作方式：
1. 扫描 .md 文件中的图片引用 `![](image.jpg)`
2. 图片位于 `{md所在目录}/{md文件名}/` 下
3. 并行调用 LLMClient（支持同步/异步两种底层实现）
4. 在原图片引用下方插入识别结果

支持模型（model_config.json 中以 _ocr 后缀的 key）：
  - qwen-vl-max_ocr:   Qwen-VL-Max（推荐，默认）
  - glm-4.6v_ocr:      GLM-4.6V
  - paddleocr-vl1.6_ocr: PaddleOCR-VL-1.6（异步 Job API）
"""

import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from backend.config.settings import Settings
from backend.core.llm.client import LLMClient

logger = logging.getLogger("infosystem.ocr")


# ── Markdown 图片解析 ──────────────────────────────

_IMAGE_PATTERN = re.compile(
    r'!\[.*?\]\(([^)]+\.(?:png|jpg|jpeg|gif|webp|bmp))\)',
    re.IGNORECASE,
)


def _find_image_refs(md_content: str) -> list[tuple[int, int, str]]:
    """找到 markdown 中所有图片引用。

    Returns:
        [(match_start, match_end, image_filename), ...]
    """
    refs = []
    for m in _IMAGE_PATTERN.finditer(md_content):
        refs.append((m.start(), m.end(), m.group(1)))
    return refs


_OCR_BLOCK_START = "<!-- OCR_RESULT_BLOCK_START -->"
_OCR_BLOCK_END = "<!-- OCR_RESULT_BLOCK_END -->"


def _has_ocr_result(md_content: str, ref_end: int, next_ref_start: int | None = None) -> bool:
    """检查图片引用之后、下一张图片引用之前是否已有 OCR 结果。

    只查当前图片到下一张图片之间的窗口，避免把文档后面其他图片的 OCR 结果
    误判为当前图片已有结果。
    """
    window = md_content[ref_end:next_ref_start] if next_ref_start is not None else md_content[ref_end:]
    return _OCR_BLOCK_START in window


_OCR_INSERT_TEMPLATE = "\n" + _OCR_BLOCK_START + "\n---\n图片OCR结果如下：\n{}\n---\n" + _OCR_BLOCK_END

# ── 学术向 OCR 提示词（存于提示词库 ocr_system.txt，可用 /prompts 查看/编辑） ──
from backend.pipelines._prompts import load_system_prompt

def _load_ocr_prompt() -> str:
    try:
        return load_system_prompt("ocr").strip()
    except Exception:
        return "请提取这张图片中的所有文字内容。"

# ── 参考文献段剔除（信息量太小时可选去掉） ─────────
_REF_HEADING = re.compile(r"^#{1,4}\s*(References|Bibliography|参考(?:文献)?)\s*$", re.IGNORECASE)


def _strip_references(content: str) -> str:
    """去掉文档末尾的 References/参考文献 段。

    仅当该标题出现在文档后 40% 处才截断，避免误伤正文里的同名小节。
    """
    lines = content.split("\n")
    total = len(lines)
    strip_at = -1
    for i, line in enumerate(lines):
        if _REF_HEADING.match(line.strip()):
            if i > total * 0.6:
                strip_at = i
    if strip_at != -1:
        return "\n".join(lines[:strip_at]).rstrip() + "\n"
    return content


# ── OCR 服务 ──────────────────────────────────────

class OCRService:
    """对 Markdown 文档中的图片进行 OCR 识别。

    所有模型统一走 LLMClient 的 submit_image() + wait_job() 接口。
    LLMClient 内部根据 job_api_type 决定用线程池同步调用还是原生异步 API。
    """

    def __init__(self, model_key: str = "qwen-vl-max_ocr"):
        settings = Settings()
        self.client: LLMClient = settings.get_llm_client(model_key=model_key)
        # 提示词从提示词库加载（ocr_system.txt），每次实例化读一次，改库后新任务即生效
        self.prompt: str = _load_ocr_prompt()

    # ── 单图 OCR ────────────────────────────────

    def ocr_image(self, image_path: str) -> Optional[str]:
        """对单张图片进行 OCR，返回文字内容，失败返回 None。

        统一使用 submit_image + wait_job 异步接口。
        LLMClient 内部自行决定是走线程池（OpenAI 格式）还是原生 Job API（PaddleOCR）。
        """
        try:
            ticket = self.client.submit_image(
                image_path=image_path,
                prompt=self.prompt,
                max_tokens=8192,
            )
            return self.client.wait_job(ticket, timeout=300).strip()
        except Exception as exc:
            logger.warning("OCR failed for %s: %s", image_path, exc)
            return None

    @staticmethod
    def _resolve_image(md_dir: Path, md_stem: str, img_name: str) -> tuple[Path | None, str]:
        """解析图片实际路径。

        优先 {md_dir}/{md_stem}/{img_name}（Notion 约定：md 同名的文件夹）；
        退回 {md_dir}/{img_name}（图片与 md 同目录，如 mineru 的 images/xxx）。
        两者都出现时优先子文件夹。返回 (path, "subfolder"|"same_dir"|"").
        """
        p_sub = md_dir / md_stem / img_name
        if p_sub.exists():
            return p_sub, "subfolder"
        p_same = md_dir / img_name
        if p_same.exists():
            return p_same, "same_dir"
        return None, ""

    # ── 单文件处理 ──────────────────────────────

    def process_file(self, md_path: str, parallel: bool = True, retry: bool = False,
                     inplace: bool = False, min_size_kb: int = 7,
                     strip_refs: bool = True) -> dict:
        """处理单个 .md 文件中的所有图片。

        默认生成新文件 `{md_stem}_ocr.md`（保留原文件）；`inplace=True` 时原地修改。
        图片定位：优先 `{md_dir}/{md_stem}/`（Notion 约定），退回 `{md_dir}/`（图片与 md 同目录）。
        小于 `min_size_kb` KB 的图片跳过（疑似装饰图/头像/二维码），节省 API 调用。
        可选剔除末尾 References/参考文献 段。

        Returns:
            {"file", "output", "total", "success", "failed", "skipped", "images", "warn_same_dir"}
        """
        md_file = Path(md_path)
        if not md_file.exists():
            raise FileNotFoundError(f"File not found: {md_path}")

        md_dir = md_file.parent
        md_stem = md_file.stem
        out_path = md_file if inplace else (md_dir / f"{md_stem}_ocr.md")

        content = md_file.read_text(encoding="utf-8")
        refs = _find_image_refs(content)

        result = {
            "file": str(md_file),
            "output": str(out_path),
            "total": len(refs),
            "success": 0,
            "failed": 0,
            "skipped": 0,
            "images": [],
            "warn_same_dir": False,
        }

        if not refs:
            return result

        # ── 构建待处理列表 ──
        tasks: list[dict] = []
        for i, (start, end, img_name) in enumerate(refs):
            next_start = refs[i + 1][0] if i + 1 < len(refs) else None
            if retry and _has_ocr_result(content, end, next_start):
                result["skipped"] += 1
                result["images"].append({"image": img_name, "status": "skipped", "reason": "has_result"})
                continue

            img_path, loc = self._resolve_image(md_dir, md_stem, img_name)
            if img_path is None:
                print(f"  ⚠ 图片不存在: {img_name}")
                result["failed"] += 1
                result["images"].append({"image": img_name, "status": "missing"})
                continue
            if loc == "same_dir":
                result["warn_same_dir"] = True

            # 尺寸跳过（装饰图/头像/二维码）
            try:
                size = img_path.stat().st_size
            except OSError:
                size = 0
            if size < min_size_kb * 1024:
                result["skipped"] += 1
                result["images"].append({
                    "image": img_name, "status": "skipped",
                    "reason": f"decorative(<{min_size_kb}KB, {size}B)",
                })
                continue

            tasks.append({"start": start, "end": end, "img_name": img_name, "img_path": str(img_path),
                          "next_start": next_start})

        if not tasks:
            # 全部跳过/缺失：若开了 strip_refs，仍生成新文件（去掉参考文献段）
            if strip_refs:
                stripped = _strip_references(md_file.read_text(encoding="utf-8"))
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(stripped, encoding="utf-8")
            return result

        # ── 执行 OCR ──
        ocr_results: list[dict] = []
        if parallel and len(tasks) > 1:
            with ThreadPoolExecutor(max_workers=min(len(tasks), 8)) as pool:
                future_map = {
                    pool.submit(self.ocr_image, t["img_path"]): t
                    for t in tasks
                }
                for future in as_completed(future_map):
                    t = future_map[future]
                    ocr_text = future.result()
                    ocr_results.append({**t, "ocr_text": ocr_text})
        else:
            for t in tasks:
                print(f"  🔍 OCR: {t['img_name']}...", end=" ", flush=True)
                t0 = time.time()
                ocr_text = self.ocr_image(t["img_path"])
                elapsed = time.time() - t0
                ocr_results.append({**t, "ocr_text": ocr_text})
                status = "✅" if ocr_text else "❌"
                print(f"{status} ({elapsed:.1f}s)", flush=True)

        # ── 在内存中从底部向上插入（避免位置偏移），最后一次性写回 ──
        new_content = md_file.read_text(encoding="utf-8")
        ocr_results.sort(key=lambda x: x["end"], reverse=True)
        for item in ocr_results:
            start, end, img_name, ocr_text = item["start"], item["end"], item["img_name"], item["ocr_text"]
            next_start = item.get("next_start")

            if retry and _has_ocr_result(new_content, end, next_start):
                result["skipped"] += 1
                continue

            if ocr_text:
                insertion = _OCR_INSERT_TEMPLATE.format(ocr_text)
                line_end = new_content.find("\n", end)
                insert_pos = line_end + 1 if line_end != -1 else len(new_content)
                new_content = new_content[:insert_pos] + insertion + new_content[insert_pos:]
                result["success"] += 1
                result["images"].append({"image": img_name, "status": "success", "len": len(ocr_text)})
            else:
                result["failed"] += 1
                result["images"].append({"image": img_name, "status": "failed"})

        # 可选剔除末尾参考文献
        if strip_refs:
            new_content = _strip_references(new_content)

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(new_content, encoding="utf-8")
        return result

    # ── 目录批量处理 ──────────────────────────────

    def process_directory(self, directory: str, parallel: bool = True, retry: bool = False,
                          inplace: bool = False, min_size_kb: int = 7,
                          strip_refs: bool = True) -> list[dict]:
        """处理目录下所有 .md 文件（跳过本工具生成的 *_ocr.md，避免重复处理）。"""
        md_dir = Path(directory)
        if not md_dir.is_dir():
            raise NotADirectoryError(f"Directory not found: {directory}")

        md_files = sorted(md_dir.glob("*.md"))
        md_files = [p for p in md_files if not p.stem.lower().endswith("_ocr")]
        if not md_files:
            print(f"  没有找到可处理的 .md 文件（*_ocr.md 已自动跳过）: {directory}")
            return []

        results = []
        for md_file in md_files:
            print(f"\n📄 {md_file.name}")
            r = self.process_file(str(md_file), parallel=parallel, retry=retry,
                                  inplace=inplace, min_size_kb=min_size_kb, strip_refs=strip_refs)
            results.append(r)
            print(f"   总计 {r['total']} 图 → {r['success']} 成功 / {r['failed']} 失败 / {r['skipped']} 跳过"
                  + (f" → {r['output']}" if not inplace else ""))

        return results
