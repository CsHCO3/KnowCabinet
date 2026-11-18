#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把本地档案切成「适合 AI 分析」的批次，并逐批调用 LLM 提炼求职干货。

与 archive.py 的分工
--------------------
archive.py  只管"记录事实"：长期档案、时间轴去重、媒体归档。
analyze.py  只管"处理档案"：切批 → 生成提示 → 逐批调用 → 存结果 → 记录进度。
两者互不修改对方的数据，各自有独立的进度文件。

批次规则（可用命令行覆盖）
--------------------------
  x = 每批最大发言数           默认 200
  y = 每批最大正文字数         默认 12000
  z = 批次末尾必须的沉默时长    默认 10 分钟（找不到就退到整段最大空隙）
所有批次在"去时间戳"的正文上切分；不是"凑满就切"，而是"在自然停顿处切"。
每批提交时，首尾自动补上相邻批次的边界信息（上一批末尾 10 条 / 下一批开头 10 条）作为上下文。

产出
----
  <群>/提炼/批次/batch-001.md ...   每批的提炼结果（独立文件）
  <群>/提炼/批次/batch-001.input.txt  实际提交给模型的正文（可复查提示词与输入）
  <群>/提炼/_state.json            哪些批次已处理、每批的范围与统计
  <群>/提炼/INDEX.md               总览
  <群>/ocr_full.json               图片 OCR 完整文本（不参与提炼，供人工/后续检索）

用法
----
  python analyze.py plan   --group 27贝壳校招互帮互助群          # 只切批，不调 LLM
  python analyze.py ocr    --group 27贝壳校招互帮互助群          # 对归档图片跑完整 OCR
  python analyze.py run    --group 群名 [--limit 3] [--dry-run]  # 逐批提炼
  python analyze.py status --group 群名
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "_tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import clean_json as cj  # noqa: E402
import archive as A  # noqa: E402
import llm_client as llm  # noqa: E402

CODE_DIR = Path(__file__).resolve().parent
_PARENTS = CODE_DIR.parents
_PROJECT = _PARENTS[4] if len(_PARENTS) > 4 and _PARENTS[4].name == "infosystem" else CODE_DIR
_DEFAULT_DATA = _PROJECT / "data" / "community" / "qq" / "pipeline"
ROOT = Path(os.environ.get("QQ_PIPELINE_DATA_DIR", str(_DEFAULT_DATA))).resolve()
CODE_ROOT = CODE_DIR
PROMPT_FP = CODE_DIR / "prompts" / "distill_prompt.md"
CONFIG_FP = CODE_DIR / "llm_config.json"

# ── 批次参数（默认值，命令行可覆盖） ──
# 实测：中文约 0.77 token/字（batch-008：5350 字 = 4396 prompt_tokens；全档案 7543 字 ≈ 5800 token）。
# 因此 y 只是安全阀，不是精算值；决定"在哪切"的是 x_soft（凑够就找停顿）与 z（停顿要多长）。
MAX_MSGS = 800          # x_hard：每批绝对上限（超过就必须切，哪怕没有停顿）
SOFT_MSGS = 300         # x_soft：到这么多条就去找停顿，找到就切
MAX_CHARS = 50000       # y：每批最大正文字数（安全阀；按 0.77 token/字 ≈ 3.8 万 token）
MIN_SILENCE_MIN = 30    # z：优先采用的停顿时长（分钟）
SILENCE_PROBE = 3       # 找停顿的探测倍率：先找 z 分钟，找不到就放宽到 z×3、z×9 …
CTX_TAIL = 10           # 上下文：上一批末尾带几条
CTX_HEAD = 10           # 上下文：下一批开头带几条
MIN_BATCH_MSGS = 20     # 小于这么多条就不单独成批（并入前一批，前提是不超上限）
LONG_MSG_CHARS = 240    # 单条 ≥ 这么多字 → 在批次正文里标【长文】（并进原文留存）
LONG_MSG_LINES = 6      # 或换行 ≥ 这么多行（代码/清单粘贴）


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def prompt_meta(fp: Path) -> dict:
    """提示词指纹：把"这批结果是用哪份提示词跑出来的"写进台账。

    没有这个指纹，换提示词重跑后批次文件混在一起就说不清了（kb.py 合成时会核对）。
    """
    text = fp.read_text(encoding="utf-8")
    try:
        rel = fp.resolve().relative_to(CODE_DIR).as_posix()
    except ValueError:
        rel = fp.as_posix()
    return {"file": rel, "name": fp.name,
            "sha1": hashlib.sha1(text.encode("utf-8")).hexdigest()[:10], "chars": len(text)}


OUT_LIMIT = 2400        # 提炼正文硬上限（程序侧兜底）。实测：13k 字输入的批次模型自然产出 ~1500 字，
                        # 21 批实测 917~1933 字（max 1933）→ 2400 不会误砍，又能挡住发疯输出。
OUT_LIMIT_AUTO_RATIO = 0.16   # --out-limit auto：按本批正文字数的比例给上限
OUT_LIMIT_AUTO_MIN = 1200
OUT_LIMIT_AUTO_MAX = 2600


def resolve_out_limit(arg, n_chars: int) -> int:
    """--out-limit 可给数字，也可给 auto：按输入规模自适应（安全阀跟着输入走，别固定死）。"""
    if arg is None or (isinstance(arg, str) and arg.strip().lower() == "auto"):
        return max(OUT_LIMIT_AUTO_MIN, min(OUT_LIMIT_AUTO_MAX, int(n_chars * OUT_LIMIT_AUTO_RATIO)))
    return int(arg)


