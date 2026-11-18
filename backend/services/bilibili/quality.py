"""入库质量闸门（P-07 / D51）。

背景：`BV1Ghpy6gE5x` 只抓到 48 个字符（B站 AI 字幕串成了《三国演义》张飞片段），
流水线照旧让 LLM 生成摘要并晋升 `dense/video`，把假知识写进了检索库。

闸门规则（2026-10-19 库主裁定）：
1. 字幕正文 < `MIN_SUBTITLE_CHARS` 字 → **不生成摘要、不晋升 dense**，记入
   `data/videos/quarantine.json` 等人工验收；
2. 无字幕/抓取失败 → 同样只留待验记录（用户："不入库，之后让人工验"）。

关于"主题词自检"（曾计划加）：早期用"摘要 3-gram 在字幕中的占比"衡量**区分不出来**
——13 条正常样本落在 0.287–0.756，而张飞那条幻觉是 0.278，正好在正常区间内。

**2026-10-19 换判据：用 bge-reranker-v2-m3 给 (标题, 字幕) 打相关性分**，实测可分：
13 条真视频 logit 落在 +2.65 ~ +5.96，把标题和别的视频字幕错配（构造的"字幕与主题无关"）
落在 −8.42 ~ −3.53，中间空出 6.2 个 logit。故新增第二条闸门：
`subtitle_off_topic()` —— 分数低于 `TOPIC_MIN_LOGIT`（0.0，模型自身的判定边界）即判为
"字幕与主题不符"，与"太短"一样走人工验收。
模型缺失/无 GPU 时**静默跳过**（返回 None），不阻断入库。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
# 视频隔离名单属于视频流水线状态，跟着视频数据走（data/bilibili/）
QUARANTINE_PATH = PROJECT_ROOT / "data" / "bilibili" / "quarantine.json"
from backend.config import deployment

RERANK_MODEL_DIR = Path(os.getenv("RERANK_MODEL_DIR") or PROJECT_ROOT / ".models" / "bge-reranker-v2-m3")

MIN_SUBTITLE_CHARS = 200
# 标定见上：真视频最低 +2.65，错配最高 −3.53，取模型判定边界 0 作阈值
TOPIC_MIN_LOGIT = 0.0
# 字幕末尾截断长度：分数只用开头一段算，省显存也够判断主题
TOPIC_TEXT_CHARS = 800

_reranker_cache: dict = {"tokenizer": None, "model": None, "failed": False}
_TAG_CACHE: dict[str, list[str]] = {}


def _load_reranker():
    """惰性加载本地 reranker；不可用返回 None（调用方跳过该闸门）。"""
    if _reranker_cache["failed"]:
        return None
    if _reranker_cache["model"] is not None:
        return _reranker_cache["model"]
    if os.environ.get("KB_TOPIC_GATE", "1") in ("0", "false", "False"):
        _reranker_cache["failed"] = True
        return None
    if not (RERANK_MODEL_DIR / "config.json").is_file():
        _reranker_cache["failed"] = True
        return None
    try:
        import torch  # noqa: F401
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(str(RERANK_MODEL_DIR))
        model = AutoModelForSequenceClassification.from_pretrained(str(RERANK_MODEL_DIR))
        device = "cuda" if torch.cuda.is_available() else "cpu"
        model = model.to(device).eval()
        _reranker_cache.update(tokenizer=tokenizer, model=model, device=device)
        return model
    except Exception:  # noqa: BLE001 — 环境缺依赖/显存不足都退化为"不检查"
        _reranker_cache["failed"] = True
        return None


def topic_score(title: str, text: str) -> float | None:
    """(标题, 字幕) 相关性 logit；模型不可用时返回 None。"""
    text = (text or "").strip()[:TOPIC_TEXT_CHARS]
    title = (title or "").strip()
    if not title or not text:
        return None
    if _load_reranker() is None:
        return None
    import torch

    tokenizer = _reranker_cache["tokenizer"]
    model = _reranker_cache["model"]
    try:
        encoded = tokenizer(title, text, return_tensors="pt", truncation=True,
                            max_length=512).to(_reranker_cache["device"])
        with torch.no_grad():
            logits = model(**encoded).logits.view(-1)
        return float(logits[0])
    except Exception:  # noqa: BLE001
        return None


def subtitle_off_topic(title: str, text: str, *, scorer=None,
                       min_logit: float = TOPIC_MIN_LOGIT) -> bool | None:
    """字幕是否与标题/主题不符。返回 None = 判据不可用（不阻断入库）。

    `scorer` 供单测注入，签名 `(title, text) -> float|None`。
    """
    fn = scorer or topic_score
    score = fn(title, text)
    if score is None:
        return None
    return score < min_logit


def subtitle_too_short(text: str | None) -> bool:
    """字幕正文是否短到不可信（含空）。"""
    return len((text or "").strip()) < MIN_SUBTITLE_CHARS


def _flatten_tags(raw) -> list[str]:
    """标签来源格式不统一：有 list、有 `'["a", "b"]'` 字符串、有逗号/顿号串，统一拆平。"""
    out: list[str] = []
    for item in raw or []:
        s = str(item).strip()
        if not s:
            continue
        if s.startswith("[") and s.endswith("]"):
            try:
                val = json.loads(s)
                if isinstance(val, list):
                    out.extend(str(x).strip() for x in val if str(x).strip())
                    continue
            except (json.JSONDecodeError, TypeError):
                pass
            s = s.strip("[]").replace('"', "").replace("'", "")
        out.extend(x.strip() for x in s.replace("、", ",").replace("，", ",").split(",")
                   if x.strip())
    seen, uniq = set(), []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def _tags_of(bvid: str) -> list[str]:
    """取视频官方标签：本地 meta 卡 → bili.db → 现问接口（都取不到返回空）。

    本地优先是因为这道闸在流水线最前面，绝大多数视频在 L0 元信息里已经存过标签，
    不必为判主题再多打一次 B 站接口。
    """
    if bvid in _TAG_CACHE:
        return _TAG_CACHE[bvid]
    meta = PROJECT_ROOT / "knowledgebase" / "community" / "bilibili" / "meta" / f"{bvid}.json"
    tags: list[str] = []
    try:
        payload = json.loads(meta.read_text(encoding="utf-8"))
        tags = _flatten_tags(payload.get("tags"))
    except (OSError, json.JSONDecodeError):
        tags = []
    if not tags:
        try:
            import sqlite3
            conn = sqlite3.connect(PROJECT_ROOT / "data" / "bili.db")
            row = conn.execute("SELECT bili_tags FROM videos WHERE bvid=?", (bvid,)).fetchone()
            conn.close()
            tags = _flatten_tags([(row or [""])[0] or ""])
        except Exception:  # noqa: BLE001
            tags = []
    if not tags:
        try:
            from backend.services.bilibili.collect import get_video_tags
            tags = _flatten_tags(get_video_tags(bvid) or [])
        except Exception:  # noqa: BLE001
            tags = []
    _TAG_CACHE[bvid] = tags
    return tags


def video_topic(bvid: str, title: str = "", limit: int = 8) -> str:
    """把"这条视频是什么"拼成一句判据：标题 + 官方标签。

    标签比标题更能说明主题（`BV1sKbS6WEvK` 的标题是"为什么加了索引，SQL 反而更慢了？"，
    标签里直接写着 数据库索引/SQL/数据库优化）。取不到标签就只用标题。
    """
    t = (title or "").split(" ｜ 标签")[0].strip()
    tags = _tags_of(bvid)[:limit]
    if not tags:
        return t
    return f"{t} ｜ 标签：{'、'.join(tags)}"


JUDGE_PROMPT = """下面是一条 B 站视频的标题与官方标签，以及流水线替它抓到的字幕片段（开头和结尾）。

