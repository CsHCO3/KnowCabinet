# -*- coding: utf-8 -*-
"""元信息检索 + 单视频速查。

- `bili-find`：在 data/bili_servey 攒下的几千条"只有元信息"的视频上做关键词检索，
  挑出 candidate 再交给 `bili-visual-scan` 做深度解析（画面+字幕→浓缩→入库）。
- `bili-info`：给定 BV，一次拿到元信息 + 字幕概况 +（可选）画面抽样。

两者都不调用 LLM，纯本地，秒级返回。
"""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path

SURVEY_DIR = Path(__file__).resolve().parents[3] / "data" / "bili_servey"
CACHE_FILE = SURVEY_DIR / "_detail_cache.json"


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def _add(corpus: dict, bvid: str, rec: dict) -> None:
    if not bvid or not bvid.startswith("BV"):
        return
    cur = corpus.setdefault(bvid, {"bvid": bvid, "title": "", "url": "",
                                   "author": "", "desc": "", "tags": "",
                                   "view": 0, "duration": 0, "pubdate": 0})
    for k, v in rec.items():
        if v in (None, "", 0) or k in cur and cur[k] not in ("", 0, None):
            continue
        cur[k] = v


def _walk(obj, out: list) -> None:
    """递归找带 bvid 的字典。"""
    if isinstance(obj, dict):
        if obj.get("bvid"):
            out.append(obj)
        for v in obj.values():
            _walk(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk(v, out)


def _from_dict(d: dict) -> dict:
    return {
        "title": _norm(d.get("title")),
        "url": d.get("url") or f"https://www.bilibili.com/video/{d['bvid']}",
        "author": _norm(d.get("upper_name") or d.get("author") or d.get("up")),
        "desc": _norm(d.get("desc") or d.get("description")),
        "tags": _norm(d.get("tags")),
        "view": int(d.get("view") or 0),
        "duration": int(float(d.get("duration") or 0)),
        "pubdate": int(d.get("pubdate") or 0),
    }


def load_corpus() -> dict[str, dict]:
    """把 bili_servey 里所有元信息合成 bvid → 记录。"""
    corpus: dict[str, dict] = {}
    if not SURVEY_DIR.exists():
        return corpus
    for p in SURVEY_DIR.rglob("*"):
        if p.suffix.lower() not in (".json", ".csv"):
            continue
        try:
            if p.suffix.lower() == ".json":
                if p == CACHE_FILE:
                    continue
                buf: list = []
                _walk(json.loads(p.read_text(encoding="utf-8-sig")), buf)
                for d in buf:
                    _add(corpus, d["bvid"], _from_dict(d))
            else:
                with p.open(encoding="utf-8-sig", newline="") as f:
                    for row in csv.DictReader(f):
                        bv = (row.get("bvid") or row.get("BV号") or "").strip()
                        _add(corpus, bv, _from_dict(row))
        except Exception:
            continue
    # 详情缓存里有 desc/tags，覆盖到同一批 bvid 上
    if CACHE_FILE.exists():
        try:
            detail = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            for bv, d in detail.items():
                if isinstance(d, dict):
                    _add(corpus, bv, _from_dict({**d, "bvid": bv}))
        except Exception:
            pass
    return corpus


def _score(rec: dict, toks: list[str]) -> tuple[int, list[str]]:
    """关键词打分：标题命中 5 分，标签 3 分，简介 2 分，作者 2 分。多个词取交集。"""
    title = rec["title"].lower()
    tags = rec["tags"].lower()
    desc = rec["desc"].lower()
    author = rec["author"].lower()
    score, where, hit = 0, [], 0
    for t in toks:
        s = 0
        if t in title:
            s += 5
            where.append("标题")
        if t in tags:
            s += 3
            where.append("标签")
        if t in desc:
            s += 2
            where.append("简介")
        if t in author:
            s += 2
            where.append("作者")
        if s:
            hit += 1
            score += s
    if hit < len(toks):          # 必须每个词都命中
        return 0, []
    return score, sorted(set(where))


def search(query: str, limit: int = 20, min_view: int = 0,
           sort: str = "relevance") -> list[dict]:
    toks = [t for t in re.split(r"[\s,，、]+", _norm(query).lower()) if t]
    if not toks:
        return []
    out = []
    for rec in load_corpus().values():
        score, where = _score(rec, toks)
        if not score or rec["view"] < min_view:
            continue
        out.append({**rec, "score": score, "where": where})
    if sort == "view":
        out.sort(key=lambda r: (-r["view"], -r["score"]))
    else:
        out.sort(key=lambda r: (-r["score"], -r["view"]))
    return out[:limit]


def info(bvid: str) -> dict:
    """单视频速查：元信息 + 字幕概况。bvid/URL 可带 ?p=N 指定分P。"""
    from backend.services.bilibili.subtitle import (
        _extract_bvid,
        _parse_page,
        get_bilibili_timestamped_subtitle,
    )

    base = _extract_bvid(bvid or "") or (bvid or "").split("?")[0]
    r = get_bilibili_timestamped_subtitle(f"https://www.bilibili.com/video/{base}",
                                          page=_parse_page(bvid))
    segs = r.get("segments") or []
    out = {
        "bvid": base,
        "ok": bool(r.get("ok")),
        "error": r.get("error", ""),
        "title": r.get("title", ""),
        "author": (r.get("owner") or {}).get("name", ""),
        "duration": r.get("duration", 0),
        "pubdate": r.get("pubdate", 0),
        "desc": (r.get("desc") or "")[:300],
        "stat": {k: (r.get("stat") or {}).get(k) for k in ("view", "like", "favorite", "reply")},
        "page": r.get("page", 1),
        "page_count": r.get("page_count", 1),
        "page_title": r.get("page_title", ""),
        "pages": [
            {"cid": pp.get("cid"), "duration": pp.get("duration"), "part": pp.get("part", "")}
            for pp in (r.get("pages") or [])
        ],
        "subtitle": {
            "segments": len(segs),
            "covers": round(max((s["end"] for s in segs), default=0), 1),
            "header": [s["text"] for s in segs[:3]],
        } if segs else {},
    }
    return out
