"""B站视频分析 Pipeline。

支持两种模式:
  - URL 模式: 提取字幕 → LLM 分析 → 后处理 → 截图 → 合成报告
  - 本地 Txt 批处理: 批量处理字幕 txt 文件
"""
# ── 路径引导：向上查找项目根目录（CLAUDE.md 为标记）──
import sys
from pathlib import Path
_root = Path(__file__).resolve()
for _p in _root.parents:
    if (_p / "CLAUDE.md").exists() or (_p / "README.md").exists():
        _root = _p; break
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
# ─────────────────────────────────────────────────────
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any
from backend.config.settings import Settings
from backend.core.llm.client import LLMClient
from backend.core.pipeline.step import StepDef, RetryPolicy, Context
from backend.core.pipeline.engine import PipelineEngine
from backend.core.pipeline.batch import BatchProcessor
from backend.core.job.schema import build_job
from backend.core.job.json_store import JsonJobStore
from backend.services.container import ServiceContainer

from backend.pipelines._prompts import load_prompt
from backend.utils.paths import Paths

from backend.path_setup import PROJECT_ROOT


# ── 工具函数 ─────────────────────────────────────

def _safe_name(raw: str, fallback: str = "untitled") -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|]+", "_", raw).strip().strip(".")
    return cleaned or fallback


def _format_segments(segments: list[dict]) -> str:
    lines = []
    for idx, seg in enumerate(segments, start=1):
        text = str(seg.get("text", "")).strip()
        if text:
            lines.append(f"L{idx:04d}: {text}")
    return "\n".join(lines)


def _build_line_to_second(segments: list[dict]) -> dict[int, float]:
    """构建行号 → 秒数的映射表，供截图标记转换使用。"""
    mapping: dict[int, float] = {}
    for idx, seg in enumerate(segments, start=1):
        try:
            mapping[idx] = float(seg.get("start", 0.0) or 0.0)
        except Exception:
            mapping[idx] = 0.0
    return mapping


def _convert_shotline_markers(text: str, line_to_second: dict[int, float]) -> tuple[str, int]:
    """将 [SHOTLINE:行号|理由] 转换为 [SHOT:秒数|理由]。

    只在文本包含 [SHOTLINE: 标记时有效；不匹配则原样返回。
    标记格式由 prompts/bili/screenshot_user_extension.txt 约定（[SHOTLINE:行号|理由]，≤8 处），
    改提示词格式必须与本函数同步。
    """
    pattern = re.compile(r"\[SHOTLINE:(\d+)(?:\|([^\]]+))?\]")
    count = 0

    def repl(m: re.Match) -> str:
        nonlocal count
        line_no = int(m.group(1))
        reason = (m.group(2) or "").strip()
        sec = line_to_second.get(line_no)
        if sec is None:
            return f"[无效行号:{line_no}]"
        count += 1
        return f"[SHOT:{sec:.3f}|{reason}]" if reason else f"[SHOT:{sec:.3f}]"

    return pattern.sub(repl, text), count


def _has_shot_markers(text: str) -> bool:
    """检查文本中是否含有 [SHOT:秒数] 标记。"""
    return bool(re.search(r"\[SHOT:\d+(?:\.\d+)?", str(text)))


def _load_screenshot_extension() -> str:
    """加载截图指令扩展文本，追加到 user prompt 中。"""
    ext_path = Path(__file__).resolve().parent / "prompts" / "bili" / "screenshot_user_extension.txt"
    if ext_path.exists():
        return ext_path.read_text(encoding="utf-8")
    return ""


def _dump_prompts(
    log_dir: Path,
    bvid: str,
    system: str,
    user: str,
    subtitle_path: Path | None = None,
    response: str | None = None,
) -> Path:
    """将当前运行的完整提示词写入日志目录。

    对每次 pipeline 执行生成一份快照，方便审查 prompt 内容和调试。
    """
    log_dir.mkdir(parents=True, exist_ok=True)

    (log_dir / "system_prompt.txt").write_text(system, encoding="utf-8")
    (log_dir / "user_prompt.txt").write_text(user, encoding="utf-8")

    # 如果已有字幕文件，复制一份到日志目录
    if subtitle_path and subtitle_path.exists():
        shutil.copy2(subtitle_path, log_dir / subtitle_path.name)

    if response:
        (log_dir / "llm_response.txt").write_text(response, encoding="utf-8")

    # 记录元信息
    meta_lines = [
        f"bvid: {bvid}",
        f"time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"system_prompt: {len(system)} chars",
        f"user_prompt: {len(user)} chars",
        f"screenshot_extension: {'YES' if '[SHOTLINE:' in user else 'no'}",
    ]
    (log_dir / "meta.txt").write_text("\n".join(meta_lines) + "\n", encoding="utf-8")

    return log_dir


# ── Step handlers ─────────────────────────────────

def extract_subtitle(ctx: Context, llm: LLMClient | None) -> dict:
    services: ServiceContainer = ctx["_container"]
    bili = services.get("bilibili")
    result = bili.get_subtitle(ctx["url"])
    if not result.get("ok"):
        raise RuntimeError(f"字幕提取失败: {result.get('error', 'unknown')}")
    return result


