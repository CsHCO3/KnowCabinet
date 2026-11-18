"""B站 API 路由 — 采集/提取/问答/管理。"""

from __future__ import annotations

from fastapi import APIRouter, Request, Header, Form, Query
from fastapi.responses import HTMLResponse, JSONResponse

from api.services.bilibili_service import BilibiliService
from api.services.bili_task_manager import BiliTaskManager
from api.services import bili_db
from api.schemas.bilibili import (
    APIResponse,
    CommitRequest,
    ExtractSubmitRequest,
    QueryRunRequest,
    BatchDeleteRequest,
)

router = APIRouter(prefix="/api/bilibili", tags=["bilibili"])
service = BilibiliService()


def _render(request: Request, name: str, **extra) -> HTMLResponse:
    """渲染模板，注入 request。"""
    templates = request.app.state.templates
    return templates.TemplateResponse(request, name, {"request": request, **extra})


# ═══════════════════════════════════════════════════════
# Page 1: 采集
# ═══════════════════════════════════════════════════════


@router.post("/collect/check-url")
async def collect_check_url(url: str = Form(...)):
    """检查视频 URL，返回视频信息 + 合集信息。"""
    result = service.check_url(url.strip())
    return result


@router.post("/collect/search-author")
async def collect_search_author(
    mid: str = Form(...),
    count: int = Form(30),
):
    """搜索作者视频（支持 MID 数字或完整 space URL）。"""
    return service.search_author(mid, count)


@router.get("/collect/favorites")
async def collect_favorites(request: Request):
    """获取收藏夹列表。"""
    try:
        folders = service.list_favorites()
        return folders
    except RuntimeError as e:
        return APIResponse(success=False, error=str(e))


@router.post("/collect/favorites/{fav_id}/preview")
async def collect_favorites_preview(
    fav_id: int,
    count: int = Form(0),
):
    """获取收藏夹内视频预览。"""
    return service.get_favorites_preview(fav_id, count)


@router.post("/collect/commit")
async def collect_commit(req: CommitRequest):
    """提交视频到数据库（去重）。"""
    result = service.commit_videos(req.videos)
    return APIResponse(
        success=True,
        data=result,
        message=f"成功入库 {result['inserted']} 条，跳过 {result['skipped']} 条",
    )


@router.post("/collect/enrich-batch")
async def collect_enrich_batch(bvids: list[str]):
    """批量获取视频完整信息（用于收藏夹队列补全）。"""
    from api.services.bilibili_service import BilibiliService as BS
    import time as _t
    results = []
    for bvid in bvids:
        try:
            from backend.services.bilibili.collect import get_video_view as _gvv
            data = _gvv(bvid)
            if data:
                owner = data.get("owner", {}) or {}
                stat = data.get("stat", {}) or {}
                results.append({
                    "bvid": bvid,
                    "title": data.get("title", ""),
                    "author": owner.get("name", ""),
                    "author_mid": owner.get("mid", 0),
                    "duration": data.get("duration", 0),
                    "pubdate": data.get("pubdate", 0),
                    "pic": data.get("pic", ""),
                    "view": stat.get("view", 0),
                })
            else:
                results.append({"bvid": bvid})
        except Exception:
            results.append({"bvid": bvid})
        _t.sleep(0.3)
    return {"videos": results}


@router.post("/videos/enrich-existing")
async def enrich_existing_videos(bvids: list[str]):
    """对已入库的视频批量补全信息。"""
    return service.enrich_videos_batch(bvids)


@router.post("/videos/reset-stuck")
async def reset_stuck_videos():
    """将卡在 extracting 状态的视频重置为 none。"""
    stuck = bili_db.get_videos(subtitle_status="extracting")
    count = 0
    for v in stuck:
        bili_db.update_video(v["id"], {"subtitle_status": "none", "summary_status": "none"})
        count += 1
    return {"reset": count, "message": f"已重置 {count} 个卡住的视频"}


# ═══════════════════════════════════════════════════════
# Page 2: 提取
# ═══════════════════════════════════════════════════════


@router.get("/extract/list")
async def extract_list(
    request: Request,
    hx_request: bool = Header(False),
    keyword: str = Query(""),
    author: str = Query(""),
    view_min: str = Query("0"),
    view_max: str = Query("0"),
):
    """列出未提取的视频。"""
    try:
        vmin = int(view_min) if view_min.strip() else 0
    except (ValueError, TypeError):
        vmin = 0
    try:
        vmax = int(view_max) if view_max.strip() else 0
    except (ValueError, TypeError):
        vmax = 0

    videos = service.get_videos(
        subtitle_status="none",
        keyword=keyword,
        author=author,
        view_min=vmin,
        view_max=vmax,
        order_by="created_at",
        order_dir="DESC",
    )
    total = service.count_videos(subtitle_status="none")

    if hx_request:
        return _render(request, "bilibili/partials/extract_table.html",
                       videos=videos, total=total,
                       keyword=keyword, author=author)

    return {
        "videos": videos,
        "total": total,
        "filters": {"keyword": keyword, "author": author, "view_min": vmin, "view_max": vmax},
    }