def enforce_limit(text: str, limit: int = OUT_LIMIT) -> tuple[str, int]:
    """按"条目边界"把正文裁到 limit 内，返回 (正文, 省略条数)。

    只在整行处切，不截断半句话；被裁掉的行数会标注出来，避免"看着像完整"。
    """
    lines = [l.rstrip() for l in text.strip().splitlines() if l.strip()]
    kept: list[str] = []
    total = 0
    for l in lines:
        add = len(l) + (1 if kept else 0)
        if total + add > limit and kept:
            break
        kept.append(l)
        total += add
    dropped = len(lines) - len(kept)
    body = "\n".join(kept)
    if dropped:
        note = f"（正文达上限，省略 {dropped} 条）"
        while kept and len(body) + len(note) + 1 > limit:
            kept.pop()
            dropped += 1
            body = "\n".join(kept)
        body = (body + "\n" + note) if body else note
    return body, dropped


@dataclass
class Batch:
    idx: int
    start_ms: int
    end_ms: int
    n_msgs: int
    n_chars: int
    start_local: str
    end_local: str
    silence_min: float
    lines: list[str]
    media: list[str]


# ══════════════════════════════ 读取档案 ══════════════════════════════

def group_dir(group: str) -> Path:
    return ROOT / group


def load_archive(group: str) -> tuple[list[dict], Path]:
    gd = group_dir(group)
    if not (gd / "消息.tsv").is_file():
        raise SystemExit(f"没有找到档案：{gd / '消息.tsv'}")
    rows = A.load_index(gd / "消息.tsv")
    if not rows:
        raise SystemExit("档案里没有消息")
    for i, r in enumerate(rows, 1):     # rowno = 消息.tsv 里的第几条；导入只往后追加，所以行号稳定
        r["rowno"] = i
    return rows, gd


def is_long_msg(text: str) -> bool:
    """信息量大的片段：长正文或长换行（粘贴代码/清单）。分布实测见 _tools/msg_stats.py。"""
    return len(text) >= LONG_MSG_CHARS or text.count("\n") >= LONG_MSG_LINES


def line_of(row: dict, img_hint: dict[str, str]) -> str:
    """去掉时间戳的一行：`A: 文字 [图片:x] [文件:y]`；长文加【长文N字】标记。"""
    seg = row["text"]
    marks = []
    for pair in row["media"].split(","):
        if not pair.strip():
            continue
        kind, _, fn = pair.partition(":")
        if not fn:
            continue
        mark = f"[{'图片' if kind == 'image' else '文件'}:{fn}]"
        if kind == "image" and img_hint.get(fn.lower()):
            mark += f"（{img_hint[fn.lower()]}）"
        marks.append(mark)
    if marks:
        seg = f"{seg} {' '.join(marks)}".strip()
    if seg and is_long_msg(row["text"]):
        seg = f"【长文{len(row['text'])}字 行#{row.get('rowno', '?')}】{seg}"
    # 每行都带 行#k：模型可以据此用 [Q#] 指认"整段逐字留存"（程序照行号去抄原文，
    # 模型不重打正文就不会抄错）。k = 消息.tsv 的第 k 条数据，导入只往后追加，所以稳定。
    return f"行#{row.get('rowno', '?')} {row['label']}: {seg}" if seg else f"行#{row.get('rowno', '?')} {row['label']}:"


def load_img_hints(gd: Path, limit: int = 20) -> dict[str, str]:
    """给图片附一句 ≤20 字指引（来自 OCR 50 字摘要的首段），只做轻度提示。

    键统一小写（见 `archive.load_ocr` 的说明）：文件名大小写各处不一致，
    不统一会出现"OCR 有结果、提示却没贴上"这种静默失效。
    """
    full = A.load_json(gd / "ocr_full.json", {})
    hints: dict[str, str] = {}
    for fn, data in list((full or {}).items())[:limit]:
        s = (data.get("summary") if isinstance(data, dict) else str(data)) or ""
        if s:
            hints[str(fn).lower()] = s[:20]
    return hints


# ══════════════════════════════ 切批 ══════════════════════════════

def load_pinned_plan(out: Path, rows: list[dict]) -> list[dict]:
    """上一次切批计划里，可以**钉住不动**的批次 → 「已封口批次」（R47）。

    为什么钉住：批次号是**位置序号**，每次从头重算的话最后一批的内容会随新消息变化
    （实测证据：`_batches.json` 里最后一批 `silence_min=0.0` —— 它是被"数据结尾"切断的，
    不是被群聊静默切断的）。钉住后：已封口批次永不重切、`kb.py add` 可以纯追加，
    新消息只影响尾巴，边界仍然落在群聊静默上。

    两类情况：
      · **台账里还有最后一批的行**（正常增量）→ 最后一批算"未封口尾巴"，允许继续吸收新消息；
      · **台账里已经没有最后一批的行**（历史流水没迁过来 / 整段被删）→ 连它一起钉住，
        新消息从 `max(idx)+1` 起新开批次，**绝不覆盖历史批次产物**。
    """
    prev = A.load_json(out / "_batches.json", [])
    if not isinstance(prev, list) or not prev:
        return []
    last = prev[-1]
    has_rows = any(int(last.get("start_ms", 0)) <= r["ts"] <= int(last.get("end_ms", -1))
                   for r in rows)
    return list(prev) if not has_rows else list(prev[:-1])


