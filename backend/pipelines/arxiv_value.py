"""arXiv 论文批量研读价值汇总 Pipeline。

流程:
  1. 解析输入文件（JSON / Markdown）→ 论文条目列表
  2. 构建 Job → BatchProcessor 逐篇调用 LLM 提取结构化信息
  3. 排序聚合 → LLM 汇总 → 输出最终报告
"""

# ── 路径引导 ──
import sys
from pathlib import Path
_root = Path(__file__).resolve()
for _p in _root.parents:
    if (_p / "CLAUDE.md").exists() or (_p / "README.md").exists():
        _root = _p; break
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
# ──────────────

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.config.settings import Settings
from backend.core.llm.client import LLMClient
from backend.core.job.schema import build_job
from backend.core.job.json_store import JsonJobStore
from backend.core.pipeline.batch import BatchProcessor

from backend.pipelines._prompts import load_prompt
from backend.utils.paths import Paths


# ── 输入解析 ─────────────────────────────────────

def _strip_version(arxiv_id: str) -> str:
    return re.sub(r"v\d+$", "", (arxiv_id or "").strip())


def _parse_arxiv_json(input_file: Path) -> list[dict]:
    raw = json.loads(input_file.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("JSON 输入应为论文对象数组")

    items: list[dict] = []
    for idx, paper in enumerate(raw):
        if not isinstance(paper, dict):
            continue
        summary = str(paper.get("summary") or paper.get("abstract") or "").strip()
        title = str(paper.get("title") or f"paper_{idx}").strip()
        if not summary:
            continue
        arxiv_id = str(paper.get("arxiv_id") or "").strip()
        items.append({
            "type": "text",
            "data": summary,
            "metadata": {
                "title": title,
                "arxiv_id": arxiv_id,
                "arxiv_id_base": _strip_version(arxiv_id),
                "authors": paper.get("authors", []),
                "published": paper.get("published", ""),
                "primary_category": paper.get("primary_category", ""),
                "entry_url": paper.get("entry_url", ""),
                "pdf_url": paper.get("pdf_url", ""),
                "has_code_or_data": paper.get("has_code_or_data", False),
                "code_data_links": paper.get("code_data_links", []),
                "is_accepted": paper.get("is_accepted", False),
                "page_count": paper.get("page_count"),
            },
        })
    return items


def _parse_arxiv_markdown(input_file: Path) -> list[dict]:
    text = input_file.read_text(encoding="utf-8")
    sections = re.split(r"(?=^##\s+)", text, flags=re.MULTILINE)

    items: list[dict] = []
    for section in sections:
        if not section.strip().startswith("##"):
            continue

        title_m = re.search(r"^##\s+\d+\.\s+(.+)$", section, flags=re.MULTILINE)
        title = title_m.group(1).strip() if title_m else "Untitled"

        def field(name: str) -> str:
            m = re.search(rf"^-\s*{re.escape(name)}:\s*(.*)$", section, flags=re.MULTILINE)
            return m.group(1).strip() if m else ""

        abstract_m = re.search(r"###\s+Abstract\s*\n([\s\S]+?)(?:\n##\s+|\Z)", section)
        abstract = abstract_m.group(1).strip() if abstract_m else ""
        if not abstract:
            continue

        arxiv_id = field("arXiv ID")
        authors = [a.strip() for a in field("Authors").split(",") if a.strip()]
        items.append({
            "type": "text",
            "data": abstract,
            "metadata": {
                "title": title,
                "arxiv_id": arxiv_id,
                "arxiv_id_base": _strip_version(arxiv_id),
                "authors": authors,
                "published": field("Published"),
                "primary_category": field("Category"),
                "entry_url": field("Entry"),
                "pdf_url": field("PDF"),
                "page_count": field("Page Count"),
                "is_accepted": field("Accepted").lower() in {"yes", "true", "1"},
                "has_code_or_data": field("Code/Data").lower() in {"yes", "true", "1"},
                "code_data_links": [],
            },
        })
    return items


# ── LLM 输出解析 ─────────────────────────────────

def _extract_json_block(text: str) -> dict:
    candidate = text.strip()
    if candidate.startswith("```"):
        m = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", candidate)
        if m:
            candidate = m.group(1).strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start >= 0 and end > start:
            return json.loads(candidate[start: end + 1])
        raise ValueError("模型输出不是有效 JSON")


def _truncate(text: str, limit: int = 120) -> str:
    clean = re.sub(r"\s+", " ", str(text)).strip()
    return clean if len(clean) <= limit else clean[:limit - 1].rstrip() + "…"


def _build_compact(parsed: dict, meta: dict) -> dict:
    value = parsed.get("reading_value", {})
    fit = parsed.get("fit_for_reader", {})
    opensource = parsed.get("open_source", {})
    problem = parsed.get("problem_and_contribution", {}).get("problem", "unknown")

    return {
        "title": meta.get("title", "Untitled"),
        "arxiv_id": meta.get("arxiv_id", ""),
        "published": meta.get("published", ""),
        "category": meta.get("primary_category", ""),
        "score": value.get("score", 0),
        "level": value.get("level", "low"),
        "value_reason": _truncate(value.get("reason", "unknown")),
        "theme": _truncate(problem),
        "directions": (parsed.get("research_direction", []) or ["unknown"])[:3],
        "technical_stack": (parsed.get("technical_stack", []) or ["unknown"])[:4],
        "tools": (parsed.get("tooling_and_frameworks", []) or ["unknown"])[:4],
        "open_source": {
            "code": opensource.get("code", "unknown"),
            "data": opensource.get("data", "unknown"),
            "known_links": (opensource.get("known_links", []) or [])[:3],
            "evidence": _truncate(opensource.get("evidence", "unknown")),
        },
        "fit_for_reader": {
            "target_roles": (fit.get("target_roles", []) or ["unknown"])[:3],
            "prerequisites": (fit.get("prerequisites", []) or ["unknown"])[:3],
            "estimated_reading_time": fit.get("estimated_reading_time", "normal"),
        },
        "next_actions": (parsed.get("next_actions", []) or ["unknown"])[:3],
    }


def _fallback_report(payload: dict) -> str:
    papers = payload["papers"]
    lines = [
        "# arXiv 批量研读价值汇总",
        "",
        f"- 来源文件：{payload['source_file']}",
        f"- 条目数：{payload['total_items']}",
        f"- 用户画像：{payload.get('user_profile', '未提供')}",
        "",
        "## 论文清单",
    ]
    for idx, p in enumerate(papers, 1):
        lines.append(
            f"- {idx}. {p['title']} ({p.get('arxiv_id', '')}) "
            f"— 价值: {p.get('level', 'low')} / {p.get('score', 0)}"
        )
        lines.append(f"  - {p.get('value_reason', '')}")
    return "\n".join(lines) + "\n"


# ── 公开 API ─────────────────────────────────────

def run_arxiv_value_pipeline(
    input_file: str,
    source_format: str = "json",
    output_dir: str | None = None,
    llm: LLMClient | None = None,
    settings: Settings | None = None,
    workers: int = 4,
    user_profile: str = "",
    item_template: str = "arxiv_value",
    batch_template: str = "arxiv_batch",
) -> str:
    """运行 arXiv 论文价值分析 Pipeline。

    Returns:
        最终报告文件路径。
    """
    settings = settings or Settings()
    if llm is None:
        llm = settings.get_llm_client("v4flash")

    input_path = Path(input_file)
    output_path = Path(output_dir) if output_dir else Paths().arxiv_analysis
    output_path.mkdir(parents=True, exist_ok=True)

    # 1. 解析输入
    if source_format == "json":
        items = _parse_arxiv_json(input_path)
    elif source_format == "md":
        items = _parse_arxiv_markdown(input_path)
    else:
        raise ValueError("source_format 仅支持 json / md")

    if not items:
        raise ValueError("未解析出可处理的论文摘要条目")

    # 2. 构建 Job 并持久化
    batch_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_arxiv_value"
    store = JsonJobStore(str(settings.job_storage_dir))
    job = build_job(items, batch_id=batch_id, source_file=str(input_path))
    store.save(job)

    # 3. 批量处理
    compact_items: list[dict] = []
    compact_errors: list[dict] = []

    def process_item(item_input: dict) -> str:
        meta = item_input.get("metadata", {})
        sys_prompt, user_prompt = load_prompt(
            item_template,
            arxiv_id=str(meta.get("arxiv_id", "")),
            title=str(meta.get("title", "")),
            authors=", ".join(meta.get("authors", [])[:12]) if isinstance(meta.get("authors"), list) else "",
            published=str(meta.get("published", "")),
            primary_category=str(meta.get("primary_category", "")),
            abstract_text=str(item_input.get("data", "")),
            has_code_or_data=str(meta.get("has_code_or_data", False)),
            code_data_links=", ".join(meta.get("code_data_links", [])) if isinstance(meta.get("code_data_links"), list) else "",
            is_accepted=str(meta.get("is_accepted", False)),
            page_count=str(meta.get("page_count", "")),
        )
        return llm.call(
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=2200,
            temperature=0.2,
        )

    def on_success(item, result: str) -> None:
        meta = item.input.get("metadata", {})
        try:
            parsed = _extract_json_block(result)
            compact_items.append(_build_compact(parsed, meta))
        except Exception as exc:
            compact_errors.append({
                "title": meta.get("title", "Untitled"),
                "arxiv_id": meta.get("arxiv_id", ""),
                "error": str(exc),
            })
            compact_items.append({
                "title": meta.get("title", "Untitled"),
                "arxiv_id": meta.get("arxiv_id", ""),
                "published": meta.get("published", ""),
                "category": meta.get("primary_category", ""),
                "score": 0, "level": "low",
                "value_reason": "解析失败",
                "theme": _truncate(str(item.input.get("data", "")), 120),
                "directions": ["unknown"],
                "technical_stack": ["unknown"],
                "tools": ["unknown"],
                "open_source": {"code": "unknown", "data": "unknown", "known_links": [], "evidence": "unknown"},
                "fit_for_reader": {"target_roles": ["unknown"], "prerequisites": ["unknown"], "estimated_reading_time": "normal"},
                "next_actions": ["unknown"],
            })

    batch = BatchProcessor(store, process_item, workers=workers, on_success=on_success)
    batch.run(job)

    # 4. 排序聚合
    compact_items.sort(key=lambda p: (-int(p.get("score", 0)), str(p.get("published", "")), str(p.get("title", ""))))

    payload = {
        "source_file": str(input_path),
        "total_items": len(items),
        "success_items": len(compact_items) - len(compact_errors),
        "failed_items": len(compact_errors),
        "user_profile": user_profile or "未提供",
        "papers": compact_items,
        "errors": compact_errors,
    }

    # 5. LLM 汇总报告
    report_path = output_path / f"{batch_id}_summary.md"
    try:
        sys_prompt, user_prompt = load_prompt(
            batch_template,
            user_profile=user_profile or "未提供",
            source_file=str(input_path),
            batch_count=str(len(compact_items)),
            papers_json=json.dumps(payload, ensure_ascii=False, indent=2),
        )
        report_text = llm.call(
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=2600,
            temperature=0.2,
        )
    except Exception as exc:
        report_text = _fallback_report(payload)

    report_path.write_text(report_text, encoding="utf-8")
    return str(report_path)


__all__ = ["run_arxiv_value_pipeline"]
