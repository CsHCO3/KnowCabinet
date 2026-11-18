"""远程转换任务（pdf / mineru）提交与详情 API。

复用 backend.services.pdf_remote 的提交链路，与 CLI 完全一致：
上传/选已有文件 → 建 job 记录（含源文件归档进 job 目录）→ 前端任务列表可见。
"""

import asyncio
import os
import shlex
from pathlib import Path

from fastapi import APIRouter, Request, File, UploadFile, Form

from api.schemas.common import APIResponse

router = APIRouter(prefix="/api", tags=["pdf-tasks"])

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_INBOX_DIR = _PROJECT_ROOT / "data" / "remote_inbox"
_DEFAULT_LOCAL_DIR = _PROJECT_ROOT / "data" / "pdfs"

# 支持格式（marker 只收 pdf；mineru 收更广）
_MINERU_EXTS = {".pdf", ".pptx", ".docx", ".xlsx",
                ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
_SUPPORTED_EXTS = {".pdf"} | _MINERU_EXTS


def _save_upload(file: UploadFile) -> str:
    """把上传文件存进 data/remote_inbox/，返回本地绝对路径。"""
    _INBOX_DIR.mkdir(parents=True, exist_ok=True)
    name = os.path.basename(file.filename or "upload")
    dest = _INBOX_DIR / name
    if dest.exists():
        stem, ext = os.path.splitext(name)
        i = 1
        while dest.exists():
            dest = _INBOX_DIR / f"{stem}_{i}{ext}"
            i += 1
    with open(dest, "wb") as fh:
        while True:
            chunk = file.file.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
    return str(dest)


def _remove_safe(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def _load_jobs():
    from backend.services.pdf_remote.parser import _load_jobs as _lj
    return _lj()


def _tail_remote_log(log_file: str | None) -> str:
    """尽力拉远端日志 tail；失败返回空串。"""
    if not log_file:
        return ""
    try:
        from backend.services.pdf_remote.ssh import SSHClient
        c = SSHClient()
        if not c.connect():
            return ""
        try:
            out, _, _ = c.execute_command(
                f"tail -15 {shlex.quote(log_file)} 2>/dev/null || true")
            return out
        finally:
            c.close()
    except Exception:
        return ""


@router.get("/pdf/engines")
def engines():
    """两引擎能力 + 可用选项，前端据此渲染表单。"""
    return {
        "engines": [
            {
                "key": "marker",
                "label": "marker（论文）",
                "exts": [".pdf"],
                "options": [
                    {"key": "translate", "label": "标记后续翻译", "type": "bool"},
                ],
            },
            {
                "key": "mineru",
                "label": "mineru（office/图片/pdf）",
                "exts": sorted(_MINERU_EXTS),
                "options": [
                    {"key": "method", "label": "解析方法", "type": "select",
                     "choices": ["auto", "txt", "ocr"]},
                    {"key": "lang", "label": "语言", "type": "text"},
                    {"key": "backend", "label": "后端", "type": "text", "default": "pipeline"},
                    {"key": "formula", "label": "公式解析", "type": "bool"},
                    {"key": "table", "label": "表格解析", "type": "bool"},
                ],
            },
        ],
    }


@router.get("/pdf/gpu-status")
def gpu_status():
    """远端 GPU 显存状态（只读诊断，不触发清理）。"""
    from backend.services.pdf_remote.parser import PDFParser
    parser = PDFParser()
    try:
        if not parser.ssh_client.connect():
            return APIResponse(success=False, error="SSH 连接失败")
        st = parser._gpu_status()
        if not st.get("ok"):
            return APIResponse(success=False, error=st.get("stderr") or st.get("stdout") or "GPU 状态不可用")
        from backend.config.settings import Settings
        need = int(Settings().remote_min_gpu_free_mb)
        return APIResponse(success=True, data={
            **st, "required_mb": need,
            "ok_for_submit": st["free_mb"] >= need,
        })
    finally:
        parser.close()


@router.get("/pdf/local-files")
def local_files(dir: str = ""):
    """列本地目录（默认 data/pdfs）里的支持格式文件，供"选已有"。"""
    base = (Path(dir).resolve() if dir else _DEFAULT_LOCAL_DIR)
    if not base.is_dir():
        return {"files": [], "dir": str(base), "error": "目录不存在"}
    files = []
    for p in sorted(base.iterdir()):
        if p.is_file() and p.suffix.lower() in _SUPPORTED_EXTS:
            files.append({"path": str(p), "name": p.name, "size": p.stat().st_size})
    return {"files": files, "dir": str(base)}


@router.post("/pdf/submit")
async def submit(
    request: Request,
    engine: str = Form("marker"),
    source: str = Form("upload"),   # upload | existing
    paths: str = Form(""),          # source=existing 时，逗号分隔的本地路径
    translate: bool = Form(False),  # marker
    method: str = Form(""),         # mineru
    lang: str = Form(""),
    backend: str = Form("pipeline"),
    formula: str = Form(""),
    table: str = Form(""),
    files: list[UploadFile] = File(default=[]),
):
    """提交远程转换任务：上传文件或选已有 → 调 backend 同链路提交。"""
    file_list: list[str] = []
    saved: list[str] = []
    if source == "upload":
        for f in files or []:
            if f and f.filename:
                p = _save_upload(f)
                saved.append(p)
                file_list.append(p)
    elif source == "existing":
        file_list = [p.strip() for p in paths.split(",") if p.strip()]

    if not file_list:
        return APIResponse(success=False, error="没有可提交的文件")

    # 引擎扩展名校验兜底
    exts = {".pdf"} if engine == "marker" else _MINERU_EXTS
    bad = [p for p in file_list if os.path.splitext(p)[1].lower() not in exts]
    if bad:
        for p in saved:
            _remove_safe(p)
        return APIResponse(success=False, error=f"引擎 {engine} 不支持: {', '.join(os.path.basename(b) for b in bad[:5])}")

    def _do():
        from backend.services.pdf_remote.service import RemotePDFService
        svc = RemotePDFService()
        if engine == "mineru":
            return svc.submit_mineru(
                file_list=file_list, method=method or None, lang=lang or None,
                backend=backend or "pipeline",
                formula=formula or None, table=table or None)
        return svc.submit_batch(file_list=file_list, translate_requested=translate)

    result = await asyncio.to_thread(_do)
    if result.get("success"):
        job_id = result.get("job_id")
        task_id = None
        if job_id:
            # 建统一任务：submit 完成 + 后台 collect 轮询
            import api.services.task_store as store
            import api.services.task_executor as executor
            from api.services.task_handlers import _run_collect_poll
            task_type = "mineru" if engine == "mineru" else "convert"
            names = ", ".join(os.path.basename(f) for f in file_list[:3])
            task = store.create(task_type, f"{task_type}: {names}",
                                [{"key": "submit", "label": "提交上传"},
                                 {"key": "collect", "label": "收集结果"}],
                                options={"engine": engine, "file_list": file_list,
                                         "translate": translate, "method": method,
                                         "lang": lang, "backend": backend,
                                         "formula": formula, "table": table})
            store.update_stage(task.id, "submit", status="completed", progress=40,
                               detail=f"已上传 {result.get('uploaded_count', 0)} 个文件")
            store.update_stage(task.id, "collect", status="running",
                               remote_job_id=job_id, progress=50, detail=f"job {job_id}")
            executor.submit(task.id, _run_collect_poll, job_id)
            task_id = task.id
        return APIResponse(success=True, message="任务已提交", data={
            "job_id": job_id,
            "task_id": task_id,
            "uploaded_count": result.get("uploaded_count"),
            "remote_log_file": result.get("remote_log_file"),
        })
    # 失败 → 清理已存 inbox 文件
    for p in saved:
        _remove_safe(p)
    err = next((s.get("error") for s in result.get("steps", [])
                if s.get("status") == "failed"), result.get("error", "提交失败"))
    return APIResponse(success=False, error=err)


@router.get("/pdf/jobs/{job_id}")
def job_detail(job_id: str):
    """任务详情：本地记录字段。日志单独接口（避免 SSH 卡顿阻塞详情）。"""
    job = next((j for j in _load_jobs() if j.get("id") == job_id), None)
    if not job:
        return APIResponse(success=False, error="任务不存在")
    return APIResponse(success=True, data=dict(job))


@router.get("/pdf/jobs/{job_id}/log")
def job_log(job_id: str):
    """尽力拉远端日志 tail；SSH 不可达时返回空。"""
    job = next((j for j in _load_jobs() if j.get("id") == job_id), None)
    if not job:
        return APIResponse(success=False, error="任务不存在")
    return APIResponse(success=True, data={"log_tail": _tail_remote_log(job.get("remote_log_file"))})
