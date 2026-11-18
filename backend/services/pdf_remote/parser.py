"""PDF 远程解析业务逻辑。

从 remote/pdf_parser_test.py 迁移，配置由 Settings 管理。
"""

import hashlib
import json
import logging
import os
import shlex
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from backend.config.deployment import require_env
from backend.config.settings import Settings
from backend.services.pdf_remote.ssh import SSHClient

logger = logging.getLogger("infosystem.pdf_remote")
_settings = Settings()

from backend.path_setup import PROJECT_ROOT as _ROOT
_JOBS_FILE = _ROOT / ".data" / "remote_jobs.json"

# mineru 支持的输入扩展名(pdf/图片/office)
MINERU_SUPPORTED_EXTS = {
    ".pdf", ".pptx", ".docx", ".xlsx",
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff",
}


# ── 作业状态管理 ─────────────────────────────────


def _load_jobs() -> list[dict]:
    if not _JOBS_FILE.exists():
        return []
    try:
        return json.loads(_JOBS_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to load remote jobs file: %s", exc)
        return []


def _save_jobs(jobs: list[dict]) -> None:
    _JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _JOBS_FILE.write_text(
        json.dumps(jobs, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def _add_job_record(record: dict) -> None:
    jobs = _load_jobs()
    jobs.append(record)
    _save_jobs(jobs)


def _has_active_job_for_files(files: list[str]) -> bool:
    """检查是否存在正在运行或已提交的作业，包含相同文件。

    如果同一组文件已经提交过且还在跑，返回 True，避免重复提交。
    """
    active_statuses = {"running", "submitted"}
    existing = _load_jobs()
    file_names = {os.path.basename(f) for f in files}
    completed_found = []
    for job in existing:
        uploaded = job.get("uploaded_files", [])
        job_files = {os.path.basename(u.get("local", "")) for u in uploaded}
        if not (job_files & file_names):
            continue
        if job.get("status") in active_statuses:
            return True  # 有活跃 job，阻止提交
        if job.get("status") in ("collected", "duplicate"):
            completed_found.append(job.get("id", "?"))
    # 不阻止，但警告已完成的重复
    if completed_found:
        logger.warning("文件 %s 已在已完成作业 %s 中出现过，将重新提交",
                       files, completed_found)
    return False


def _update_job(job_id: str, **updates) -> None:
    jobs = _load_jobs()
    for j in jobs:
        if j.get("id") == job_id:
            j.update(updates)
            break
    _save_jobs(jobs)


def _compute_file_hash(filepath: str) -> str:
    """计算文件的 SHA256 哈希（用于去重）。"""
    try:
        h = hashlib.sha256()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception as exc:
        logger.warning("Failed to hash %s: %s", filepath, exc)
        return ""


def _to_rel_path(abs_path: str) -> str:
    """将绝对路径转为相对于 PROJECT_ROOT 的路径。"""
    try:
        return str(Path(abs_path).resolve().relative_to(_ROOT))
    except ValueError:
        return abs_path


def _find_jobs_by_file_hash(file_hash: str, job_type: str | None = None) -> list[dict]:
    """按文件哈希查找已有的非失败 job(可按类型过滤)。

    job_type=None 时兼容旧行为: 只查 convert(marker) 作业。
    job_type="mineru" 时只查 mineru 作业, 避免跨引擎误伤。
    """
    jobs = _load_jobs()
    if not file_hash:
        return []
    result = []
    for j in jobs:
        jt = j.get("type") or "convert"
        if job_type is None:
            if jt != "convert":
                continue
        elif jt != job_type:
            continue
        if j.get("status") == "failed":
            continue
        uploaded = j.get("uploaded_files", [])
        for u in uploaded:
            if u.get("hash") == file_hash:
                result.append(j)
                break
    return result


class PDFParser:
    def __init__(self):
        self.ssh_client = SSHClient()

    def _collect_top_level_pdfs(self, root_dir: str) -> list[str]:
        return self._collect_top_level_files(root_dir, {".pdf"})

    def _collect_top_level_files(self, root_dir: str, exts: set[str]) -> list[str]:
        """扫描目录顶层的、扩展名在 exts 内的文件(不递归)。"""
        files = []
        for name in os.listdir(root_dir):
            full = os.path.join(root_dir, name)
            if os.path.isfile(full) and os.path.splitext(name)[1].lower() in exts:
                files.append(full)
        return files

    def _build_convert_command(self, file_names: list[str]) -> str:
        file_count = len(file_names)
        # 并行度可调：默认 2（实测 4 进程对大 PDF 会 OOM 丢结果，2 进程 4/4 稳定）；可用 MARKER_WORKERS 覆盖
        worker_n = os.environ.get("MARKER_WORKERS", "2")
        workers = f" --workers {worker_n}" if file_count > 1 else ""
        files_arg = ""
        if file_names:
            files_arg = f" --files {shlex.quote(','.join(file_names))}"
        convert_cmd = (
            f"{shlex.quote(_settings.remote_python)} {shlex.quote(_settings.remote_convert_script)} "
            f"--batch {shlex.quote(_settings.remote_input_dir)} "
            f"--output {shlex.quote(_settings.remote_output_dir)}{workers}{files_arg} --export-images"
        )
        return (
            "bash -lc "
            + shlex.quote(
                "set -e; "
                f"LOG_DIR={shlex.quote(_settings.remote_log_dir.rstrip('/'))}; "
                "mkdir -p \"$LOG_DIR\"; "
                "LOG_FILE=\"$LOG_DIR/convert_$(date +%Y%m%d_%H%M%S).log\"; "
                f"{_settings.remote_activate or 'true'}; "
                f"nohup {convert_cmd} > \"$LOG_FILE\" 2>&1 < /dev/null & "
                "PID=$!; "
                "echo STARTED_PID=$PID; "
                "echo LOG_FILE=$LOG_FILE"
            )
        )

    def _build_mineru_command(self, file_names: list[str],
                              method: str | None = None,
                              lang: str | None = None,
                              backend: str = "pipeline",
                              formula: str | None = None,
                              table: str | None = None) -> str:
        """构建远端 mineru_convert.py 批处理命令(串行、pipeline 后端)。"""
        files_arg = ""
        if file_names:
            files_arg = f" --files {shlex.quote(','.join(file_names))}"
        opts = [f"--backend {shlex.quote(backend)}"]
        if method:
            opts.append(f"--method {shlex.quote(method)}")
        if lang:
            opts.append(f"--lang {shlex.quote(lang)}")
        if formula is not None:
            opts.append(f"--formula {shlex.quote(str(formula))}")
        if table is not None:
            opts.append(f"--table {shlex.quote(str(table))}")

        convert_cmd = (
            f"{shlex.quote(_settings.remote_python)} {shlex.quote(_settings.remote_mineru_script)} "
            f"--batch {shlex.quote(_settings.remote_mineru_input_dir)} "
            f"--output {shlex.quote(_settings.remote_mineru_output_dir)}{files_arg} "
            + " ".join(opts)
        )
        return (
            "bash -lc "
            + shlex.quote(
                "set -e; "
                f"LOG_DIR={shlex.quote(_settings.remote_mineru_log_dir.rstrip('/'))}; "
                "mkdir -p \"$LOG_DIR\"; "
                "LOG_FILE=\"$LOG_DIR/mineru_$(date +%Y%m%d_%H%M%S).log\"; "
                f"{_settings.remote_activate or 'true'}; "
                f"nohup {convert_cmd} > \"$LOG_FILE\" 2>&1 < /dev/null & "
                "PID=$!; "
                "echo STARTED_PID=$PID; "
                "echo LOG_FILE=$LOG_FILE"
            )
        )

    @staticmethod
    def _parse_launch_info(stdout: str) -> tuple[str, str]:
        pid = ""
        log_file = ""
        for line in stdout.splitlines():
            if line.startswith("STARTED_PID="):
                pid = line.split("=", 1)[1].strip()
            elif line.startswith("LOG_FILE="):
                log_file = line.split("=", 1)[1].strip()
        return pid, log_file

    def _run_remote_preflight(self) -> tuple[str, str, int]:
        cmd = (
            "bash -lc "
            + shlex.quote(
                "set -e; "
                f"test -f {shlex.quote(_settings.remote_convert_script)}; "
                f"test -x {shlex.quote(_settings.remote_python)}; "
                f"mkdir -p {shlex.quote(_settings.remote_input_dir)} "
                f"{shlex.quote(_settings.remote_output_dir)} "
                f"{shlex.quote(_settings.remote_log_dir)}; "
                f"{_settings.remote_activate or 'true'}; "
                f"{shlex.quote(_settings.remote_python)} -V; "
                f"echo CONVERT_SCRIPT={shlex.quote(_settings.remote_convert_script)}"
            )
        )
        return self.ssh_client.execute_command(cmd)

    def _run_mineru_preflight(self) -> tuple[str, str, int]:
        cmd = (
            "bash -lc "
            + shlex.quote(
                "set -e; "
                f"test -f {shlex.quote(_settings.remote_mineru_script)}; "
                f"test -x {shlex.quote(_settings.remote_python)}; "
                f"mkdir -p {shlex.quote(_settings.remote_mineru_input_dir)} "
                f"{shlex.quote(_settings.remote_mineru_output_dir)} "
                f"{shlex.quote(_settings.remote_mineru_log_dir)}; "
                f"{_settings.remote_activate or 'true'}; "
                f"{shlex.quote(_settings.remote_python)} -V; "
                f"echo MINERU_SCRIPT={shlex.quote(_settings.remote_mineru_script)}"
            )
        )
        return self.ssh_client.execute_command(cmd)

    # ── GPU / Ollama 预检 ──────────────────────────

    def _run_ollama_cleanup(self) -> tuple[str, str, int]:
        """执行远端 ollama_cleanup.py：清理 UNTIL=Forever 且最近无请求的模型。

        脚本未部署时不阻断提交（只是少一次清理机会），由 GPU 显存预检兜底。
        """
        script = _settings.remote_ollama_cleanup_script
        if not script:
            return "", "", 0
        inner = (
            f"if [ -f {shlex.quote(script)} ]; then "
            f"{shlex.quote(_settings.remote_python)} {shlex.quote(script)} "
            "--recent-window 180 2>&1; "
            "else echo 'ollama_cleanup.py not deployed, skip'; fi"
        )
        return self.ssh_client.execute_command(f"bash -lc {shlex.quote(inner)}")

    def _gpu_status(self) -> dict:
        """查询远端 GPU 显存。返回 ok/free_mb/total_mb/gpus/processes/stdout/stderr。"""
        import csv
        import io

        cmd = ("nvidia-smi --query-gpu=index,name,memory.total,memory.used,memory.free "
               "--format=csv,noheader,nounits")
        out, err, rc = self.ssh_client.execute_command(cmd)
        gpus = []
        try:
            for row in csv.reader(io.StringIO(out)):
                if len(row) < 5:
                    continue
                gpus.append({
                    "index": int(row[0]),
                    "name": row[1],
                    "total_mb": int(row[2]),
                    "used_mb": int(row[3]),
                    "free_mb": int(row[4]),
                })
        except Exception:
            gpus = []
        free_mb = max((g["free_mb"] for g in gpus), default=0)
        total_mb = max((g["total_mb"] for g in gpus), default=0)
        procs_out, _, _ = self.ssh_client.execute_command(
            "nvidia-smi --query-compute-apps=pid,process_name,used_memory "
            "--format=csv,noheader,nounits 2>/dev/null || true")
        processes = []
        for line in procs_out.splitlines():
            parts = [p.strip() for p in line.split(",", 2)]
            if len(parts) == 3:
                processes.append({"pid": parts[0], "name": parts[1],
                                  "used_mb": parts[2]})
        return {
            "ok": rc == 0 and bool(gpus),
            "free_mb": free_mb,
            "total_mb": total_mb,
            "gpus": gpus,
            "processes": processes,
            "stdout": out,
            "stderr": err,
        }

    def _run_gpu_preflight(self) -> tuple[str, str, int]:
        """远端显存检查。不足时返回明确错误，避免上传后远端才失败。"""
        st = self._gpu_status()
        if not st["ok"]:
            return "", f"无法获取远端 GPU 状态: {st['stderr'] or st['stdout']}"[:500], 1
        need = int(_settings.remote_min_gpu_free_mb)
        if st["free_mb"] < need:
            detail = "; ".join(
                f"GPU{g['index']} {g['name']} free={g['free_mb']}MB" for g in st["gpus"])
            return "", (
                f"远端显存不足：最大空闲 {st['free_mb']}MB，需要至少 {need}MB（{detail}）。"
                "请释放 GPU（Ollama 常驻模型等）后重试。"), 1
        return st["stdout"], "", 0

    # ── 源文件归档（提交时复制进 job 目录，原件保留）──────────

    @staticmethod
    def _local_job_dir(job_id: str) -> str:
        """job 的本地结果/源文件归档目录。"""
        return str(_ROOT / "data" / "remote_pdf" / job_id)

    def _archive_sources(self, uploaded_files: list[dict], job_id: str) -> list[str]:
        """把本地源文件复制进 data/remote_pdf/{job_id}/（自包含），并记 u['job_copy']。

        原件保留在 data/pdfs 等原位，不再被搬走；返回副本路径列表供失败清理。
        """
        job_dir = self._local_job_dir(job_id)
        os.makedirs(job_dir, exist_ok=True)
        copied: list[str] = []
        used: set[str] = set()
        for u in uploaded_files:
            orig = u["local"]
            orig_abs = (_ROOT / orig) if not os.path.isabs(orig) else Path(orig)
            if not orig_abs.exists():
                continue  # 源文件已不存在，跳过
            name = os.path.basename(str(orig_abs))
            if name in used:
                # 同名文件（如多处 paper.pdf）：用父目录名做前缀，避免互相覆盖
                parent = os.path.basename(os.path.dirname(str(orig_abs)))
                name = f"{parent}_{name}" if parent else f"{len(used)}_{name}"
            used.add(name)
            dest = os.path.join(job_dir, name)
            shutil.copy2(str(orig_abs), dest)
            # local 保持原路径（源文件保留在 data/pdfs 等原位，不搬走）；
            # 另记 job 目录内的自包含副本。
            u["job_copy"] = _to_rel_path(dest)
            copied.append(dest)
        return copied

    @staticmethod
    def _cleanup_copies(copied: list[str]) -> None:
        """提交失败时删除已复制进 job 目录的副本。"""
        for dest in copied:
            try:
                if os.path.exists(dest):
                    os.remove(dest)
            except Exception:
                pass

    def submit_batch_task(self, file_list: list[str] | None = None,
                          translate_requested: bool = False) -> Dict[str, Any]:
        require_env("REMOTE_PYTHON", "REMOTE_CONVERT_SCRIPT", "REMOTE_INPUT_DIR", "REMOTE_OUTPUT_DIR", "REMOTE_LOG_DIR")
        result: Dict[str, Any] = {
            "success": False,
            "steps": [],
            "uploaded_count": 0,
            "local_pdfs_dir": str(_settings.local_pdfs_dir),
            "remote_input_dir": _settings.remote_input_dir,
            "remote_output_dir": _settings.remote_output_dir,
            "remote_log_dir": _settings.remote_log_dir,
        }

        try:
            if file_list:
                files = [f for f in file_list if os.path.isfile(f) and f.lower().endswith(".pdf")]
                if not files:
                    result["steps"].append({"step": "scan_local_pdfs", "status": "failed", "error": "No valid PDF files in --files"})
                    return result
            else:
                local_pdfs_dir = os.path.abspath(str(_settings.local_pdfs_dir))
                if not os.path.isdir(local_pdfs_dir):
                    result["steps"].append({"step": "scan_local_pdfs", "status": "failed", "error": f"Directory not found: {local_pdfs_dir}"})
                    return result
                files = self._collect_top_level_pdfs(local_pdfs_dir)
                if not files:
                    result["steps"].append({"step": "scan_local_pdfs", "status": "failed", "error": f"No top-level PDFs in: {local_pdfs_dir}"})
                    return result
            result["steps"].append({"step": "scan_local_pdfs", "status": "success", "count": len(files)})

            # ── 去重：先按文件名去活跃job，再按哈希去所有非失败job ──
            new_files = [f for f in files if not _has_active_job_for_files([f])]
            if len(new_files) < len(files):
                logger.info("Skipped %d file(s) already in active jobs", len(files) - len(new_files))

            for f in list(new_files):
                file_hash = _compute_file_hash(f)
                existing = _find_jobs_by_file_hash(file_hash) if file_hash else []
                if existing:
                    logger.warning("文件 %s (SHA256=%s...) 已在作业 %s 中提交过，跳过",
                                   os.path.basename(f), file_hash[:12],
                                   [e["id"] for e in existing])
                    new_files.remove(f)

            if not new_files:
                result["steps"].append({"step": "dedup", "status": "failed",
                                        "error": "All files already have existing jobs (by filename or hash)"})
                return result
            files = new_files
            result["steps"].append({"step": "dedup", "status": "success", "count": len(files)})

            pre_stdout, pre_stderr, pre_exit = self._run_remote_preflight()
            result["preflight_stdout"] = pre_stdout
            result["preflight_stderr"] = pre_stderr
            if pre_exit != 0:
                result["steps"].append({"step": "remote_preflight", "status": "failed", "error": pre_stderr[:500] or f"exit={pre_exit}"})
                return result
            result["steps"].append({"step": "remote_preflight", "status": "success"})

            # Ollama Forever 模型清理 + GPU 显存预检（不足直接失败，不上传）
            clean_out, clean_err, _ = self._run_ollama_cleanup()
            result["ollama_cleanup"] = (clean_out or clean_err).strip()[:2000]
            gpu_out, gpu_err, gpu_exit = self._run_gpu_preflight()
            result["gpu_status"] = self._gpu_status()
            if gpu_exit != 0:
                result["steps"].append({"step": "gpu_check", "status": "failed",
                                        "error": gpu_err[:500]})
                return result
            result["steps"].append({"step": "gpu_check", "status": "success",
                                    "detail": f"free={result['gpu_status']['free_mb']}MB"})

            uploaded_files = []
            uploaded = 0
            for local_file in files:
                remote = f"{_settings.remote_input_dir.rstrip('/')}/{os.path.basename(local_file)}"
                if not self.ssh_client.upload_file(local_file, remote):
                    result["steps"].append({"step": "upload_files", "status": "failed", "error": f"Upload failed: {local_file}"})
                    return result
                uploaded_files.append({
                    "local": _to_rel_path(local_file),
                    "hash": _compute_file_hash(local_file),
                    "remote": remote,
                })
                uploaded += 1

            result["uploaded_count"] = uploaded
            result["uploaded_files"] = uploaded_files
            result["steps"].append({"step": "upload_files", "status": "success", "count": uploaded})

            uploaded_names = [os.path.basename(f) for f in files]
            job_id = uuid.uuid4().hex[:12]

            # 建 job 前把本地源文件复制进 job 目录（自包含；原件保留在 data/pdfs 等原位）
            copied = self._archive_sources(uploaded_files, job_id)
            result["job_id"] = job_id
            result["steps"].append({"step": "archive_sources", "status": "success", "count": len(copied)})

            command = self._build_convert_command(uploaded_names)
            stdout, stderr, exit_code = self.ssh_client.execute_command(command)
            result["command"] = command
            result["stdout"] = stdout
            result["stderr"] = stderr
            pid, log_file = self._parse_launch_info(stdout)
            result["remote_pid"] = pid
            result["remote_log_file"] = log_file

            if exit_code == 0 and pid:
                result["steps"].append({"step": "trigger_convert", "status": "success"})
                result["success"] = True
                _add_job_record({
                    "id": job_id,
                    "type": "convert",
                    "submitted_at": datetime.now().isoformat(timespec="seconds"),
                    "uploaded_files": uploaded_files,
                    "remote_input_dir": _settings.remote_input_dir,
                    "remote_output_dir": _settings.remote_output_dir,
                    "remote_pid": pid,
                    "remote_log_file": log_file,
                    "status": "running",
                    "translate_requested": translate_requested,
                })
            else:
                # 启动失败 → 清理已复制的副本（原件本来就在原位，无需搬回）
                self._cleanup_copies(copied)
                result["steps"].append({"step": "trigger_convert", "status": "failed", "error": stderr[:500] or f"exit={exit_code}, pid={pid or 'N/A'}"})

            return result

        except Exception as e:
            logger.error("Batch submit failed: %s", e, exc_info=True)
            result["steps"].append({"step": "exception", "status": "failed", "error": str(e)})
            return result

    # ── MinerU 转换(office/图片/PDF)───────────────

    def submit_mineru_task(self,
                           file_list: list[str],
                           method: str | None = None,
                           lang: str | None = None,
                           backend: str = "pipeline",
                           formula: str | None = None,
                           table: str | None = None) -> Dict[str, Any]:
        """提交远程 mineru 转换任务(pdf/pptx/docx/xlsx/图片)。

        文件由调用方显式指定(--files); 产物为 {stem}.tar(md+images),
        经 warmup 收集并解包。复用既有去重/预检/上传/nohup 拉起流程。
        """
        require_env("REMOTE_PYTHON", "REMOTE_MINERU_SCRIPT", "REMOTE_MINERU_INPUT_DIR", "REMOTE_MINERU_OUTPUT_DIR", "REMOTE_MINERU_LOG_DIR")
        result: Dict[str, Any] = {
            "success": False,
            "steps": [],
            "uploaded_count": 0,
            "remote_input_dir": _settings.remote_mineru_input_dir,
            "remote_output_dir": _settings.remote_mineru_output_dir,
            "remote_log_dir": _settings.remote_mineru_log_dir,
        }

        try:
            if not file_list:
                result["steps"].append({"step": "resolve_files", "status": "failed",
                                        "error": "未指定任何文件(--files 必填)"})
                return result
            files = [f for f in file_list
                     if os.path.isfile(f) and os.path.splitext(f)[1].lower() in MINERU_SUPPORTED_EXTS]
            if not files:
                result["steps"].append({"step": "resolve_files", "status": "failed",
                                        "error": "没有支持的文件(pdf/pptx/docx/xlsx/图片)"})
                return result
            result["steps"].append({"step": "resolve_files", "status": "success", "count": len(files)})

            # ── 去重：先按文件名去活跃job，再按哈希去所有非失败job ──
            new_files = [f for f in files if not _has_active_job_for_files([f])]
            if len(new_files) < len(files):
                logger.info("Skipped %d file(s) already in active jobs", len(files) - len(new_files))

            for f in list(new_files):
                file_hash = _compute_file_hash(f)
                existing = _find_jobs_by_file_hash(file_hash, "mineru") if file_hash else []
                if existing:
                    logger.warning("文件 %s (SHA256=%s...) 已在 mineru 作业 %s 中提交过，跳过",
                                   os.path.basename(f), file_hash[:12],
                                   [e["id"] for e in existing])
                    new_files.remove(f)

            if not new_files:
                result["steps"].append({"step": "dedup", "status": "failed",
                                        "error": "所有文件都已有存在的作业(按文件名或哈希)"})
                return result
            files = new_files
            result["steps"].append({"step": "dedup", "status": "success", "count": len(files)})

            pre_stdout, pre_stderr, pre_exit = self._run_mineru_preflight()
            result["preflight_stdout"] = pre_stdout
            result["preflight_stderr"] = pre_stderr
            if pre_exit != 0:
                result["steps"].append({"step": "remote_preflight", "status": "failed",
                                        "error": pre_stderr[:500] or f"exit={pre_exit}"})
                return result
            result["steps"].append({"step": "remote_preflight", "status": "success"})

            # Ollama Forever 模型清理 + GPU 显存预检（不足直接失败，不上传）
            clean_out, clean_err, _ = self._run_ollama_cleanup()
            result["ollama_cleanup"] = (clean_out or clean_err).strip()[:2000]
            gpu_out, gpu_err, gpu_exit = self._run_gpu_preflight()
            result["gpu_status"] = self._gpu_status()
            if gpu_exit != 0:
                result["steps"].append({"step": "gpu_check", "status": "failed",
                                        "error": gpu_err[:500]})
                return result
            result["steps"].append({"step": "gpu_check", "status": "success",
                                    "detail": f"free={result['gpu_status']['free_mb']}MB"})

            uploaded_files = []
            uploaded = 0
            for local_file in files:
                remote = f"{_settings.remote_mineru_input_dir.rstrip('/')}/{os.path.basename(local_file)}"
                if not self.ssh_client.upload_file(local_file, remote):
                    result["steps"].append({"step": "upload_files", "status": "failed",
                                            "error": f"Upload failed: {local_file}"})
                    return result
                uploaded_files.append({
                    "local": _to_rel_path(local_file),
                    "hash": _compute_file_hash(local_file),
                    "remote": remote,
                })
                uploaded += 1

            result["uploaded_count"] = uploaded
            result["uploaded_files"] = uploaded_files
            result["steps"].append({"step": "upload_files", "status": "success", "count": uploaded})

            uploaded_names = [os.path.basename(f) for f in files]
            job_id = uuid.uuid4().hex[:12]

            # 建 job 前把本地源文件复制进 job 目录（自包含；原件保留在 data/pdfs 等原位）
            copied = self._archive_sources(uploaded_files, job_id)
            result["job_id"] = job_id
            result["steps"].append({"step": "archive_sources", "status": "success", "count": len(copied)})

            command = self._build_mineru_command(uploaded_names, method=method, lang=lang,
                                                 backend=backend, formula=formula, table=table)
            stdout, stderr, exit_code = self.ssh_client.execute_command(command)
            result["command"] = command
            result["stdout"] = stdout
            result["stderr"] = stderr
            pid, log_file = self._parse_launch_info(stdout)
            result["remote_pid"] = pid
            result["remote_log_file"] = log_file

            if exit_code == 0 and pid:
                result["steps"].append({"step": "trigger_mineru", "status": "success"})
                result["success"] = True
                _add_job_record({
                    "id": job_id,
                    "type": "mineru",
                    "submitted_at": datetime.now().isoformat(timespec="seconds"),
                    "uploaded_files": uploaded_files,
                    "remote_input_dir": _settings.remote_mineru_input_dir,
                    "remote_output_dir": _settings.remote_mineru_output_dir,
                    "remote_log_dir": _settings.remote_mineru_log_dir,
                    "method": method,
                    "lang": lang,
                    "backend": backend,
                    "formula": formula,
                    "table": table,
                    "remote_pid": pid,
                    "remote_log_file": log_file,
                    "status": "running",
                })
            else:
                # 启动失败 → 清理已复制的副本（原件本来就在原位，无需搬回）
                self._cleanup_copies(copied)
                result["steps"].append({"step": "trigger_mineru", "status": "failed",
                                        "error": stderr[:500] or f"exit={exit_code}, pid={pid or 'N/A'}"})

            return result

        except Exception as e:
            logger.error("Mineru submit failed: %s", e, exc_info=True)
            result["steps"].append({"step": "exception", "status": "failed", "error": str(e)})
            return result

    # ── 翻译相关 ─────────────────────────────────

    def _build_translate_command(self, md_names: list[str],
                                 model: str = "qwen3:8b",
                                 detect: bool = True,
                                 content_type: str = "paper",
                                 prompt_override: str | None = None) -> str:
        """构建远程翻译命令。"""
        args = [
            f"--input {shlex.quote(_settings.remote_output_dir.rstrip('/'))}",
            f"--output {shlex.quote(_settings.remote_translate_dir.rstrip('/'))}",
            f"--model {shlex.quote(model)}",
            f"--content-type {shlex.quote(content_type)}",
        ]
        if not detect:
            args.append("--no-detect")
        if prompt_override:
            args.append(f"--prompt-override {shlex.quote(prompt_override)}")
        if md_names:
            args.append(f"--files {shlex.quote(','.join(md_names))}")

        translate_cmd = (
            f"{shlex.quote(_settings.remote_python)} {shlex.quote(_settings.remote_translate_script)} "
            + " ".join(args)
        )
        return (
            "bash -lc "
            + shlex.quote(
                "set -e; "
                f"LOG_DIR={shlex.quote(_settings.remote_log_dir.rstrip('/'))}; "
                "mkdir -p \"$LOG_DIR\"; "
                "LOG_FILE=\"$LOG_DIR/translate_$(date +%Y%m%d_%H%M%S).log\"; "
                f"{_settings.remote_activate or 'true'}; "
                f"nohup {translate_cmd} > \"$LOG_FILE\" 2>&1 < /dev/null & "
                "PID=$!; "
                "echo STARTED_PID=$PID; "
                "echo LOG_FILE=$LOG_FILE"
            )
        )

    @staticmethod
    def _find_jobs_by_type(job_type: str, statuses: set[str] | None = None) -> list[dict]:
        """查找指定类型和状态的 job 列表。"""
        jobs = _load_jobs()
        result = []
        for j in jobs:
            if j.get("type") != job_type:
                continue
            if statuses and j.get("status") not in statuses:
                continue
            result.append(j)
        return result

    def submit_translate_task(self,
                              md_file_names: list[str] | None = None,
                              model: str = "qwen3:8b",
                              detect: bool = True,
                              content_type: str = "paper",
                              prompt_override: str | None = None,
                              source_job_id: str | None = None) -> Dict[str, Any]:
        """提交远程翻译任务。

        不上传文件，直接引用 remote_output_dir 中已有的 .md 文件。
        """
        require_env("REMOTE_PYTHON", "REMOTE_TRANSLATE_SCRIPT", "REMOTE_OUTPUT_DIR", "REMOTE_TRANSLATE_DIR", "REMOTE_LOG_DIR")
        result: Dict[str, Any] = {
            "success": False,
            "steps": [],
            "remote_md_dir": _settings.remote_output_dir,
            "remote_translate_dir": _settings.remote_translate_dir,
        }

        try:
            # ── 确定要翻译的文件 ──
            if source_job_id and not md_file_names:
                jobs = _load_jobs()
                for j in jobs:
                    if j.get("id") == source_job_id:
                        uploaded = j.get("uploaded_files", [])
                        md_file_names = [
                            os.path.basename(u.get("local", "")).replace(".pdf", ".md")
                            for u in uploaded
                        ]
                        break

            if not md_file_names:
                result["steps"].append({"step": "resolve_md_files", "status": "failed",
                                        "error": "No .md files specified or resolved"})
                return result

            result["md_file_names"] = md_file_names
            result["steps"].append({"step": "resolve_md_files", "status": "success",
                                    "count": len(md_file_names)})

            # ── 预检 + 创建翻译目录 ──
            pre_stdout, pre_stderr, pre_exit = self._run_remote_preflight()
            check_cmd = (
                "bash -lc "
                + shlex.quote(
                    f"test -f {shlex.quote(_settings.remote_translate_script)}; "
                    f"mkdir -p {shlex.quote(_settings.remote_translate_dir)}"
                )
            )
            chk_stdout, chk_stderr, chk_exit = self.ssh_client.execute_command(check_cmd)
            if chk_exit != 0:
                result["steps"].append({"step": "translate_preflight", "status": "failed",
                                        "error": chk_stderr[:500] or f"exit={chk_exit}"})
                return result
            result["steps"].append({"step": "translate_preflight", "status": "success"})

            # ── 检查远端 .md 是否存在 ──
            ls_out, _, ls_rc = self.ssh_client.execute_command(
                f"ls -1 {shlex.quote(_settings.remote_output_dir)}"
            )
            remote_files = set(ls_out.splitlines()) if ls_rc == 0 else set()
            missing = [n for n in md_file_names if n not in remote_files]
            if missing:
                result["steps"].append({"step": "check_md_files", "status": "failed",
                                        "error": f"Remote .md not found: {missing}"})
                return result
            result["steps"].append({"step": "check_md_files", "status": "success"})

            # ── 构建并执行翻译命令 ──
            command = self._build_translate_command(
                md_file_names, model, detect, content_type, prompt_override
            )
            stdout, stderr, exit_code = self.ssh_client.execute_command(command)
            result["command"] = command
            result["stdout"] = stdout
            result["stderr"] = stderr
            pid, log_file = self._parse_launch_info(stdout)

            if exit_code == 0 and pid:
                result["steps"].append({"step": "trigger_translate", "status": "success"})
                result["success"] = True
                result["remote_pid"] = pid
                result["remote_log_file"] = log_file

                _add_job_record({
                    "id": uuid.uuid4().hex[:12],
                    "type": "translate",
                    "submitted_at": datetime.now().isoformat(timespec="seconds"),
                    "md_file_names": md_file_names,
                    "remote_md_dir": _settings.remote_output_dir,
                    "remote_translate_dir": _settings.remote_translate_dir,
                    "model": model,
                    "detect": detect,
                    "content_type": content_type,
                    "prompt_override": prompt_override,
                    "source_job_id": source_job_id,
                    "remote_pid": pid,
                    "remote_log_file": log_file,
                    "status": "running",
                })
            else:
                result["steps"].append({"step": "trigger_translate", "status": "failed",
                                        "error": stderr[:500] or f"exit={exit_code}"})

            return result

        except Exception as e:
            logger.error("Translate submit failed: %s", e, exc_info=True)
            result["steps"].append({"step": "exception", "status": "failed", "error": str(e)})
            return result

    # ── 收集结果 ─────────────────────────────────

    def check_and_collect(self, output_dir: str | None = None) -> list[dict]:
        """SSH 检查所有未完成的远程作业，下载已完成的结果。

        Returns:
            每条 collected job 一个 dict 的列表。
        """
        if output_dir is None:
            # 结果目录固定为项目内 data/remote_pdf（与 Paths.remote_pdf 一致）
            output_dir = str(_ROOT / "data" / "remote_pdf")
        results: list[dict] = []
        jobs = _load_jobs()
        pending = [j for j in jobs if j.get("status") in ("submitted", "running", "failed")]
        if not pending:
            logger.info("No pending remote jobs to check")
            return results

        if not self.ssh_client.connect():
            logger.error("SSH connection failed, cannot check jobs")
            return results

        try:
            for job in pending:
                job_id = job["id"]
                job_type = job.get("type", "convert")
                pid = job.get("remote_pid", "")
                log_file = job.get("remote_log_file", "")
                entry: dict = {"job_id": job_id, "type": job_type, "status": "unknown", "files": []}

                # 1. Check if process is still running
                proc_alive = False
                if pid:
                    out, _, rc = self.ssh_client.execute_command(f"ps -p {pid} >/dev/null 2>&1")
                    proc_alive = rc == 0
                entry["process_alive"] = proc_alive

                if job_type == "translate":
                    self._collect_translate_job(job, entry, output_dir, proc_alive)
                elif job_type == "mineru":
                    self._collect_mineru_job(job, entry, output_dir, proc_alive)
                else:
                    self._collect_convert_job(job, entry, output_dir, proc_alive)

                results.append(entry)

            return results

        finally:
            self.ssh_client.close()

    def _collect_convert_job(self, job: dict, entry: dict,
                              output_dir: str, proc_alive: bool) -> None:
        """收集 convert 类型作业的结果。"""
        job_id = job["id"]
        pid = job.get("remote_pid", "")
        log_file = job.get("remote_log_file", "")
        remote_output_dir = job.get("remote_output_dir", _settings.remote_output_dir)

        # Check for output .md files
        remote_md_files: list[str] = []
        collected_files_this_job = []
        expected_base_names = {
            os.path.basename(f.get("remote", "")).replace(".pdf", ".md")
            for f in job.get("uploaded_files", [])
        }

        ls_out, _, ls_rc = self.ssh_client.execute_command(
            f"ls -1 {shlex.quote(remote_output_dir)}"
        )
        remote_dir_files = set(ls_out.splitlines()) if ls_rc == 0 else set()

        for expected_md in expected_base_names:
            if expected_md in remote_dir_files:
                md_path = f"{remote_output_dir.rstrip('/')}/{expected_md}"
                remote_md_files.append(md_path)

        entry["remote_md_found"] = len(remote_md_files)

        if proc_alive:
            entry["status"] = "running"
            _update_job(job_id, status="running")
        elif remote_md_files:
            entry["status"] = "completed"

            for md_remote in remote_md_files:
                md_name = os.path.basename(md_remote)
                local_dir = os.path.join(output_dir, job_id)
                os.makedirs(local_dir, exist_ok=True)
                local_path = os.path.join(local_dir, md_name)
                if self.ssh_client.download_file(md_remote, local_path):
                    file_info = {"remote": md_remote, "local": local_path}
                    entry["files"].append(file_info)
                    collected_files_this_job.append(file_info)

                    # Also try downloading image tarball
                    tar_remote = md_remote.replace(".md", ".tar")
                    if os.path.basename(tar_remote) in remote_dir_files:
                        tar_local = os.path.join(local_dir, os.path.basename(tar_remote))
                        if self.ssh_client.download_file(tar_remote, tar_local):
                            tar_info = {"remote": tar_remote, "local": tar_local}
                            entry["files"].append(tar_info)
                            collected_files_this_job.append(tar_info)

            if collected_files_this_job:
                _update_job(
                    job_id,
                    status="collected",
                    collected_at=datetime.now().isoformat(timespec="seconds"),
                )

                # Dedup: mark duplicate convert jobs
                collected_names = {
                    os.path.basename(f.get("local", ""))
                    for f in job.get("uploaded_files", [])
                }
                if collected_names:
                    dup_count = 0
                    jobs_all = _load_jobs()
                    for other in jobs_all:
                        if other.get("id") == job_id or other.get("status") != "running":
                            continue
                        if other.get("type") != "convert":
                            continue
                        other_names = {
                            os.path.basename(u.get("local", ""))
                            for u in other.get("uploaded_files", [])
                        }
                        if other_names & collected_names:
                            _update_job(other["id"], status="duplicate")
                            dup_count += 1
                    if dup_count:
                        logger.info("Marked %d duplicate convert job(s) as 'duplicate'", dup_count)
            else:
                _update_job(job_id, status="failed_download")
        else:
            # Process dead, no output files found
            log_tail = ""
            if log_file:
                log_out, _, _ = self.ssh_client.execute_command(
                    f"tail -5 {shlex.quote(log_file)} 2>/dev/null || true"
                )
                log_tail = log_out.strip()
            entry["status"] = "failed"
            entry["log_tail"] = log_tail
            _update_job(job_id, status="failed")

    def _collect_mineru_job(self, job: dict, entry: dict,
                            output_dir: str, proc_alive: bool) -> None:
        """收集 mineru 类型作业的结果。

        mineru 产物: {output}/{stem}.md(原样) + {output}/{stem}.tar(仅 images/)。
        收集时 md 平铺到 {output_dir}/{job_id}/ 顶层, tar 图片包作兄弟文件, 不解包。
        """
        job_id = job["id"]
        log_file = job.get("remote_log_file", "")
        remote_output_dir = job.get("remote_output_dir", _settings.remote_mineru_output_dir)

        # 期望产物: 每个上传文件 stem → {stem}.md + {stem}.tar
        expected_stems = {
            os.path.splitext(os.path.basename(f.get("remote", "")))[0]
            for f in job.get("uploaded_files", [])
        }
        expected_mds = {s + ".md" for s in expected_stems}

        ls_out, _, ls_rc = self.ssh_client.execute_command(
            f"ls -1 {shlex.quote(remote_output_dir)} 2>/dev/null || true"
        )
        remote_files = set(ls_out.splitlines()) if ls_rc == 0 else set()

        found_mds = [m for m in expected_mds if m in remote_files]
        entry["remote_md_found"] = len(found_mds)

        if proc_alive:
            entry["status"] = "running"
            _update_job(job_id, status="running")
        elif found_mds:
            entry["status"] = "completed"
            local_dir = os.path.join(output_dir, job_id)
            os.makedirs(local_dir, exist_ok=True)

            for md_name in found_mds:
                md_remote = f"{remote_output_dir.rstrip('/')}/{md_name}"
                md_local = os.path.join(local_dir, md_name)
                if self.ssh_client.download_file(md_remote, md_local):
                    entry["files"].append({"remote": md_remote, "local": md_local, "type": "original"})
                # 图片 tar(可选: 无图时可能不存在)
                tar_name = md_name[:-len(".md")] + ".tar"
                if tar_name in remote_files:
                    tar_remote = f"{remote_output_dir.rstrip('/')}/{tar_name}"
                    tar_local = os.path.join(local_dir, tar_name)
                    if self.ssh_client.download_file(tar_remote, tar_local):
                        entry["files"].append({"remote": tar_remote, "local": tar_local, "type": "images"})

            if entry["files"]:
                _update_job(job_id, status="collected",
                            collected_at=datetime.now().isoformat(timespec="seconds"))
            else:
                _update_job(job_id, status="failed_download")
        else:
            # Process dead but no md found
            log_tail = ""
            if log_file:
                log_out, _, _ = self.ssh_client.execute_command(
                    f"tail -10 {shlex.quote(log_file)} 2>/dev/null || true"
                )
                log_tail = log_out.strip()
            entry["status"] = "failed"
            entry["log_tail"] = log_tail
            _update_job(job_id, status="failed")

    def _collect_translate_job(self, job: dict, entry: dict,
                                output_dir: str, proc_alive: bool) -> None:
        """收集 translate 类型作业的结果。"""
        job_id = job["id"]
        pid = job.get("remote_pid", "")
        log_file = job.get("remote_log_file", "")
        remote_translate_dir = job.get("remote_translate_dir", _settings.remote_translate_dir)
        remote_md_dir = job.get("remote_md_dir", _settings.remote_output_dir)
        md_file_names = job.get("md_file_names", [])

        # Expected cn_ files in translate dir
        cn_expected = {f"cn_{name}" for name in md_file_names}

        ls_cn_out, _, ls_cn_rc = self.ssh_client.execute_command(
            f"ls -1 {shlex.quote(remote_translate_dir)} 2>/dev/null || true"
        )
        remote_cn_files = set(ls_cn_out.splitlines()) if ls_cn_rc == 0 else set()

        found_cn = [f for f in cn_expected if f in remote_cn_files]
        entry["remote_cn_found"] = len(found_cn)

        if proc_alive:
            entry["status"] = "running"
            _update_job(job_id, status="running")
        elif found_cn:
            entry["status"] = "completed"
            collected = []

            for cn_name in found_cn:
                cn_remote = f"{remote_translate_dir.rstrip('/')}/{cn_name}"
                local_dir = os.path.join(output_dir, "translate", job_id)
                os.makedirs(local_dir, exist_ok=True)
                cn_local = os.path.join(local_dir, cn_name)
                if self.ssh_client.download_file(cn_remote, cn_local):
                    info = {"remote": cn_remote, "local": cn_local, "type": "translation"}
                    entry["files"].append(info)
                    collected.append(info)

                # Also download the original .md (翻译和没翻译的一起拉回)
                orig_name = cn_name[len("cn_"):]
                orig_remote = f"{remote_md_dir.rstrip('/')}/{orig_name}"
                orig_local = os.path.join(local_dir, orig_name)
                if self.ssh_client.download_file(orig_remote, orig_local):
                    info = {"remote": orig_remote, "local": orig_local, "type": "original"}
                    entry["files"].append(info)
                    collected.append(info)

            if collected:
                _update_job(
                    job_id,
                    status="collected",
                    collected_at=datetime.now().isoformat(timespec="seconds"),
                )

                # Mark source convert job as translate_done
                source_id = job.get("source_job_id")
                if source_id:
                    _update_job(source_id, translate_done=True)
            else:
                _update_job(job_id, status="failed_download")
        else:
            # Process dead but no cn_ files found
            log_tail = ""
            if log_file:
                log_out, _, _ = self.ssh_client.execute_command(
                    f"tail -10 {shlex.quote(log_file)} 2>/dev/null || true"
                )
                log_tail = log_out.strip()
            entry["status"] = "failed"
            entry["log_tail"] = log_tail
            _update_job(job_id, status="failed")

    def close(self) -> None:
        self.ssh_client.close()
