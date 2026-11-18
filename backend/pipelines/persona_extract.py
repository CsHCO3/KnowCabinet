"""Persona 画像提取 Pipeline — 从 AI 对话记录中逐轮提取人物画像要点。

流程:
  1. chat_parser 按自定义标记切块并配对为 (user, ai) 轮次
  2. BatchProcessor 并行调用 LLM 提炼每一轮
  3. 汇总全部轮次结果输出最终 Markdown 报告

支持:
  - 自定义标记格式（通过 --user-pattern / --ai-pattern）
  - 自定义 prompt 模板（system + user，高度 DIY）
  - 并行处理（--workers）
  - 断点续传（基于 JobStore，同输入文件自动续跑）
  - 短轮次短路（避免浪费 API 调用）
"""

import re
import sys
import threading
from datetime import datetime
from pathlib import Path
_root = Path(__file__).resolve()
for _p in _root.parents:
    if (_p / "CLAUDE.md").exists() or (_p / "README.md").exists():
        _root = _p
        break
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from backend.config.settings import Settings
from backend.core.llm.client import LLMClient
from backend.core.job.schema import build_job, Job, JobItem, ItemStatus
from backend.core.job.json_store import JsonJobStore
from backend.core.pipeline.batch import BatchProcessor
from backend.pipelines._prompts import load_prompt
from backend.utils.chat_parser import ChatParser, ChatRound
from backend.utils.paths import Paths


# ── 默认值 ─────────────────────────────────────────
DEFAULT_USER_PATTERN = r"^##\s*【您】"
DEFAULT_AI_PATTERN = r"^##\s*【对方】"
SHORT_CONTENT_THRESHOLD = 100  # 合并字符 ≤ 此值时跳过 LLM
DEFAULT_TEMPLATE_NAME = "round"
DEFAULT_TEMPLATE_SUBDIR = "persona"


# ── 内部数据结构 ──────────────────────────────────

def _make_round_items(
    rounds: list[ChatRound],
    story_context: str = "",
    task_requirements: str = "",
) -> list[dict]:
    """将 ChatRound 列表转为 Job 所需的 items 列表。"""
    items: list[dict] = []
    for r in rounds:
        items.append({
            "type": "text",
            "data": r.user_content + "\n" + r.ai_content,
            "metadata": {
                "round_no": r.round_no,
                "user_line": r.user_line,
                "ai_line": r.ai_line,
                "user_content": r.user_content,
                "ai_content": r.ai_content,
                "story_context": story_context,
                "task_requirements": task_requirements,
            },
        })
    return items


def _format_raw_round(user_content: str, ai_content: str) -> str:
    """短轮次直接原文输出。"""
    clean_user = user_content.rstrip()
    clean_ai = ai_content.rstrip()
    return (
        "### 本轮原文\n"
        f"- 用户输入：{clean_user if clean_user else '（空）'}\n"
        f"- AI回复：{clean_ai if clean_ai else '（空）'}\n"
    )


def _format_round_output(round_no: int, summary: str) -> str:
    return f"## 第{round_no}轮\n\n{summary.strip()}\n"


def _compose_report(
    input_path: Path,
    rounds: list[ChatRound],
    results: dict[int, str],
    errors: list[tuple[int, str]],
) -> str:
    """将所有轮次结果组装为最终报告。"""
    lines = [
        "# Persona 画像提取报告",
        "",
        f"- 源文件: {input_path}",
        f"- 总轮次: {len(rounds)}",
        f"- 成功: {len(results)}",
        f"- 失败: {len(errors)}",
        f"- 处理时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "---",
        "",
    ]

    for r in rounds:
        summary = results.get(r.round_no)
        if summary is not None:
            lines.append(_format_round_output(r.round_no, summary))
        else:
            # 该轮失败，输出占位
            err_msg = ""
            for rn, msg in errors:
                if rn == r.round_no:
                    err_msg = msg
                    break
            lines.append(_format_round_output(
                r.round_no,
                "### 本轮画像要点\n"
                "- 用户核心观点及经历：提取失败\n"
                "- AI核心观点：提取失败\n"
                "- 双方可沉淀特征：提取失败\n"
                f"\n> 错误：{err_msg}" if err_msg else "",
            ))
        lines.append("")

    return "\n".join(lines)


# ── 主入口 ─────────────────────────────────────────