def split_batches(rows: list[dict], img_hint: dict[str, str],
                  max_msgs: int, max_chars: int, min_silence_min: int,
                  soft_msgs: int = SOFT_MSGS,
                  pinned: list[dict] | None = None) -> list[Batch]:
    """在自然停顿处切批：每批目标 soft_msgs 条，尽量停在 ≥z 分钟的停顿上，硬顶 max_msgs。

    规则（顺序很重要，先找停顿再看上限）：
      1. 攒够 soft_msgs 条后，若紧邻处就是 ≥z 分钟的停顿，就地切；
      2. 否则继续往后看，在 soft_msgs ~ max_msgs 之间找**第一个** ≥z 分钟的停顿，切在那里
         （这样批次会停在一个话题真正结束的地方，而不是凑满条数硬切）；
      3. 一直没停顿（密集长对话）就切在 max_msgs，并在日志里给告警；
      4. 到 max_chars（安全阀）也必须切。

    **增量语义（R47）**：`pinned` 里的批次（已封口）原样保留、永不重切；
    只对"不在任何已封口区间内"的行重新扫描，编号从 `len(pinned)+1` 起。
    要全量重切（换了切批参数 / 黑名单回删后想让批次号重排）请用 `--resplit`。
    """
    sealed: list[Batch] = []
    if pinned:
        sealed = [Batch(**{**d, "lines": list(d.get("lines") or []),
                           "media": list(d.get("media") or [])}) for d in pinned]
        ranges = [(b.start_ms, b.end_ms) for b in sealed]
        tail = [r for r in rows if not any(s <= r["ts"] <= e for s, e in ranges)]
        # 一致性检查：只告警，绝不重排（重排会让知识库的来源记号全错位）
        for b in sealed:
            now = sum(1 for r in rows if b.start_ms <= r["ts"] <= b.end_ms)
            if now != b.n_msgs:
                log(f"[注意] batch-{b.idx:03d} 已封口，但区间内条数变了"
                    f"（记录 {b.n_msgs}，现在 {now}）—— 按规则不重切；要重算请加 --resplit")
    else:
        tail = rows

    n = len(tail)
    gaps = [(tail[i + 1]["ts"] - tail[i]["ts"]) / 60000 for i in range(n - 1)]

    cuts: list[int] = []
    start = 0
    while start < n:
        chars = 0
        cut = None
        for i in range(start, n):
            chars += len(tail[i]["text"]) + 24   # 粗算代号与媒体标记开销
            count = i - start + 1
            if count >= soft_msgs and (gaps[i] >= min_silence_min if i < n - 1 else True):
                cut = i + 1
                break
            if count >= max_msgs or chars >= max_chars:
                cut = i + 1
                break
        if cut is None:
            cut = n
        cuts.append(cut)          # 必须补这一刀，否则尾部会丢
        start = cut
        if cut >= n:
            break

    fresh: list[Batch] = []
    prev = 0
    base = len(sealed)
    for k, cut in enumerate(cuts, 1):
        chunk = tail[prev:cut]
        if not chunk:
            continue
        sil = (tail[cut]["ts"] - tail[cut - 1]["ts"]) / 60000 if cut < n else 0.0
        media = sorted({p.partition(":")[2] for r in chunk for p in r["media"].split(",") if p.strip()})
        fresh.append(Batch(
            idx=base + k, start_ms=chunk[0]["ts"], end_ms=chunk[-1]["ts"],
            n_msgs=len(chunk), n_chars=sum(len(r["text"]) for r in chunk),
            start_local=chunk[0]["time"], end_local=chunk[-1]["time"],
            silence_min=round(sil, 1),
            lines=[line_of(r, img_hint) for r in chunk], media=media,
        ))
        prev = cut

    # 尾部太小：只要合并后不超硬顶，就并入前一批。
    # 只在**新切出来的那几批之间**合并（fresh[-1] 并进 fresh[-2]），绝不碰已封口批次。
    if len(fresh) >= 2 and fresh[-1].n_msgs < MIN_BATCH_MSGS:
        if fresh[-2].n_msgs + fresh[-1].n_msgs <= max_msgs:
            tail_b = fresh.pop()
            prev_b = fresh[-1]
            prev_b.n_msgs += tail_b.n_msgs
            prev_b.n_chars += tail_b.n_chars
            prev_b.end_ms = tail_b.end_ms
            prev_b.end_local = tail_b.end_local
            prev_b.silence_min = tail_b.silence_min
            prev_b.lines += tail_b.lines
            prev_b.media = sorted(set(prev_b.media) | set(tail_b.media))
    return sealed + fresh


def batch_warnings(batches: list[Batch], min_silence_min: int) -> list[str]:
    """标出没能停在 ≥z 分钟停顿上的批次（密集长对话里无法避免，提示即可）。"""
    return [f"batch-{b.idx:03d} 末尾沉默 {b.silence_min} 分钟 < z={min_silence_min}"
            for b in batches[:-1] if b.silence_min < min_silence_min]


def name_table(gd: Path, batch: "Batch | None" = None) -> str:
    """代号 -> 用户名的对照，放进提示里便于模型写清"谁说的"。

    只列**本批出现过的**代号：大群有几百人，把全量映射塞进每一批纯属浪费（实测 395 人 ≈4000 字/批）。
    """
    rev = A.load_names_reverse(gd / "names.tsv")
    used: set[str] | None = None
    if batch is not None:
        used = set()
        for line in batch.lines:
            lab, _, _ = line.partition(":")
            if lab:
                used.add(lab.strip())
    pairs = [f"{lab}={rev[lab]}" for lab in sorted(rev, key=lambda x: (len(x), x))
             if used is None or lab in used]
    if not pairs:
        return ""
    return f"本批出现的代号对照（{len(pairs)} 人）：" + "，".join(pairs)


def build_user_prompt(b: Batch, batches: list[Batch], rows: list[dict],
                      img_hint: dict[str, str], names: str = "") -> str:
    """批次正文 + 首尾上下文（上一批末尾 / 下一批开头）+ 代号对照。"""
    parts = []
    if names:
        parts += [names, ""]
    if b.idx > 1:
        prev = batches[b.idx - 2]
        tail = prev.lines[-CTX_TAIL:]
        parts += [f"【上一批末尾 {len(tail)} 条，仅供衔接上下文，不要在此提炼】"] + tail + [""]
    parts.append(f"【本批正文 {b.n_msgs} 条，{b.start_local} ~ {b.end_local}（已去时间戳），请提炼】")
    parts += b.lines
    if b.idx < len(batches):
        nxt = batches[b.idx]
        head = nxt.lines[:CTX_HEAD]
        parts += ["", f"【下一批开头 {len(head)} 条，仅供衔接上下文，不要在此提炼】"] + head
    return "\n".join(parts)


