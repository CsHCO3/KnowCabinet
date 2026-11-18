"""文件对话 Pipeline — 读取文件夹全部文件 + 用户问题 → LLM 回答。

不维护历史，每次调用为独立单轮对话。
支持文本文件（.txt .md .py .json …）和常见图片格式。
"""

import sys
from pathlib import Path
_root = Path(__file__).resolve()
for _p in _root.parents:
    if (_p / "CLAUDE.md").exists() or (_p / "README.md").exists():
        _root = _p; break
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

import base64
import logging
from datetime import datetime
from pathlib import Path

from backend.core.llm.client import LLMClient
from backend.core.pipeline.step import StepDef, Context
from backend.core.pipeline.engine import PipelineEngine
from backend.pipelines._prompts import load_prompt
from backend.utils.paths import Paths

logger = logging.getLogger("infosystem.file_chat")

# 单文件大小上限（字节）
MAX_TEXT_FILE_SIZE = 512 * 1024  # 512 KB
MAX_IMAGE_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

TEXT_EXTENSIONS = {
    ".txt", ".md", ".json", ".py", ".js", ".ts", ".jsx", ".tsx",
    ".html", ".css", ".xml", ".yaml", ".yml", ".toml", ".ini",
    ".cfg", ".conf", ".csv", ".log", ".sql", ".sh", ".bat", ".ps1",
    ".env", ".tex", ".rst", ".java", ".cpp", ".c", ".h", ".hpp",
    ".go", ".rs", ".rb", ".php", ".pl", ".lua", ".r", ".scala",
    ".kt", ".swift", ".m", ".mm", ".vue", ".svelte",
}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}


def _read_text_file(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        try:
            return path.read_text(encoding="gbk")
        except Exception:
            return None


def _encode_image(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("utf-8")


def _collect_file_parts(input_dir: str) -> tuple[list[dict], dict]:
    """扫描文件夹，构造 OpenAI 多部分消息 content 数组。

    Returns:
        (parts, stats) — parts 为 content 数组，stats 含 file_count/image_count/skipped_count。
    """
    folder = Path(input_dir)
    if not folder.is_dir():
        raise NotADirectoryError(f"目录不存在: {input_dir}")

    text_chunks: list[str] = []
    image_parts: list[dict] = []
    skipped: list[str] = []

    for f in sorted(folder.iterdir()):
        if not f.is_file():
            continue
        ext = f.suffix.lower()
        size = f.stat().st_size

        if ext in TEXT_EXTENSIONS:
            if size > MAX_TEXT_FILE_SIZE:
                skipped.append(f"{f.name} (文本文件过大，已跳过)")
                continue
            content = _read_text_file(f)
            if content is not None:
                text_chunks.append(f"--- {f.name} ---\n{content}")
            else:
                skipped.append(f"{f.name} (无法解码)")

        elif ext in IMAGE_EXTENSIONS:
            if size > MAX_IMAGE_FILE_SIZE:
                skipped.append(f"{f.name} (图片过大，已跳过)")
                continue
            try:
                b64 = _encode_image(f)
                image_parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/{ext[1:]};base64,{b64}"},
                })
            except Exception:
                skipped.append(f"{f.name} (读取失败)")

        else:
            skipped.append(f"{f.name} (不支持的格式)")

    parts: list[dict] = []

    # 文本合并为一块
    if text_chunks:
        parts.append({
            "type": "text",
            "text": "以下是用户上传的文件内容：\n\n" + "\n\n".join(text_chunks),
        })

    # 图片依次追加
    parts.extend(image_parts)

    # 跳过提示
    if skipped:
        parts.append({
            "type": "text",
            "text": "\n以下文件被跳过（不予处理）：\n" + "\n".join(f"- {s}" for s in skipped),
        })

    if not parts:
        raise ValueError(f"未从目录读取到任何可处理的内容: {input_dir}")

    stats = {
        "file_count": len(text_chunks),
        "image_count": len(image_parts),
        "skipped_count": len(skipped),
    }
    return parts, stats


def chat_step(ctx: Context, llm: LLMClient | None) -> str:
    if llm is None:
        raise RuntimeError("file_chat 需要 LLM 实例")

    question = ctx.get("question", "").strip()
    input_dir = ctx.get("input_dir", "")

    # 收集文件内容
    parts, stats = _collect_file_parts(input_dir)

    # 用户问题追加到最后
    if question:
        parts.append({"type": "text", "text": f"\n用户问题：{question}"})
    else:
        parts.append({"type": "text", "text": "\n请基于以上文件内容给出分析总结。"})

    # 加载系统提示词
    system_prompt, _ = load_prompt("file_chat", sub_dir="file_chat")

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": parts},
    ]

    # 构造 extra_body
    extra_body = {}
    if ctx.get("enable_search"):
        extra_body["enable_search"] = True
    if ctx.get("enable_think"):
        extra_body["thinking"] = {"type": "enabled"}

    logger.info(
        "已读取 %(files)d 个文件 + %(imgs)d 张图片 (%(skipped)d 个跳过), "
        "问题=%(q)s, search=%(search)s, think=%(think)s",
        {
            "files": stats["file_count"],
            "imgs": stats["image_count"],
            "skipped": stats["skipped_count"],
            "q": question[:50] if question else "(无)",
            "search": ctx.get("enable_search", False),
            "think": ctx.get("enable_think", False),
        },
    )

    return llm.call(
        messages=messages,
        max_tokens=4000,
        temperature=0.3,
        extra_body=extra_body or None,
    )


def run_file_chat(
    input_dir: str,
    question: str = "",
    llm: LLMClient | None = None,
    output_dir: str | None = None,
    enable_search: bool = False,
    enable_think: bool = False,
) -> Context:
    """文件对话入口：读取文件夹全部文件 + 问题 → LLM 回答。"""
    engine = PipelineEngine([
        StepDef(name="file_chat", handler=chat_step, outputs=["chat_result"]),
    ], llm=llm)

    ctx = engine.run({
        "question": question,
        "input_dir": input_dir,
        "output_dir": output_dir or str(Paths().file_chat),
        "enable_search": enable_search,
        "enable_think": enable_think,
    })

    # 持久化
    out_dir = Path(ctx["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = "chat"
    if question:
        safe_name = "".join(c if c.isalnum() or c in " _-" else "_" for c in question[:30]).strip()
    report_path = out_dir / f"{safe_name}_{ts}.md"
    report_path.write_text(ctx["chat_result"], encoding="utf-8")
    ctx["report_path"] = str(report_path)

    return ctx


__all__ = ["run_file_chat"]
