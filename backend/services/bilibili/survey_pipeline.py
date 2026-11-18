"""B站视频调研三阶段管道。

模块化解耦设计，每步产出独立 JSON 文件，支持手工编辑干预。

Stage 1 — 作品-作者级信息收集:  data/bili_servey/videos/
Stage 2 — 简介收集 + 评分:       data/bili_servey/servey/
Stage 3 — 字幕提取:              data/bili_subtitles_out/
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.services.bilibili.collect import (
    get_video_collection as _get_video_collection,
    get_author_info as _get_author_info,
    get_all_author_videos as _get_all_author_videos,
    get_video_view as _get_video_view,
    extract_bvid as _extract_bvid,
    extract_mid as _extract_mid,
)
from backend.services.bilibili.favorites import (
    get_videos as _get_fav_videos,
    export_csv as _export_fav_csv,
)
from backend.services.bilibili.subtitle import get_bilibili_subtitle_with_retry as _get_subtitle
from backend.services.bilibili.screener import (
    save_videos_to_json as _save_videos_to_json,
    load_videos_from_json as _load_videos_from_json,
    screen_videos as _screen_videos,
    filter_by_action as _filter_by_action,
)
from backend.core.llm.client import LLMClient

# ── 基础路径 ────────────────────────────────────

DEFAULT_VIDEOS_DIR = Path("data/bili_servey/videos")
DEFAULT_SERVEY_DIR = Path("data/bili_servey/servey")
DEFAULT_KNOWLEDGEBASE_DIR = Path("data/bili_subtitles_out")
PROMPTS_DIR = Path("backend/pipelines/prompts/bili")
DEFAULT_SYSTEM_PROMPT = PROMPTS_DIR / "video_scorer_system.txt"


# ═══════════════════════════════════════════════
# 辅助：确保目录存在
# ═══════════════════════════════════════════════

def _ensure_dir(p: str | Path) -> Path:
    p = Path(p)
    p.mkdir(parents=True, exist_ok=True)
    return p


# ═══════════════════════════════════════════════
# 标准化工具 — 将各种来源格式统一为标准 schema
# ═══════════════════════════════════════════════

STAGE1_SCHEMA_FIELDS = [
    "bvid", "aid", "cid", "title", "description", "duration",
    "pubdate", "pic", "owner_name", "owner_mid",
    "view", "like", "fav", "coin", "share", "danmaku", "reply",
    "stage1_source", "stage1_source_detail",
    "enriched",
]


def _normalize_record(
    raw: dict,
    source: str,
    source_detail: str = "",
) -> dict:
    """将任意来源的视频 dict 转换为标准化 schema。

    处理三种输入格式:
      - collection: 含 view/like/fav/coin/share/danmaku/reply + arc 包装
      - author_space: 含 play/favorite/comment/length/description
      - favorites CSV: 仅有 bvid/title/url
    """
    # 字段名映射：各类来源 → 标准名
    bvid = raw.get("bvid") or raw.get("bvid", "")
    aid = raw.get("aid") or raw.get("aid", 0)
    cid = raw.get("cid") or 0
    title = raw.get("title") or raw.get("title", "")
    pic = raw.get("pic") or raw.get("pic", "")

    # 统计字段：兼容 collection (view/like/fav/coin) 和 author (play/favorite)
    view = raw.get("view") or raw.get("play", 0)
    like = raw.get("like", 0)
    fav = raw.get("fav") or raw.get("favorite", 0)
    coin = raw.get("coin", 0)
    share = raw.get("share", 0)
    danmaku = raw.get("danmaku", 0)
    reply = raw.get("reply") or raw.get("comment", 0)

    # 时长
    duration = raw.get("duration", 0)
    if not duration and raw.get("length"):
        # length 是 "10:46" 格式，转为秒
        try:
            parts = str(raw["length"]).split(":")
            if len(parts) == 2:
                duration = int(parts[0]) * 60 + int(parts[1])
            elif len(parts) == 3:
                duration = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        except (ValueError, TypeError):
            duration = 0

    # 描述
    description = raw.get("description") or raw.get("desc") or ""

    # 作者信息：支持嵌套 owner 对象或扁平 owner_name/owner_mid 字段
    owner = raw.get("owner")
    if isinstance(owner, dict) and (owner.get("name") or owner.get("mid")):
        owner_name = owner.get("name", "")
        owner_mid = owner.get("mid", 0)
    else:
        owner_name = raw.get("owner_name", "")
        owner_mid = raw.get("owner_mid", 0)

    return {
        "bvid": bvid,
        "aid": aid if aid else None,
        "cid": cid if cid else None,
        "title": title,
        "description": description,
        "duration": duration,
        "pubdate": raw.get("pubdate", 0),
        "pic": pic,
        "owner_name": owner_name,
        "owner_mid": owner_mid,
        "view": view,
        "like": like,
        "fav": fav,
        "coin": coin,
        "share": share,
        "danmaku": danmaku,
        "reply": reply,
        "stage1_source": source,
        "stage1_source_detail": source_detail,
        "enriched": False,
    }


def _make_output_name(prefix: str, *parts: str) -> str:
    """生成输出文件名。"""
    clean = "_".join(p for p in parts if p)
    return f"{clean}.json"


# ═══════════════════════════════════════════════
# Stage 1 — 作品-作者级信息收集
# ═══════════════════════════════════════════════

def export_favorites_to_json(
    fav_id: int,
    output_dir: str | Path | None = None,
    max_items: int = 0,
) -> Path:
    """导出收藏夹为标准化 JSON（同目录下也写一份 CSV）。

    Args:
        fav_id: 收藏夹 ID（可从 BilibiliService.list_folders() 获取）。
        output_dir: 输出目录，默认 data/bili_servey/videos/。
        max_items: 最大导出数，0=全部。

    Returns:
        保存的 JSON 文件路径。
    """
    out_dir = _ensure_dir(output_dir or DEFAULT_VIDEOS_DIR)

    # 获取收藏夹原始数据
    raw_videos = _get_fav_videos(fav_id, max_items=max_items)
    if not raw_videos:
        raise ValueError(f"收藏夹 {fav_id} 没有视频")

    # 也写一份 CSV 到同目录（用户要求的）
    csv_path = out_dir / f"favorites_{fav_id}.csv"
    _export_fav_csv(fav_id, str(csv_path), max_items=max_items)

    # 标准化
    normalized = [_normalize_record(v, "favorites", str(fav_id)) for v in raw_videos]

    json_path = out_dir / f"favorites_{fav_id}.json"
    _save_videos_to_json(normalized, str(json_path), {
        "type": "favorites",
        "fav_id": fav_id,
        "csv_path": str(csv_path),
    })
    return json_path


def collect_collection_videos(
    url_or_bvid: str,
    output_dir: str | Path | None = None,
) -> Path | None:
    """从视频 URL 查找所属合集，保存合集中所有视频。

    如果视频不属于任何合集，返回 None。
    """
    out_dir = _ensure_dir(output_dir or DEFAULT_VIDEOS_DIR)
    result = _get_video_collection(url_or_bvid)

    if not result.get("ok"):
        return None

    season_id = result["season_id"]
    title = result.get("title", "unknown")
    normalized = [
        _normalize_record(v, "collection", title)
        for v in result.get("videos", [])
    ]

    json_path = out_dir / f"collection_{season_id}.json"
    _save_videos_to_json(normalized, str(json_path), {
        "type": "collection",
        "season_id": season_id,
        "season_title": title,
        "source_video": url_or_bvid,
    })
    return json_path


def collect_author_top_videos(
    mid: int,
    output_dir: str | Path | None = None,
    order: str = "click",
    max_pages: int = 5,
) -> Path:
    """收集作者的热门/最新视频。

    Args:
        mid: 作者用户 ID。
        output_dir: 输出目录。
        order: 排序，click=最多播放, pubdate=最新发布, stow=最多收藏。
        max_pages: 最大翻页数（每页 30 条）。

    Returns:
        保存的 JSON 文件路径。
    """
    out_dir = _ensure_dir(output_dir or DEFAULT_VIDEOS_DIR)

    # 顺便获取作者信息（存在 meta 中）
    author_info = _get_author_info(mid)
    author_name = author_info.get("name", str(mid))

    raw_videos = _get_all_author_videos(mid, order=order, max_pages=max_pages)
    normalized = [
        _normalize_record(v, "author_space", order)
        for v in raw_videos
    ]

    # 给每个视频补上作者信息（作者空间 API 返回的 vlist 不包含 owner 信息）
    for v in normalized:
        v["owner_name"] = author_name
        v["owner_mid"] = mid

    json_path = out_dir / f"author_{mid}_{order}.json"
    _save_videos_to_json(normalized, str(json_path), {
        "type": "author_space",
        "mid": mid,
        "author_name": author_name,
        "order": order,
    })
    return json_path


def collect_from_video_url(
    url_or_bvid: str,
    output_dir: str | Path | None = None,
    author_order: str = "click",
    author_max_pages: int = 5,
) -> dict[str, Any]:
    """捆绑收集：从视频 URL 同时找出合集 + 作者，分别保存。

    Returns:
        {
            "collection_path": Path | None,   # 合集JSON路径（如不在合集中则为 None）
            "author_path": Path | None,        # 作者视频JSON路径
            "mid": int | None,                 # 作者 mid
            "author_name": str,
            "video": {"bvid": ..., "title": ...},  # 源视频基本信息
        }
    """
    out_dir = _ensure_dir(output_dir or DEFAULT_VIDEOS_DIR)
    bvid = _extract_bvid(url_or_bvid)
    if not bvid:
        raise ValueError(f"无法从 URL 提取 BVID: {url_or_bvid}")

    # 先查 view API 获取基本信息 + 作者
    view_data = _get_video_view(bvid)
    if view_data:
        mid = view_data.get("owner", {}).get("mid")
        video_info = {
            "bvid": bvid,
            "title": view_data.get("title", ""),
            "owner_name": view_data.get("owner", {}).get("name", ""),
            "owner_mid": mid,
        }
    else:
        mid = None
        video_info = {"bvid": bvid, "title": "", "owner_name": "", "owner_mid": None}

    # 找合集
    collection_path = collect_collection_videos(bvid, output_dir=out_dir)

    # 找作者（从 view_data 取 mid，如果合集没有提供的话）
    if not mid and collection_path:
        # 从已保存的合集 JSON 中读取 mid
        try:
            coll_data = json.loads(collection_path.read_text(encoding="utf-8"))
            videos = coll_data.get("videos", [])
            if videos:
                mid = videos[0].get("owner_mid")
        except (json.JSONDecodeError, FileNotFoundError):
            pass

    author_path = None
    author_name = ""
    if mid:
        author_info = _get_author_info(mid)
        author_name = author_info.get("name", str(mid))
        video_info["owner_name"] = author_name
        video_info["owner_mid"] = mid
        author_path = collect_author_top_videos(
            mid, output_dir=out_dir, order=author_order, max_pages=author_max_pages
        )

    return {
        "collection_path": collection_path,
        "author_path": author_path,
        "mid": mid,
        "author_name": author_name,
        "video": video_info,
    }


# ═══════════════════════════════════════════════
# Stage 2 — 简介收集 + 评分
# ═══════════════════════════════════════════════

def enrich_videos_from_file(
    input_path: str | Path,
    output_path: str | Path | None = None,
    delay: float = 0.35,
) -> Path:
    """对 JSON 文件中每个视频调 view API 补全信息。

    幂等操作：已 enriched 的记录会被覆盖更新。
    跳过没有 bvid 的记录。

    Args:
        input_path: 输入的 JSON 文件路径（Stage 1 产出的任意 JSON）。
        output_path: 输出路径。默认在输入文件同目录生成 enriched_{原文件名}。

    Returns:
        输出文件路径。
    """
    input_path = Path(input_path)
    if not output_path:
        output_path = input_path.parent / f"enriched_{input_path.name}"

    videos = _load_videos_from_json(str(input_path))
    if not videos:
        raise ValueError(f"输入文件无视频记录: {input_path}")

    enriched_count = 0
    skipped_count = 0

    for idx, v in enumerate(videos):
        bvid = v.get("bvid") or ""
        aid = v.get("aid") or 0
        if not bvid and not aid:
            skipped_count += 1
            continue

        # 优先用 bvid，没有 bvid 时用 aid 查
        if bvid:
            view_data = _get_video_view(bvid)
        else:
            # 直接调用 view API（aid 参数）
            import urllib.request, json as _json
            from backend.services.bilibili.collect import _build_headers as _bh
            try:
                req = urllib.request.Request(
                    f"https://api.bilibili.com/x/web-interface/view?aid={aid}",
                    headers=_bh(),
                )
                resp = urllib.request.urlopen(req, timeout=15)
                raw = _json.loads(resp.read().decode("utf-8"))
                view_data = raw.get("data") if raw.get("code") == 0 else None
            except Exception:
                view_data = None
        if not view_data:
            skipped_count += 1
            continue

        # 补全字段（view API 返回的 bvid 优先覆盖）
        view_bvid = view_data.get("bvid") or ""
        if view_bvid:
            v["bvid"] = view_bvid
        v["aid"] = view_data.get("aid", v.get("aid"))
        v["title"] = view_data.get("title", v.get("title"))
        v["description"] = view_data.get("desc", v.get("description"))
        v["duration"] = view_data.get("duration", v.get("duration", 0))
        v["pubdate"] = view_data.get("pubdate", v.get("pubdate", 0))
        v["pic"] = view_data.get("pic", v.get("pic", ""))

        owner = view_data.get("owner", {})
        v["owner_name"] = owner.get("name", v.get("owner_name"))
        v["owner_mid"] = owner.get("mid", v.get("owner_mid"))

        stat = view_data.get("stat", {})
        v["view"] = stat.get("view", v.get("view", 0))
        v["like"] = stat.get("like", v.get("like", 0))
        v["fav"] = stat.get("favorite", v.get("fav", 0))
        v["coin"] = stat.get("coin", v.get("coin", 0))
        v["share"] = stat.get("share", v.get("share", 0))
        v["danmaku"] = stat.get("danmaku", v.get("danmaku", 0))
        v["reply"] = stat.get("reply", v.get("reply", 0))

        v["enriched"] = True
        enriched_count += 1

        if (idx + 1) % 5 == 0 or idx == 0:
            print(f"  enrich: {idx+1}/{len(videos)} 条 (bvid={bvid})")
        time.sleep(delay)

    # 保存
    _save_videos_to_json(videos, str(output_path), {
        "description": "enriched from view API",
        "source": str(input_path),
        "enriched_count": enriched_count,
        "skipped_count": skipped_count,
    })
    print(f"  enrich 完成: {enriched_count} 条丰富, {skipped_count} 条跳过")
    return Path(output_path)


def score_videos_from_file(
    input_path: str | Path,
    output_path: str | Path | None = None,
    llm: LLMClient | None = None,
    profile_path: str | Path | None = None,
    system_prompt_path: str | Path | None = None,
    relevance_weight: float = 0.6,
    popularity_weight: float = 0.4,
) -> Path:
    """对已丰富的视频执行评分+排序。

    强制执行：必须提供 input_path 和 profile_path。

    Args:
        input_path: 输入的 JSON 文件路径（建议先 enrich 再评分）。
        output_path: 输出路径。默认在 servey/ 目录下生成 ranked_{原文件名}。
        llm: LLM 客户端。如为 None，仅计算热度分（不调 LLM）。
        profile_path: 用户画像文件路径。**必填，无默认值**。
        system_prompt_path: system prompt 路径，默认用 video_scorer_system.txt。
        relevance_weight: 兴趣匹配权重。
        popularity_weight: 热度权重。

    Returns:
        输出文件路径。
    """
    input_path = Path(input_path)
    if not input_path.exists():
        raise FileNotFoundError(f"输入文件不存在: {input_path}")

    if not profile_path:
        raise ValueError("profile_path 是必填参数，请指定用户画像文件路径")
    profile_path = Path(profile_path)
    if not profile_path.exists():
        raise FileNotFoundError(f"画像文件不存在: {profile_path}")

    # 确定输出路径
    if output_path:
        output_path = Path(output_path)
    else:
        servey_dir = _ensure_dir(DEFAULT_SERVEY_DIR)
        output_path = servey_dir / f"ranked_{input_path.name}"

    videos = _load_videos_from_json(str(input_path))
    if not videos:
        raise ValueError(f"输入文件无视频记录: {input_path}")

    profile_text = profile_path.read_text(encoding="utf-8")

    if llm:
        system_path = system_prompt_path or DEFAULT_SYSTEM_PROMPT
        system_text = system_path.read_text(encoding="utf-8")
        from backend.pipelines._prompts import _render
        system_text = _render(system_text, {"user_profile": profile_text})

        ranked = _screen_videos(
            videos, llm,
            output_path=str(output_path),
            profile_text=profile_text,
            relevance_weight=relevance_weight,
            popularity_weight=popularity_weight,
        )
    else:
        # 仅热度排序
        from backend.services.bilibili.screener import _calc_popularity_score
        for v in videos:
            v["relevance_score"] = 0
            v["popularity_score"] = _calc_popularity_score(v)
            v["importance_score"] = round(
                (v["popularity_score"] / 100) * popularity_weight * 100, 1
            )
            v["suggest_action"] = "skip"
            v["_screened_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        videos.sort(key=lambda x: x.get("importance_score", 0), reverse=True)
        for idx, v in enumerate(videos, 1):
            v["rank"] = idx

        _save_videos_to_json(videos, str(output_path), {
            "description": "popularity-only ranking (no LLM)",
            "source": str(input_path),
            "profile": str(profile_path),
            "relevance_weight": relevance_weight,
            "popularity_weight": popularity_weight,
        })

    print(f"  评分完成: {len(videos)} 条已排序 → {output_path}")
    return Path(output_path)


# ═══════════════════════════════════════════════
# Stage 3 — 字幕提取
# ═══════════════════════════════════════════════

def extract_subtitles_for_ranked(
    input_path: str | Path,
    output_dir: str | Path | None = None,
    top_n: int = 10,
    min_score: float = 50.0,
) -> list[dict[str, Any]]:
    """对视频列表提取字幕。

    三个步骤完全解耦——这个函数不依赖视频是否被 enrich 或评分过：
      - 如果视频有 importance_score，按分数阈值筛选
      - 如果视频没有评分（从未经过 Stage 2），则全部处理（按 top_n 截断）
      - 基本信息不足的（如无 description）会通过 view API 实时补全

    Args:
        input_path: JSON 文件路径（任意来源：合集/作者/收藏夹/手动编辑 均可）。
        output_dir: 输出目录，默认为 data/bili_subtitles_out/。
        top_n: 最大提取数。
        min_score: 最低 importance_score 阈值（仅当视频有评分时生效）。

    Returns:
        成功提取字幕的视频记录列表（每条记录附加 subtitle_path 字段）。
    """
    out_dir = _ensure_dir(output_dir or DEFAULT_KNOWLEDGEBASE_DIR)
    input_path = Path(input_path)

    videos = _load_videos_from_json(str(input_path))
    if not videos:
        raise ValueError(f"输入文件无视频记录: {input_path}")

    # 解耦设计：只有数据有评分时才按分数过滤，否则处理所有（按 top_n 截断）
    has_scores = any(v.get("importance_score") is not None for v in videos)
    if has_scores:
        candidates = _filter_by_action(videos, min_score=min_score)[:top_n]
        if not candidates:
            print("  没有视频达到最低分数阈值")
            return []
    else:
        candidates = videos[:top_n]
        print(f"  无评分数据，直接处理前 {len(candidates)} 个视频")

    results = []
    for idx, v in enumerate(candidates, 1):
        bvid = v.get("bvid", "")
        title = v.get("title", bvid)
        url = f"https://www.bilibili.com/video/{bvid}"

        print(f"  [{idx}/{len(candidates)}] 提取字幕: {title[:40]}...", end=" ")

        sub_result = _get_subtitle(url)
        if not sub_result.get("ok"):
            print(f"失败: {sub_result.get('error', 'unknown')}")
            continue

        # 保存完整 JSON
        json_path = out_dir / f"{bvid}_subtitle.json"
        json_path.write_text(
            json.dumps(sub_result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # 保存纯文本
        text_content = sub_result.get("text", "")
        txt_path = out_dir / f"{bvid}_subtitle.txt"
        txt_path.write_text(text_content, encoding="utf-8")

        # 更新记录
        v["subtitle_path"] = str(json_path)
        v["subtitle_txt_path"] = str(txt_path)
        results.append(v)

        dur = sub_result.get("duration", 0)
        m, s = divmod(dur, 60)
        print(f"OK ({m}:{s:02d}, {len(sub_result.get('segments',[]))} 段)")

        time.sleep(0.5)  # 礼貌间隔

    # 也保存一份结果清单
    manifest = out_dir / "_extract_manifest.json"
    manifest.write_text(
        json.dumps({
            "source": str(input_path),
            "extracted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_extracted": len(results),
            "videos": [
                {"bvid": r["bvid"], "title": r.get("title", ""),
                 "subtitle_path": r.get("subtitle_path", ""),
                 "importance_score": r.get("importance_score", 0)}
                for r in results
            ],
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"\n  字幕提取完成: {len(results)}/{len(candidates)} 个成功 → {out_dir}")
    return results


# ═══════════════════════════════════════════════
# 独立运行测试
# ═══════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    print("B站视频调研管道模块 - 非直接运行入口")
    print("请通过 python -m backend.main bili-survey-stage* 调用")