@router.post("/extract/submit")
async def extract_submit(req: ExtractSubmitRequest):
    """提交批量提取+总结任务。"""
    if not req.bvids:
        return APIResponse(success=False, error="请选择至少一个视频")
    task_id = service.extract_and_summarize(req.bvids)
    return APIResponse(success=True, data={"task_id": task_id}, message="提取任务已提交")


@router.get("/extract/task/{task_id}/status")
async def extract_task_status(task_id: str):
    """查询提取任务状态。"""
    status = service.get_extract_status(task_id)
    if not status:
        return APIResponse(success=False, error="任务不存在")
    return APIResponse(success=True, data=status)


@router.get("/extract/task/{task_id}/content")
async def extract_task_content(task_id: str):
    """获取任务中已提取视频的总结内容。"""
    status = service.get_extract_status(task_id)
    if not status:
        return APIResponse(success=False, error="任务不存在")

    result = status.get("result") or {}
    results_list = result.get("results", [])

    # 捞出状态为 summarized 的 bvid
    items = []
    for r in results_list:
        if r.get("status") != "summarized":
            continue
        bvid = r.get("bvid", "")
        if not bvid:
            continue
        video = bili_db.get_video_by_bvid(bvid)
        if video and video.get("summary_text"):
            items.append({
                "bvid": bvid,
                "title": video.get("title", ""),
                "author": video.get("author", ""),
                "summary_text": video.get("summary_text", ""),
            })

    return APIResponse(success=True, data={"task_id": task_id, "videos": items})


@router.get("/extract/tasks")
async def extract_tasks(request: Request, hx_request: bool = Header(False)):
    """返回所有提取任务的统一列表（不分 running/recent），供任务面板轮询。"""
    all_tasks = BiliTaskManager.list_tasks(task_type="extract")
    # 统一列表：pending+running 在前，completed/failed/cancelled 在后，各段按时间倒序
    active = [t for t in all_tasks if t["status"] in ("pending", "running")]
    done = [t for t in all_tasks if t["status"] in ("completed", "failed", "cancelled")]
    tasks = active + done[:10]  # 最近10个已完成

    if hx_request:
        return _render(request, "bilibili/partials/extract_tasks.html", tasks=tasks)
    return {"tasks": tasks}


@router.post("/extract/task/{task_id}/cancel")
async def extract_task_cancel(task_id: str):
    """取消提取任务。"""
    ok = service.cancel_extract(task_id)
    if ok:
        return APIResponse(success=True, message="任务已取消")
    return APIResponse(success=False, error="无法取消（任务不存在或已完成）")


# ═══════════════════════════════════════════════════════
# Page 3: 问答
# ═══════════════════════════════════════════════════════


@router.get("/videos")
async def videos_list(
    request: Request,
    hx_request: bool = Header(False),
    keyword: str = Query(""),
    author: str = Query(""),
    subtitle_status: str = Query(""),
    summary_status: str = Query(""),
    limit: int = Query(200),
):
    """视频管理列表（全状态，可筛选）。"""
    videos = service.get_videos(
        keyword=keyword, author=author,
        subtitle_status=subtitle_status or None,
        summary_status=summary_status or None,
        limit=limit,
    )
    if hx_request:
        return _render(request, "bilibili/partials/videos_table.html",
                       videos=videos, total=len(videos))
    return {"videos": videos, "total": len(videos)}


@router.get("/videos/overview")
def videos_overview():
    """视频状态概览（字幕/摘要状态计数）。"""
    return {
        "total": service.count_videos(),
        "subtitle": {s: service.count_videos(subtitle_status=s)
                     for s in ("none", "extracting", "extracted", "failed")},
        "summary": {s: service.count_videos(summary_status=s)
                    for s in ("none", "summarized", "failed")},
    }


@router.post("/videos/extract")
async def videos_extract(req: BatchDeleteRequest):
    """对选中的视频执行 提取字幕 + 生成摘要（后台任务），完成后摘要晋升 dense/video。"""
    ids = req.video_ids
    if not ids:
        return APIResponse(success=False, error="请选择至少一个视频")
    bvids = [b["bvid"] for b in service.get_videos(limit=500) if b["id"] in ids]
    if not bvids:
        return APIResponse(success=False, error="选中的视频不存在")
    task_id = service.extract_and_summarize(bvids)
    return APIResponse(success=True, data={"task_id": task_id}, message="提取任务已提交")


@router.post("/videos/batch-delete")
def videos_batch_delete(req: BatchDeleteRequest):
    """批量删除视频（含字幕文件）。"""
    if not req.video_ids:
        return APIResponse(success=False, error="请选择至少一个视频")
    res = service.batch_delete_video(req.video_ids, cleanup_files=True)
    return APIResponse(success=True, data=res, message=f"删除 {len(req.video_ids)} 个视频")


@router.get("/videos/extract/status/{task_id}")
def videos_extract_status(task_id: str):
    st = service.get_extract_status(task_id)
    if not st:
        return APIResponse(success=False, error="任务不存在")
    return APIResponse(success=True, data=st)


