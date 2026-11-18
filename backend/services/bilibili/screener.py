"""B站视频筛选排序服务。

设计目标：模块化解耦，每步产出独立JSON文件，支持人工干预。

流程：
  Step 1 - collect:  收集视频（合集/作者空间）→ 保存 raw_videos.json
         ↓ 人工可编辑 raw_videos.json（删除不想要的条目）
  Step 2 - screen:   LLM 兴趣评分 + 热度加权排序 → 保存 ranked_videos.json
         ↓ 人工可编辑 ranked_videos.json（调整顺序/删条目/改score）
  Step 3 - extract:  对高分视频逐个提取字幕（调用现有 pipeline）

这种设计的优势：
  - 用户可以在任意步骤之间手动编辑 JSON（删除/修改），不用跑代码
  - 每步结果独立可复用，不必每次从头跑
  - 评分逻辑透明，权重参数可调
"""

from __future__ import annotations

import json
import math
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.config.settings import Settings
from backend.core.llm.client import LLMClient
from backend.pipelines._prompts import load_prompt, _render

# ── 路径常量 ──
_PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "pipelines" / "prompts" / "bili"
_DEFAULT_PROFILE_PATH = _PROMPTS_DIR / "video_scorer_profile.txt"
_DEFAULT_SYSTEM_PROMPT = _PROMPTS_DIR / "video_scorer_system.txt"


# ═══════════════════════════════════════════════
# Step 1: Collect — 保存视频列表为 JSON
# ═══════════════════════════════════════════════

