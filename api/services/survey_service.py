"""论文调研 Service 层 — 统一任务管线（search→download→convert→translate）。

survey 现在是 UnifiedTask（持久化到 tasks.json），后台由 TaskExecutor 跑多阶段管线。
auto_convert / auto_translate / translate_model 等选项真正接线到 RemotePDFService。
"""

from __future__ import annotations

import json as _json
import re
import sys
import time as _time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from api.schemas.survey import SurveyRequest, SurveyResult, PaperSummary, ProgressInfo
from api.schemas.task import UnifiedTask
import api.services.task_store as store
import api.services.task_executor as executor
from api.services.task_handlers import _wait_remote_job


# ── 旧版 surveys.json 迁移（一次性，导入历史记录） ───
_LEGACY_SURVEYS_FILE = Path(__file__).resolve().parent.parent.parent / ".data" / "surveys.json"


def _migrate_legacy_surveys() -> None:
    if not _LEGACY_SURVEYS_FILE.exists():
        return
    try:
        raw = _json.loads(_LEGACY_SURVEYS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return
    migrated = 0
    for tid, s in raw.items():
        if store.get(tid):
            continue
        try:
            created = datetime.fromisoformat(s.get("created_at")) if s.get("created_at") else datetime.now()
        except Exception:
            created = datetime.now()
        task = UnifiedTask(
            id=tid,
            type="survey",
            title="调研（历史）",
            status="completed" if s.get("status") == "completed" else "failed",
            created_at=created,
            updated_at=datetime.now(),
            completed_at=datetime.now(),
            options={},
            stages=[{"key": "search", "label": "搜索 arXiv", "status": "completed", "detail": "历史记录"}],
            result={"papers": s.get("papers", [])} if s.get("papers") else None,
            error=s.get("error"),
        )
        with store._lock:
            store._tasks[tid] = task
        migrated += 1
    if migrated:
        store._save_tasks()
        print(f"migrated {migrated} legacy surveys")


_migrate_legacy_surveys()


# ── 阶段辅助 ──────────────────────────────────────

def _download_survey_pdfs(json_path: str, top_n: int = 15) -> list[str]:
    """读 survey JSON，下载前 top_n 篇 PDF 到 data/pdfs，返回本地路径列表。"""
    from backend.services.arxiv.search import download_paper_from_source
    from backend.config.settings import Settings

    pdf_dir = str(Settings().local_pdfs_dir)
    Path(pdf_dir).mkdir(parents=True, exist_ok=True)
    paths = []
    try:
        with open(json_path, encoding="utf-8") as f:
            data = _json.load(f)
        papers = data.get("papers", [])
    except Exception:
        return paths
    for p in papers[:top_n]:
        source = p.get("arxiv_id") or p.get("pdf_url", "")
        if not source:
            continue
        try:
            path = download_paper_from_source(
                source=source, output_dir=pdf_dir,
                title_hint=p.get("title", "")[:60], timeout=60)
            if path:
                paths.append(path)
        except Exception:
            continue
    return paths


def _submit_err(result: dict) -> str:
    for s in result.get("steps", []):
        if s.get("status") == "failed":
            return s.get("error", "未知")
    return result.get("error", "提交失败")


def _is_stage_completed(task_id: str, key: str) -> bool:
    task = store.get(task_id)
    if not task:
        return False
    for s in task.stages:
        if s.key == key:
            return s.status == "completed"
    return False


def _run_survey_pipeline(task_id: str, req: SurveyRequest) -> None:
    """survey 管线：search → download → convert → translate。

    支持阶段级断点续跑：已完成阶段跳过（复用 task.result），未完成的从断点续跑。
    末尾自动归档（run_archive）把产物归入 knowledgebase/papers/。
    """
    # 读取既有结果（断点续跑用）
    task_result: dict = {}
    task = store.get(task_id)
    if task and task.result:
        task_result = dict(task.result)
    json_path = task_result.get("json_path", "")
    pdf_paths: list[str] = task_result.get("pdf_paths", [])
    convert_job_id = task_result.get("convert_job_id")

    try:
        # ── search（已完成则跳过）──
        if not _is_stage_completed(task_id, "search"):
            from backend.services.arxiv.survey import run_survey

            def cb(percent: int, step: str) -> None:
                store.update_stage(task_id, "search", status="running", progress=percent, detail=step)

            result = run_survey(
                raw_query=req.raw_query or req.keywords or "",
                output_dir=None,
                max_papers=req.max_results,
                download=False,          # 下载独立成阶段
                progress_callback=cb,
            )
            if "error" in result:
                store.update_stage(task_id, "search", status="failed", error=result["error"])
                store.set_status(task_id, "failed", error=result["error"])
                return
            json_path = result.get("json_path", "")
            papers = []
            try:
                with open(json_path, encoding="utf-8") as f:
                    papers = _json.load(f).get("papers", [])
            except Exception:
                pass
            task_result = {"json_path": json_path, "papers": papers, "pdf_paths": []}
            store.set_result(task_id, task_result)
            store.update_stage(task_id, "search", status="completed", progress=40,
                               detail=f"搜索 {result.get('total_searched', 0)} 篇")
            if executor.is_cancelled(task_id):
                return
        # search 已完成但 result 缺 papers 时补读
        if "papers" not in task_result and json_path:
            try:
                with open(json_path, encoding="utf-8") as f:
                    task_result["papers"] = _json.load(f).get("papers", [])
                store.set_result(task_id, task_result)
            except Exception:
                pass

        # ── download（已完成则复用 pdf_paths）──
        if req.auto_download and not _is_stage_completed(task_id, "download"):
            pdf_paths = _download_survey_pdfs(json_path, top_n=max(1, req.top_n))
            if not pdf_paths:
                store.update_stage(task_id, "download", status="failed", detail="下载 PDF 失败")
                store.set_status(task_id, "failed", error="下载 PDF 失败")
                return
            task_result["pdf_paths"] = pdf_paths
            store.set_result(task_id, task_result)
            store.update_stage(task_id, "download", status="completed", progress=55,
                               detail=f"下载 {len(pdf_paths)} 篇")
            if executor.is_cancelled(task_id):
                return
        elif req.auto_download:
            pdf_paths = task_result.get("pdf_paths", [])

        # ── convert（已完成则跳过）──
        if req.auto_convert and not _is_stage_completed(task_id, "convert"):
            # PDF 可能被上次转换归档进 job 目录（data/pdfs 路径失效）→ 重新下载
            if not pdf_paths or not all(Path(p).exists() for p in pdf_paths):
                pdf_paths = _download_survey_pdfs(json_path, top_n=max(1, req.top_n))
                task_result["pdf_paths"] = pdf_paths
                store.set_result(task_id, task_result)
            if not pdf_paths:
                store.update_stage(task_id, "convert", status="failed", detail="没有可转换的 PDF")
                store.set_status(task_id, "failed", error="没有可转换的 PDF")
                return
            from backend.services.pdf_remote.service import RemotePDFService
            svc = RemotePDFService()
            sub = svc.submit_batch(file_list=pdf_paths, translate_requested=req.auto_translate)
            if not sub.get("success"):
                err = _submit_err(sub)
                store.update_stage(task_id, "convert", status="failed", error=err)
                store.set_status(task_id, "failed", error=f"转换提交失败: {err}")
                return
            convert_job_id = sub.get("job_id")
            task_result["convert_job_id"] = convert_job_id
            store.set_result(task_id, task_result)
            store.update_stage(task_id, "convert", status="running", remote_job_id=convert_job_id,
                               progress=60, detail=f"已提交转换 job {convert_job_id}")
            st = _wait_remote_job(task_id, convert_job_id)
            if executor.is_cancelled(task_id):
                return
            if st != "collected":
                store.update_stage(task_id, "convert", status="failed", error=f"远程转换未完成({st})")
                store.set_status(task_id, "failed", error=f"远程转换未完成({st})")
                return
            store.update_stage(task_id, "convert", status="completed", progress=85,
                               detail=f"转换完成 job={convert_job_id}")

        # ── translate（已完成则跳过）──
        if req.auto_translate and not _is_stage_completed(task_id, "translate"):
            if not convert_job_id:
                store.update_stage(task_id, "translate", status="failed", detail="无转换任务")
                store.set_status(task_id, "failed", error="无转换任务")
                return
            from backend.services.pdf_remote.service import RemotePDFService
            svc = RemotePDFService()
            tr = svc.submit_translate(source_job_id=convert_job_id, model=req.translate_model,
                                      detect=req.detect_lang, content_type=req.content_type)
            if not tr.get("success"):
                err = _submit_err(tr)
                store.update_stage(task_id, "translate", status="failed", error=err)
                store.set_status(task_id, "failed", error=f"翻译提交失败: {err}")
                return
            tr_job = tr.get("job_id")
            task_result["translate_job_id"] = tr_job
            store.set_result(task_id, task_result)
            store.update_stage(task_id, "translate", status="running", remote_job_id=tr_job,
                               progress=90, detail=f"已提交翻译 job {tr_job}")
            st = _wait_remote_job(task_id, tr_job)
            if executor.is_cancelled(task_id):
                return
            if st != "collected":
                store.update_stage(task_id, "translate", status="failed", error=f"翻译未完成({st})")
                store.set_status(task_id, "failed", error=f"翻译未完成({st})")
                return
            store.update_stage(task_id, "translate", status="completed", progress=100, detail="翻译完成")

        # ── 末尾自动归档 ──
        try:
            from backend.services.archive.service import run_archive
            run_archive()
            task_result["archived"] = True
            store.set_result(task_id, task_result)
        except Exception as e:
            print(f"survey archive failed: {e}")

        store.set_status(task_id, "completed")
    except Exception as e:
        print(f"survey pipeline {task_id} failed: {e}")
        store.set_status(task_id, "failed", error=str(e))


class SurveyService:

    @staticmethod
    def create(req: SurveyRequest) -> str:
        """创建统一 survey 任务并后台执行管线，返回 task_id。"""
        raw_query = (req.raw_query or req.keywords or "").strip()
        title = f"调研: {raw_query[:40]}" if raw_query else "调研"
        task = store.create("survey", title, [
            {"key": "search", "label": "搜索 arXiv"},
            {"key": "download", "label": "下载 PDF"},
            {"key": "convert", "label": "远程转换"},
            {"key": "translate", "label": "远程翻译"},
        ], options=req.model_dump())
        if not req.auto_download:
            store.update_stage(task.id, "download", status="skipped", detail="未启用")
        if not req.auto_convert:
            store.update_stage(task.id, "convert", status="skipped", detail="未启用")
        if not req.auto_translate:
            store.update_stage(task.id, "translate", status="skipped", detail="未启用")
        executor.submit(task.id, _run_survey_pipeline, req)
        return task.id

    @staticmethod
    def list() -> list[UnifiedTask]:
        return store.list_tasks(task_type="survey")

    @staticmethod
    def get(task_id: str) -> UnifiedTask | None:
        return store.get(task_id)

    @staticmethod
    def get_progress(task_id: str) -> ProgressInfo | None:
        task = store.get(task_id)
        if not task:
            return None
        if not task.stages:
            return ProgressInfo(percent=0, current_step=task.status)
        pct = int(sum(s.progress for s in task.stages) / len(task.stages))
        current = next((s.detail for s in task.stages if s.status == "running"), task.status)
        return ProgressInfo(percent=pct, current_step=current)

    @staticmethod
    def cancel(task_id: str) -> bool:
        ok = executor.cancel(task_id)
        if ok:
            store.set_status(task_id, "cancelled")
        return ok

    @staticmethod
    def to_survey_result(task: UnifiedTask) -> SurveyResult:
        """把统一任务转回旧 SurveyResult（供 survey_table.html 渲染）。"""
        papers = []
        if task.result:
            for p in task.result.get("papers", []):
                papers.append(PaperSummary(
                    title=p.get("title", ""),
                    arxiv_id=p.get("arxiv_id", ""),
                    authors=p.get("authors", []),
                    year=p.get("year") or 0,
                    pdf_url=p.get("pdf_url", ""),
                    entry_url=p.get("entry_url", ""),
                    citations=p.get("citations", 0),
                    ccf_rank=p.get("ccf_rank", ""),
                    score=p.get("score", 0.0),
                    abstract=p.get("abstract", ""),
                    primary_category=p.get("primary_category", ""),
                    is_accepted=p.get("is_accepted", False),
                    has_code_or_data=p.get("has_code_or_data", False),
                    page_count=p.get("page_count"),
                ))
        return SurveyResult(
            task_id=task.id,
            status=task.status,
            papers=papers,
            progress=SurveyService.get_progress(task.id),
            error=task.error,
        )

    # ── 智能搜索：AI 解析 ──────────────────────────

    @staticmethod
    def parse_query(text: str = "", md_content: str = "") -> dict:
        """AI 解析用户输入的文本 + .md 文件内容，生成 raw_query + 解释。"""
        from backend.config.settings import Settings
        from backend.pipelines._prompts import load_prompt

        combined = text.strip()
        if md_content.strip():
            combined += "\n\n--- .md 文件内容 ---\n\n" + md_content.strip()

        if not combined:
            return {"raw_query": "", "explanation": "未提供输入内容", "parsed": {}}

        settings = Settings()
        llm = settings.get_llm_client(model_key="v4flash")

        sys_prompt, user_prompt = load_prompt("smart_query", sub_dir="arxiv", user_input=combined)

        try:
            result = llm.call(
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=1000,
                temperature=0.1,
            )
            import json as _json2
            text = result.strip()
            if text.startswith("```"):
                m = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
                if m:
                    text = m.group(1).strip()
            parsed = _json2.loads(text)
            return {
                "raw_query": parsed.get("raw_query", ""),
                "explanation": parsed.get("explanation", ""),
                "parsed": {},
            }
        except Exception as e:
            return {"raw_query": "", "explanation": f"AI 解析失败: {e}", "parsed": {}}

    # ── 智能搜索：解析 raw_query → 字段 ────────────

    @staticmethod
    def parse_raw_query(raw_query: str) -> dict:
        """机械解析 raw_query，拆解为各字段（用于双向同步）。"""
        if not raw_query.strip():
            return {}

        result: dict[str, str] = {}
        date_m = re.search(r'submittedDate:\[(\d{8})\s+TO\s+(\d{8})\]', raw_query)
        if date_m:
            result["date_from"] = date_m.group(1)
            result["date_to"] = date_m.group(2)

        for prefix, field in {
            'all:': 'query', 'ti:': 'title', 'au:': 'author',
            'cat:': 'category', 'abs:': 'abstract',
        }.items():
            pattern = re.escape(prefix) + r'(?:"([^"]+)"|(\S+))'
            m = re.search(pattern, raw_query)
            if m:
                result[field] = m.group(1) or m.group(2)

        sort_m = re.search(r'sortBy=(\w+)', raw_query)
        if sort_m:
            result["sort_by"] = sort_m.group(1)
        sort_o = re.search(r'sortOrder=(\w+)', raw_query)
        if sort_o:
            result["sort_order"] = sort_o.group(1)
        return result

    # ── 快速测试：搜索 ────────────────────────────

    @staticmethod
    def quick_search(
        keywords: str = "",
        raw_query: str = "",
        title: str = "",
        author: str = "",
        category: str = "",
        abstract: str = "",
        date_from: str = "",
        date_to: str = "",
        max_results: int = 10,
        sort_by: str = "relevance",
        sort_order: str = "descending",
        page: int = 1,
    ) -> list[PaperSummary]:
        """快速搜索 arXiv，同步返回。"""
        try:
            from backend.services.arxiv.search import ArxivSearchParams, search_arxiv

            start = max(0, (page - 1) * max_results)
            params = ArxivSearchParams(
                raw_query=raw_query,
                query=keywords,
                title=title,
                author=author,
                category=category,
                abstract=abstract,
                date_from=date_from,
                date_to=date_to,
                start=start,
                max_results=max_results,
                sort_by=sort_by,
                sort_order=sort_order,
            )
            papers = search_arxiv(params, timeout=15)
            return [
                PaperSummary(
                    title=p.title,
                    authors=p.authors,
                    year=int(p.published[:4]) if len(p.published) >= 4 else 0,
                    arxiv_id=p.arxiv_id,
                    pdf_url=p.pdf_url,
                    entry_url=p.entry_url,
                    abstract=p.summary,
                    primary_category=p.primary_category,
                    is_accepted=p.is_accepted,
                    has_code_or_data=p.has_code_or_data,
                    page_count=p.page_count,
                )
                for p in papers
            ]
        except Exception as e:
            print(f"quick_search error: {e}")
            return []

    # ── LLM 分析 ─────────────────────────────────

    @staticmethod
    def analyze(arxiv_ids: list[str]) -> str:
        """启动 LLM 分析。

        TODO: 需要先把 arxiv_ids 转为 survey JSON 格式输入文件，
              再调用 backend.pipelines.arxiv_value.run_arxiv_value_pipeline()。
              目前前端 /survey/analyze 页已废弃，此方法待新页面确认后重写。
        """
        return uuid.uuid4().hex[:12]