# ══════════════════════════════ OCR 完整文本 ══════════════════════════════

def cmd_ocr(args) -> int:
    """对**指定范围**内的聊天记录条目里的图片跑 OCR（R39）。

    范围 = 批次选择（`--batch 2-4`）+ 时间窗（`--since/--until`），二者可叠加，缺省=全群。
    产出：全文 → `ocr_full.json`；≤50 字摘要 → `ocr_cache.json` + **回填到 `记录.md` 该条下面**
    （`> OCR: …`），一眼能扫。提炼仍然不读全文（R19：避免 token 被图片吃掉）。

    这是 OCR 的**唯一**入口，只能手动调用；`archive.py import` 永远不碰它（R40）。
    """
    gd = group_dir(args.group)
    meta = A.load_json(gd / "_meta.json", {})
    full = A.load_json(gd / "ocr_full.json", {})
    if not isinstance(full, dict):
        full = {}
    # 键统一小写（文件名大小写各处不一致，不统一会让"已识别"查不出来 -> 每次重跑）
    full = {str(k).lower(): v for k, v in full.items()}

    sel = A.select_batches(meta, args.batch)
    sel_set = set(sel) if args.batch else None
    since = A.to_ms(args.since) if args.since else 0
    until = A.to_ms(args.until) if args.until else 0
    if since and until and until < since:
        raise SystemExit("--until 比 --since 早，检查一下")

    rows = A.load_index(gd / "消息.tsv")
    scoped = [r for r in rows if A.row_in_scope(r, sel_set, since, until)]
    # 只认还在正文里引用的图片；已判定过（有全文或无文字空标记）的不再重复跑
    referenced = {p.partition(":")[2].lower() for r in scoped for p in r["media"].split(",")
                  if p.strip().startswith("image:")}

    # 批次目录里的图片按 clean 名索引（同一张图可能被多个批次引用）
    by_name: dict[str, Path] = {}
    for f in sorted((gd / "batches").glob("*/resources/images/*")):
        if f.is_file():
            by_name.setdefault(cj.clean_filename(f.name).lower(), f)

    def done(fn: str) -> bool:
        d = full.get(fn)
        if not isinstance(d, dict):
            return False
        return bool(d.get("full")) or bool(d.get("none"))

    todo = [by_name[fn] for fn in sorted(referenced) if fn in by_name and (args.force or not done(fn))]
    scope_txt = ("、".join(sel)) if args.batch else "全部批次"
    if args.since or args.until:
        scope_txt += f"；时间窗 {args.since or '不限'} ~ {args.until or '不限'}"
    log(f"[OCR] 范围：{scope_txt}")
    log(f"[OCR] 范围里 {len(scoped)} 条记录、引用 {len(referenced)} 张图；"
        f"归档里找到 {sum(1 for fn in referenced if fn in by_name)} 张、已有全文/已判定 "
        f"{sum(1 for fn in referenced if done(fn))} 张 → 待办 {len(todo)} 张")
    if args.limit:
        if args.limit > cj.OCR_HARD_MAX:
            log(f"[OCR] --limit {args.limit} 超硬上限，收敛为 {cj.OCR_HARD_MAX}")
            args.limit = cj.OCR_HARD_MAX
        todo = todo[:args.limit]
        log(f"[OCR] --limit {args.limit}，本次最多处理 {len(todo)} 张")
    if args.dry_run:
        log(f"[dry-run] 不启动 OCR 引擎、不写文件。会识别这 {len(todo)} 张：" +
            ("、".join(cj.clean_filename(f.name) for f in todo[:8]) +
             (f"…等 {len(todo)} 张" if len(todo) > 8 else "") if todo else "（没有）"))
        return 0
    if todo:
        engine = cj.start_ocr_engine()
        if engine is None:
            log("[OCR] 引擎不可用")
            return 1
        try:
            for i, fp in enumerate(todo, 1):
                fn = cj.clean_filename(fp.name)
                log(f"[OCR] ({i}/{len(todo)}) {fn}")
                r = cj.ocr_image_full(engine, fp)
                if r:
                    full[fn] = {**r, "file": fp.name, "size": fp.stat().st_size}
                else:
                    # 记下"这张图没有文字"，避免以后每次都重跑（--force 可强制重来）
                    full[fn] = {"none": True, "file": fp.name, "size": fp.stat().st_size}
        finally:
            engine.close()
        A.save_json(gd / "ocr_full.json", full)
        log(f"[OCR] 已写入 {gd / 'ocr_full.json'}（共 {len(full)} 张，其中有文字 "
            f"{sum(1 for v in full.values() if isinstance(v, dict) and v.get('full'))} 张）")

    # 50 字摘要同步进"记录.md"（不动消息.tsv）
    summary = {k: v.get("summary", "") for k, v in full.items()
               if isinstance(v, dict) and v.get("summary")}
    A.save_ocr(gd / "ocr_cache.json", summary)
    meta2 = A.load_json(gd / "_meta.json", {})
    A.write_record_md(gd, meta2, A.load_names_reverse(gd / "names.tsv"),
                      A.load_index(gd / "消息.tsv"), summary)
    A.write_status_md(gd, meta2, A.load_names_reverse(gd / "names.tsv"),
                      A.load_index(gd / "消息.tsv"), summary)
    log(f"[OCR] 摘要已回填 记录.md / 状态.md（{len(summary)} 张）")
    return 0


# ══════════════════════════════ 切批 / 提炼 ══════════════════════════════

