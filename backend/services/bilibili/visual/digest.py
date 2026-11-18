# -*- coding: utf-8 -*-
"""把一条视频压成知识条目，写 VISUAL_ROOT/<bv>/digest.md（并留下用到的截图）。

**先按字幕切块，再让画面当证据**：
1. 读完整字幕，让模型按原片讲述顺序切成若干论述块（一个问题、一个案例、一种方案、一段总结）；
2. 每块单独写：该块的字幕全文 + 落在该时间段里的截图（有就发图，没有就只读字幕）；
3. 用上的截图存到 VISUAL_ROOT/<bv>/images_used/，md 里用相对路径嵌进去。

这样"某一段没截到图"只是少一张图，不会整段消失。

用法: python digest.py BV1Kppn6tEem [--thr 0]
      python digest.py BV1Kppn6tEem --text-only     # 画面已判定无用，只读字幕
"""
import argparse
import base64
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

import requests

from . import seg_filter  # noqa: F401  (装 tempfile 补丁)
from . import pipeline as P  # noqa: E402
from .llm import no_think  # noqa: E402
from backend.config.settings import Settings  # noqa: E402

from .paths import VISUAL_ROOT as ROOT, kb_id, subtitle_cache_path

# 用户明确不要的"入门书内容"：git 基本命令、命令行操作
BASIC_RE = re.compile(
    r"git\s+(add|commit|log|status|init|restore|clone|clean)\b"
    r"|Win\+R|Ctrl\+C|复制粘贴", re.I)

TAGS = "agent架构 / 工具与产品 / 就业与求职 / 课程与资料 / 方法论 / 踩坑经验 / 其它"

AUDIENCE = "读者是做 AI / Agent 工程的技术人。他看到一个标题就收藏，关注技术本身，**不打算买课**。"

OUTLINE_PROMPT = """这是一条 B 站技术视频的完整字幕（ASR 转写，有谐音错字）。

按**原片讲述顺序**把全片切成若干块，供逐块整理成笔记。规矩：
1. 每块是一个完整的论述单元：一个争议、一个案例、一种方案、一类盲区、一段总结；
   不要按句子切，也不要把好几件事塞进一块。
2. 首尾相接覆盖全片（第一块从 0 开始，最后一块到片尾），块数 4~20。
3. 标题 6~14 字，要能看出这一块在讲什么，不要用"第一部分""内容介绍"这种空标题。
4. 全片都在寒暄/卖课时可以只给 1 块。块数 4~10（10 分钟以内的视频 4~8 块）。

字幕：
{subs}

只输出 JSON：
{{"blocks":[{{"t0":0,"t1":33,"topic":"开场的争议与双方观点"}}]}}"""

BLOCK_PROMPT = """你在整理一条 B 站技术视频的**一个论述块**（{t0}~{t1}）。{audience}

这一块里讲者说的全部内容（ASR 转写，有谐音错字）：
{sub}

{visual}

按**讲者讲述的顺序**把这一块写成知识条目正文：

1. 讲者分了几点、举了什么例子、给了什么数字和结论，按原顺序写清楚，一个都不许漏。
2. 有画面时：画面上的文字/结构要并进来（清单逐条抄，**绝不概括成"共 N 个"**），
   与讲者的口头解释对应上；画面只是证据，正文的主线仍然是讲述顺序。
3. 错字要改：ASR 的谐音错字按上下文纠正（埃吉特→Agent、外边code→Vibe Coding、
   黑暗大魔学→大模型）。**英文产品名没把握就别改**，宁可保留原词。已知写法：
   DeepSeek Harness、Vibe Coding、Agent、RAG、MCP、Streamlit。
4. 只写这一块里有的，不要编，不要预告后面的内容。
5. 不写入门常识（git 命令、怎么装软件、什么是 API key）。
6. 长度 150~500 字，信息多就长，别注水。

tag 从这些里选一个：{tags}

只输出 JSON：
{{"used":2, "tag":"方法论", "title":"6~14 字小标题",
 "body":"markdown 正文，分点用有序列表"}}

（used：这一块对技术读者有用程度 0~3，0=纯寒暄/卖课/广告，1=泛泛而谈，2=要点，3=方案/数据/结论）
{ocr}"""