def analyze_with_llm(ctx: Context, llm: LLMClient | None) -> str:
    sub = ctx["subtitle_data"]
    subtitle_text = _format_segments(sub.get("segments", []))
    line_to_sec = _build_line_to_second(sub.get("segments", []))

    desc = sub.get("desc", "")
    if len(desc) > 500:
        desc = desc[:500] + "…"

    # 加载基础提示词
    system, user = load_prompt(
        "bilibili",
        sub_dir="bili",
        filename=sub.get("title", "video"),
        subtitle_content=subtitle_text,
        user_profile=ctx.get("user_profile", "无"),
        video_desc=desc or "无",
        task_prompt=ctx.get("task_prompt") or "无额外要求，按默认格式输出",
    )

    # 如果启用了截图，追加截图指令到 user prompt
    if ctx.get("screenshots", False):
        ext = _load_screenshot_extension()
        if ext:
            user += ext

    # ── 日志：保存本次运行的完整提示词到 .data/<run_ts>/ ──
    log_dir = Path(str(PROJECT_ROOT)) / ".data" / ctx["_run_ts"]
    bvid = sub.get("bvid", "unknown")
    _dump_prompts(log_dir, bvid, system, user)
    ctx["_prompt_log_dir"] = str(log_dir)

    # 缓存 line→second 映射供后续截图使用
    ctx["_line_to_second"] = line_to_sec

    result = llm.call(
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=4000,
        temperature=0.3,
    )

    # 追加保存 LLM 响应
    (log_dir / "llm_response.txt").write_text(result, encoding="utf-8")
    return result


def postprocess(ctx: Context, llm: LLMClient | None) -> str:
    """后处理：将 [SHOTLINE:行号] 转换为 [SHOT:秒数]。

    这是一个"插件式"设计——各变换独立运行，检查前置条件，不报错。
    新版 prompt 已移除"时间线总结"章节，因此不再需要 _inject_timeline_shots。
    """
    analysis = ctx["analysis"]
    line_to_sec = ctx.get("_line_to_second", {})
    screenshots = ctx.get("screenshots", False)

    # 只有截图启用且 LLM 输出了 [SHOTLINE: 标记时才转换
    if screenshots and line_to_sec and re.search(r"\[SHOTLINE:\d+", analysis):
        analysis, converted = _convert_shotline_markers(analysis, line_to_sec)

    return analysis


def compose_report(ctx: Context, llm: LLMClient | None) -> dict:
    sub = ctx["subtitle_data"]
    analysis = ctx["processed_analysis"]
    url = ctx["url"]
    title = sub.get("title", "")
    bvid = sub.get("bvid", "")
    desc = sub.get("desc", "")
    owner = sub.get("owner", {})
    stat = sub.get("stat", {})
    duration = sub.get("duration", 0)
    pic = sub.get("pic", "")
    videos = sub.get("videos", 1)
    pages = sub.get("pages", [])

    # ── 保存原始字幕到 .data/ ──
    try:
        subtitle_text = sub.get("text", "")
        if subtitle_text:
            from backend.path_setup import PROJECT_ROOT as _PR
            temp_dir = _PR / ".data"
            temp_dir.mkdir(parents=True, exist_ok=True)
            sub_path = temp_dir / f"{bvid or 'unknown'}_subtitle.txt"
            sub_path.write_text(subtitle_text, encoding="utf-8")
    except Exception:
        pass

    lines = [
        "# B站视频解析报告",
        "",
        f"- 标题: {title}",
        f"- URL: {url}",
    ]
    if bvid:
        lines.append(f"- BVID: {bvid}")
    if owner and owner.get("name"):
        lines.append(f"- UP主: {owner['name']} (mid: {owner.get('mid', '?')})")
    if stat:
        parts = []
        for k, label in [("view", "播放"), ("like", "点赞"), ("coin", "硬币"),
                          ("favorite", "收藏"), ("share", "转发"), ("danmaku", "弹幕"), ("reply", "评论")]:
            if k in stat:
                parts.append(f"{label}: {stat[k]}")
        if parts:
            lines.append(f"- 数据: {' / '.join(parts)}")
    if duration:
        m, s = divmod(duration, 60)
        lines.append(f"- 时长: {m}:{s:02d}")
    if pic:
        lines.append(f"- 封面: {pic}")
    if videos and videos > 1:
        parts_info = " / ".join(f"P{p.get('page','?')}: {p.get('part','?')}" for p in (pages or []))
        lines.append(f"- 分P ({videos}P): {parts_info}")
    if desc:
        desc_short = desc.strip()[:300]
        if desc_short:
            lines.append(f"- 简介: {desc_short}{'…' if len(desc.strip()) > 300 else ''}")

    lines.extend([
        f"- 处理时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "---",
        "",
    ])

    header = "\n".join(lines)

    safe = _safe_name(title[:30], fallback=bvid or "video")
    if ctx.get("output_dir"):
        output_dir = Path(ctx["output_dir"])
    else:
        # 默认落到 data/videos/{bvid}/（与归档结构一致），不再写 output/bilibili
        output_dir = PROJECT_ROOT / "data" / "videos" / (bvid or "unknown")
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / f"analysis_{safe}.md"
    report_path.write_text(header + analysis, encoding="utf-8")

    # ── 报告副本同步到 prompt 日志目录 ──
    log_dir = ctx.get("_prompt_log_dir")
    if log_dir:
        log_path = Path(log_dir) / f"{bvid or 'report'}.md"
        log_path.write_text(header + analysis, encoding="utf-8")

    return str(report_path)