def clean_stale_batches(gd: Path, batches: list[Batch], force: bool = False) -> list[int]:
    """删掉「上一轮切批留下的、这一轮已经不存在的」批次产物，并清掉 `_state.json` 里的幽灵条目。

    为什么必须删（实测踩过两次同类问题）：素材一变（黑名单回删、删图对账），批次号会重排
    （6 批 → 5 批）。旧的 `batch-006.md` **还躺在磁盘上**，于是：
      · `kb.py` 把它当成一个真批次继续重放 → 已经并进 batch-005 的内容又被并一遍，
        来源记号变成 006K*，`check` 却"一切对齐"（因为两边都算上了这个幽灵）；
      · `analyze.py status` 报"批次文件 5 批，已提炼 6 批"。
    只靠"内容变了就重跑"（_stale_reason）挡不住这个 —— 那是**多出来的**批次，不是变了的批次。
    """
    keep = {b.idx for b in batches}
    out = gd / "提炼" / "批次"
    gone: set[int] = set()
    if out.is_dir():
        # 不能用 glob("batch-*.md")：它连 batch-002.full.md 一起匹配，同一路径被列两次，
        # 第二次 unlink() 抛 FileNotFoundError —— 清理删到一半就崩（2026-10-18 实测：
        # 71 个批次文件被删掉 70 个后抛异常）。改成单次 glob + 严格文件名正则 + missing_ok。
        stale: list[tuple[int, Path]] = []
        for fp in sorted(out.glob("batch-*")):
            m = re.fullmatch(r"batch-(\d+)\.(?:md|input\.txt|full\.md)", fp.name)
            if m and fp.is_file() and int(m.group(1)) not in keep:
                stale.append((int(m.group(1)), fp))
        # 护栏：要删的比要留的还多 → 说明"台账(消息.tsv)与提炼产物不再对应"（数据根被换过 /
        # 消息流水没迁全），这不是正常重切批，必须人工确认后才允许删。
        stale_ids = {i for i, _ in stale}
        if stale_ids and not force and len(stale_ids) > len(keep):
            raise SystemExit(
                f"\u26a0 要删的过期批次 {len(stale_ids)} 个 > 要保留 {len(keep)} 批。\n"
                f"  这不是正常重切批，更像 **消息.tsv 与 提炼\\批次 不再对应**"
                f"（数据根被换过？消息流水没迁全？）。\n"
                f"  当前切批算出来只有 {len(keep)} 批，磁盘上却有 {len(stale_ids) + len(keep)} 个批次。\n"
                f"  先补回 消息.tsv / batches 再跑；确认要给这个数据根换血，加 --force-stale-clean。")
        for idx, fp in stale:
            gone.add(idx)
            fp.unlink(missing_ok=True)
    ghost: set[int] = set()
    state_fp = gd / "提炼" / "_state.json"
    if state_fp.is_file():
        st = A.load_json(state_fp, {})
        for k in list((st.get("done") or {})):
            try:
                if int(k) not in keep:
                    ghost.add(int(k))
                    del st["done"][k]
            except ValueError:
                continue
        if ghost:
            A.save_json(state_fp, st)
    if gone or ghost:
        log(f"[清理] 上一轮切法还剩 {len(keep)} 批；已删过期产物 "
            f"{', '.join('batch-%03d' % i for i in sorted(gone)) or '（无）'}"
            f"，清掉台账里的幽灵条目 "
            f"{', '.join('batch-%03d' % i for i in sorted(ghost)) or '（无）'}")
    return sorted(gone | ghost)


def media_gate(gd: Path, rows: list[dict], group: str) -> None:
    """顺序闸门（R42）：档案里还有"有图但没做过删图对账"的批次时，拒绝往下做归纳。

    为什么要有这道闸门（实测返工两次）：标准流程是
    `json → txt → 删图 → OCR → 归纳`。而"删图"这一步改了素材，批次就要重切、重提炼、
    知识库要重建 —— 等归纳做完再回来删图，前面两次 LLM 调用全白跑，而且很容易漏掉某个
    下游产物（实测漏了"过期批次文件"，KB 把已经并掉的内容又并了一遍）。

    没图的批次不算（你说的"中间两步没有导出图片就可以忽略"）；`--no-media-check` 可强行跳过。
    """
    meta = A.load_json(gd / "_meta.json", {})
    pend = A.pending_media_batches(meta, rows)
    if not pend:
        return
    nums = ",".join(str(int(b.split("-")[1].split("_")[0])) for b in pend)
    lst = "\n".join(f"     batch-{int(b.split('-')[1].split('_')[0]):03d}：{n} 处媒体引用" 
                    for b, n in pend.items())
    raise SystemExit(
        f"⚠ 顺序闸门（R42）：这些批次还有图没做过「删图对账」，先别归纳 ——\n{lst}\n"
        f"  标准流程是 json → txt → 删图 → OCR → 归纳。先归纳再回来删图，\n"
        f"  就得重切批 + 重提炼 + 重建知识库（已经返工过两次）。\n"
        f"  ① 先看一眼（不写盘）：\n"
        f"       python archive.py prune-missing --group \"{group}\" --batch {nums} --dry-run\n"
        f"  ② 确认好了就去掉 --dry-run 跑一次；**一张都不删也会记下「这批图我确认过了」**\n"
        f"  ③ 图确实都留着、也不想记这一笔：给本命令加 --no-media-check 跳过闸门")


def cmd_plan(args) -> int:
    rows, gd = load_archive(args.group)
    hints = load_img_hints(gd)
    out = gd / "提炼"
    (out / "批次").mkdir(parents=True, exist_ok=True)
    resplit = bool(getattr(args, "resplit", False))
    pinned = [] if resplit else load_pinned_plan(out, rows)
    batches = split_batches(rows, hints, args.max_msgs, args.max_chars, args.silence,
                            soft_msgs=args.soft_msgs, pinned=pinned)
    A.save_json(out / "_batches.json", [asdict(b) for b in batches])
    clean_stale_batches(gd, batches, force=getattr(args, "force_stale_clean", False))
    print(f"共 {len(batches)} 批（x_soft={args.soft_msgs} / x_hard={args.max_msgs} 条 / "
          f"y≤{args.max_chars} 字 / z={args.silence} 分钟）"
          + (f"；其中已封口 {len(pinned)} 批钉住不动" if pinned else
             "；全量重切（--resplit）" if resplit else ""))
    fresh = batches[len(pinned):]
    for w in batch_warnings(fresh, args.silence):
        print("  ⚠ " + w)
    if pinned:
        print(f"  （已封口 batch-001 ~ batch-{len(pinned):03d} 不重切；下面只列尾巴）")
    for b in fresh:
        print(f"  batch-{b.idx:03d}  {b.start_local} ~ {b.end_local}  "
              f"{b.n_msgs:>4} 条 {b.n_chars:>5} 字  末尾沉默 {b.silence_min} 分钟  媒体 {len(b.media)}")
    return 0