ABS_PROMPT = """下面是一条视频按时间顺序整理出的各节标题和要点摘录。用 2~3 句话概括：
这条视频在讲什么、对技术读者最有用的是哪一块。
只有视频里确实出现卖课、报价、报名、引流时，才加一句"本质上是课程/产品推销"；没有就别提。
不要评价好坏，不要罗列小节。

{sections}"""

EDIT_PROMPT = """下面的条目来自一条技术视频，读者是做 AI / Agent 工程的技术人，**他不打算买课**。
逐条判断该不该留：

- 整条在讲讲者或公司的资历、成绩、荣誉、团队规模、服务过多少客户 → 删
- 整条在卖课、报价、优惠、报名、扫码、加群、引流 → 删
- 标题是"讲师介绍""关于我""履历""课程价格"之类 → 删
- 条目里既有履历/广告又夹着技术内容：把履历和广告的句子删掉，只留下技术部分，返回改写后的 body
- 其它一律保留

输入：
{items}

只输出 JSON：
{{"items":[{{"i":0,"drop":true}},{{"i":1,"drop":false,"body":"改写后的正文；不用改就不写 body"}}]}}"""


def fmt_t(s: float) -> str:
    return f"{int(s) // 60:02d}:{int(s) % 60:02d}"


def _json_obj(txt: str) -> dict:
    start = (txt or "").find("{")
    if start < 0:
        return {}
    try:
        obj, _ = json.JSONDecoder().raw_decode(txt[start:])
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _post(client, content, max_tokens=6000, temperature=0.2, attempts=5) -> str:
    payload = {"model": client.model, "max_tokens": max_tokens, "temperature": temperature,
               "messages": [{"role": "user", "content": content}]}
    for i in range(attempts):
        try:
            r = requests.post(client.endpoint,
                              headers={"Authorization": "Bearer " + client.api_key,
                                       "Content-Type": "application/json"},
                              json=payload, timeout=300)
            m = r.json()["choices"][0]["message"]
            txt = (m.get("content") or "").strip() or (m.get("reasoning_content") or "").strip()
            if txt:
                return txt
        except Exception as e:
            print(f"   重试{i+1}: {str(e)[:60]}")
            time.sleep(4 * (i + 1))      # 连接被重置时别硬撞，退避一下
    return ""


def _text_keys() -> tuple[str, ...]:
    """纯文字块用的模型链：`VISUAL_TEXT_MODEL` 指定的优先，v4flash 兜底。

    默认（不设环境变量）就是 v4flash，与旧行为一致。
    """
    pref = (os.environ.get("VISUAL_TEXT_MODEL") or "").strip()
    return tuple(dict.fromkeys(([pref] if pref else []) + ["v4flash"]))


def _text_call(prompt: str, max_tokens: int = 6000, temperature: float = 0.2) -> str:
    """纯文字块走文本模型，首选失败自动退到兜底模型。

    视觉模型（glm-4.6v）处理一块带图的内容要 40~90 秒，而绝大多数块根本没有图
    （14 条实测只有 2 条真用上截图）。同一段提示词交给 flash 类文本模型只要几秒。

    glm 家的 flash 默认开思考，会把 max_tokens 全烧在 reasoning 上、正文为空，
    所以对 glm-* 显式关掉思考（不开的话整条视频的块都会退化成空块）。
    """
    last = ""
    for key in _text_keys():
        try:
            client = Settings().get_llm_client(key)
            extra = no_think(key)
            txt = client.call([{"role": "user", "content": prompt}],
                              max_tokens=max_tokens, temperature=temperature,
                              extra_body=extra) or ""
            if txt.strip():
                return txt
            last = f"{key}: 空正文"
        except Exception as e:
            last = f"{key}: {str(e)[:70]}"
    if last:
        print(f"   文本模型失败: {last}")
    return ""


def _img_part(path: Path) -> dict:
    b64 = base64.b64encode(path.read_bytes()).decode()
    return {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}}


