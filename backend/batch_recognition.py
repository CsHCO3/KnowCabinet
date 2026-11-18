#!/usr/bin/env python3
"""批量图片识别工具 — 用视觉模型识别文件夹中所有图片，输出文字内容。

用法:
    # 提示词从命令行参数传入
    python backend/batch_recognition.py ./images --prompt "请提取这张图片中的所有文字"

    # 提示词从文件读取
    python backend/batch_recognition.py ./images --prompt-file ./ocr_prompt.txt

    # 同时指定 system prompt（来自文件）+ user prompt
    python backend/batch_recognition.py ./images \\
        --system-prompt ./ocr_system.txt \\
        --prompt "提取这张图片中的文字"

    # 指定模型 + 输出格式
    python backend/batch_recognition.py ./images \\
        --prompt "描述图片内容" \\
        --model qwen-vl-max_ocr \\
        --output ./results \\
        --format md

    # 指定图片扩展名筛选
    python backend/batch_recognition.py ./images \\
        --prompt "识别文字" \\
        --ext png,jpg,jpeg
"""

# ── 路径引导：向上查找项目根目录（CLAUDE.md / README.md 为标记）──
import sys
from pathlib import Path
_root = Path(__file__).resolve()
for _p in _root.parents:
    if (_p / "CLAUDE.md").exists() or (_p / "README.md").exists():
        _root = _p; break
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
# ─────────────────────────────────────────────────────

import argparse
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from backend.config.settings import Settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("batch_recognition")


# ── 支持的图片格式 ──────────────────────────────

DEFAULT_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}

# ── 输出格式模板 ────────────────────────────────

def _format_md_result(image_path: str, elapsed: float, text: str) -> str:
    name = Path(image_path).name
    return (
        f"## {name}\n\n"
        f"- **文件**: {image_path}\n"
        f"- **耗时**: {elapsed:.1f}s\n\n"
        f"### 识别结果\n\n{text}\n\n"
        f"---\n"
    )


def _format_txt_result(image_path: str, elapsed: float, text: str) -> str:
    sep = "=" * 60
    return (
        f"{sep}\n"
        f"文件: {image_path}\n"
        f"耗时: {elapsed:.1f}s\n"
        f"{sep}\n"
        f"{text}\n\n"
    )


# ── 核心识别逻辑 ────────────────────────────────

def recognize_image(llm, image_path: str, system_prompt: str,
                    user_prompt: str, max_tokens: int) -> Optional[str]:
    """用视觉模型识别单张图片。

    使用 LLMClient 的消息构建能力，传入 system_prompt 和 user_prompt。
    """
    try:
        ticket = llm.submit_image(
            image_path=image_path,
            prompt=user_prompt,
            max_tokens=max_tokens,
            temperature=0.3,
        )
        result = llm.wait_job(ticket, timeout=300)
        if system_prompt:
            # 如果有 system prompt，需要通过 call 接口重新构建消息
            # submit_image 不支持 system_prompt，所以直接走 call 接口
            return _recognize_with_system(llm, image_path, system_prompt,
                                          user_prompt, max_tokens)
        return result.strip()
    except Exception as exc:
        logger.warning("识别失败 %s: %s", image_path, exc)
        return None


def _recognize_with_system(llm, image_path: str, system_prompt: str,
                           user_prompt: str, max_tokens: int) -> Optional[str]:
    """支持 system prompt 的图片识别（走 call 接口手动构建消息）。"""
    try:
        messages = llm.build_messages(
            system_prompt=system_prompt,
            user_prompt=user_prompt or "请分析这张图片。",
            input_data=image_path,
            input_type="image",
        )
        result = llm.call(
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.3,
        )
        return result.strip()
    except Exception as exc:
        logger.warning("识别失败(带system prompt) %s: %s", image_path, exc)
        return None


# ── 图片扫描 ────────────────────────────────────

def scan_images(directory: str, extensions: set) -> list[Path]:
    """扫描目录下所有指定扩展名的图片文件（非递归）。"""
    img_dir = Path(directory)
    if not img_dir.is_dir():
        raise NotADirectoryError(f"目录不存在: {directory}")

    images = sorted(
        f for f in img_dir.iterdir()
        if f.is_file() and f.suffix.lower() in extensions
    )
    return images


# ── 批量处理 ────────────────────────────────────

