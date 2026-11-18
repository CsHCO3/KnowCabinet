"""Bilibili 服务 — 字幕提取 & 收藏夹导出 & 合集/作者收集。"""

from typing import Any

from backend.services.bilibili.subtitle import get_bilibili_timestamped_subtitle
from backend.services.bilibili.favorites import (
    get_current_user,
    list_folders,
    get_videos,
    export_csv,
    load_videos_from_csv,
)
from backend.services.bilibili.collect import (
    get_video_collection,
    get_author_info,
    get_author_stat,
    get_author_videos,
    get_all_author_videos,
    resolve_space_url,
    extract_bvid,
    extract_mid,
    get_comments,
    get_video_view,
)


class BilibiliService:
    def get_subtitle(self, url: str) -> dict[str, Any]:
        return get_bilibili_timestamped_subtitle(url)

    # ── 收藏夹相关 ──

    def get_current_user(self) -> dict[str, Any]:
        """获取当前登录用户信息。"""
        return get_current_user()

    def list_folders(self) -> list[dict[str, Any]]:
        """列出所有收藏夹。"""
        return list_folders()

    def get_fav_videos(self, fav_id: int, max_items: int = 0) -> list[dict[str, Any]]:
        """获取收藏夹视频列表。"""
        return get_videos(fav_id, max_items=max_items)

    def export_fav_csv(self, fav_id: int, output_path: str, max_items: int = 0) -> str:
        """导出收藏夹到 CSV。"""
        return export_csv(fav_id, output_path, max_items=max_items)

    def load_csv(self, csv_path: str) -> list[dict[str, Any]]:
        """从 CSV 加载视频列表。"""
        return load_videos_from_csv(csv_path)

    # ── 合集关联 ──

    def get_collection(self, url_or_bvid: str) -> dict[str, Any]:
        """查询视频所属合集及合集中所有视频。"""
        return get_video_collection(url_or_bvid)

    # ── 作者空间 ──

    def get_author(self, mid: int) -> dict[str, Any]:
        """获取作者基本信息和统计。"""
        info = get_author_info(mid)
        stat = get_author_stat(mid)
        return {"info": info, "stat": stat}

    def get_author_videos(
        self, mid: int, order: str = "pubdate", page: int = 1, page_size: int = 30
    ) -> dict[str, Any]:
        """获取作者视频列表（需要 WBI 签名）。"""
        return get_author_videos(mid, order=order, page=page, page_size=page_size)

    def get_all_author_videos(self, mid: int, order: str = "pubdate", max_pages: int = 10) -> list[dict[str, Any]]:
        """遍历分页获取作者全部视频。"""
        return get_all_author_videos(mid, order=order, max_pages=max_pages)

    def resolve_space(self, space_url: str) -> dict[str, Any]:
        """从 space.bilibili.com/xxx 一次性获取作者信息和视频列表。"""
        return resolve_space_url(space_url)

    # ── 评论 ──

    def get_video_view(self, bvid: str) -> dict[str, Any] | None:
        """查询视频 view API 完整数据。"""
        return get_video_view(bvid)

    def get_comments(self, aid: int, page: int = 1, page_size: int = 20, sort: int = 2) -> dict[str, Any]:
        """获取视频评论。"""
        return get_comments(aid, page=page, page_size=page_size, sort=sort)