def outline(segs: list[dict], title: str = "") -> list[dict]:
    """按讲述逻辑把全片切块（覆盖全片、时间升序）。没有字幕就不切。"""
    if not segs:
        return []
    subs = "\n".join(f"[{int(s['start'])}] {s['text']}" for s in segs)[:24000]
    txt = _text_call(OUTLINE_PROMPT.format(subs=subs), 4000, 0.2)
    obj = _json_obj(txt)
    blocks = [b for b in obj.get("blocks", [])
              if isinstance(b.get("t0"), (int, float)) and b.get("t1", 0) > b.get("t0", 0)]
    if not blocks:
        return []
    blocks.sort(key=lambda b: float(b["t0"]))
    end = max(s["end"] for s in segs)
    for i, b in enumerate(blocks):
        b["t0"] = max(0.0, float(b["t0"]))
        b["t1"] = min(float(b["t1"]), end)
        if i + 1 < len(blocks):          # 首尾相接，别留缝也别叠
            b["t1"] = float(blocks[i + 1]["t0"])
    blocks[-1]["t1"] = end
    return [b for b in blocks if b["t1"] - b["t0"] >= 5]


def _split_inline_list(body: str) -> str:
    """模型常把有序列表写成一行（"1. xx 2. yy"或"…。2. yy"），拆回多行才好读。"""
    if "\n" in body:
        return body
    body = re.sub(r"(?<=\S)\s+(?=\d+[.、]\s)", "\n", body)
    body = re.sub(r"(?<=[。；：！？])\s*(?=\d+[.、]\s*\S)", "\n", body)
    return body


def _pick_frames(frames: list[dict], t0: float, t1: float, limit: int = 3) -> list[dict]:
    got = [f for f in frames if t0 - 2 <= f["t"] <= t1 + 2]
    if len(got) <= limit:
        return got
    step = len(got) / limit
    return [got[int(i * step)] for i in range(limit)]


def _dedup_speech(body: str) -> str:
    """同一句"讲者说"被贴到好几条上时，只留第一条。"""
    seen: set[str] = set()
    out = []
    for line in body.splitlines():
        m = re.match(r"^\s*讲者说[:：]\s*(.+)$", line)
        if m:
            key = re.sub(r"\s+", "", m.group(1))[:40]
            if key in seen:
                continue
            seen.add(key)
        out.append(line)
    return "\n".join(out)


def _norm_md(s: str) -> str:
    s = re.sub(r"(?m)^\s*\d+[.、)]\s*", "", s or "")
    return re.sub(r"[\s\-#*>`|·:：（）()\[\]【】]+", "", s)


def _same_section(sec: dict, title: str, body: str) -> bool:
    from .dedup_pages import text_sim
    if title and text_sim(sec.get("title", ""), title) >= 0.8:
        return True
    return text_sim(_norm_md(sec.get("body", "")), _norm_md(body)) >= 0.55


def editor_pass(sections: list[dict]) -> list[dict]:
    """文字审一遍：删讲者履历/卖课/广告条目，或把夹在里面的广告句子剪掉。"""
    items = [{"i": i, "title": s.get("title", ""), "body": s["body"][:800]}
             for i, s in enumerate(sections)]
    try:
        resp = _text_call(EDIT_PROMPT.format(
            items=json.dumps(items, ensure_ascii=False)), 6000, 0.1)
    except Exception as e:
        print("  审稿跳过:", str(e)[:80])
        return sections
    obj = _json_obj(resp)
    verdict = {int(x["i"]): x for x in obj.get("items", []) if "i" in x}
    if not verdict:
        print("  审稿没给结论，原样保留")
        return sections

    def n_items(b: str) -> int:
        return len(re.findall(r"(?m)^\s*\d+[.、)]", b))

    out = []
    for i, s in enumerate(sections):
        v = verdict.get(i, {})
        if v.get("drop"):
            print(f"  审稿删掉「{s.get('title','')}」")
            continue
        nb = str(v.get("body") or "").strip()
        if nb and len(nb) > 40:
            if n_items(nb) < n_items(s["body"]):
                print(f"  审稿改写丢了清单条目，保持原文「{s.get('title','')}」")
            elif nb[-1] not in "。；；:)）】\"'`":
                print(f"  审稿改写疑似截断，保持原文「{s.get('title','')}」")
            else:
                s = {**s, "body": nb}
                print(f"  审稿精简「{s.get('title','')}」→ {len(nb)} 字")
        out.append(s)
    return out


