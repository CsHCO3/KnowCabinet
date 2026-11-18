"""QQ pipeline 只读 API。

写操作（QCE 导出、媒体对账、LLM 提炼、知识合成）只在 CLI 显式确认后执行；
这里不提供同步写端点，避免 Web 请求触发长任务或误花 LLM 费用。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from api.schemas.common import APIResponse
from backend.services.community.qq.service import QQPipelineService

router = APIRouter(prefix="/api/community/qq", tags=["community-qq"])


def _call(func, *args, **kwargs):
    try:
        result = func(*args, **kwargs)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return APIResponse(
        success=result.ok,
        data={
            "exit_code": result.exit_code,
            "timeout": result.timeout,
            "code_root": result.cwd,
            "output": result.output,
        },
        error=None if result.ok else f"命令退出码 {result.exit_code}",
    )


@router.get("/export-status")
def export_status():
    return _call(QQPipelineService().export_status)


@router.get("/export-dry-run")
def export_dry_run(only: str | None = None, overlap: float | None = None):
    return _call(QQPipelineService().export_dry_run, only=only, overlap=overlap)


@router.get("/archive-list")
def archive_list():
    return _call(QQPipelineService().archive_list)


@router.get("/groups/{group}/plan")
def plan(group: str):
    return _call(QQPipelineService().analyze_plan, group)


@router.get("/groups/{group}/analyze-status")
def analyze_status(group: str):
    return _call(QQPipelineService().analyze_status, group)


@router.get("/groups/{group}/kb-check")
def kb_check(group: str):
    return _call(QQPipelineService().kb_check, group)


@router.get("/verify")
def verify():
    result = QQPipelineService().verify()
    return APIResponse(success=result["all_ok"], data=result, error=None if result["all_ok"] else "存在未通过的判据")