判断这段字幕**是不是这条视频的**。以下三种都算"不是"：
1. 字幕其实是歌曲歌词（片头片尾 / 背景音乐被 ASR 当成台词）；
2. 字幕来自另一条完全无关的节目（张冠李戴，最常见于多轨视频选错轨）；
3. 字幕内容与标题和标签都毫无交集（标题讲 AI，字幕在讲珊瑚保育）。

标题模糊但字幕与标签里的主题对得上时，算"是"。

只输出 JSON，不要解释：
{{"same": true 或 false, "why": "20 字以内"}}

标题与标签：{title}

字幕开头：
{head}

字幕结尾：
{tail}"""


def _json_first(text: str) -> dict:
    start = (text or "").find("{")
    if start < 0:
        return {}
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[start:])
        return obj if isinstance(obj, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def judge_subtitle_llm(title: str, text: str, *, model_key: str = "glm-flash",
                       judge=None, head_chars: int = 900,
                       tail_chars: int = 300) -> tuple[bool | None, str]:
    """让 LLM 判断"字幕是不是这条视频的"。返回 (是/否/判不了, 理由)。

    比 reranker 读得懂"张冠李戴"和"歌词"这两种 reranker 容易判反的情况，
    但要多花一次调用、而且模型可能偷懒，所以只当复核用。
    `judge` 供单测注入，签名 `(prompt) -> str`。
    """
    body = (text or "").strip()
    if not body or not (title or "").strip():
        return None, "缺标题或字幕"
    prompt = JUDGE_PROMPT.format(
        title=title.strip(), head=body[:head_chars],
        tail=body[-tail_chars:] if len(body) > head_chars else "")
    try:
        if judge is not None:
            raw = judge(prompt) or ""
        else:
            from backend.config.settings import Settings
            client = Settings().get_llm_client(model_key)
            extra = {"thinking": {"type": "disabled"}} if model_key.startswith("glm") else None
            raw = client.call([{"role": "user", "content": prompt}],
                              max_tokens=200, temperature=0, extra_body=extra) or ""
    except Exception as e:  # noqa: BLE001
        return None, f"判不了：{str(e)[:60]}"
    obj = _json_first(raw)
    if "same" not in obj:
        return None, "模型没给结论"
    same = bool(obj.get("same"))
    return same, str(obj.get("why") or "")[:40]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _load() -> dict:
    try:
        payload = json.loads(QUARANTINE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"entries": []}
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
        return {"entries": []}
    return payload


def record_quarantine(bvid: str, title: str = "", reason: str = "",
                      subtitle_chars: int = 0, note: str = "") -> dict:
    """把一条"不入库、待人工验收"的记录落盘（按 bvid 去重，保留最近一次）。"""
    payload = _load()
    entries = [entry for entry in payload["entries"]
               if isinstance(entry, dict) and entry.get("bvid") != bvid]
    entry = {
        "bvid": bvid,
        "title": title,
        "reason": reason,
        "subtitle_chars": int(subtitle_chars),
        "note": note,
        "recorded_at": _now(),
    }
    entries.append(entry)
    entries.sort(key=lambda item: item.get("recorded_at") or "", reverse=True)
    payload["entries"] = entries
    QUARANTINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    QUARANTINE_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return entry


def list_quarantine() -> list[dict]:
    return list(_load()["entries"])