def save_videos_to_json(
    videos: list[dict[str, Any]],
    output_path: str | Path,
    source_info: dict[str, Any] | None = None,
) -> str:
    """将视频列表保存为 JSON，供后续筛选使用。

    Args:
        videos: 视频字典列表。每个字典应包含 bvid, title, description,
                play, like, coin, fav 等字段（从合集/作者空间获取的格式）。
        output_path: 输出路径。
        source_info: 来源信息（如 {"type": "collection", "season_title": "..."}）。

    该文件设计为人类可读、可手工编辑的格式：
      - 每行一个视频，方便增删
      - 可以直接用文本编辑器打开删除不想要的条目
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    entry = {
        "meta": {
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total": len(videos),
            "source": source_info or {},
        },
        "videos": videos,
    }

    output_path.write_text(
        json.dumps(entry, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return str(output_path)


def load_videos_from_json(file_path: str | Path) -> list[dict[str, Any]]:
    """从之前保存的 JSON 文件加载视频列表。"""
    file_path = Path(file_path)
    data = json.loads(file_path.read_text(encoding="utf-8"))
    # 支持两种格式：含 meta 的 entry 和纯列表
    if isinstance(data, dict) and "videos" in data:
        return data["videos"]
    if isinstance(data, list):
        return data
    return []


# ═══════════════════════════════════════════════
# Step 2: Screen — 兴趣评分 + 热度加权排序
# ═══════════════════════════════════════════════

def _load_condensed_profile() -> str:
    """加载精简版用户画像。"""
    path = _DEFAULT_PROFILE_PATH
    if path.exists():
        return path.read_text(encoding="utf-8")
    # fallback: 从完整的 user_profile.md 提取前几段
    full_path = path.parent / "user_profile.md"
    if full_path.exists():
        text = full_path.read_text(encoding="utf-8")
        # 只提取一、二、三节
        sections = re.findall(r"## .+?(?=\n## |\Z)", text, re.DOTALL)
        return "\n".join(sections[:3]) if sections else text[:1500]
    return "(无用户画像)"


def _calc_popularity_score(video: dict[str, Any]) -> float:
    """根据播放量、点赞率、收藏率计算热度得分 (0-100)。

    使用对数尺度避免头部效应：
      - 播放量得分: log10(view) 归一化到 0-50
      - 点赞率得分: (like/view) 归一化到 0-25（基准 5% 点赞率=25分）
      - 收藏率得分: (fav/view) 归一化到 0-25（基准 2% 收藏率=25分）
    """
    view = float(video.get("view") or video.get("play", 0))
    like = float(video.get("like", 0))
    fav = float(video.get("fav", 0))
    coin = float(video.get("coin", 0))

    # 播放量得分 (对数)：1k=30, 10k=40, 100k=50, 1M=60, 封顶 50 分
    play_score = 0.0
    if view > 0:
        play_score = min(50.0, max(0.0, (math.log10(view) - 1) * 10))

    # 点赞率得分：点赞/播放 比例，乘 500 映射到 0-25（5% 点赞率=25分）
    like_ratio = like / view if view > 0 else 0
    like_score = min(25.0, like_ratio * 500)

    # 收藏率得分：收藏/播放 比例，乘 1250 映射到 0-25（2% 收藏率=25分）
    fav_ratio = fav / view if view > 0 else 0
    fav_score = min(25.0, fav_ratio * 1250)

    # 硬币率加分（bonus）：有人投币说明认可度高
    coin_ratio = coin / view if view > 0 else 0
    coin_bonus = min(10.0, coin_ratio * 2000)  # 0.5% 投币率=10分

    popularity = play_score + like_score + fav_score + coin_bonus
    return round(min(100.0, popularity), 1)


def _build_video_prompt(video: dict[str, Any]) -> str:
    """为单个视频构建评分用的 user prompt。"""
    title = video.get("title", "无标题")
    desc = video.get("desc") or video.get("description", "无简介")
    # 截断过长的简介
    if len(desc) > 500:
        desc = desc[:500] + "…"
    bvid = video.get("bvid", "")
    owner_name = video.get("owner", {}).get("name") if isinstance(video.get("owner"), dict) else (
        video.get("owner_name", "")
    )
    stat_info = ""
    for k, label in [("view", "播放"), ("like", "点赞"), ("coin", "硬币"),
                      ("fav", "收藏"), ("danmaku", "弹幕"), ("reply", "评论")]:
        val = video.get(k) or video.get({"view": "play"}.get(k, k), 0)
        if val:
            stat_info += f"  {label}: {val}\n"

    return (
        f"# 视频信息\n"
        f"BVID: {bvid}\n"
        f"标题: {title}\n"
        f"UP主: {owner_name}\n"
        f"统计数据:\n{stat_info}"
        f"简介:\n{desc}\n"
    )


def screen_videos(
    videos: list[dict[str, Any]],
    llm: LLMClient,
    output_path: str | Path | None = None,
    profile_text: str | None = None,
    relevance_weight: float = 0.6,
    popularity_weight: float = 0.4,
    batch_size: int = 5,
    delay: float = 0.5,
) -> list[dict[str, Any]]:
    """对视频列表进行兴趣评分 + 热度排序。

    Args:
        videos: 视频字典列表（来自合集或作者空间）。
        llm: LLM 客户端。
        output_path: 结果输出路径（可选），保存后可手工编辑。
        profile_text: 用户画像文本。为 None 时自动从 video_scorer_profile.txt 加载。
        relevance_weight: 兴趣匹配权重 (0-1)。
        popularity_weight: 热度权重 (0-1)。
        batch_size: 每批打分数量（LLM 调用并发数）。
        delay: 每批间隔秒数。

    Returns:
        排序后的视频列表，每个视频附加了:
          - relevance_score: LLM 给出的兴趣分 (0-10)
          - popularity_score: 热度分 (0-100)
          - importance_score: 加权总分 (0-100)
          - reasoning: LLM 的评分理由
          - suggest_action: LLM 建议的动作
    """
    if not videos:
        return []

    # 加载用户画像
    if profile_text is None:
        profile_text = _load_condensed_profile()

    # 加载 system prompt
    sys_path = _DEFAULT_SYSTEM_PROMPT
    system_prompt = sys_path.read_text(encoding="utf-8")
    system_prompt = _render(system_prompt, {"user_profile": profile_text})

    results: list[dict] = []

    for i in range(0, len(videos), batch_size):
        batch = videos[i:i + batch_size]
        batch_results = []

        for video in batch:
            user_prompt = _build_video_prompt(video)

            try:
                response = llm.call(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    max_tokens=500,
                    temperature=0.3,
                )
                # 解析 JSON 响应
                score_data = _parse_llm_score(response)
            except Exception as e:
                score_data = {
                    "relevance_score": 5,
                    "reasoning": f"LLM调用失败: {e}",
                    "key_topics": [],
                    "suggest_action": "maybe",
                }

            popularity_score = _calc_popularity_score(video)
            relevance = score_data.get("relevance_score", 5)
            importance = round(
                (relevance / 10.0) * relevance_weight * 100
                + (popularity_score / 100.0) * popularity_weight * 100,
                1,
            )

            entry = dict(video)
            entry.update({
                "relevance_score": relevance,
                "popularity_score": popularity_score,
                "importance_score": importance,
                "reasoning": score_data.get("reasoning", ""),
                "key_topics": score_data.get("key_topics", []),
                "suggest_action": score_data.get("suggest_action", "maybe"),
                "_screened_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
            batch_results.append(entry)
            time.sleep(delay)

        results.extend(batch_results)

    # 按 importance_score 降序排列
    results.sort(key=lambda x: x.get("importance_score", 0), reverse=True)

    # 加排名
    for idx, r in enumerate(results, 1):
        r["rank"] = idx

    # 保存（如指定路径）
    if output_path:
        save_path = Path(output_path)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        out_entry = {
            "meta": {
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "total": len(results),
                "relevance_weight": relevance_weight,
                "popularity_weight": popularity_weight,
                "scoring_method": "relevance*weight + popularity*weight",
            },
            "videos": results,
        }
        save_path.write_text(
            json.dumps(out_entry, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    return results


def _parse_llm_score(response: str) -> dict[str, Any]:
    """从 LLM 响应中解析 JSON 评分结果。"""
    # 尝试提取 ```json ... ``` 块
    json_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", response, re.DOTALL)
    if json_match:
        response = json_match.group(1)

    # 尝试直接解析
    response = response.strip()
    try:
        data = json.loads(response)
        return {
            "relevance_score": int(data.get("relevance_score", 5)),
            "reasoning": str(data.get("reasoning", "")),
            "key_topics": list(data.get("key_topics", [])),
            "suggest_action": str(data.get("suggest_action", "maybe")),
        }
    except json.JSONDecodeError:
        pass

    # fallback: 尝试从文本中提取数字
    score_match = re.search(r"(?:relevance_score|score)[:\s]+(\d+)", response, re.IGNORECASE)
    score = int(score_match.group(1)) if score_match else 5
    action_match = re.search(r'(?:suggest_action|action)[:\s]+"(\w+)"', response)
    action = action_match.group(1) if action_match else "maybe"

    return {
        "relevance_score": min(10, max(0, score)),
        "reasoning": response[:200],
        "key_topics": [],
        "suggest_action": action,
    }


# ═══════════════════════════════════════════════
# 工具：合并与查看结果
# ═══════════════════════════════════════════════

def print_top_videos(
    ranked: list[dict[str, Any]],
    top_n: int = 10,
) -> None:
    """友好打印排名靠前的视频。"""
    print(f"\n{'='*60}")
    print(f" 排名  | 得分  | 兴趣分 | 热度分 | 建议   | 标题")
    print(f"{'-'*60}")
    for v in ranked[:top_n]:
        rank = v.get("rank", "?")
        imp = v.get("importance_score", 0)
        rel = v.get("relevance_score", "?")
        pop = v.get("popularity_score", "?")
        action = v.get("suggest_action", "?")
        title = (v.get("title") or "?")[:40]
        print(f" {rank:>4} | {imp:>4} | {str(rel):>4} | {str(pop):>4} | {action:<6} | {title}")
    print(f"{'='*60}\n")


def filter_by_action(
    ranked: list[dict[str, Any]],
    min_score: float = 50.0,
) -> list[dict[str, Any]]:
    """根据重要性分数筛选值得提取字幕的视频。"""
    return [v for v in ranked if v.get("importance_score", 0) >= min_score]


# ═══════════════════════════════════════════════
# 独立运行测试
# ═══════════════════════════════════════════════

if __name__ == "__main__":
    # 简单的自测：纯热度评分
    test_videos = [
        {"bvid": "BV1weJP6NEF5", "title": "Test", "desc": "测试视频",
         "view": 50000, "like": 3000, "fav": 500, "coin": 100},
        {"bvid": "BV1xxx", "title": "Test2", "desc": "测试视频2",
         "view": 1000, "like": 50, "fav": 10, "coin": 5},
        {"bvid": "BV1yyy", "title": "Test3", "desc": "测试视频3",
         "view": 1000000, "like": 50000, "fav": 20000, "coin": 5000},
    ]
    for v in test_videos:
        pop = _calc_popularity_score(v)
        print(f"{v['bvid']}: view={v['view']}, popularity_score={pop}")