def extract_frames(ctx: Context, llm: LLMClient | None) -> None:
    """根据 [SHOT:秒数] 标记截图并嵌入报告。"""
    report_path = Path(ctx["report_path"])
    if not report_path.exists():
        return

    from backend.services.bilibili.screenshot import apply_screenshot_hints_to_markdown

    sub = ctx["subtitle_data"]
    entry = sub.get("entry_url") or sub.get("url", "")
    bvid = re.search(r"(BV[a-zA-Z0-9]{10})", entry)
    bvid = bvid.group(1) if bvid else "video"
    title = sub.get("title", "")

    result = apply_screenshot_hints_to_markdown(
        md_path=report_path,
        bvid=bvid,
        title=title,
        task_root=report_path.parent,
    )
    ctx["_frame_result"] = result


# ── Pipeline 定义 ─────────────────────────────────

def _url_pipeline_steps() -> list[StepDef]:
    return [
        StepDef(
            name="extract_subtitle",
            handler=extract_subtitle,
            outputs=["subtitle_data"],
        ),
        StepDef(
            name="analyze",
            handler=analyze_with_llm,
            outputs=["analysis"],
            retry=RetryPolicy(max_attempts=3, backoff=2.0),
        ),
        StepDef(
            name="postprocess",
            handler=postprocess,
            outputs=["processed_analysis"],
        ),
        StepDef(
            name="compose_report",
            handler=compose_report,
            outputs=["report_path"],
        ),
        StepDef(
            name="extract_frames",
            handler=extract_frames,
            skip_if=lambda ctx: not ctx.get("screenshots", False)
                                or not _has_shot_markers(str(ctx.get("processed_analysis", ""))),
        ),
    ]


# ── 公开 API ─────────────────────────────────────

def run_url_pipeline(
    url: str,
    llm: LLMClient,
    container: ServiceContainer,
    output_dir: str | None = None,
    user_profile: str = "",
    task_prompt: str = "",
    screenshots: bool = False,
) -> Context:
    """分析单个 B 站视频。

    Args:
        screenshots: 是否启用视频截图提取（默认关闭，开启需 ffmpeg）。
    """
    engine = PipelineEngine(_url_pipeline_steps(), llm=llm)

    # 生成本次运行的唯一时间戳（贯穿所有步骤，用于日志目录）
    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    ctx = engine.run({
        "url": url,
        "_container": container,
        "output_dir": output_dir,
        "user_profile": user_profile,
        "task_prompt": task_prompt,
        "screenshots": screenshots,
        "_run_ts": run_ts,
    })
    return ctx


def run_local_txt_batch(
    input_dir: str,
    output_dir: str,
    llm: LLMClient,
    workers: int = 3,
    user_profile: str = "",
) -> None:
    """批量处理本地字幕 txt 文件。"""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    txt_files = sorted(Path(input_dir).glob("*.txt"))
    if not txt_files:
        print(f"未找到 txt 文件: {input_dir}")
        return

    items = []
    for f in txt_files:
        items.append({
            "type": "text",
            "data": f.read_text(encoding="utf-8"),
            "metadata": {"source": str(f), "filename": f.name},
        })

    settings = Settings()
    store = JsonJobStore(str(settings.job_storage_dir))
    batch_id = f"bilibili_local_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    job = build_job(items, batch_id=batch_id, source_file=input_dir)
    store.save(job)

    def process(item_input: dict) -> str:
        meta = item_input.get("metadata", {})
        filename = meta.get("filename", "unknown.txt")
        content = item_input.get("data", "")

        system, user = load_prompt(
            "bilibili",
            sub_dir="bili",
            filename=filename,
            subtitle_content=content,
            user_profile=user_profile,
        )

        return llm.call(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=4000,
            temperature=0.3,
        )

    def on_success(item, result: str) -> None:
        meta = item.input.get("metadata", {})
        filename = meta.get("filename", f"item_{item.id}.txt")
        stem = Path(filename).stem
        out_file = out / f"{stem}.md"
        out_file.write_text(
            f"# B站字幕知识点提取\n\n"
            f"文件: {filename}\n"
            f"处理时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"---\n\n{result}",
            encoding="utf-8",
        )

    batch = BatchProcessor(store, process, workers=workers, on_success=on_success)
    batch.run(job)


__all__ = ["run_url_pipeline", "run_local_txt_batch"]