def _load_or_make_batches(args, rows, gd) -> list[Batch]:
    """R47：每次都**重新算一遍**（已封口批次钉住，所以重算不会改动历史批次），
    这样单独跑 `run` 也能吃到新入库的消息，不必先手动 `plan`。"""
    fp = gd / "提炼" / "_batches.json"
    hints = load_img_hints(gd)
    (gd / "提炼").mkdir(parents=True, exist_ok=True)
    resplit = bool(getattr(args, "resplit", False))
    pinned = [] if resplit else load_pinned_plan(gd / "提炼", rows)
    batches = split_batches(rows, hints, args.max_msgs, args.max_chars, args.silence,
                            soft_msgs=args.soft_msgs, pinned=pinned)
    A.save_json(fp, [asdict(b) for b in batches])
    return batches


def _stale_reason(d: dict | None, n_msgs: int, start_local: str, end_local: str) -> str:
    """已提炼批次现在是否「输入已变」——切批规则/黑名单/归档变了之后会发生。

    这是最容易静默丢素材的地方：批次号还在 done 里，但那一批的内容已经重切过，
    不重跑就等于后面新增的消息永远不被提炼。
    """
    if not d:
        return ""
    if d.get("n_msgs") != n_msgs:
        return f"条数 {d.get('n_msgs')} → {n_msgs}"
    r = list(d.get("range") or [])
    if r != [start_local, end_local]:
        return f"区间 {r} → [{start_local}, {end_local}]"
    return ""