def run_persona_pipeline(
    input_file: str,
    llm: LLMClient,
    *,
    output_dir: str | None = None,
    user_pattern: str = DEFAULT_USER_PATTERN,
    ai_pattern: str = DEFAULT_AI_PATTERN,
    template_name: str = DEFAULT_TEMPLATE_NAME,
    template_subdir: str = DEFAULT_TEMPLATE_SUBDIR,
    story_context: str = "",
    task_requirements: str = "",
    workers: int = 4,
    force: bool = False,
) -> dict:
    """运行 Persona 画像提取 Pipeline。

    Args:
        input_file:      对话记录文件路径。
        llm:             LLMClient 实例。
        output_dir:      输出目录（默认 Pipeline 默认输出路径）。
        user_pattern:    用户段落标记正则。
        ai_pattern:      AI 段落标记正则。
        template_name:   prompt 模板名称（不含 _system/_user 后缀）。
        template_subdir: prompt 模板所在子目录。
        story_context:   注入 system prompt 的背景设定。
        task_requirements:  注入 user prompt 的额外要求。
        workers:         并行线程数。
        force:           强制重新处理（忽略已有进度）。

    Returns:
        Context dict，含 report_path 和 summary。
    """
    input_path = Path(input_file)
    if not input_path.exists():
        raise FileNotFoundError(f"输入文件不存在: {input_file}")

    out_dir = Path(output_dir) if output_dir else Paths().output_root / "persona"
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── 1. 切块 ──────────────────────────────────────────────
    parser = ChatParser(user_pattern, ai_pattern)
    try:
        rounds = parser.parse(input_file)
    except ValueError as e:
        print(f"[persona] 切块失败: {e}")
        print(f"[persona] 当前 user_pattern: {user_pattern}")
        print(f"[persona] 当前 ai_pattern:   {ai_pattern}")
        print("[persona] 可通过 --user-pattern / --ai-pattern 自定义")
        raise

    total = len(rounds)
    print(f"[persona] 共切出 {total} 轮对话")

    # ── 2. Job / 断点续传 ───────────────────────────────────
    batch_id = f"persona_{input_path.stem}"
    settings = Settings()
    store = JsonJobStore(str(settings.job_storage_dir))

    existing_jobs = store.list_jobs()
    preloaded_results: dict[int, str] = {}
    job: Job

    if not force and batch_id in existing_jobs:
        job = store.load(batch_id)
        print(f"[persona] 发现已有进度 ({job.summary()})，将断点续传...")

        # 预加载已完成条目的结果
        for item in job.items:
            meta = item.input.get("metadata", {})
            rn = meta.get("round_no")
            if rn is not None and item.status == ItemStatus.SUCCESS and item.output:
                preloaded_results[rn] = item.output
        print(f"[persona] 已加载 {len(preloaded_results)} 个已完成轮次")
    else:
        if force and batch_id in existing_jobs:
            store.delete(batch_id)
            print("[persona] 强制重新处理，已清除旧进度")

        items = _make_round_items(rounds, story_context, task_requirements)
        job = build_job(items, batch_id=batch_id, source_file=str(input_file))

    store.save(job)

    # ── 3. 批量处理（仅处理 pending 条目）─────────────────────
    new_results: dict[int, str] = {}
    errors: list[tuple[int, str]] = []
    lock = threading.Lock()

    def _process_round(item_input: dict) -> str:
        meta = item_input.get("metadata", {})
        user_content = meta.get("user_content", "")
        ai_content = meta.get("ai_content", "")
        story = meta.get("story_context", "")
        task_req = meta.get("task_requirements", "")

        # 短轮次短路
        merged = re.sub(r"\s+", "", f"{user_content}{ai_content}")
        if len(merged) <= SHORT_CONTENT_THRESHOLD:
            return _format_raw_round(user_content, ai_content)

        # 调用 LLM
        system, user = load_prompt(
            template_name,
            sub_dir=template_subdir,
            story_context=story,
            user_content=user_content,
            ai_content=ai_content,
            task_requirements=task_req,
        )
        try:
            return llm.call(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                max_tokens=800,
                temperature=0.2,
            )
        except Exception as e:
            # LLM 失败时返回兜底文本（不抛异常，该轮标记为失败）
            raise RuntimeError(f"LLM 调用失败: {e}")

    def _on_success(item: JobItem, result: str) -> None:
        meta = item.input.get("metadata", {})
        rn = meta.get("round_no")
        if rn is not None:
            with lock:
                new_results[rn] = result

    batch = BatchProcessor(store, _process_round, workers=workers, on_success=_on_success)
    batch.run(job)

    # ── 4. 收集失败条目 ──────────────────────────────────────
    job = store.load(batch_id)  # reload to get latest status
    for item in job.items:
        if item.status == ItemStatus.FAILED:
            meta = item.input.get("metadata", {})
            rn = meta.get("round_no", item.id)
            errors.append((rn, item.error or "未知错误"))

    # ── 5. 合并结果（预加载的 + 本轮新处理的）───────────────
    all_results = {**preloaded_results, **new_results}

    # ── 6. 生成报告 ─────────────────────────────────────────
    report_text = _compose_report(input_path, rounds, all_results, errors)
    report_path = out_dir / f"{input_path.stem}_persona.md"
    report_path.write_text(report_text, encoding="utf-8")
    print(f"[persona] 报告已生成: {report_path}")

    # ── 7. 输出概要 ─────────────────────────────────────────
    summary = job.summary()
    print(f"[persona] 完成: {summary.get('success', 0)} 成功, "
          f"{summary.get('failed', 0)} 失败, "
          f"{summary.get('pending', 0)} 剩余")
    if errors:
        print("[persona] 失败轮次:")
        for rn, err in errors:
            print(f"  - 第 {rn} 轮: {err[:120]}")

    return {
        "report_path": str(report_path),
        "rounds": total,
        "results": all_results,
        "errors": errors,
        "summary": summary,
    }