def batch_recognize(
    image_dir: str,
    user_prompt: str,
    system_prompt: str = "",
    model_key: str = "qwen-vl-max_ocr",
    output_dir: Optional[str] = None,
    output_format: str = "txt",
    extensions: set = None,
    max_workers: int = 4,
    max_tokens: int = 4096,
    summary: bool = True,
) -> dict:
    """批量识别图片。

    Args:
        image_dir: 图片目录。
        user_prompt: 用户提示词。
        system_prompt: 系统提示词（可选）。
        model_key: 模型 key（默认 qwen-vl-max_ocr）。
        output_dir: 输出目录（None = 与图片目录相同）。
        output_format: 输出格式，txt 或 md。
        extensions: 图片扩展名集合。
        max_workers: 并行线程数。
        max_tokens: 最大输出 token 数。
        summary: 是否生成汇总文件。

    Returns:
        {"total": N, "success": N, "failed": N, "results": [...], "summary_path": ...}
    """
    if extensions is None:
        extensions = DEFAULT_IMAGE_EXTENSIONS

    # ── 初始化 LLM ──
    settings = Settings()
    llm = settings.get_llm_client(model_key=model_key)

    # ── 扫描图片 ──
    images = scan_images(image_dir, extensions)
    if not images:
        logger.warning("目录中没有找到图片: %s", image_dir)
        return {"total": 0, "success": 0, "failed": 0, "results": []}

    logger.info("找到 %d 张图片，模型: %s，并行数: %d", len(images), model_key, max_workers)

    # ── 输出目录 ──
    out_dir = Path(output_dir) if output_dir else Path(image_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── 执行识别 ──
    results: list[dict] = []
    format_func = _format_md_result if output_format == "md" else _format_txt_result
    ext = ".md" if output_format == "md" else ".txt"

    if max_workers > 1 and len(images) > 1:
        # ── 并行模式 ──
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_map = {}
            for img_path in images:
                future = pool.submit(
                    _recognize_with_system if system_prompt else recognize_image,
                    llm, str(img_path), system_prompt, user_prompt, max_tokens,
                )
                future_map[future] = img_path

            for future in as_completed(future_map):
                img_path = future_map[future]
                t0 = time.time()
                text = future.result()
                elapsed = time.time() - t0
                status = "success" if text else "failed"
                results.append({
                    "image": str(img_path),
                    "name": img_path.name,
                    "status": status,
                    "text": text or "",
                    "elapsed": round(elapsed, 1),
                })
    else:
        # ── 串行模式 ──
        for img_path in images:
            logger.info("  🔍 识别: %s ...", img_path.name)
            t0 = time.time()
            if system_prompt:
                text = _recognize_with_system(llm, str(img_path), system_prompt,
                                              user_prompt, max_tokens)
            else:
                text = recognize_image(llm, str(img_path), "", user_prompt, max_tokens)
            elapsed = time.time() - t0
            status = "success" if text else "failed"
            icon = "✅" if text else "❌"
            logger.info("  %s %s (%.1fs)", icon, img_path.name, elapsed)
            results.append({
                "image": str(img_path),
                "name": img_path.name,
                "status": status,
                "text": text or "",
                "elapsed": round(elapsed, 1),
            })

    # ── 写入输出 ──
    success_count = 0
    failed_count = 0
    for r in results:
        if r["status"] == "success":
            success_count += 1
            out_file = out_dir / f"{Path(r['image']).stem}{ext}"
            content = format_func(r["image"], r["elapsed"], r["text"])
            out_file.write_text(content, encoding="utf-8")
        else:
            failed_count += 1

    # ── 汇总文件 ──
    summary_path = None
    if summary and results:
        if output_format == "md":
            summary_path = out_dir / "_summary.md"
            lines = ["# 批量识别汇总\n", f"**目录**: {image_dir}\n",
                     f"**模型**: {model_key}\n",
                     f"**总计**: {len(results)} 图 | ✅ {success_count} 成功 | ❌ {failed_count} 失败\n\n"]
            for r in results:
                icon = "✅" if r["status"] == "success" else "❌"
                lines.append(f"- {icon} **{r['name']}** ({r['elapsed']}s)\n")
                if r["text"]:
                    lines.append(f"  > {r['text'][:200].replace(chr(10), ' ')}\n")
            summary_path.write_text("".join(lines), encoding="utf-8")
        else:
            summary_path = out_dir / "_summary.json"
            summary_data = {
                "model": model_key,
                "directory": image_dir,
                "total": len(results),
                "success": success_count,
                "failed": failed_count,
                "results": [
                    {
                        "image": r["image"],
                        "name": r["name"],
                        "status": r["status"],
                        "elapsed": r["elapsed"],
                        "text_length": len(r["text"]),
                        "text_preview": r["text"][:200] if r["text"] else "",
                    }
                    for r in results
                ],
            }
            summary_path.write_text(
                json.dumps(summary_data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    logger.info("完成: %d 成功 / %d 失败 / 共 %d 张",
                success_count, failed_count, len(results))

    return {
        "total": len(results),
        "success": success_count,
        "failed": failed_count,
        "results": results,
        "summary_path": str(summary_path) if summary_path else None,
    }


# ── CLI ─────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="批量图片识别 — 用视觉模型识别文件夹中所有图片",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s ./images --prompt "请提取这张图片中的所有文字"
  %(prog)s ./images --prompt-file ./prompt.txt
  %(prog)s ./images --prompt "描述图片" --system-prompt ./system.txt
  %(prog)s ./images --prompt "识别文字" --model glm-4.6v_ocr --format md
  %(prog)s ./images --prompt "识别" --ext png,jpg --output ./results
        """,
    )
    p.add_argument("directory", help="图片文件夹路径")
    p.add_argument("--prompt", "-p", default="",
                   help="用户提示词（直接传入）")
    p.add_argument("--prompt-file", "-f", default="",
                   help="从文件读取用户提示词（优先级高于 --prompt）")
    p.add_argument("--system-prompt", "-s", default="",
                   help="系统提示词文件路径（可选，用于设定 AI 角色/行为）")
    p.add_argument("--model", "-m", default="qwen-vl-max_ocr",
                   choices=["qwen-vl-max_ocr", "glm-4.6v_ocr", "paddleocr-vl1.6_ocr"],
                   help="视觉模型（默认 qwen-vl-max_ocr）")
    p.add_argument("--output", "-o", default="",
                   help="输出目录（默认与图片目录相同）")
    p.add_argument("--format", "-t", default="txt", choices=["txt", "md"],
                   help="输出格式：txt（默认）或 md")
    p.add_argument("--ext", default="",
                   help="图片扩展名筛选，逗号分隔（默认: png,jpg,jpeg,webp,bmp,gif）")
    p.add_argument("--workers", "-w", type=int, default=4,
                   help="并行线程数（默认 4，设为 1 禁用并行）")
    p.add_argument("--max-tokens", type=int, default=4096,
                   help="最大输出 token 数（默认 4096）")
    p.add_argument("--no-summary", action="store_true",
                   help="不生成汇总文件")
    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    # ── 解析提示词 ──
    user_prompt = args.prompt
    if args.prompt_file:
        pf = Path(args.prompt_file)
        if not pf.exists():
            logger.error("提示词文件不存在: %s", args.prompt_file)
            return 1
        user_prompt = pf.read_text(encoding="utf-8").strip()

    if not user_prompt:
        logger.error("请提供提示词（--prompt 或 --prompt-file）")
        return 1

    # ── 解析 system prompt ──
    system_prompt = ""
    if args.system_prompt:
        sp = Path(args.system_prompt)
        if not sp.exists():
            logger.error("系统提示词文件不存在: %s", args.system_prompt)
            return 1
        system_prompt = sp.read_text(encoding="utf-8").strip()

    # ── 解析扩展名 ──
    extensions = DEFAULT_IMAGE_EXTENSIONS
    if args.ext:
        extensions = {f".{e.strip().lstrip('.')}" for e in args.ext.split(",")}

    # ── 执行 ──
    try:
        result = batch_recognize(
            image_dir=args.directory,
            user_prompt=user_prompt,
            system_prompt=system_prompt,
            model_key=args.model,
            output_dir=args.output or None,
            output_format=args.format,
            extensions=extensions,
            max_workers=args.workers,
            max_tokens=args.max_tokens,
            summary=not args.no_summary,
        )
    except Exception as e:
        logger.exception("执行失败: %s", e)
        return 1

    # ── 打印汇总 ──
    total = result["total"]
    success = result["success"]
    failed = result["failed"]
    print(f"\n{'='*50}")
    print(f"批量识别完成")
    print(f"{'='*50}")
    print(f"  图片目录: {args.directory}")
    print(f"  模型:     {args.model}")
    print(f"  总计:     {total} 张")
    print(f"  成功:     {success} 张 ✅")
    print(f"  失败:     {failed} 张 ❌")
    if result.get("summary_path"):
        print(f"  汇总:     {result['summary_path']}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
