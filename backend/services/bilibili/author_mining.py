"""作者维度挖掘：从 bili.db 的标注结果聚合"宝藏作者"。

前置：视频已有 category（由 bili-annotate / 提取流程写入）。
产物：markdown + json 报告，按计算机相关视频数排序，聚合每个作者的
分类分布、代表视频和抽取到的仓库/文档链接。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

# 计算机相关类别（"其它"不计入）
CS_CATEGORIES = {"计算机理论", "编程技巧", "开源项目", "工具", "求职经验", "行业观察"}
TREASURE_MIN_CS = 3  # 计算机相关 >= 3 条视为宝藏作者


def _author_key(v: dict) -> tuple:
    mid = v.get("author_mid") or 0
    return (mid, v.get("author", ""))


def mine_authors(output_path: str | Path | None = None) -> Path:
    """聚合 bili.db 中已分类视频，产出作者挖掘报告。返回 md 路径。"""
    from api.services import bili_db

    videos = bili_db.get_videos()
    classified = [v for v in videos if (v.get("category") or "").strip()]
    if not classified:
        raise ValueError("bili.db 里没有已分类的视频——先跑提取流程或 bili-annotate")

    authors: dict[tuple, dict] = {}
    for v in classified:
        key = _author_key(v)
        a = authors.setdefault(key, {
            "author": v.get("author", ""),
            "mid": v.get("author_mid") or 0,
            "videos": [],
        })
        a["videos"].append(v)

    rows = []
    for a in authors.values():
        vids = a["videos"]
        cat_hist: dict[str, int] = {}
        for v in vids:
            cat_hist[v["category"]] = cat_hist.get(v["category"], 0) + 1
        cs_n = sum(n for c, n in cat_hist.items() if c in CS_CATEGORIES)
        repos = sorted({u for v in vids
                        for u in _load_json_list(v.get("repo_urls"))})
        best = max(vids, key=lambda v: v.get("view") or 0)
        rows.append({
            "author": a["author"],
            "mid": a["mid"],
            "total": len(vids),
            "cs_total": cs_n,
            "cat_hist": dict(sorted(cat_hist.items(), key=lambda x: -x[1])),
            "repos": repos,
            "avg_view": round(sum(v.get("view") or 0 for v in vids) / len(vids)),
            "best_video": {"title": best.get("title", ""), "view": best.get("view") or 0,
                           "bvid": best.get("bvid", "")},
        })
    rows.sort(key=lambda r: (-r["cs_total"], -r["avg_view"]))

    treasures = [r for r in rows if r["cs_total"] >= TREASURE_MIN_CS]

    out = Path(output_path) if output_path else Path("data/bili_servey/author_mining.md")
    out.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# B站作者挖掘报告",
        "",
        f"> 生成时间 {datetime.now():%Y-%m-%d %H:%M} · "
        f"数据范围：bili.db 已标注视频 {len(classified)} 条 / 共 {len(videos)} 条 · "
        f"作者 {len(rows)} 位 · 宝藏作者（计算机相关 ≥{TREASURE_MIN_CS} 条）{len(treasures)} 位",
        "",
    ]
    if treasures:
        lines += ["## 宝藏作者", "",
                  "| 作者 | mid | 计算机相关/总视频 | 分类分布 | 平均播放 | 仓库/文档 |",
                  "|---|---|---|---|---|---|"]
        for r in treasures:
            hist = "、".join(f"{c}×{n}" for c, n in r["cat_hist"].items())
            repos = "<br>".join(r["repos"][:4]) + (f" 等{len(r['repos'])}个" if len(r["repos"]) > 4 else "")
            lines.append(f"| {r['author']} | {r['mid']} | {r['cs_total']}/{r['total']} "
                         f"| {hist} | {r['avg_view']:,} | {repos or '—'} |")
        lines.append("")
    else:
        lines += ["（暂无达到宝藏阈值的作者——标注量还太少，先多提取几个视频）", ""]

    lines += ["## 全部作者（按计算机相关数排序）", "",
              "| # | 作者 | 计算机相关/总视频 | 分类分布 | 代表作（播放最高） |",
              "|---|---|---|---|---|"]
    for i, r in enumerate(rows, 1):
        hist = "、".join(f"{c}×{n}" for c, n in r["cat_hist"].items())
        lines.append(f"| {i} | {r['author']} | {r['cs_total']}/{r['total']} | {hist} "
                     f"| {r['best_video']['title'][:30]} ({r['best_video']['view']:,}) |")

    out.write_text("\n".join(lines), encoding="utf-8")
    out.with_suffix(".json").write_text(
        json.dumps({"generated_at": datetime.now().isoformat(timespec="seconds"),
                    "total_classified": len(classified),
                    "treasure_min_cs": TREASURE_MIN_CS,
                    "authors": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    return out


def _load_json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return [str(x) for x in v] if isinstance(v, list) else []
    except (json.JSONDecodeError, TypeError):
        return []