def digest_one(bvid: str, thr="0", refresh: bool = False, page: int = 1) -> dict | None:
    """逐块整理一条视频。没有保留帧时退化成只读字幕（visual: none）。

    page=1 的目录名/id/source/multipart 与旧版逐字节一致（kb_id 对 P1 是恒等映射）。
    """
    kid = kb_id(bvid, page)
    rep, frames = {}, []
    rp = ROOT / kid / f"report_thr{thr}.json"
    if rp.exists():
        rep = json.loads(rp.read_text(encoding="utf-8"))
        frames = rep.get("keep_frames") or []
    if not rep:
        sp = ROOT / kid / "sections.json"
        rep = json.loads(sp.read_text(encoding="utf-8")) if sp.exists() else {"title": bvid}

    segs = P.load_segments(bvid, page)
    if not segs:
        # 分块是按字幕的讲述顺序切的，没字幕就无从切起（画面驱动分块是另一件事，见待定）
        print("  没有可用字幕，跳过浓缩（不入库）")
        return None
    ocr = {}
    op = ROOT / kid / f"ocr_thr{thr}.json"
    if op.exists():
        ocr = json.loads(op.read_text(encoding="utf-8"))
    kd = ROOT / kid / f"frames_keep_thr{thr}"
    img_dir = ROOT / kid / "images_used"
    shutil.rmtree(img_dir, ignore_errors=True)
    img_dir.mkdir(parents=True, exist_ok=True)

    blocks = outline(segs, rep.get("title", ""))
    if not blocks:
        print("  切块失败")
        return None
    print(f"  切成 {len(blocks)} 块")

    vclient = Settings().get_llm_config("glm-4.6v_ocr")   # 带图的块：视觉模型
    # 纯文字的块走 _text_call（默认 v4flash，可用 VISUAL_TEXT_MODEL 换成 glm-flash 等）
    # 每块结果落盘：网络断了重跑时只补缺的块，不用从头再来
    cache_path = ROOT / kid / "blocks_cache.json"
    cache = {}
    if cache_path.exists() and not refresh:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))

    # 每帧只归一个块：块边界上的帧会被相邻两块都算进去，同一张图出现两遍
    owner: dict[str, int] = {}
    for i, b in enumerate(blocks):
        for f in _pick_frames(frames, float(b["t0"]), float(b["t1"])):
            owner.setdefault(f["file"], i)

    sections = []
    for bi, b in enumerate(blocks):
        t0, t1 = float(b["t0"]), float(b["t1"])
        imgs = [f for f in frames if owner.get(f["file"]) == bi]
        imgs = imgs[:3]
        # 缓存键带上"这块有没有图"：以后补画面重跑时，只有带图的块会重写，纯字幕块直接复用
        ck = f"{t0:.0f}-{t1:.0f}-{'img' if imgs else 'txt'}"
        sub = P.local_subs(segs, t0, t1)[:2600]
        visual, parts = "这一块**没有**截到画面，只根据字幕写。", []
        used_imgs = []
        if imgs:
            visual = ("这一块有 %d 张画面（按时间排序，图里是讲者当时展示的内容），"
                      "画面上看到的文字和结构要并进正文。" % len(imgs))
            for f in imgs:
                p = kd / f["file"]
                if p.exists():
                    parts.append(_img_part(p))
                    used_imgs.append(f)
        ocr_txt = "\n".join((ocr.get(f"{f['t']:.2f}s") or {}).get("text", "") for f in used_imgs)
        ocr_hint = f"\n画面上 OCR 到的文字（有错字，以图为准）：\n{ocr_txt[:1200]}" if ocr_txt else ""

        prompt = BLOCK_PROMPT.format(
            t0=fmt_t(t0), t1=fmt_t(t1), audience=AUDIENCE, sub=sub,
            visual=visual, tags=TAGS, ocr=ocr_hint)
        t_start = time.time()
        obj = cache.get(ck) or {}
        if not obj.get("body"):
            if parts:
                raw = _post(vclient, parts + [{"type": "text", "text": prompt}], 6000, 0.2)
            else:
                raw = _text_call(prompt, 6000, 0.2)
            obj = _json_obj(raw)
            if not obj.get("body"):     # 偶尔吐空，补一次狠一点的
                retry = prompt + "\n\n注意：正文不能为空，至少 100 字，按讲述顺序把这一块的要点写全。"
                if parts:
                    obj = _json_obj(_post(vclient, parts + [{"type": "text", "text": retry}], 6000, 0.3))
                else:
                    obj = _json_obj(_text_call(retry, 6000, 0.3))
            if obj.get("body"):
                cache[ck] = obj
                cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
        else:
            print(f"  {fmt_t(t0)} 用缓存")
        if not obj.get("body"):
            print(f"  {fmt_t(t0)} 空块，跳过")
            continue
        body = _split_inline_list(_dedup_speech(str(obj["body"]).strip()))
        title = obj.get("title") or b.get("topic", "")
        dup = next((s for s in sections if _same_section(s, title, body)), None)
        if dup:
            if len(body) > len(dup["body"]):
                dup["body"] = body
                dup["images"] = dup.get("images", []) + used_imgs
            print(f"  {fmt_t(t0)} 与「{dup['title']}」重复，合并")
            continue
        sections.append({"t": t0, "t1": t1, "title": title,
                         "tag": obj.get("tag", ""), "body": body,
                         "used": obj.get("used", 2), "images": used_imgs})
        print(f"  {fmt_t(t0)}-{fmt_t(t1)} [{obj.get('tag','')}] {title} "
              f"{len(body)} 字 图{len(used_imgs)} ({time.time() - t_start:.0f}s)")

    if not sections:
        print("没有产出")
        return None
    sections = editor_pass(sections)
    if not sections:
        print("审稿后什么都不剩")
        return None

    # 用上的图拷出来，md 里按相对路径引用
    for s in sections:
        keep = []
        for f in s.get("images", []):
            src = kd / f["file"]
            if src.exists():
                shutil.copy2(src, img_dir / f["file"])
                keep.append(f)
        s["images"] = keep

    brief = "\n".join(f"- {s['title']}：{s['body'][:80]}" for s in sections)
    abs_resp = _text_call(ABS_PROMPT.format(sections=brief), 500, 0.3)

    visual_tag = "slides" if frames else "none"
    tags = []
    for s in sections:
        if s.get("tag") and s["tag"] not in tags:
            tags.append(s["tag"])
    n_img = sum(len(s["images"]) for s in sections)
    # 多P：每集各成一条（P1 不带后缀，P2+ 的 id 带 _pN），multipart 记"第几集/共几集"，
    # 免得单集条目看起来像"收全了"。P1 的字符串与旧版一致（1/{pc}）。
    multipart = ""
    _cache = subtitle_cache_path(bvid, page)
    if _cache.exists():
        try:
            pc = int(json.loads(_cache.read_text(encoding="utf-8")).get("page_count") or 1)
        except Exception:
            pc = 1
        if pc > 1:
            multipart = f"multipart: {page}/{pc}\n"
    src_url = (f"https://www.bilibili.com/video/{bvid}"
               + (f"?p={page}" if page > 1 else ""))
    head = [f"---\nid: {kid}\ntitle: {rep.get('title', bvid)}\n"
            f"source: \"{src_url}\"\n"
            f"source_type: video\nvisual: {visual_tag}\nslides: {len(frames)}\n"
            f"images: {n_img}\nsections: {len(sections)}\n{multipart}"
            f"tags: [{', '.join(tags)}]\n---\n",
            f"# {rep.get('title', bvid)}\n",
            f"> {abs_resp.strip()}\n"]
    if not frames:
        head.append("\n> 画面：整条视频没有整页 PPT/架构图，以下内容全部来自字幕。\n")

    body = []
    for i, s in enumerate(sections, 1):
        body.append(f"\n## {i}. {s['title']}（{fmt_t(s['t'])}）· {s.get('tag', '')}\n")
        for f in s["images"]:
            body.append(f"![{fmt_t(f['t'])}](_frames/{kid}/{f['file']})\n")
        body.append(s["body"].strip() + "\n")
    text = "\n".join(head + body)
    text = "\n".join(ln for ln in text.splitlines() if not BASIC_RE.search(ln))
    out = ROOT / kid / "digest.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"{len(sections)} 节 / {len(text)} 字 / {n_img} 张图 -> {out}")
    return {"path": out, "sections": sections, "chars": len(text),
            "images": n_img, "visual": visual_tag}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bvid")
    ap.add_argument("--thr", default="0")
    ap.add_argument("--text-only", action="store_true", help="忽略截图，只读字幕")
    ap.add_argument("--refresh", action="store_true", help="丢掉块缓存，全部重写")
    a = ap.parse_args()
    if a.text_only:
        # 用一个不存在的 thr 名，读不到 report 就退化成纯字幕
        digest_one(a.bvid, "none", a.refresh)
    else:
        digest_one(a.bvid, a.thr, a.refresh)


if __name__ == "__main__":
    main()
