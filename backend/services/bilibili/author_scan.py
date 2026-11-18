"""收藏夹锚点 → 作者维度宝藏扫描。

口径（2026-10-28 用户澄清）：判断标准是**作者发布的视频**里计算机类 > threshold，
不是收藏夹里的浓度——用户的收藏是"锚点式"的：有价值的作者只随手存一两条当入口，
之后顺着作者/合集自行拓宽，不会反复收藏。

流程：
  1. 取收藏夹最新 anchors 条视频作为作者锚点（复用 24h 全量缓存的前 N 条）
  2. 去重出候选作者；默认漏斗=锚点视频本身是计算机类（--mids 指定时跳过漏斗）
  3. 每位作者：拉其发布的视频列表（按播放降序，Top max_pages×30 条）
     → 流式分类标题并累计计算机类数，> threshold 即达标停止分类
  4. 达标作者取播放 Top-N 标题 → AI 确认具体领域 + 3-5 个代表作
  5. 结果按 mid 增量并入注册表 data/bili_servey/treasure_authors.{json,md}

标签全局缓存 data/bili_servey/_title_labels.json（bvid→类别）：
跨收藏夹/作者复用，中断可续、重扫免费。

用法：
  python -m backend.main bili-author-scan --fav-id 51087974                # 最新150条锚点全扫
  python -m backend.main bili-author-scan --fav-id 51087974 --mids 325864133,12890453   # 只测指定作者
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from backend.services.bilibili.favorites import get_videos as _get_fav_videos

_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "pipelines" / "prompts" / "bili"
DEFAULT_REGISTRY = Path("data/bili_servey/treasure_authors.json")
_LABELS_CACHE = Path("data/bili_servey/_title_labels.json")

# 计算机相关类别（七类体系中"其它"不计入）
CS_CATEGORIES = {"计算机理论", "编程技巧", "开源项目", "工具", "求职经验", "行业观察"}


# ═══════════════════════════════════════════════
# 标题分类（全局标签缓存 + 批量 LLM）
# ═══════════════════════════════════════════════

def _load_prompt(name: str) -> str:
    return (_PROMPTS_DIR / name).read_text(encoding="utf-8").strip()


def _load_label_cache() -> dict[str, str]:
    """全局 bvid→类别 缓存；顺带把旧版按收藏夹分的 _fav_labels_*.json 迁移合并。"""
    cache: dict[str, str] = {}
    if _LABELS_CACHE.exists():
        try:
            cache = json.loads(_LABELS_CACHE.read_text(encoding="utf-8")).get("labels", {})
        except (json.JSONDecodeError, OSError):
            cache = {}
    for old in _LABELS_CACHE.parent.glob("_fav_labels_*.json"):
        try:
            legacy = json.loads(old.read_text(encoding="utf-8")).get("labels", {})
        except (json.JSONDecodeError, OSError):
            continue
        merged = sum(1 for k, v in legacy.items() if v and k not in cache)
        cache.update({k: v for k, v in legacy.items() if v})
        old.rename(old.with_suffix(".json.migrated"))
        if merged:
            print(f"  [迁移] {old.name} → 全局缓存（并入 {merged} 条）")
    return cache


def _save_label_cache(cache: dict[str, str]) -> None:
    _LABELS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    _LABELS_CACHE.write_text(
        json.dumps({"labels": cache}, ensure_ascii=False), encoding="utf-8")


def _classify_batch(llm, retry_call, batch: list[dict]) -> list[str] | None:
    """对一批视频（dict，含 title）做 LLM 分类。失败返回 None。"""
    system = _load_prompt("classify_titles.txt")
    numbered = "\n".join(f"{i + 1}. {v['title']}" for i, v in enumerate(batch))
    user = f"共 {len(batch)} 条标题，labels 数组长度必须是 {len(batch)}：\n\n{numbered}"
    for _ in range(2):
        try:
            raw = retry_call(llm, [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ], max_tokens=2000, temperature=0.1)
        except RuntimeError:
            return None
        try:
            obj = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
            got = obj.get("labels")
            if isinstance(got, list) and len(got) == len(batch):
                return [str(x) for x in got]
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def _classify_titles(llm, retry_call, videos: list[dict], batch_size: int = 20) -> dict[str, str]:
    """全量分类（带缓存）。返回 {bvid: 类别}，单批失败降级'其它'。"""
    cache = _load_label_cache()
    todo = [v for v in videos if v["bvid"] not in cache]
    if cache:
        hit = sum(1 for v in videos if v["bvid"] in cache)
        print(f"  标签缓存命中 {hit} 条，待分类 {len(todo)} 条")
    for start in range(0, len(todo), batch_size):
        batch = todo[start:start + batch_size]
        labels = _classify_batch(llm, retry_call, batch)
        if labels is None:
            labels = ["其它"] * len(batch)
            print(f"  [warn] 第 {start}-{start + len(batch)} 批分类失败，按'其它'")
        for v, lab in zip(batch, labels):
            cache[v["bvid"]] = lab
        _save_label_cache(cache)
        print(f"  分类进度: {min(start + batch_size, len(todo))}/{len(todo)}", flush=True)
    return {v["bvid"]: cache[v["bvid"]] for v in videos}


def _cs_count_author(llm, retry_call, videos: list[dict], threshold: int,
                     batch_size: int = 20) -> tuple[int, int]:
    """流式统计作者视频里的计算机类数量：累计 > threshold 即达标停止。

    Returns:
        (计算机类数, 实际抽样标题数)
    """
    cache = _load_label_cache()
    cs = sum(1 for v in videos if cache.get(v["bvid"]) in CS_CATEGORIES)
    done = sum(1 for v in videos if v["bvid"] in cache)
    todo = [v for v in videos if v["bvid"] not in cache]
    if cs > threshold:
        return cs, done
    for start in range(0, len(todo), batch_size):
        batch = todo[start:start + batch_size]
        labels = _classify_batch(llm, retry_call, batch)
        if labels is None:
            labels = ["其它"] * len(batch)
        for v, lab in zip(batch, labels):
            cache[v["bvid"]] = lab
        cs += sum(1 for lab in labels if lab in CS_CATEGORIES)
        done += len(batch)
        _save_label_cache(cache)
        print(f"      已分类 {done} 条，计算机类累计 {cs}", flush=True)
        if cs > threshold:
            break
    return cs, done


# ═══════════════════════════════════════════════
# 收藏夹原始数据缓存（24h）
# ═══════════════════════════════════════════════

def _load_or_pull_raw(fav_id: int, max_age_hours: float = 24) -> list[dict]:
    """收藏夹原始数据缓存：24h 内复用，避免反复全量拉取触发 B 站 412 风控。"""
    cache = Path(f"data/bili_servey/_fav_raw_{fav_id}.json")
    if cache.exists():
        try:
            obj = json.loads(cache.read_text(encoding="utf-8"))
            pulled = datetime.fromisoformat(obj["pulled_at"])
            age_h = (datetime.now() - pulled).total_seconds() / 3600
            if obj.get("videos") and age_h < max_age_hours:
                print(f"  使用缓存：{obj['pulled_at']} 拉取的 {len(obj['videos'])} 条"
                      f"（{age_h:.1f}h 前后，{max_age_hours}h 内有效；删缓存文件可强制刷新）")
                return obj["videos"]
        except (json.JSONDecodeError, KeyError, ValueError):
            pass
    raw = _get_fav_videos(fav_id)
    if raw:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(
            {"pulled_at": datetime.now().isoformat(timespec="seconds"), "videos": raw},
            ensure_ascii=False), encoding="utf-8")
        print(f"  已缓存 {len(raw)} 条 → {cache}")
    return raw


# ═══════════════════════════════════════════════
# 作者领域确认
# ═══════════════════════════════════════════════

def _confirm_author(llm, retry_call, author: str, vids: list[dict]) -> dict:
    """对达标作者确认领域与代表作。返回注册表条目（不含扫描元信息）。"""
    system = _load_prompt("author_domain_confirm.txt")
    listing = "\n".join(
        f"- title: {v['title']} | bvid: {v['bvid']} | play: {v.get('play', 0) or v.get('view', 0)}"
        for v in vids)
    raw = retry_call(llm, [
        {"role": "system", "content": system},
        {"role": "user", "content": f"UP主：{author}\n\n{listing}"},
    ], max_tokens=2000, temperature=0.2)

    entry = {"domain": "未确认", "confidence": 0, "note": "", "representative": []}
    try:
        obj = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
    except (json.JSONDecodeError, ValueError):
        obj = {}
    by_bvid = {v["bvid"]: v for v in vids}
    reps = []
    for r in (obj.get("representative") or []):
        v = by_bvid.get(str(r.get("bvid", "")))   # 只认输入清单里的 bvid，防编造
        if v:
            reps.append({"title": v["title"], "bvid": v["bvid"],
                         "url": f"https://www.bilibili.com/video/{v['bvid']}",
                         "play": v.get("play", 0) or v.get("view", 0)})
    if len(reps) < 3:   # 兜底：按播放补齐到 3 个
        chosen = {r["bvid"] for r in reps}
        for v in vids:
            if len(reps) >= 3:
                break
            if v["bvid"] not in chosen:
                reps.append({"title": v["title"], "bvid": v["bvid"],
                             "url": f"https://www.bilibili.com/video/{v['bvid']}",
                             "play": v.get("play", 0) or v.get("view", 0)})
    entry.update({
        "domain": str(obj.get("domain", "未确认"))[:40],
        "confidence": int(obj.get("confidence") or 0),
        "note": str(obj.get("note", ""))[:200],
        "representative": reps[:5],
    })
    return entry


# ═══════════════════════════════════════════════
# 注册表
# ═══════════════════════════════════════════════

def _merge_registry(registry: dict, entries: list[dict]) -> int:
    """按 mid 增量合并：已存在的更新内容、保留首次确认时间。返回新增数。"""
    known = {e["mid"]: e for e in registry.get("authors", [])}
    added = 0
    for e in entries:
        old = known.get(e["mid"])
        if old:
            e["first_confirmed_at"] = old.get("first_confirmed_at") or e["scanned_at"]
        else:
            e["first_confirmed_at"] = e["scanned_at"]
            added += 1
        known[e["mid"]] = e
    registry["authors"] = sorted(known.values(), key=lambda x: -(x.get("cs_count") or 0))
    return added


def _render_md(registry: dict) -> str:
    lines = [
        "# 宝藏作者注册表",
        "",
        f"> 口径：作者发布的视频（按播放 Top 抽样）中计算机类 > {registry.get('threshold', '?')} · "
        f"最近扫描：{registry.get('last_scan', '?')} · "
        f"收录 {len(registry.get('authors', []))} 位作者",
        "",
    ]
    for e in registry.get("authors", []):
        lines += [
            f"## {e['author']}（mid={e['mid']}）— {e['domain']}（置信度 {e['confidence']}/10）",
            "",
            f"- 计算机类视频：抽样 {e.get('sampled_n', '?')} 条中 {e['cs_count']} 条"
            f"（作者共发布 {e.get('author_total', '?')} 个视频）",
        ]
        if e.get("anchor"):
            lines.append(f"- 收藏锚点：{e['anchor']}")
        if e.get("note"):
            lines.append(f"- {e['note']}")
        lines.append("- 代表视频：")
        for r in e.get("representative", []):
            lines.append(f"  - [{r['title']}]({r['url']})（播放 {r['play']:,}）")
        lines.append("")
    return "\n".join(lines)


# ═══════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════

def scan_authors_from_favorites(
    fav_id: int,
    anchors: int = 150,
    mids: list[int] | None = None,
    threshold: int = 20,
    max_pages: int = 5,
    top_n: int = 30,
    batch_size: int = 20,
    output: str | Path | None = None,
    model_key: str = "glm-flash",
) -> Path:
    """从收藏锚点出发，按"作者发布的视频"维度扫描宝藏作者。

    Args:
        anchors: 取收藏夹最新 N 条作为锚点（用户口径：最新150条=近几年的引入）。
        mids: 只扫指定作者（测试用）；None=扫全部候选。
        threshold: 作者发布的视频里计算机类 > N 才算宝藏。
        max_pages: 每作者最多拉多少页视频（30条/页，按播放降序抽样）。
    """
    from backend.config.settings import Settings
    from api.services.bilibili_service import _llm_call_retry_empty
    from backend.services.bilibili.collect import get_all_author_videos, get_author_stat

    llm = Settings().get_llm_client(model_key)

    print(f"[1/4] 取收藏夹 {fav_id} 最新 {anchors} 条作为作者锚点...")
    raw = _load_or_pull_raw(fav_id)[:anchors]
    if not raw:
        raise ValueError(f"收藏夹 {fav_id} 没有视频")

    # 去重出候选作者（保留每位作者最新的一条锚点）
    by_author: dict[int, dict] = {}
    for v in raw:
        mid = v.get("upper_mid") or 0
        if mid and mid not in by_author:
            by_author[mid] = {"mid": mid, "name": v.get("upper_name") or str(mid),
                              "anchor_title": v["title"]}
    print(f"    锚点去重出 {len(by_author)} 位候选作者")

    # 锚点漏斗：锚点视频本身是计算机类的作者才进入发布视频扫描
    # （用户收藏是锚点式的：有价值的作者才会被存，锚点非计算机→作者大概率非技术号）
    if mids:
        candidates = [(m, by_author[m]) for m in mids if m in by_author]
        missing = [m for m in mids if m not in by_author]
        if missing:
            print(f"    [提示] 这些 mid 不在锚点里，直接照扫: {missing}")
            candidates += [(m, {"mid": m, "name": str(m), "anchor_title": ""}) for m in missing]
        print(f"    指定测试模式：只扫 {len(candidates)} 位")
    else:
        print(f"    锚点漏斗：先分类 {len(raw)} 条锚点标题...")
        label_map = _classify_titles(llm, _llm_call_retry_empty, raw, batch_size)
        candidates = [(mid, info) for mid, info in by_author.items()
                      if label_map.get(_anchor_bvid(raw, mid)) in CS_CATEGORIES]
        print(f"    锚点为计算机类的作者：{len(candidates)} / {len(by_author)} 位")

    print(f"[2/4] 逐作者拉取发布视频（按播放 Top{max_pages * 30} 抽样）并计数计算机类...")
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    qualified: list[tuple[dict, list[dict], int, int, int]] = []  # (info, videos, cs, sampled, total)
    for i, (mid, info) in enumerate(candidates, 1):
        print(f"  ({i}/{len(candidates)}) {info['name']} (mid={mid})...", flush=True)
        try:
            stat = get_author_stat(mid) or {}
            videos = get_all_author_videos(mid, order="click", max_pages=max_pages)
        except Exception as e:
            print(f"      拉取失败: {e}")
            continue
        if not videos:
            print(f"      没拉到视频列表（风控或空号），跳过")
            time.sleep(1)
            continue
        cs, sampled = _cs_count_author(llm, _llm_call_retry_empty, videos, threshold, batch_size)
        total = stat.get("video") or len(videos)
        verdict = "达标 ✓" if cs > threshold else f"未达标（{cs}≤{threshold}）"
        print(f"      {verdict}：计算机类 {cs} / 抽样 {sampled} / 作者共 {total} 个视频")
        if cs > threshold:
            qualified.append((info, videos, cs, sampled, total))
        time.sleep(1)  # 作者间节流，space API 有风控

    print(f"[3/4] 达标 {len(qualified)} 位，逐位确认领域...")
    entries = []
    for info, videos, cs, sampled, total in qualified:
        try:
            entry = _confirm_author(llm, _llm_call_retry_empty,
                                    info["name"], videos[:top_n])
        except Exception as e:
            print(f"    {info['name']} 确认失败: {e}")
            continue
        entry.update({
            "author": info["name"], "mid": info["mid"],
            "cs_count": cs, "sampled_n": sampled, "author_total": total,
            "anchor": info.get("anchor_title", ""), "scanned_at": now,
        })
        entries.append(entry)
        print(f"    {info['name']}: {entry['domain']}（置信度 {entry['confidence']}）")

    print("[4/4] 写入注册表...")
    out = Path(output) if output else DEFAULT_REGISTRY
    out.parent.mkdir(parents=True, exist_ok=True)
    registry = {"authors": []}
    if out.exists():
        try:
            registry = json.loads(out.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"  [warn] 注册表损坏，重建: {out}")
    registry.update({"threshold": threshold, "last_scan": now})
    added = _merge_registry(registry, entries)
    out.write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8")
    md = out.with_suffix(".md")
    md.write_text(_render_md(registry), encoding="utf-8")

    print(f"\n扫描完成：达标 {len(qualified)} 位，本次写入 {len(entries)} 位"
          f"（新增 {added}）；注册表共 {len(registry['authors'])} 位 → {md}")
    return md


def _anchor_bvid(raw: list[dict], mid: int) -> str | None:
    for v in raw:
        if v.get("upper_mid") == mid:
            return v["bvid"]
    return None