@router.get("/query/videos")
async def query_videos(
    request: Request,
    hx_request: bool = Header(False),
    keyword: str = Query(""),
    author: str = Query(""),
    view_min: str = Query("0"),
    view_max: str = Query("0"),
):
    """列出已提取的视频（供问答选择）。"""
    try:
        vmin = int(view_min) if view_min.strip() else 0
    except (ValueError, TypeError):
        vmin = 0
    try:
        vmax = int(view_max) if view_max.strip() else 0
    except (ValueError, TypeError):
        vmax = 0

    videos = service.get_videos(
        subtitle_status="extracted",
        keyword=keyword,
        author=author,
        view_min=vmin,
        view_max=vmax,
    )
    has_summary = service.count_videos(subtitle_status="extracted", summary_status="summarized")

    if hx_request:
        return _render(request, "bilibili/partials/query_video_table.html",
                       videos=videos, total=len(videos), has_summary=has_summary)

    return {"videos": videos, "total": len(videos), "has_summary": has_summary}


@router.post("/query/run")
async def query_run(req: QueryRunRequest):
    """创建并运行问答任务。"""
    if not req.video_ids:
        return APIResponse(success=False, error="请选择至少一个视频")
    if not req.prompt.strip():
        return APIResponse(success=False, error="请输入问题")

    task_id = service.run_query(
        video_ids=req.video_ids,
        prompt=req.prompt,
        prompt_file_path=req.prompt_file_path,
        filter_mode=req.filter_mode,
        keyword=req.keyword,
    )
    return APIResponse(success=True, data={"task_id": task_id}, message="问答任务已提交")


@router.get("/query/tasks")
async def query_tasks_list():
    """列出历史问答任务。"""
    tasks = service.list_query_tasks()
    return {"tasks": tasks}


@router.get("/query/tasks/{task_id}")
async def query_task_detail(task_id: int):
    """获取单个问答任务详情。"""
    task = service.get_query_task(task_id)
    if not task:
        return APIResponse(success=False, error="任务不存在")

    # 尝试解包 result_text（JSON 格式：{text, used_videos}）
    raw = task.get("result_text", "")
    if raw and raw.startswith("{"):
        try:
            import json
            parsed = json.loads(raw)
            task["result_text"] = parsed.get("text", raw)
            task["used_videos"] = parsed.get("used_videos", [])
        except (json.JSONDecodeError, TypeError):
            pass  # 保留原始文本

    return APIResponse(success=True, data=task)


@router.delete("/query/tasks/{task_id}")
async def delete_query_task(task_id: int):
    """删除问答任务。"""
    ok = service.delete_query_task(task_id)
    if ok:
        return APIResponse(success=True, message="已删除")
    return APIResponse(success=False, error="任务不存在")


# ═══════════════════════════════════════════════════════
# 通用管理
# ═══════════════════════════════════════════════════════


@router.delete("/videos/{video_id}")
async def delete_video(video_id: int, cleanup_files: bool = Query(False)):
    """删除视频记录，可选清理关联文件。"""
    result = service.delete_video(video_id, cleanup_files=cleanup_files)
    if not result["deleted"]:
        return APIResponse(success=False, error="视频不存在")
    return APIResponse(success=True, data=result, message="已删除")


@router.post("/videos/batch-delete")
async def batch_delete_video(req: BatchDeleteRequest):
    """批量删除视频记录。"""
    if not req.video_ids:
        return APIResponse(success=False, error="请选择至少一个视频")
    result = service.batch_delete_video(req.video_ids, cleanup_files=req.cleanup_files)
    return APIResponse(
        success=True,
        data=result,
        message=f"已删除 {result['deleted']} 条",
    )


@router.get("/videos")
async def list_videos(
    request: Request,
    hx_request: bool = Header(False),
    keyword: str = Query(""),
    author: str = Query(""),
    source_type: str = Query(""),
    view_min: str = Query("0"),
    view_max: str = Query("0"),
    subtitle_status: str = Query(""),
    summary_status: str = Query(""),
    limit: int = Query(200),
    offset: int = Query(0),
):
    """全量视频列表（带组合筛选）。"""
    # 空字符串转为 None 以表示不筛选
    ss = subtitle_status if subtitle_status else None
    ms = summary_status if summary_status else None

    try:
        vmin = int(view_min) if view_min.strip() else 0
    except (ValueError, TypeError):
        vmin = 0
    try:
        vmax = int(view_max) if view_max.strip() else 0
    except (ValueError, TypeError):
        vmax = 0

    videos = service.get_videos(
        subtitle_status=ss,
        summary_status=ms,
        keyword=keyword,
        author=author,
        source_type=source_type,
        view_min=vmin,
        view_max=vmax,
        limit=limit,
        offset=offset,
    )
    total = service.count_videos(
        subtitle_status=ss,
        summary_status=ms,
        keyword=keyword,
        author=author,
        source_type=source_type,
    )

    return {
        "videos": videos,
        "total": total,
        "filters": {
            "keyword": keyword, "author": author, "source_type": source_type,
            "view_min": view_min, "view_max": view_max,
            "subtitle_status": subtitle_status, "summary_status": summary_status,
        },
    }
