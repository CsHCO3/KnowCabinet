"""B站 Service 层 — 采集/提取/问答 全流程。"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

# ── 路径引导 ──
BACKEND_DIR = Path(__file__).resolve().parent.parent.parent / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
# ─────────────────

from api.services import bili_db
from api.services.bili_task_manager import BiliTaskManager

# ── 提示词文件路径 ──

_PROMPTS_DIR = BACKEND_DIR / "pipelines" / "prompts" / "bili"


def _load_prompt(filename: str) -> str:
    path = _PROMPTS_DIR / filename
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return ""


_SUMMARY_SYSTEM_PROMPT = _load_prompt("extract_summary.txt")
_QUERY_SYSTEM_PROMPT = _load_prompt("query_system.txt")
_ANNOTATE_SYSTEM_PROMPT = _load_prompt("annotate_video.txt")

# P-08：摘要单次调用最多吃 8000 字，超限走 map-reduce（分段提炼→合并），
# 不再静默丢弃超长部分（14 条里 2 条超限、最长曾丢 64%）。
_SUMMARY_CHAR_LIMIT = 8000
_SUMMARY_CHUNK_SIZE = 6000


def _chunk_text(text: str, chunk_size: int = _SUMMARY_CHUNK_SIZE) -> list[str]:
    """把字幕文本按行边界切成约 chunk_size 的块，保留完整行。"""
    lines = text.splitlines(keepends=True)
    if not lines:
        return [text] if text else []
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for line in lines:
        if size + len(line) > chunk_size and buf:
            chunks.append("".join(buf))
            buf, size = [], 0
        buf.append(line)
        size += len(line)
    if buf:
        chunks.append("".join(buf))
    return chunks


_REDUCE_SYSTEM_PROMPT = (
    "你是视频总结合并助手。以下是对同一个视频字幕分段提炼出的多份要点总结，"
    "请把它们合并为一份最终要点总结：\n"
    "1. 去除分段交界处的重复内容与截断残句；\n"
    "2. 保持信息完整：数据、名称、术语、数字不得丢失；不添加新信息；\n"
    "3. 输出格式与单段总结一致：先一句话核心主题，再按主题分点总结。"
)


def _llm_call_retry_empty(llm, messages: list[dict], max_tokens: int = 2000,
                          temperature: float = 0.3) -> str:
    """思考型模型（glm-5.3-flash 始终思考）偶发把 token 全花在思考上、正文返回空；
    空结果按次加大 max_tokens 重试，三次仍空则报错。"""
    last = ""
    for attempt in range(1, 4):
        last = llm.call(messages=messages, max_tokens=max_tokens * attempt,
                        temperature=temperature)
        if last and last.strip():
            return last
        time.sleep(2 * attempt)
    raise RuntimeError(f"LLM 连续 3 次返回空内容（base max_tokens={max_tokens}）")


def _summarize_subtitle(llm, text_content: str) -> str:
    """字幕摘要。短文本单次调用；超限自动 map-reduce，不再截断丢内容。"""
    if len(text_content) <= _SUMMARY_CHAR_LIMIT:
        return _llm_call_retry_empty(llm, [
            {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": text_content},
        ])

    chunks = _chunk_text(text_content)
    part_summaries = []
    for i, chunk in enumerate(chunks, 1):
        part_summaries.append(_llm_call_retry_empty(llm, [
            {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content":
                f"（长视频字幕第 {i}/{len(chunks)} 段，段边界处可能截断，"
                f"只总结本段内容）\n\n{chunk}"},
        ]))

    joined = "\n\n".join(
        f"── 第 {i} 段总结 ──\n{s}" for i, s in enumerate(part_summaries, 1)
    )
    return _llm_call_retry_empty(llm, [
        {"role": "system", "content": _REDUCE_SYSTEM_PROMPT},
        {"role": "user", "content": joined},
    ])


def _parse_json_loose(raw: str) -> dict | None:
    """宽松解析 LLM 输出的 JSON（容忍代码块围栏与前后杂文字）。"""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(raw[start:end + 1])
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _merge_tags(llm_tags: list[str], bili_tags: list[str], cap: int = 8) -> list[str]:
    """LLM 具体 tag 在前（查询词多为具体词，tag bonus 命中率高），官方宽 tag 补后，去重 cap。"""
    def _ok(t: str) -> bool:
        t = str(t).strip()
        return bool(t) and "," not in t and "[" not in t and "]" not in t
    out: list[str] = []
    for t in list(llm_tags or []) + list(bili_tags or []):
        if _ok(t) and t not in out:
            out.append(t)
    return out[:cap]


def _annotate_video(llm, video: dict, bili_tags: list[str] | None = None) -> dict | None:
    """对已有摘要的视频做分类 + 仓库/文档链接抽取。失败返回 None。"""
    if not _ANNOTATE_SYSTEM_PROMPT:
        return None
    official = ", ".join(bili_tags or []) or "无"
    user = (
        f"标题：{video.get('title', '')}\n"
        f"UP主：{video.get('author', '')}\n"
        f"官方标签（UP主打的，分类的重要依据）：{official}\n"
        f"简介：{(video.get('description') or '')[:500] or '（无）'}\n\n"
        f"内容摘要：\n{(video.get('summary_text') or '')[:3000]}"
    )
    raw = _llm_call_retry_empty(
        llm,
        [
            {"role": "system", "content": _ANNOTATE_SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        max_tokens=2000,
        temperature=0.1,
    )
    ann = _parse_json_loose(raw)
    if not ann or not ann.get("category"):
        return None
    return {
        "category": str(ann["category"]),
        "tags": [str(t) for t in (ann.get("tags") or [])][:8],
        "repo_urls": [str(u) for u in (ann.get("repo_urls") or []) if str(u).startswith("http")],
        "bili_tags": list(bili_tags or []),
    }


def _apply_annotation(video_id: int, ann: dict | None) -> bool:
    """把标注结果写入 db（ann 为 None 时不写）。"""
    if not ann:
        return False
    return bili_db.update_video(video_id, {
        "category": ann["category"],
        "tags": json.dumps(ann["tags"], ensure_ascii=False),
        "repo_urls": json.dumps(ann["repo_urls"], ensure_ascii=False),
        "bili_tags": json.dumps(ann.get("bili_tags", []), ensure_ascii=False),
    })


def _load_json_list(raw) -> list[str]:
    """db 里 JSON 数组字符串 → list[str]（空/损坏返回 []）。"""
    if not raw:
        return []
    try:
        value = json.loads(raw)
        return [str(x) for x in value] if isinstance(value, list) else []
    except json.JSONDecodeError:
        return []


def annotate_missing(limit: int = 0) -> dict:
    """已摘要视频的存量回补（CLI `bili-annotate` 用）。每条视频顺序补三样、单项失败继续：
    1) bili_tags 为空 → 拉官方标签写库；
    2) category 为空 → 走 _annotate_video（带官方标签信号）；
    3) 已晋升 dense 文件存在 → 重写 frontmatter（补 tags/section/year）。
    """
    from backend.config.settings import Settings
    from backend.services.archive.service import _FRONTMATTER_RE, promote_to_dense

    llm = Settings().get_llm_client("glm-flash")
    rows = bili_db.get_videos(summary_status="summarized", limit=limit or 0)
    annotated, failed, tags_pulled, dense_rewritten = 0, 0, 0, 0
    for i, v in enumerate(rows, 1):
        bvid = v.get("bvid", "")
        prefix = f"  [{i}/{len(rows)}] {bvid}:"

        # 1) 官方标签
        bili_tags = _load_json_list(v.get("bili_tags"))
        if not bili_tags:
            try:
                from backend.services.bilibili.collect import get_video_tags
                bili_tags = get_video_tags(bvid)
                if bili_tags:
                    bili_db.update_video(v["id"], {
                        "bili_tags": json.dumps(bili_tags, ensure_ascii=False),
                    })
                    tags_pulled += 1
            except Exception:
                bili_tags = []

        # 2) 分类标注（category 已有的复用库内值，不重花 LLM 钱）
        ann: dict | None = None
        if not (v.get("category") or "").strip():
            try:
                ann = _annotate_video(llm, v, bili_tags)
            except Exception as e:
                print(f"{prefix} 标注失败 {e}")
                failed += 1
            if ann:
                if _apply_annotation(v["id"], ann):
                    annotated += 1
                    print(f"{prefix} {ann['category']}"
                          f" | {'; '.join(ann['repo_urls'][:2]) or '无链接'}")
                else:
                    failed += 1
                    print(f"{prefix} 标注解析失败")
        else:
            ann = {"category": v["category"], "tags": _load_json_list(v.get("tags"))}

        # 3) 已晋升 dense 文件重写头部（promote_to_dense 幂等覆盖，自己重新生成 frontmatter）
        dense_path = Path("knowledgebase/community/bilibili/dense") / f"{bvid}.md"
        if dense_path.exists():
            try:
                text = dense_path.read_text(encoding="utf-8")
                m = _FRONTMATTER_RE.match(text)
                year = ""
                if v.get("pubdate"):
                    year = str(datetime.fromtimestamp(v["pubdate"]).year)
                meta = {
                    "title": v.get("title", ""),
                    "authors": v.get("author", ""),
                    "source": f"https://www.bilibili.com/video/{bvid}",
                    "section": (ann or {}).get("category", "") or (v.get("category") or ""),
                    "year": year,
                    "tags": _merge_tags((ann or {}).get("tags") or [], bili_tags),
                }
                promote_to_dense("video", bvid, text[m.end():] if m else text, meta)
                dense_rewritten += 1
            except Exception as e:
                print(f"{prefix} dense 头重写失败 {e}")
    return {"total": len(rows), "annotated": annotated, "failed": failed,
            "tags_pulled": tags_pulled, "dense_rewritten": dense_rewritten}


# ═══════════════════════════════════════════════
# 后台工作函数（模块级，供 BiliTaskManager 调用）
# ═══════════════════════════════════════════════


def _write_l0_fallback(video: dict, reason: str) -> Path | None:
    """P-09：无字幕/字幕不可信的视频落 L0 线索卡（元数据可检索，禁摘要）。

    已有 meta 文件（批量富化产物）则合并——保留其简介/官方标签/来源，只更新状态。
    下次 kb-scan 后可被检索命中，支撑"当场调研"。
    """
    bvid = video.get("bvid", "")
    if not bvid:
        return None
    try:
        meta_dir = Path("knowledgebase/community/bilibili/meta")
        meta_dir.mkdir(parents=True, exist_ok=True)
        path = meta_dir / f"{bvid}.json"
        existing: dict = {}
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                existing = {}
        pubdate = video.get("pubdate") or 0
        year = datetime.fromtimestamp(pubdate).year if pubdate else (existing.get("year") or "")
        meta = {
            **existing,
            "bvid": bvid,
            "title": existing.get("title") or video.get("title", ""),
            "abstract": existing.get("abstract") or (video.get("description") or "")[:500],
            "tags": existing.get("tags", []),
            "authors": existing.get("authors") or video.get("author", ""),
            "year": existing.get("year") or year,
            "view": existing.get("view") or video.get("view", 0),
            "fav": existing.get("fav") or video.get("fav_count", 0),
            "source": sorted(set(existing.get("source", []) or []) | {f"提取失败:{reason}"}),
            "status": "提取失败待验",
            "fail_reason": reason,
            "added_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        return path
    except Exception:
        return None


def _run_extract(task_id: str, bvids: list[str]) -> dict:
    """后台批量提取字幕 + 通用总结。"""
    from backend.services.bilibili.subtitle import get_bilibili_subtitle_with_retry
    from backend.config.settings import Settings
    from backend.core.llm.client import LLMClient

    settings = Settings()
    llm = settings.get_llm_client("glm-flash")
    subtitle_dir = Path("data/bili_subtitles")
    subtitle_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for idx, bvid in enumerate(bvids):
        if BiliTaskManager.is_cancelled(task_id):
            results.append({"bvid": bvid, "status": "cancelled", "remaining": len(bvids) - idx})
            break

        video = bili_db.get_video_by_bvid(bvid)
        if not video:
            results.append({"bvid": bvid, "status": "skipped", "reason": "not_in_db"})
            continue
        if "_p" in bvid:
            # 分P 行（bvid 形如 BVxxx_p2，见 visual/paths.kb_id）不是真 BV 号：
            # 拼出 URL 去抓会退化成 P1（_extract_bvid 会截出前半段），静默抓错集。
            results.append({"bvid": bvid, "status": "skipped", "reason": "multipart_part"})
            continue
        if video["subtitle_status"] == "extracted":
            results.append({"bvid": bvid, "status": "skipped", "reason": "already_extracted"})
            continue

        bili_db.update_video(video["id"], {"subtitle_status": "extracting"})

        # ── 提取字幕 ──
        url = f"https://www.bilibili.com/video/{bvid}"
        try:
            sub_result = get_bilibili_subtitle_with_retry(url)
        except Exception as e:
            bili_db.update_video(video["id"], {"subtitle_status": "failed"})
            results.append({"bvid": bvid, "status": "failed", "error": f"提取异常: {e}"})
            continue

        if not sub_result.get("ok"):
            err = sub_result.get("error", "unknown")
            bili_db.update_video(video["id"], {"subtitle_status": "failed"})
            # Q7：无字幕/抓取失败同样"不入库、进人工待验清单"，否则清单不全，
            # 只能靠查 bili.db 的 subtitle_status 才知道漏了哪些（P-20 就是这种）。
            from backend.services.bilibili.quality import record_quarantine

            record_quarantine(bvid, video.get("title", ""), "no_subtitle", 0,
                              note=f"字幕抓取失败：{err}")
            _write_l0_fallback(video, err)   # P-09：降级为 L0 线索卡，元数据仍可检索
            results.append({"bvid": bvid, "status": "failed", "error": err})
            continue

        # ── 保存字幕文件 ──
        sub_path = subtitle_dir / f"{bvid}.json"
        sub_path.write_text(
            json.dumps(sub_result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        bili_db.update_video(video["id"], {
            "subtitle_status": "extracted",
            "subtitle_path": str(sub_path),
        })

        # ── 通用总结 ──
        text_content = sub_result.get("text", "")
        if not text_content:
            bili_db.update_video(video["id"], {
                "subtitle_status": "empty",
                "summary_status": "skipped",
            })
            from backend.services.bilibili.quality import record_quarantine

            record_quarantine(bvid, video.get("title", ""), "no_text", 0,
                              note="字幕文件存在但没有文本")
            _write_l0_fallback(video, "no_text")   # P-09
            results.append({"bvid": bvid, "status": "no_text"})
            continue

        # ── 质量闸门（P-07 / D51 / D54）：字幕不可信 → 不入库，留人工验收 ──
        from backend.services.bilibili.quality import (
            record_quarantine, subtitle_off_topic, subtitle_too_short, topic_score,
        )

        if subtitle_too_short(text_content):
            record_quarantine(bvid, video.get("title", ""), "subtitle_too_short",
                              len(text_content.strip()))
            _write_l0_fallback(video, f"subtitle_too_short({len(text_content.strip())}字)")   # P-09
            bili_db.update_video(video["id"], {
                "subtitle_status": "quarantined",
                "summary_status": "skipped",
            })
            results.append({"bvid": bvid, "status": "quarantined",
                            "chars": len(text_content.strip())})
            BiliTaskManager._update_progress(task_id, f"{idx + 1}/{len(bvids)}")
            continue

        # 第二条闸门：字幕内容与标题主题是否相符（bge-reranker-v2-m3，标定见 quality.py）
        if subtitle_off_topic(video.get("title", ""), text_content) is True:
            score = topic_score(video.get("title", ""), text_content)
            record_quarantine(bvid, video.get("title", ""), "subtitle_off_topic",
                              len(text_content.strip()),
                              note=f"reranker 相关性 logit={score:.3f} < 0，判为字幕与主题不符")
            _write_l0_fallback(video, f"subtitle_off_topic(logit={score:.2f})")   # P-09
            bili_db.update_video(video["id"], {
                "subtitle_status": "quarantined",
                "summary_status": "skipped",
            })
            results.append({"bvid": bvid, "status": "quarantined",
                            "reason": "subtitle_off_topic", "logit": score})
            BiliTaskManager._update_progress(task_id, f"{idx + 1}/{len(bvids)}")
            continue

        try:
            summary = _summarize_subtitle(llm, text_content)
            bili_db.update_video(video["id"], {
                "summary_status": "summarized",
                "summary_text": summary,
            })
            # 官方标签（UP主手打）：失败不阻塞任何流程
            bili_tags: list[str] = []
            try:
                from backend.services.bilibili.collect import get_video_tags
                bili_tags = get_video_tags(bvid)
            except Exception:
                bili_tags = []
            # 标注：分类 + 仓库/文档链接抽取（失败不影响摘要结果）
            ann = None
            try:
                ann = _annotate_video(llm, {**video, "summary_text": summary}, bili_tags)
            except Exception:
                ann = None
            _apply_annotation(video["id"], ann)
            # 摘要晋升 dense/video（L1 高浓度知识库，供 RAG 检索）
            year = ""
            if video.get("pubdate"):
                year = str(datetime.fromtimestamp(video["pubdate"]).year)
            merged = _merge_tags(ann.get("tags") if ann else [], bili_tags)
            from backend.services.archive.service import promote_to_dense
            promote_to_dense("video", bvid, summary, {
                "title": video.get("title", ""),
                "authors": video.get("author", ""),
                "source": f"https://www.bilibili.com/video/{bvid}",
                "section": (ann or {}).get("category", ""),
                "year": year,
                "tags": merged,
            })
            results.append({
                "bvid": bvid,
                "status": "summarized",
                "chars": len(text_content),
                "map_reduced": len(text_content) > _SUMMARY_CHAR_LIMIT,
                "category": (ann or {}).get("category", ""),
                "repo_urls": (ann or {}).get("repo_urls", []),
            })
        except Exception as e:
            bili_db.update_video(video["id"], {"summary_status": "failed"})
            results.append({"bvid": bvid, "status": "summarize_failed", "error": str(e)})

        BiliTaskManager._update_progress(task_id, f"{idx + 1}/{len(bvids)}")

    return {"results": results, "total": len(bvids), "completed": len(results)}


def _run_query(task_id: str, video_ids: list[int], prompt: str,
               prompt_file_path: str, filter_mode: str, keyword: str) -> dict:
    """后台执行问答任务。"""
    from backend.config.settings import Settings
    from backend.core.llm.client import LLMClient

    settings = Settings()
    llm = settings.get_llm_client("glm-flash")

    # 1. 读取对应视频的 summary_text
    videos = [bili_db.get_video(vid) for vid in video_ids]
    videos = [v for v in videos if v and v.get("summary_text")]

    # 2. 筛选
    if filter_mode == "keyword" and keyword:
        kw = keyword.lower()
        filtered = [v for v in videos if kw in v["summary_text"].lower()]
    else:
        filtered = list(videos)
    # keyword_llm 模式：先标记 keyword 匹配的视频，让 LLM 自行选择
    keyword_matched = []
    if filter_mode == "keyword_llm" and keyword:
        kw = keyword.lower()
        keyword_matched = [v for v in videos if kw in v["summary_text"].lower()]

    # 记录最终使用的视频信息
    used_videos = [
        {"id": v["id"], "bvid": v["bvid"], "title": v.get("title", ""), "author": v.get("author", "")}
        for v in (keyword_matched if filter_mode == "keyword_llm" else filtered)
    ]

    if filter_mode == "keyword":
        videos = filtered
    # keyword_llm 模式：把 keyword_matched 的摘要放在前面，但保留全部
    # 这样 LLM 可以看到标记的重点部分

    if not videos:
        return {
            "result_text": "没有找到匹配的视频内容",
            "video_count": 0,
            "used_videos": [],
        }

    # 3. 组装 prompt
    system_prompt = _QUERY_SYSTEM_PROMPT or "你是一个视频内容分析助手。根据以下视频总结内容回答用户的问题。"
    user_content = f"用户问题：{prompt}\n\n"
    if keyword:
        mode_note = ""
        if filter_mode == "keyword":
            mode_note = "（以下视频均包含关键词，已全量使用）"
        elif filter_mode == "keyword_llm":
            mode_note = f"（以下列表中，标注 ★ 的视频包含关键词「{keyword}」，请优先参考）"
        user_content += f"筛选模式：{filter_mode}{mode_note}\n\n"
    user_content += "以下是相关视频的内容总结：\n\n"

    for v in videos:
        title = v.get("title", "未知标题")
        author = v.get("author", "未知作者")
        prefix = "★ " if keyword_matched and v in keyword_matched else "  "
        user_content += f"--- {prefix}{title} (UP主: {author}) ---\n{v['summary_text']}\n\n"

    # 如果启用了 prompt_file，追加文件内容
    if prompt_file_path:
        pf_path = Path(prompt_file_path)
        if pf_path.exists():
            extra = pf_path.read_text(encoding="utf-8")
            user_content += f"--- 附加提示词 ---\n{extra}\n"

    # 4. 调 LLM
    try:
        result = llm.call(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content[:16000]},
            ],
            max_tokens=4000,
            temperature=0.3,
        )
    except Exception as e:
        return {
            "result_text": f"LLM 调用失败: {e}",
            "video_count": len(videos),
            "used_videos": used_videos,
            "error": str(e),
        }

    return {
        "result_text": result,
        "video_count": len(videos),
        "used_videos": used_videos,
    }


# ═══════════════════════════════════════════════
# Service 类
# ═══════════════════════════════════════════════


class BilibiliService:

    # ── 采集 ──

    @staticmethod
    def check_url(url: str) -> dict:
        """检查视频 URL/ BVID，返回视频信息和合集信息。"""
        from backend.services.bilibili.collect import (
            extract_bvid, get_video_view, get_video_collection,
        )

        bvid = extract_bvid(url)
        if not bvid:
            return {"ok": False, "error": "无法解析 BVID"}

        # 检查是否已在数据库中
        existing = bili_db.get_video_by_bvid(bvid)
        already_in_db = existing is not None

        view_data = get_video_view(bvid)
        if not view_data:
            return {
                "ok": False, "error": "无法获取视频信息",
                "bvid": bvid, "already_in_db": already_in_db,
            }

        owner = view_data.get("owner", {}) or {}
        stat = view_data.get("stat", {}) or {}

        video_info = {
            "bvid": bvid,
            "title": view_data.get("title", ""),
            "author": owner.get("name", ""),
            "author_mid": owner.get("mid", 0),
            "duration": view_data.get("duration", 0),
            "pubdate": view_data.get("pubdate", 0),
            "pic": view_data.get("pic", ""),
            "description": view_data.get("desc", ""),
            "view": stat.get("view", 0),
            "like_count": stat.get("like", 0),
            "fav_count": stat.get("favorite", 0),
            "danmaku": stat.get("danmaku", 0),
            "reply": stat.get("reply", 0),
            "already_in_db": already_in_db,
        }

        # 检查合集
        collection_result = get_video_collection(bvid)
        collection = None
        if collection_result and collection_result.get("ok"):
            collection = {
                "season_id": collection_result.get("season_id"),
                "title": collection_result.get("title", ""),
                "count": collection_result.get("count", 0),
                "videos": collection_result.get("videos", []),
            }

        return {
            "ok": True,
            "video": video_info,
            "collection": collection,
        }

    @staticmethod
    def search_author(mid_input: str, count: int = 30) -> dict:
        """搜索作者视频（支持 MID 数字或完整 space URL）。"""
        from backend.services.bilibili.collect import (
            extract_mid, get_author_info, get_author_videos, get_author_stat,
        )

        mid = extract_mid(mid_input)
        if not mid:
            return {"ok": False, "error": f"无效的作者 MID: {mid_input}"}

        try:
            info = get_author_info(mid)
            stat = get_author_stat(mid)
            result = get_author_videos(mid, order="pubdate", page=1, page_size=min(count, 30))
        except Exception as e:
            return {"ok": False, "error": str(e)}

        videos = []
        for v in result.get("videos", []):
            existing = bili_db.get_video_by_bvid(v.get("bvid", ""))
            videos.append({
                "bvid": v.get("bvid", ""),
                "title": v.get("title", ""),
                "author": info.get("name", ""),
                "author_mid": mid,
                "play": v.get("play", 0),
                "duration_str": v.get("length", ""),
                "pubdate": v.get("pubdate", 0),
                "pic": v.get("pic", ""),
                "already_in_db": existing is not None,
            })

        return {
            "ok": True,
            "author": info,
            "stat": stat,
            "videos": videos,
        }

    @staticmethod
    def list_favorites() -> list[dict]:
        """获取收藏夹列表。"""
        from backend.services.bilibili.favorites import list_folders

        try:
            folders = list_folders()
            return [
                {"id": f["id"], "title": f["title"], "count": f.get("count", 0)}
                for f in folders
            ]
        except Exception as e:
            raise RuntimeError(f"获取收藏夹失败: {e}")

    @staticmethod
    def get_favorites_preview(fav_id: int, count: int = 0) -> dict:
        """获取收藏夹内视频预览。"""
        from backend.services.bilibili.favorites import get_videos

        try:
            fav_videos = get_videos(fav_id, max_items=count)
        except Exception as e:
            return {"ok": False, "error": str(e)}

        videos = []
        for v in fav_videos:
            existing = bili_db.get_video_by_bvid(v.get("bvid", ""))
            videos.append({
                "bvid": v["bvid"],
                "title": v.get("title", ""),
                "url": v.get("url", ""),
                "fav_time": v.get("fav_time", ""),
                "pubdate": v.get("pubdate", 0),
                "already_in_db": existing is not None,
            })

        return {"ok": True, "videos": videos, "total": len(videos)}

    @staticmethod
    def commit_videos(videos: list[dict]) -> dict:
        """批量提交视频到数据库（去重）。

        收藏夹来源的视频缺少作者/播放/时长信息，会自动调用 view API 补全。
        """
        import time as _time
        enriched = []
        for v in videos:
            if not v.get("author") and not v.get("view"):
                try:
                    from backend.services.bilibili.collect import get_video_view as _gvv
                    data = _gvv(v.get("bvid", ""))
                    if data:
                        owner = data.get("owner", {}) or {}
                        stat = data.get("stat", {}) or {}
                        v["author"] = owner.get("name", "")
                        v["author_mid"] = owner.get("mid", 0)
                        v["duration"] = data.get("duration", 0)
                        v["view"] = stat.get("view", 0)
                        v["like_count"] = stat.get("like", 0)
                        v["fav_count"] = stat.get("favorite", 0)
                        v["danmaku"] = stat.get("danmaku", 0)
                        v["reply"] = stat.get("reply", 0)
                        v["description"] = data.get("desc", "")
                        v["pic"] = data.get("pic", "")
                except Exception:
                    pass
            enriched.append(v)
        return bili_db.insert_videos_batch(enriched)

    @staticmethod
    def enrich_videos_batch(bvids: list[str]) -> dict:
        """对已入库但缺少信息的视频批量补全数据。"""
        import time as _time
        updated = 0
        failed = 0
        for bvid in bvids:
            video = bili_db.get_video_by_bvid(bvid)
            if not video:
                failed += 1
                continue
            try:
                from backend.services.bilibili.collect import get_video_view as _gvv
                data = _gvv(bvid)
                if data:
                    owner = data.get("owner", {}) or {}
                    stat = data.get("stat", {}) or {}
                    bili_db.update_video(video["id"], {
                        "author": owner.get("name", video.get("author", "")),
                        "author_mid": owner.get("mid", video.get("author_mid", 0)),
                        "duration": data.get("duration", video.get("duration", 0)),
                        "view": stat.get("view", video.get("view", 0)),
                        "like_count": stat.get("like", video.get("like_count", 0)),
                        "fav_count": stat.get("favorite", video.get("fav_count", 0)),
                        "danmaku": stat.get("danmaku", video.get("danmaku", 0)),
                        "reply": stat.get("reply", video.get("reply", 0)),
                        "description": data.get("desc", video.get("description", "")),
                        "pic": data.get("pic", video.get("pic", "")),
                    })
                    updated += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
            _time.sleep(0.3)
        return {"updated": updated, "failed": failed}

    # ── 提取 ──

    @staticmethod
    def extract_and_summarize(bvids: list[str]) -> str:
        """提交批量字幕提取+总结任务。

        Returns:
            任务 ID。
        """
        ordered = sorted(set(bvids))
        task_id = f"extract_{abs(hash(''.join(ordered))) % 10**8:08x}"
        BiliTaskManager.submit("extract", _run_extract, ordered, task_id=task_id)
        return task_id

    @staticmethod
    def get_extract_status(task_id: str) -> dict | None:
        """查询提取任务状态。"""
        return BiliTaskManager.get_status(task_id)

    @staticmethod
    def cancel_extract(task_id: str) -> bool:
        """取消提取任务。"""
        return BiliTaskManager.cancel(task_id)

    # ── 问答 ──

    @staticmethod
    def run_query(video_ids: list[int], prompt: str, prompt_file_path: str = "",
                  filter_mode: str = "keyword", keyword: str = "") -> int:
        """创建并提交问答任务。

        Returns:
            问答任务 ID（query_tasks 表的主键）。
        """
        task_id = bili_db.create_query_task({
            "user_prompt": prompt,
            "prompt_file_path": prompt_file_path,
            "filter_mode": filter_mode,
            "keyword": keyword,
            "video_ids": list(video_ids),
            "status": "running",
        })

        db_task_id = task_id  # 保存 DB 主键

        def _run_wrapper(tid: str) -> dict:  # tid 是 BiliTaskManager 的 ID
            result = _run_query(
                tid, list(video_ids), prompt, prompt_file_path,
                filter_mode, keyword,
            )
            # 将 used_videos 与结果文本一起存储（JSON 包裹）
            import json as _json
            stored_result = _json.dumps({
                "text": result.get("result_text", ""),
                "used_videos": result.get("used_videos", []),
            }, ensure_ascii=False)
            bili_db.update_query_task(db_task_id, {  # 用 DB 主键更新
                "status": "completed",
                "result_text": stored_result,
                "completed_at": "datetime('now')",
            })
            return result

        import uuid as _uuid
        bg_task_id = _uuid.uuid4().hex[:12]
        BiliTaskManager.submit("query", _run_wrapper, task_id=bg_task_id)
        return task_id

    @staticmethod
    def get_query_task(task_id: int) -> dict | None:
        return bili_db.get_query_task(task_id)

    @staticmethod
    def list_query_tasks() -> list[dict]:
        tasks = bili_db.get_query_tasks()
        return [t for t in tasks if t.get("status") != "deleted"]

    @staticmethod
    def delete_query_task(task_id: int) -> bool:
        return bili_db.update_query_task(task_id, {"status": "deleted"})

    # ── 通用管理 ──

    @staticmethod
    def get_videos(**filters) -> list[dict]:
        return bili_db.get_videos(**filters)

    @staticmethod
    def count_videos(**filters) -> int:
        return bili_db.count_videos(**filters)

    @staticmethod
    def delete_video(video_id: int, cleanup_files: bool = False) -> dict:
        """删除视频记录，可选清理关联文件。

        Returns:
            {"deleted": True, "bvid": "...", "cleanup": {...}} 或 {"deleted": False}
        """
        video = bili_db.delete_video(video_id)
        if not video:
            return {"deleted": False}

        result = {"deleted": True, "bvid": video["bvid"]}
        if cleanup_files:
            result["cleanup"] = bili_db.cleanup_video_files(video["bvid"])
        return result

    @staticmethod
    def batch_delete_video(video_ids: list[int], cleanup_files: bool = False) -> dict:
        """批量删除。"""
        results = []
        for vid in video_ids:
            results.append(BilibiliService.delete_video(vid, cleanup_files=cleanup_files))
        return {"deleted": sum(1 for r in results if r["deleted"]), "results": results}
