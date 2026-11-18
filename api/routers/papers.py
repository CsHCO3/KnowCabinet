"""多平台论文检索 API — /papers 页：搜索任务历史 + 触发搜索 + 删除。"""

import json
import re
import subprocess
import threading
import time
from pathlib import Path

from fastapi import APIRouter, Form

from api.schemas.common import APIResponse

router = APIRouter(prefix="/api/papers", tags=["papers"])

_ROOT = Path(__file__).resolve().parent.parent.parent
_SEARCHES_DIR = _ROOT / "data" / "paper_searches"


def _make_search_id(query: str) -> str:
    ts = time.strftime("%Y%m%d_%H%M%S")
    return f"{ts}_{re.sub(r'[^\w]+', '_', query)[:30].strip('_')}"


def _read_index() -> list:
    idx_path = _SEARCHES_DIR / "_index.json"
    if not idx_path.exists():
        return []
    try:
        return json.loads(idx_path.read_text(encoding="utf-8"))
    except Exception:
        return []


def _write_index(items: list) -> None:
    _SEARCHES_DIR.mkdir(parents=True, exist_ok=True)
    (_SEARCHES_DIR / "_index.json").write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def _upsert_index(entry: dict) -> None:
    idx = [e for e in _read_index() if e.get("id") != entry.get("id")]
    idx.append(entry)
    idx.sort(key=lambda e: e.get("created_at", ""), reverse=True)
    _write_index(idx)


def _create_placeholder(search_id: str, query: str) -> None:
    _SEARCHES_DIR.mkdir(parents=True, exist_ok=True)
    created_at = time.strftime("%Y-%m-%d %H:%M:%S")
    record = {
        "id": search_id, "query": query, "created_at": created_at,
        "status": "running", "stage": "searching",
        "per_source": {}, "total": 0, "stats": {},
        "results": [], "downloaded": [], "download_attempts": [],
        "error": "",
    }
    (_SEARCHES_DIR / f"{search_id}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    _upsert_index({"id": search_id, "query": query, "created_at": created_at,
                   "per_source": {}, "total": 0, "downloaded": 0,
                   "status": "running", "stage": "searching"})


@router.get("/searches")
def papers_searches():
    """搜索任务历史（列表）。"""
    if not _SEARCHES_DIR.exists():
        return {"searches": []}
    idx = _read_index()
    if idx:
        return {"searches": idx}
    # 无索引 → 扫描目录
    items = []
    for f in sorted(_SEARCHES_DIR.glob("*.json")):
        if f.name == "_index.json":
            continue
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
            items.append({"id": r.get("id"), "query": r.get("query"),
                          "created_at": r.get("created_at"),
                          "per_source": r.get("per_source", {}),
                          "total": r.get("total", 0),
                          "downloaded": len(r.get("downloaded", [])),
                          "status": r.get("status", ""), "stage": r.get("stage", "")})
        except Exception:
            continue
    return {"searches": sorted(items, key=lambda x: x.get("created_at", ""), reverse=True)}


@router.get("/searches/{search_id}")
def papers_search_detail(search_id: str):
    """单次搜索任务详情（每平台命中/结果打分/下载）。"""
    f = _SEARCHES_DIR / f"{search_id}.json"
    if not f.exists():
        return APIResponse(success=False, error="搜索任务不存在")
    try:
        return APIResponse(success=True, data=json.loads(f.read_text(encoding="utf-8")))
    except Exception as e:
        return APIResponse(success=False, error=f"读取失败: {e}")


@router.delete("/searches/{search_id}")
def papers_search_delete(search_id: str):
    """删除搜索记录（只删记录；已下载 PDF 保留在 data/pdfs）。"""
    f = _SEARCHES_DIR / f"{search_id}.json"
    if f.exists():
        try:
            f.unlink()
        except Exception as e:
            return APIResponse(success=False, error=f"删除记录失败: {e}")
    idx = [e for e in _read_index() if e.get("id") != search_id]
    _write_index(idx)
    return APIResponse(success=True, message="已删除搜索记录")


@router.post("/search")
def papers_search_trigger(
    query: str = Form(...),
    rows: int = Form(15),
    no_gs: bool = Form(False),
    max_download: int = Form(0),
    inst_download: int = Form(0),
    title: str = Form(""),
    author: str = Form(""),
    year_from: int = Form(0),
    year_to: int = Form(0),
    category: str = Form(""),
    only_sources: str = Form(""),
    exclude_sources: str = Form(""),
):
    """触发一次多平台搜索（后台跑 CLI paper-search，写 data/paper_searches/）。

    提交即创建 running 占位记录，前端可立即看到任务名和阶段。
    only_sources/exclude_sources：逗号分隔的源白名单/排除（crossref/openalex/arxiv/
    google_scholar/wanfang/cnki/eprint），空则不限制。
    """
    if not query.strip():
        return APIResponse(success=False, error="关键词不能为空")
    search_id = _make_search_id(query.strip())
    _create_placeholder(search_id, query.strip())
    cmd = ["python", "-m", "backend.main", "paper-search", query,
           "--rows", str(max(1, min(50, rows))), "--max-download", str(max(0, max_download)),
           "--search-id", search_id]
    if no_gs:
        cmd.append("--no-gs")
    if inst_download:
        cmd += ["--inst-download", str(max(0, inst_download))]
    if title:
        cmd += ["--title", title]
    if author:
        cmd += ["--author", author]
    if year_from:
        cmd += ["--year-from", str(year_from)]
    if year_to:
        cmd += ["--year-to", str(year_to)]
    if category:
        cmd += ["--category", category]
    if only_sources:
        cmd += ["--only-sources", only_sources]
    if exclude_sources:
        cmd += ["--exclude-sources", exclude_sources]

    log_path = _SEARCHES_DIR / f"{search_id}.log"

    def _run():
        try:
            with open(log_path, "w", encoding="utf-8") as log_fh:
                subprocess.run(cmd, cwd=str(_ROOT), stdout=log_fh, stderr=subprocess.STDOUT,
                               text=True, encoding="utf-8")
        except Exception as e:
            try:
                record = json.loads((_SEARCHES_DIR / f"{search_id}.json").read_text(encoding="utf-8"))
                record["status"] = "failed"
                record["stage"] = "failed"
                record["error"] = str(e)[:500]
                (_SEARCHES_DIR / f"{search_id}.json").write_text(
                    json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
                _upsert_index({"id": search_id, "query": query, "created_at": record["created_at"],
                               "per_source": record.get("per_source", {}),
                               "total": record.get("total", 0),
                               "downloaded": len(record.get("downloaded", [])),
                               "status": "failed", "stage": "failed"})
            except Exception:
                pass

    threading.Thread(target=_run, daemon=True).start()
    return APIResponse(success=True, data={"search_id": search_id},
                       message="搜索任务已启动（后台运行，页面每 5 秒自动刷新进度）")
