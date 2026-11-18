# -*- coding: utf-8 -*-
"""把 data/bilibili/visual/<bv>/digest.md 落进知识库，并把标注同步进 bili.db。

一次落库做四件事：
1. 写 bili.db：视频行（标题/简介/摘要）+ 官方标签 + 七类分类 + repo_urls；
2. 补 frontmatter：authors / section(分类) / year / tags(我的方向标签 ∪ 主题词 ∪ 官方标签)；
3. 查重：和库里其它条目比，重复度高就跳过（--force 才写）；
4. 拷贝用到的截图到 dense/_frames/<bv>/。

用法:
  python apply_kb.py BV1FGpM62ENu --dry     # 只查重，不写
  python apply_kb.py --all                  # 写全部有 digest 的
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path


from .paths import VISUAL_ROOT as ROOT, DENSE_DIR as DENSE, SUB_DIR as SUB
from .paths import kb_id, split_kb_id

# 我这边按"用户关注方向"打的标签 → bili.db 的七类分类（找不到对应就归"其它"）
CATEGORY_MAP = {
    "agent架构": "计算机理论",
    "方法论": "编程技巧",
    "工具与产品": "工具",
    "就业与求职": "求职经验",
    "课程与资料": "其它",
    "踩坑经验": "编程技巧",
    "其它": "其它",
}


def _shingles(text: str, k: int = 5) -> set[str]:
    t = re.sub(r"\s+", "", text)
    return {t[i:i + k] for i in range(max(0, len(t) - k + 1))}


def similarity(a: str, b: str) -> float:
    sa, sb = _shingles(a), _shingles(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _body(text: str) -> str:
    """去掉 frontmatter 和标题行，只比正文。"""
    return "\n".join(ln for ln in text.splitlines()
                     if not ln.startswith(("---", "id:", "title:", "source:", "tags:",
                                           "slides:", "sections:", "# ")))


def existing(bv: str) -> str:
    p = DENSE / f"{bv}.md"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def dup_report(bv: str, text: str) -> list[tuple[str, float]]:
    """和库里所有条目比一遍，返回 [(文件名, 相似度)] 降序。"""
    out = []
    for p in DENSE.glob("*.md"):
        if p.stem == bv:
            continue
        s = similarity(_body(text), _body(p.read_text(encoding="utf-8")))
        if s >= 0.25:
            out.append((p.name, round(s, 2)))
    return sorted(out, key=lambda x: -x[1])


def owner_of(bv: str) -> str:
    p = SUB / f"{bv}.json"
    if not p.exists():
        return ""
    d = json.loads(p.read_text(encoding="utf-8"))
    return (d.get("owner") or {}).get("name", "")


def _copy_images(bv: str) -> int:
    """把这条视频用上的截图放到 dense/_frames/<bv>/，md 里的相对路径才打得开。"""
    src = ROOT / bv / "images_used"
    if not src.exists():
        return 0
    dst = DENSE / "_frames" / bv
    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)
    dst.mkdir(parents=True, exist_ok=True)
    n = 0
    for p in src.glob("*.jpg"):
        shutil.copy2(p, dst / p.name)
        n += 1
    return n


def _fm_split(text: str) -> tuple[dict, str]:
    """拆 frontmatter：返回 (字段字典, 正文)。"""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    head, body = text[3:end], text[end + 4:]
    meta = {}
    for line in head.splitlines():
        m = re.match(r"^([a-z_]+):\s*(.*)$", line.strip())
        if m:
            meta[m.group(1)] = m.group(2).strip().strip('"')
    return meta, body.lstrip("\n")


def _fm_dump(meta: dict, body: str) -> str:
    order = ["id", "title", "source", "authors", "source_type", "visual",
             "section", "year", "slides", "images", "sections", "tags", "bili_tags"]
    keys = [k for k in order if k in meta] + [k for k in meta if k not in order]
    lines = ["---"]
    for k in keys:
        v = meta[k]
        if isinstance(v, list):
            v = "[" + ", ".join(str(x) for x in v) + "]"
        lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body.lstrip("\n")


def _tag_clean(t: str) -> str:
    """kb 的解析器按空白切标签，"Agentic RAG" 会被拆成两个词；这里先粘成 AgenticRAG。"""
    return re.sub(r"\s+", "", str(t or "")).strip()


def _tag_list(raw) -> list[str]:
    if isinstance(raw, list):
        return [str(x) for x in raw]
    return [x.strip() for x in str(raw or "").strip("[]").split(",") if x.strip()]


def sync_bili_db(bv: str, meta: dict, summary: str) -> dict:
    """把这条视频写进 bili.db，并补齐官方标签 / 七类分类 / repo_urls。

    bv 传 kb_id：P1 就是 bvid；P2+ 是 "BVxxx_pN"（PLAN.md 3.3 的乙方案——表结构
    不动，bvid 列暂存 kb_id，行用 source_detail='multipart:PN' 标记，等甲方案确认
    后再迁到复合键）。官方标签接口只认真 bvid，用 split_kb_id 还原。
    """
    from api.services import bili_db

    real_bv, page = split_kb_id(bv)
    bili_db.init_db()
    info = {}
    p = SUB / f"{bv}.json"
    if p.exists():
        info = json.loads(p.read_text(encoding="utf-8"))

    my_tags = _tag_list(meta.get("tags", ""))
    data = {
        "title": meta.get("title", bv),
        "author": (info.get("owner") or {}).get("name", ""),
        "author_mid": (info.get("owner") or {}).get("mid", 0) or 0,
        "duration": int(info.get("duration") or 0),
        "pubdate": int(info.get("pubdate") or 0),
        "pic": info.get("pic", ""),
        "description": (info.get("desc") or "")[:2000],
        "summary_status": "summarized",
        "summary_text": summary[:6000],
        "subtitle_path": str(p) if p.exists() else "",
        "source_type": "video",
    }
    if page > 1:
        data["source_detail"] = f"multipart:P{page}"
    row = bili_db.get_video_by_bvid(bv)
    if row:
        bili_db.update_video(row["id"], data)
        vid = row["id"]
    else:
        vid = bili_db.insert_video({"bvid": bv, **data})
    row = bili_db.get_video_by_bvid(bv) or {}

    # 1) 官方标签（UP 主投稿时打的）——接口只认真 bvid，P2+ 用还原出来的
    bili_tags = _load_list(row.get("bili_tags"))
    if not bili_tags:
        try:
            from backend.services.bilibili.collect import get_video_tags
            bili_tags = get_video_tags(real_bv) or []
            if bili_tags:
                bili_db.update_video(vid, {"bili_tags": json.dumps(bili_tags, ensure_ascii=False)})
        except Exception as e:
            print(f"   官方标签拉取失败: {str(e)[:60]}")

    # 2) 分类 + 主题标签 + 仓库链接（复用现成标注器，官方标签作信号）
    category = (row.get("category") or "").strip()
    topic_tags = _load_list(row.get("tags"))
    repo_urls = _load_list(row.get("repo_urls"))
    if not category:
        try:
            from api.services.bilibili_service import _annotate_video, _merge_tags
            from backend.config.settings import Settings
            ann = _annotate_video(Settings().get_llm_client("glm-flash"),
                                  {"title": data["title"], "description": data["description"],
                                   "summary_text": data["summary_text"]}, bili_tags)
            if ann and ann.get("category"):
                category = ann["category"]
                topic_tags = ann.get("tags") or []
                repo_urls = ann.get("repo_urls") or []
                bili_db.update_video(vid, {
                    "category": category,
                    "tags": json.dumps(_merge_tags(topic_tags, my_tags, cap=12),
                                       ensure_ascii=False),
                    "repo_urls": json.dumps(repo_urls, ensure_ascii=False),
                })
        except Exception as e:
            print(f"   标注失败: {str(e)[:60]}")
    if not category:                       # 标注器失败时用我的方向标签兜底
        category = next((CATEGORY_MAP[t] for t in my_tags if t in CATEGORY_MAP), "其它")

    return {"category": category, "topic_tags": topic_tags,
            "bili_tags": bili_tags, "repo_urls": repo_urls,
            "my_tags": my_tags, "pubdate": data["pubdate"]}


def apply_one(bv: str, *, dry: bool = False, force: bool = True,
              min_dup: float = 0.45, skip_db: bool = False,
              page: int | None = None) -> bool:
    """落一条：bili.db 标注 → frontmatter 补全 → 写 md → 拷图。

    bv 两种传法：普通 bvid（配 page=N 指第几集），或直接传 kb_id
    （"BVxxx_p2"，bili-visual-apply CLI 就这么传）。P1 落点与旧版完全一致。
    """
    real_bv, suf = split_kb_id(bv)
    page = suf if suf > 1 else (page or 1)
    kid = kb_id(real_bv, page)
    src = ROOT / kid / "digest.md"
    if not src.exists():
        print(f"{kid}: 没有 digest.md")
        return False
    meta, body = _fm_split(src.read_text(encoding="utf-8"))
    if not meta.get("authors"):
        author = owner_of(kid)
        if author:
            meta["authors"] = author

    if not dry and not skip_db:
        ann = sync_bili_db(kid, meta, body[:4000])
        meta["section"] = ann["category"]
        if ann.get("pubdate"):
            meta["year"] = str(datetime.fromtimestamp(ann["pubdate"]).year)
        merged = []
        for t in (ann["my_tags"] + (ann.get("topic_tags") or [])
                  + (ann.get("bili_tags") or [])):
            t = _tag_clean(t)
            if t and t not in merged:
                merged.append(t)
        meta["tags"] = merged[:12]
        if ann.get("bili_tags"):
            meta["bili_tags"] = [_tag_clean(t) for t in ann["bili_tags"][:8]]
        print(f"   bili.db: {ann['category']} | 标签 {len(meta['tags'])} 个"
              f" | 链接 {len(ann.get('repo_urls') or [])} 条")

    text = _fm_dump(meta, body)

    if dry:
        print(f"{kid}: [dry] 会写 {len(text)} 字 -> {DENSE / (kid + '.md')}")
        return True
    DENSE.mkdir(parents=True, exist_ok=True)
    (DENSE / f"{kid}.md").write_text(text, encoding="utf-8")
    n_img = _copy_images(kid)
    print(f"{kid}: 已写入 {DENSE / (kid + '.md')}（{len(text)} 字，图 {n_img} 张）")
    return True


def _load_list(raw) -> list[str]:
    if not raw:
        return []
    try:
        v = json.loads(raw) if isinstance(raw, str) else raw
        return [str(x) for x in v] if isinstance(v, list) else []
    except Exception:
        return _tag_list(raw)


def prepare(bv: str) -> tuple[Path, str]:
    src = ROOT / bv / "digest.md"
    text = src.read_text(encoding="utf-8")
    author = owner_of(bv)
    if author and "authors:" not in text:
        text = text.replace("\nsource_type:", f"\nauthors: {author}\nsource_type:", 1)
    return src, text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bvids", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--min-dup", type=float, default=0.45)
    ap.add_argument("--skip-backlog", action="store_true")
    ap.add_argument("--skip-db", action="store_true", help="只写文件和图，不动 bili.db")
    a = ap.parse_args()

    bvs = a.bvids or ([p.name for p in ROOT.iterdir()
                       if p.is_dir() and (p / "digest.md").exists()] if a.all else [])
    if not bvs:
        print("没有要处理的 bv")
        return

    written = skipped = 0
    for bv in bvs:
        src = ROOT / bv / "digest.md"
        if not src.exists():
            print(f"{bv}: 没有 digest.md")
            continue
        text = src.read_text(encoding="utf-8")
        old = existing(bv)
        if old and not a.force:
            s = similarity(_body(text), _body(old))
            # 老条目多半是"只读字幕"版，新的是"看图"版，内容不重合很正常；
            # 直接覆盖会丢掉老条目里口播的流程/细节，所以默认不覆盖，只报告。
            print(f"{bv}: 库里已有（正文相似度 {s:.2f}），默认不覆盖；"
                  f"要替换加 --force")
            skipped += 1
            continue
        dups = dup_report(bv, text)
        if dups:
            top = dups[0]
            print(f"{bv}: 和其它条目最高相似度 {top[1]}（{top[0]}）")
            if top[1] >= a.min_dup and not a.force:
                print(f"  超过阈值 {a.min_dup}，可能是重复内容，跳过（--force 可强写）")
                skipped += 1
                continue
        if apply_one(bv, dry=a.dry, force=True, skip_db=a.skip_db):
            written += 1

    print(f"\n写入 {written}，跳过 {skipped}")

    if not a.dry and not a.skip_backlog:
        from . import resources
        resources.apply_staged()


if __name__ == "__main__":
    main()