def cmd_run(args) -> int:
    rows, gd = load_archive(args.group)
    if not args.dry_run and not getattr(args, "no_media_check", False):
        media_gate(gd, rows, args.group)
    batches = _load_or_make_batches(args, rows, gd)
    out = gd / "提炼"
    (out / "批次").mkdir(parents=True, exist_ok=True)
    state_fp = out / "_state.json"
    clean_stale_batches(gd, batches,      # 批次号重排后，磁盘上的旧批次产物必须先清掉
                         force=getattr(args, "force_stale_clean", False))
    state = A.load_json(state_fp, {"done": {}})   # 清理会改 _state.json，所以要在清理之后再读
    prompt_fp = Path(args.prompt).expanduser() if args.prompt else PROMPT_FP
    if not prompt_fp.is_file():
        raise SystemExit(f"提示词文件不存在：{prompt_fp}")
    prompt = prompt_fp.read_text(encoding="utf-8")
    pmeta = prompt_meta(prompt_fp)
    log(f"[提炼] 提示词：{prompt_fp.name}（{len(prompt)} 字，sha1 {pmeta['sha1']}）")
    # 闸门：这个群的批次本来是用别的提示词提炼的，说明**拿错提示词了**
    # （实测踩过：跑 AI Agent 群忘了 --prompt，默认的求职提示词把 26 批全提炼成了求职口径）
    used = {d.get("prompt", {}).get("sha1") for d in state.get("done", {}).values()
            if isinstance(d, dict) and d.get("prompt")}
    if used and pmeta["sha1"] not in used and not args.force:
        served = "、".join(sorted(x for x in used if x))
        raise SystemExit(
            f"⚠ 提示词对不上：{prompt_fp.name} 是 {pmeta['sha1']}，"
            f"但这个群已提炼的批次用的是 {served}。\n"
            f"  换个群提炼时记得指定 --prompt，例如 AI Agent 技术群：\n"
            f"    python analyze.py run --group \"{args.group}\" --prompt prompts/distill_aiagent.md\n"
            f"  确实要用这份新提示词重做整个群，就加 --force（配合 --redo 全量重跑）。")
    client = None if args.dry_run else llm.LLMClient(llm.load_config(CONFIG_FP, args.model))

    if args.redo:
        todo = [b for b in batches if not args.only or b.idx == args.only]
    else:
        todo, stale = [], []
        for b in batches:
            d = state.get("done", {}).get(str(b.idx))
            if not d:
                todo.append(b)
                continue
            why = _stale_reason(d, b.n_msgs, b.start_local, b.end_local)
            if why:
                stale.append((b, why))
                todo.append(b)      # 输入变了就必须重跑，不能拿旧结果顶着
        for b, why in stale:
            log(f"[重切] batch-{b.idx:03d} 已提炼但输入已变（{why}）→ 重跑这一批")
        if args.only:
            todo = [b for b in todo if b.idx == args.only]
    if args.limit:
        todo = todo[:args.limit]
    log(f"[提炼] 共 {len(batches)} 批，待处理 {len(todo)} 批"
        + ("（dry-run，不调 LLM）" if args.dry_run else f"，模型 {client.config.model}"))
    if not todo:
        log("[提炼] 全部已处理，无新增。")
        return 0

    for b in todo:
        user = build_user_prompt(b, batches, rows, load_img_hints(gd), name_table(gd, b))
        (out / "批次" / f"batch-{b.idx:03d}.input.txt").write_text(user, encoding="utf-8")
        if args.dry_run:
            log(f"[dry-run] batch-{b.idx:03d} 输入 {len(user)} 字，未调用")
            continue
        t0 = time.time()
        limit = resolve_out_limit(args.out_limit, b.n_chars)
        try:
            text = client.ask(prompt, user, max_tokens=max(2500, int(limit * 1.3)))
        except Exception as e:
            log(f"[提炼] batch-{b.idx:03d} 失败：{e}")
            state.setdefault("failed", {})[str(b.idx)] = str(e)
            A.save_json(state_fp, state)
            continue
        dt = time.time() - t0
        body, dropped = enforce_limit(text, limit)
        if dropped:
            log(f"[提炼] batch-{b.idx:03d} 正文超出 {limit} 字，已按条目边界省略 {dropped} 条")
        full_fp = out / "批次" / f"batch-{b.idx:03d}.full.md"
        if dropped:
            # 被砍掉的部分也存下来：正文可以只留前 N 条，但**信息不许凭空消失**
            full_fp.write_text(text.strip() + "\n", encoding="utf-8")
            log(f"[提炼] batch-{b.idx:03d} 未截断全文已存 {full_fp.name}（{len(text)} 字）")
        elif full_fp.is_file():
            full_fp.unlink()
        head = [
            f"# batch-{b.idx:03d}",
            "",
            f"- 区间：{b.start_local} ~ {b.end_local}（{b.n_msgs} 条 / {b.n_chars} 字）",
            f"- 末尾沉默：{b.silence_min} 分钟"
            + ("（末批，之后无消息）" if b.idx == len(batches) else "")
            + f"；上下文：上前 {CTX_TAIL} 条 / 下后 {CTX_HEAD} 条",
            "",
        ]
        if b.media and args.list_media:
            head += ["- 本批媒体：" + ", ".join(b.media), ""]
        (out / "批次" / f"batch-{b.idx:03d}.md").write_text(
            "\n".join(head) + body.strip() + "\n", encoding="utf-8")
        state.setdefault("done", {})[str(b.idx)] = {
            "range": [b.start_local, b.end_local], "n_msgs": b.n_msgs, "n_chars": b.n_chars,
            "seconds": round(dt, 1), "model": client.config.model, "prompt": pmeta,
            "out_chars": len(body), "out_limit": limit, "dropped_items": dropped,
            "full_out_chars": len(text) if dropped else None,
            "full_out": f"批次/batch-{b.idx:03d}.full.md" if dropped else None,
            "out": f"批次/batch-{b.idx:03d}.md", "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        state["prompt"] = pmeta
        A.save_json(state_fp, state)
        log(f"[提炼] batch-{b.idx:03d} 完成（{dt:.1f}s，正文 {len(body)} 字）")
        if args.sleep:
            time.sleep(args.sleep)

    write_index(gd, batches, state, out)
    return 0


def write_index(gd: Path, batches: list[Batch], state: dict, out: Path) -> None:
    done = state.get("done", {})
    pm = state.get("prompt") or {}
    lines = [
        f"# {gd.name} · 提炼索引",
        "",
        f"- 批次总数：{len(batches)}；已提炼：{len(done)}",
        f"- 原始档案：`../消息.tsv`（{len(A.load_index(gd / '消息.tsv'))} 条）",
        f"- 提示词：`../../{pm.get('file', 'prompts/distill_prompt.md')}`"
        + (f"（sha1 `{pm['sha1']}`）" if pm.get("sha1") else ""),
        f"- 图片 OCR 全文（不参与提炼）：`../ocr_full.json`",
        "",
        "| 批次 | 区间 | 条数 | 状态 | 结果 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for b in batches:
        d = done.get(str(b.idx))
        lines.append(f"| batch-{b.idx:03d} | {b.start_local} ~ {b.end_local} | {b.n_msgs} | "
                     f"{'已提炼' if d else '待处理'} | {'批次/batch-%03d.md' % b.idx if d else ''} |")
    (out / "INDEX.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def cmd_stamp(args) -> int:
    """给已提炼批次补记"用了哪份提示词"的指纹（历史台账里没有这个字段时用）。"""
    gd = group_dir(args.group)
    state_fp = gd / "提炼" / "_state.json"
    state = A.load_json(state_fp, {"done": {}})
    done = state.get("done") or {}
    if not done:
        raise SystemExit(f"没有已提炼批次：{state_fp}")
    fp = Path(args.prompt).expanduser() if args.prompt else PROMPT_FP
    if not fp.is_file():
        raise SystemExit(f"提示词文件不存在：{fp}")
    meta = prompt_meta(fp)
    n = sum(1 for d in done.values() if not (args.only_missing and d.get("prompt")))
    if args.dry_run:
        log(f"[stamp] dry-run：会给 {n} 批写 {meta['file']}（sha1 {meta['sha1']}）")
        return 0
    for d in done.values():
        if args.only_missing and d.get("prompt"):
            continue
        d["prompt"] = meta
    state["prompt"] = meta
    A.save_json(state_fp, state)
    log(f"[stamp] {gd.name}：{n} 批已记录提示词 {meta['file']}"
        f"（sha1 {meta['sha1']}，{meta['chars']} 字）")
    raw = A.load_json(gd / "提炼" / "_batches.json", [])
    if raw:
        batches = [Batch(**{**d, "lines": list(d["lines"]), "media": list(d["media"])}) for d in raw]
        write_index(gd, batches, state, gd / "提炼")
        log("[stamp] INDEX.md 里的提示词一行已同步")
    return 0


def cmd_status(args) -> int:
    gd = group_dir(args.group)
    out = gd / "提炼"
    state = A.load_json(out / "_state.json", {"done": {}})
    batches = A.load_json(out / "_batches.json", [])
    print(f"{gd.name}")
    print(f"  批次文件：{len(batches)} 批" + (f"，已提炼 {len(state.get('done', {}))} 批" if batches else ""))
    for b in batches:
        d = state.get("done", {}).get(str(b["idx"]))
        why = _stale_reason(d, b["n_msgs"], b["start_local"], b["end_local"])
        flag = "·" if not d else ("↻" if why else "✔")
        print(f"   {flag} batch-{b['idx']:03d}  {b['start_local']} ~ {b['end_local']}  {b['n_msgs']} 条")
        if why:
            print(f"        ↻ 输入已变（{why}）→ 需要重跑（`analyze.py run` 会自动包含它）")
    failed = state.get("failed") or {}
    for k, v in failed.items():
        print(f"   ✘ batch-{int(k):03d} 失败：{v}")
    rows, _ = load_archive(args.group)
    pend = A.pending_media_batches(A.load_json(gd / "_meta.json", {}), rows)
    if pend:
        nums = "、".join(f"batch-{int(b.split('-')[1].split('_')[0]):03d}({n}处)"
                         for b, n in pend.items())
        print(f"   ⏸ 删图对账没做：{nums} → `analyze.py run` 会拦下来（R42 顺序闸门）")
        print(f"      先 `python archive.py prune-missing --group \"{args.group}\" "
              f"--batch {','.join(str(int(b.split('-')[1].split('_')[0])) for b in pend)} --dry-run`")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="档案切批 + LLM 提炼求职干货")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--group", required=True)
        p.add_argument("--soft-msgs", type=int, default=SOFT_MSGS,
                       help=f"凑够多少条就去找停顿 x_soft（默认 {SOFT_MSGS}）")
        p.add_argument("--max-msgs", type=int, default=MAX_MSGS,
                       help=f"每批绝对上限 x_hard（默认 {MAX_MSGS}）")
        p.add_argument("--max-chars", type=int, default=MAX_CHARS,
                       help=f"每批最大正文字数 y，安全阀（默认 {MAX_CHARS}）")
        p.add_argument("--silence", type=int, default=MIN_SILENCE_MIN,
                       help=f"优先采用的停顿分钟 z（默认 {MIN_SILENCE_MIN}；找不到会逐级放宽）")
        p.add_argument("--resplit", action="store_true",
                       help="强制重新切批（改了 line_of/黑名单回删后刷新 _batches.json）")

    p_plan = sub.add_parser("plan", help="只切批，不调用模型")
    common(p_plan)
    p_plan.add_argument(
        "--force-stale-clean", action="store_true",
                       help="允许在『要删的批次比要留的还多』时也清理（只在你确认要给这个数据根换血时用）")
    p_plan.set_defaults(func=cmd_plan)

    p_run = sub.add_parser("run", help="逐批调用 LLM 提炼")
    common(p_run)
    p_run.add_argument("--model", help="llm_config.json 里的模型 key")
    p_run.add_argument("--prompt", help="提示词文件路径（默认 prompts/distill_prompt.md）"
                                      "；换群/换提炼目标时用，例如求职群 vs 技术群")
    p_run.add_argument("--limit", type=int, help="本次最多处理几批")
    p_run.add_argument("--only", type=int, help="只处理某一批（序号）")
    p_run.add_argument("--dry-run", action="store_true", help="只生成输入文件，不调用 LLM")
    p_run.add_argument("--sleep", type=float, default=0, help="每批之间间隔秒数")
    p_run.add_argument("--out-limit", default="auto",
                       help=f"每批提炼正文硬上限（数字，或 auto；auto = 输入字数×{OUT_LIMIT_AUTO_RATIO} "
                            f"夹在 {OUT_LIMIT_AUTO_MIN}~{OUT_LIMIT_AUTO_MAX} 之间）。"
                            f"超限部分会存进 batch-NNN.full.md，不会凭空丢。"
                            f"提过的批要重跑才生效：先删 提炼\\_state.json 里对应条目")
    p_run.add_argument("--list-media", action="store_true",
                       help="在结果头部列出本批全部媒体文件名（默认不列，避免几十行噪音掩盖结论）")
    p_run.add_argument("--redo", action="store_true",
                       help="连已提炼过的批次一起重跑（配合 --only N 用来重跑某一批）")
    p_run.add_argument("--force", action="store_true",
                       help="允许换用与历史批次不同的提示词（闸门默认会拒绝，免得拿错群的口径）")
    p_run.add_argument(
        "--force-stale-clean", action="store_true",
                       help="允许在『要删的批次比要留的还多』时也清理（只在你确认要给这个数据根换血时用）")
    p_run.add_argument("--no-media-check", action="store_true",
                       help="跳过「删图对账」顺序闸门（只在你确认这批图都要留着、也不想记一笔时用）")
    p_run.set_defaults(func=cmd_run)

    p_ocr = sub.add_parser("ocr", help="对【指定范围】内的归档图片跑完整 OCR（手动、唯一入口）")
    p_ocr.add_argument("--group", required=True)
    p_ocr.add_argument("--limit", type=int, help="最多处理几张")
    p_ocr.add_argument("--batch", action="append",
                       help="限定批次：2 / 2-4 / batch-002（可重复；缺省=全部批次）")
    p_ocr.add_argument("--since", help="只处理该本地时间之后的记录（'YYYY-MM-DD HH:MM:SS' 或日期）")
    p_ocr.add_argument("--until", help="只处理该本地时间之前的记录")
    p_ocr.add_argument("--force", action="store_true", help="连已判定过的图片一起重跑")
    p_ocr.add_argument("--dry-run", action="store_true",
                       help="只报告这个范围里待识别几张，不启动 OCR 引擎")
    p_ocr.set_defaults(func=cmd_ocr)

    p_st = sub.add_parser("status", help="查看切批与提炼进度")
    p_st.add_argument("--group", required=True)
    p_st.set_defaults(func=cmd_status)

    p_stamp = sub.add_parser("stamp", help="给已提炼批次补记提示词指纹（历史台账缺这个字段时用）")
    p_stamp.add_argument("--group", required=True)
    p_stamp.add_argument("--prompt", help=f"提示词文件（缺省 {PROMPT_FP.name}）")
    p_stamp.add_argument("--only-missing", action="store_true", help="只补没有 prompt 字段的批次")
    p_stamp.add_argument("--dry-run", action="store_true")
    p_stamp.set_defaults(func=cmd_stamp)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    cj.force_utf8_console()
    raise SystemExit(main())
