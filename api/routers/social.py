"""社交平台凭证管理 API 路由。"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from api.services.social_service import (
    get_bilibili_status,
    test_bilibili_cookie,
    update_bilibili_fields,
    clear_bilibili_cookie,
)

router = APIRouter(prefix="/api/settings/social", tags=["social"])


class BiliUpdateRequest(BaseModel):
    sessdata: str = ""
    bili_jct: str = ""
    dedeuserid: str = ""


# ── B站 ──


@router.get("/bilibili")
async def bilibili_status():
    """获取 B站 Cookie 状态。"""
    return get_bilibili_status()


@router.post("/bilibili/test")
async def bilibili_test():
    """测试当前 B站 Cookie 是否有效。"""
    return test_bilibili_cookie()


@router.put("/bilibili")
async def bilibili_update(req: BiliUpdateRequest):
    """更新 B站 Cookie 字段。"""
    fields = {}
    if req.sessdata:
        fields["bili_cookie_sessdata"] = req.sessdata
    if req.bili_jct:
        fields["bili_cookie_bili_jct"] = req.bili_jct
    if req.dedeuserid:
        fields["bili_cookie_dedeuserid"] = req.dedeuserid
    return update_bilibili_fields(fields)


@router.delete("/bilibili")
async def bilibili_clear():
    """清除 B站 Cookie。"""
    return clear_bilibili_cookie()


# ── 论文平台（知网/万方/GS/arXiv/OpenAlex/Crossref）──


@router.get("/papers/status")
async def papers_status():
    """论文平台登录态/机构网络状态（快速：读 cookie 文件 + 端口探测，不发真实搜索）。

    返回最近一次「重新检测」的探测结果（缓存 5 分钟）。
    """
    from api.services import paper_auth_service as pas

    return pas.get_status()


@router.post("/papers/test")
async def papers_test(force: bool = False):
    """对全部论文平台做真实连通性/登录态探测（并行；知网/万方各发一次搜索、GS 走代理 GET）。

    force=1 强制重测（8 秒内连点自动回退缓存）；默认命中 5 分钟缓存。
    """
    from api.services import paper_auth_service as pas

    return pas.test_all(force=force)
