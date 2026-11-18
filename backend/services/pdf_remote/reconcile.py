"""reconcile — 旧账清理：盘点 data/pdfs + remote_jobs + remote_pdf。

对已 collected 的 job：
  1. 若源文件仍在原位置（inbox / 外部路径）→ 搬进 data/remote_pdf/{job_id}/ 并更新记录 local；
  2. 若 job 目录缺失（孤儿记录）→ 从 remote_jobs.json 删除。
运行前自动备份 remote_jobs.json。仅搬移（不删除源文件），全程可恢复。
"""

import json
import os
import shutil
from datetime import datetime
from pathlib import Path

from backend.path_setup import PROJECT_ROOT as _ROOT
from backend.services.pdf_remote.parser import _load_jobs, _save_jobs, _to_rel_path

_JOBS_FILE = _ROOT / ".data" / "remote_jobs.json"
_REMOTE_PDF = _ROOT / "data" / "remote_pdf"


def _backup_jobs_file() -> str | None:
    if not _JOBS_FILE.exists():
        return None
    bak = _JOBS_FILE.with_name(f"remote_jobs.json.bak-{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    bak.write_text(_JOBS_FILE.read_text(encoding="utf-8"), encoding="utf-8")
    return str(bak)


def run_reconcile() -> dict:
    """执行一次盘点整理，返回报告。"""
    jobs = _load_jobs()
    report: dict = {
        "backup": None,
        "moved_sources": [],
        "deleted_orphans": [],
        "kept": 0,
        "changed": False,
    }
    report["backup"] = _backup_jobs_file()

    new_jobs = []
    for job in jobs:
        job_id = job.get("id", "")
        job_dir = (_REMOTE_PDF / job_id) if job_id else None
        status = job.get("status")

        if status != "collected":
            # running / submitted / failed 暂不动
            new_jobs.append(job)
            continue

        if not job_dir or not job_dir.exists():
            # collected 但本地 job 目录缺失 → 孤儿记录，删除
            names = [os.path.basename(u.get("local", "")) for u in job.get("uploaded_files", [])]
            report["deleted_orphans"].append({"id": job_id, "files": names})
            report["changed"] = True
            continue

        # collected 且有 job 目录：把仍在原位置的源文件搬进 job 目录
        uploaded = job.get("uploaded_files", [])
        moved_any = False
        for u in uploaded:
            orig = u.get("local", "")
            if not orig:
                continue
            orig_abs = (_ROOT / orig) if not os.path.isabs(orig) else Path(orig)
            if str(orig_abs).startswith(str(job_dir.resolve())):
                continue  # 已在 job 目录内
            if orig_abs.exists():
                dest = job_dir / os.path.basename(str(orig_abs))
                try:
                    # 只读源文件跨盘 move 会"复制成功、删原失败" → 先清只读
                    try:
                        os.chmod(str(orig_abs), 0o666)
                    except Exception:
                        pass
                    shutil.move(str(orig_abs), str(dest))
                    u["local"] = _to_rel_path(str(dest))
                    moved_any = True
                    report["moved_sources"].append({
                        "job_id": job_id, "from": str(orig_abs), "to": str(dest),
                    })
                except Exception as e:
                    report["moved_sources"].append({
                        "job_id": job_id, "from": str(orig_abs), "error": str(e),
                    })
        if moved_any:
            report["changed"] = True
        new_jobs.append(job)

    if report["changed"]:
        _save_jobs(new_jobs)
    report["kept"] = len(new_jobs)
    return report
