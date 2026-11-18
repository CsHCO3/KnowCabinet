#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""QQ 群聊长期归档：把每次导出的记录「只向后追加」地并入本地档案。

核心约定
--------
* 档案位置：D:\\QQChatExporter\\chathistory\\<群名>\\
* 每条消息都留时间戳；用户名 -> 字母的映射长期固定（names.tsv）。
* 每次导入前先合并「已覆盖时间轴」：新批次里落在已覆盖时间段内的消息
  **整条丢弃，连它提到的文件也不看**（因为第一次处理时该文件就被你判为无用删掉了，
  重新导出时它又会被缓存塞回 /export，不能因此复活）。
  同理，这些消息的媒体文件也**不搬进来**（否则会在新批次里留下没人引用的孤儿）。
* 时间轴只向后追加；同一时间戳重复出现也不会写第二遍。
* **媒体默认全留（R37）**：有图片/文件引用就登记，本地有没有文件只记数。
  「文件不在 = 你删掉了 => 摘掉这条引用」改成**手动动作**：
      python archive.py prune-missing --group 群名 [--batch 2-4] [--dry-run]
* **本工具不做 OCR（R40）**：解析聊天记录时不触发。OCR 只有一条手动路径
  `python analyze.py ocr --group 群名 [--batch 2-4]`。

典型流程
--------
1. 在 QQ Chat Exporter 里跑一次导出（导出到默认 exports/）；想留图就勾上导出媒体。
2. 在 exports/resources 里把不重要的图片删掉（想筛就筛，不筛就都留着）。
3. python archive.py import --from "C:\\Users\\HP\\Documents\\QQChatExporter\\exports"
4. 查看 <群目录>\\记录.md / 消息.tsv / 状态.md

其它子命令
----------
  python archive.py list                          # 列出所有群的档案状态（含媒体对账）
  python archive.py import --group 群名 --from <目录|json> [--dry-run]
  python archive.py prune-missing --group 群名 [--batch 2-4] [--dry-run]   # 缺文件的引用摘掉
  python archive.py prune --group 群名 --dry-run  # 按黑名单回删
  python archive.py rebuild --group 群名 [--drop-batch 批次名]             # 回滚一次导入
  python archive.py check --from <目录>            # 环境体检
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "_tools"))
import clean_json as cj  # noqa: E402

CODE_DIR = Path(__file__).resolve().parent
_PARENTS = CODE_DIR.parents
_PROJECT = _PARENTS[4] if len(_PARENTS) > 4 and _PARENTS[4].name == "infosystem" else CODE_DIR
_DEFAULT_DATA = _PROJECT / "data" / "community" / "qq" / "pipeline"
ROOT = Path(os.environ.get("QQ_PIPELINE_DATA_DIR", str(_DEFAULT_DATA))).resolve()
CODE_ROOT = CODE_DIR
EXPORT_DIR = Path(os.environ.get("QQ_EXPORT_DIR", r"C:\Users\HP\Documents\QQChatExporter\exports"))
TZ = timezone(timedelta(hours=8))

MEDIA_DIRS = ("images", "files")


# ══════════════════════════════ 基础读写 ══════════════════════════════

def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def fmt_local(ts_ms: int) -> str:
    return datetime.fromtimestamp(int(ts_ms) / 1000, TZ).strftime("%Y-%m-%d %H:%M:%S")


def to_ms(local_or_iso: str) -> int:
    """'YYYY-MM-DD HH:MM:SS'（本地 UTC+8）或 ISO8601(Z) -> epoch ms。"""
    s = str(local_or_iso).strip()
    if s.endswith("Z") or "+" in s[10:]:
        s2 = s.replace("Z", "+00:00")
        try:
            return int(datetime.fromisoformat(s2).timestamp() * 1000)
        except Exception:
            pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(s, fmt).replace(tzinfo=TZ).timestamp() * 1000)
        except ValueError:
            continue
    raise SystemExit(f"无法解析时间: {local_or_iso}")


def load_json(fp: Path, default):
    try:
        return json.loads(Path(fp).read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(fp: Path, obj) -> None:
    Path(fp).write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


# ═══════════════════ TSV 单元格：会被"看成换行"的字符 ═══════════════════
# 为什么必须显式列出：Python 的 `str.splitlines()` 不只在 \n 上断行，它还会在
# \x0b(VT) \x0c(FF) \x1c \x1d \x1e \x85 \u2028 \u2029 上断行 —— 而 `\s` 正则里
# **不含 \x1c \x1d \x1e**。实测（2026-10-08，AI 群 2026-10-07 那串"哈基米"文本炸弹
# `<$ǿĀ\x1a\x0e>` 里真的带 \x0b \x1c）：8 条消息写进 消息.tsv 后被读回时劈成 14 段，
# 两段都凑不够 5 个字段 → **整条静默消失**（21888 行写进去、只读回 21880 行），
# `_meta.json` 的 total 却是 21888 —— 两边对不上才被发现。
_CELL_BREAK_RE = re.compile(r"[\t\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029]+")


def tsv_cell(s) -> str:
    """把一个字段压成"绝对不会断行"的单个物理行（各种行分隔符 → 空格）。"""
    return _CELL_BREAK_RE.sub(" ", "" if s is None else str(s))


def split_tsv_lines(text: str) -> list[str]:
    """按 **只有 \\n** 断行（不能用 splitlines：见 _CELL_BREAK_RE 的说明）。"""
    out = []
    for line in text.split("\n"):
        if line.endswith("\r"):          # 兼容 CRLF 写出的老文件
            line = line[:-1]
        out.append(line)
    return out


def load_names(fp: Path) -> dict[str, str]:
    """names.tsv -> {名字: 字母}，按行序保留。"""
    out: dict[str, str] = {}
    if Path(fp).is_file():
        for line in split_tsv_lines(Path(fp).read_text(encoding="utf-8")):
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 2:
                out[parts[1]] = parts[0]
    return out


def load_names_reverse(fp: Path) -> dict[str, str]:
    """{字母: 名字}（给 记录.md 表用）。"""
    return {v: k for k, v in load_names(fp).items()}


def save_names(fp: Path, mapping: dict[str, str]) -> None:
    lines = ["# 代号\t用户名", "# 该映射长期固定：新批次沿用已有字母，新用户名往后追加"]
    for name, lab in sorted(mapping.items(), key=lambda kv: (len(kv[1]), kv[1])):
        lines.append(f"{lab}\t{tsv_cell(name)}")
    Path(fp).write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_index(fp: Path) -> list[dict]:
    if not Path(fp).is_file():
        return []
    rows = []
    for line in split_tsv_lines(Path(fp).read_text(encoding="utf-8")):
        if not line.strip() or line.startswith("#"):
            continue
        p = line.split("\t")
        if len(p) >= 5:
            rows.append({"time": p[0], "ts": int(p[1]), "label": p[2], "text": p[3],
                         "media": p[4], "batch": p[5] if len(p) > 5 else ""})
    return rows


def save_index(fp: Path, rows: list[dict]) -> None:
    lines = ["# 本地时间\t毫秒\t代号\t文字\t媒体\t批次"]
    for r in rows:
        lines.append("\t".join([tsv_cell(r["time"]), tsv_cell(r["ts"]), tsv_cell(r["label"]),
                                tsv_cell(r["text"]), tsv_cell(r["media"]),
                                tsv_cell(r.get("batch", ""))]))
    Path(fp).write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_ocr(fp: Path) -> dict[str, str]:
    """OCR 摘要缓存。**读到就统一成小写键**。

    为什么：媒体文件名在各处大小写不一致（`消息.tsv` 里是导出的原始大小写，
    `media_keys_of_rows`/`batch_media_files` 用小写），混着比较会让"这张图识别过没有"
    永远查不到 —— 实测踩过：OCR 结果在 `ocr_full.json` 里躺着（42 张），
    但 dry-run 报"已判定 0 张、待办 79 张"，等于每次都会重跑一遍。
    """
    d = load_json(fp, {})
    return {str(k).lower(): v for k, v in d.items()} if isinstance(d, dict) else {}


def save_ocr(fp: Path, cache: dict[str, str]) -> None:
    save_json(fp, cache)


# ══════════════════════════════ 时间轴覆盖 ══════════════════════════════

def covered_ranges(meta: dict) -> list[tuple[int, int]]:
    out = []
    for r in meta.get("covered_ranges", []):
        if isinstance(r, dict) and r.get("start_ms") and r.get("end_ms"):
            out.append((int(r["start_ms"]), int(r["end_ms"])))
    return out


def merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    rs = sorted(ranges)
    out: list[list[int]] = []
    for a, b in rs:
        if out and a <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def in_ranges(ts: int, ranges: list[tuple[int, int]]) -> bool:
    for a, b in ranges:
        if a <= ts <= b:
            return True
        if a > ts:
            break
    return False


# ══════════════════════════════ 媒体归档 ══════════════════════════════

def find_in_archives(group_dir: Path, basename: str) -> Path | None:
    """在所有批次的 resources 里按 basename 找回已归档的媒体。"""
    target = basename.lower()
    for sub in MEDIA_DIRS:
        for d in (group_dir / "batches").glob(f"*/resources/{sub}"):
            if not d.is_dir():
                continue
            for f in d.iterdir():
                if f.is_file() and cj.clean_filename(f.name).lower() == target:
                    return f
    return None


def archived_media_names(group_dir: Path) -> set[str]:
    """本群所有批次里已归档的媒体文件名（clean 后的小写形式）。"""
    names: set[str] = set()
    for sub in MEDIA_DIRS:
        for d in (group_dir / "batches").glob(f"*/resources/{sub}"):
            if not d.is_dir():
                continue
            for f in d.iterdir():
                if f.is_file():
                    names.add(cj.clean_filename(f.name).lower())
    return names


def media_keys_of_rows(rows: list[dict]) -> set[str]:
    """一批记录里引用到的媒体名（clean 后小写，去重）。"""
    out: set[str] = set()
    for r in rows:
        for p in r["media"].split(","):
            kind, _, fn = p.partition(":")
            if fn:
                out.add(cj.clean_filename(fn).lower())
    return out


def media_keys_of_items(items: list) -> set[str]:
    """TimelineItem 列表里引用到的媒体名（clean 后小写，去重）。"""
    return {cj.clean_filename(fn).lower() for it in items for kind, fn in it.media if fn}


def batch_media_files(batch_dir: Path) -> set[str]:
    """某个批次目录里**物理存在**的媒体文件名（clean 后小写）。"""
    out: set[str] = set()
    for sub in MEDIA_DIRS:
        d = batch_dir / "resources" / sub
        if d.is_dir():
            for f in d.iterdir():
                if f.is_file():
                    out.add(cj.clean_filename(f.name).lower())
    return out


def export_media_names(export_dir: Path) -> set[str]:
    """导出目录 resources/ 里现有的媒体文件名（clean 后小写）。

    注意：这个目录是**所有群共用**的，所以只用来做"文件还在不在"的存在性判断
    （文件名带内容 md5，同名=同图），绝不整目录搬。
    """
    out: set[str] = set()
    for sub in MEDIA_DIRS:
        d = export_dir / "resources" / sub
        if d.is_dir():
            for f in d.iterdir():
                if f.is_file():
                    out.add(cj.clean_filename(f.name).lower())
    return out


def media_present(fn: str, archived: set[str], extra: set[str] | None = None) -> bool:
    """媒体是否"还在"：本群任一批次里归档过，或还躺在导出目录里。"""
    if not fn:
        return False
    key = cj.clean_filename(fn).lower()
    return key in archived or (extra is not None and key in extra)


def archive_media(src_dir: Path, batch_dir: Path,
                  keep: set[str] | None = None,
                  group_dir: Path | None = None) -> dict[str, str]:
    """把这次保留在 /export 里的媒体文件移进本批次（同内容已在库则只删源文件）。

    keep：**只搬本次导出真正需要的那批文件名**（clean 后小写）。不传 = 不限制（兼容旧调用）。

    为什么必须有这个白名单：QQChatExporter 的 `resources/` 是**所有导出共用的一个目录**。
    2026-09-20 实测踩坑：连续导出两个群，先导的那个群 import 时把整个 resources 搬走，
    后导的群再 import 时文件已经没了 —— 求职群的 56 张图被搬进了 AI Agent 群的历史批次。

    group_dir：传了就在**本群所有批次**里查重，已经归档过的不再搬第二份
    （否则重复导出同一张图会在每个批次各存一份）。

    返回 {basename(lower): 批次内相对路径}。
    """
    moved: dict[str, str] = {}
    left = dupe = 0
    for sub in MEDIA_DIRS:
        s = src_dir / sub
        d = batch_dir / "resources" / sub
        if not s.is_dir():
            continue
        d.mkdir(parents=True, exist_ok=True)
        for f in sorted(s.iterdir()):
            if not f.is_file():
                continue
            base = cj.clean_filename(f.name)
            key = base.lower()
            if keep is not None and key not in keep:
                left += 1
                continue
            dst = d / f.name
            if dst.exists():
                f.unlink(missing_ok=True)
                moved[key] = f"resources/{sub}/{dst.name}"
                continue
            if group_dir is not None and find_in_archives(group_dir, f.name) is not None:
                f.unlink(missing_ok=True)       # 别群/别的批次已经有同一张图了
                dupe += 1
                continue
            shutil.move(str(f), str(dst))
            moved[key] = f"resources/{sub}/{dst.name}"
    if left:
        log(f"[媒体] /export 里另有 {left} 个文件没被本档案引用，已留在原处"
            f"（多半是别的群的导出，或落在已覆盖时间轴内、整条丢掉的那批）")
    if dupe:
        log(f"[媒体] {dupe} 个文件本群别处已归档过，没再存第二份")
    return moved


def resource_root(export_dir: Path, batch_dir: Path, group_dir: Path) -> Path:
    """优先用本批次归档目录，其次导出目录，最后在旧批次里找。"""
    return batch_dir if (batch_dir / "resources").is_dir() else export_dir


# ══════════════════════════════ 报告 ══════════════════════════════

def write_range_file(group_dir: Path, meta: dict, rows: list[dict]) -> Path:
    """写「导出范围.txt」：一眼能看出"下次该从哪天往后导出"。

    rows 为 None 时只按 _meta.json 的覆盖区间写（没有逐条数据也能给范围）。
    """
    ranges = meta.get("covered_ranges") or []
    start, end = "", ""
    if ranges:
        start, end = ranges[0].get("start", ""), ranges[-1].get("end", "")
    n = len(rows) if rows is not None else meta.get("total", "?")
    lines = [
        f"# {meta.get('group', group_dir.name)} · 导出范围",
        "",
        "> 这个文件由 archive.py 自动维护（每次 import/prune/range 都会重写），别手改。",
        "",
        f"- 群号：{meta.get('peer_uid', '')}",
        f"- 已有消息：{n} 条（导入批次 {len(meta.get('batches', []))} 个）",
        f"- **起始时间：{start or '（空）'}**",
        f"- **结束时间：{end or '（空）'}**　← 下次导出选 **≥ 这个时间**",
        f"- 最后更新：{meta.get('updated', '')}",
        "",
        "## 下次怎么导出",
        "",
        "1. QQChatExporter 里把导出时间范围设成 **从上面的结束时间往后**（少一天也行，重复部分会被时间轴去重整条丢掉）；",
        "2. 导出的 json 放进默认 exports\\ 目录，删掉不要的图片/文件；",
        "3. 回到这里跑 `python archive.py import`（会自动挑最新那份 json）；",
        "4. 再跑 `python analyze.py run --group 群名` 与 `python kb.py add --group 群名`。",
        "",
    ]
    out = group_dir / "导出范围.txt"
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def write_record_md(group_dir: Path, meta: dict, names_rev: dict[str, str],
                    rows: list[dict], ocr: dict[str, str]) -> None:
    head = [
        f"# {meta['group']} · 长期归档记录",
        "",
        f"- 群号：{meta.get('peer_uid', '')}",
        f"- 本地覆盖时间轴：{meta.get('covered_text', '（空）')}",
        f"- 消息 {len(rows)} 条；批次 {len(meta.get('batches', []))} 个",
        f"- 最后更新：{meta.get('updated', '')}",
        "",
        "## 用户名代号映射（长期固定）",
        "",
        "| 代号 | 用户 |",
        "| --- | --- |",
    ]
    for lab in sorted(names_rev, key=lambda x: (len(x), x)):
        head.append(f"| {lab} | {names_rev[lab]} |")
    head += [
        "",
        "## 记录",
        "",
        "> 行首为本地时间(UTC+8)，供合并/去重使用；`↩` 是被回复内容；`[图片:x]`、`[文件:x]` 为本地留存的媒体。",
        "",
    ]
    lines: list[str] = []
    ol = {str(k).lower(): v for k, v in (ocr or {}).items()}   # 键大小写不一致，统一小写查
    for r in rows:
        seg = r["text"]
        if r["media"]:
            marks = []
            for pair in r["media"].split(","):
                if not pair.strip():
                    continue
                kind, _, fn = pair.partition(":")
                marks.append(f"[{'图片' if kind == 'image' else '文件'}:{fn}]")
            seg = (seg + " " + " ".join(marks)).strip()
        if not seg:
            continue
        lines.append(f"[{r['time']}] {r['label']}: {seg}")
        for fn in [p.partition(":")[2] for p in r["media"].split(",") if p.startswith("image:")]:
            if ol.get(fn.lower()):
                lines.append(f"    > OCR: {ol[fn.lower()]}")
    Path(group_dir / "记录.md").write_text("\n".join(head + lines) + "\n", encoding="utf-8")


def media_summary(group_dir: Path, meta: dict, rows: list[dict]) -> list[str]:
    """每个批次的媒体对账：引用几个 / 缺文件几个 / 落盘几个文件 / 孤儿几个。

    「缺文件」= 本批引用得到、但本群所有批次 + 导出目录里都没有这个文件（你删了，或导出没带）。
    「孤儿」= 这个批次目录里有文件、但**整个档案**里没有任何记录引用它。
            注意要用全档案的引用集合，不能只用本批的 —— 同一张图可能归档在 A 批、却被 B 批
            的记录引用着（跨批次共享），按本批算会把它误报成孤儿。
    两者都不影响文字内容，但一个说明"图没了"，一个说明"文件白占了地方"。
    """
    archived = archived_media_names(group_dir)
    extra = export_media_names(EXPORT_DIR)
    all_refs = media_keys_of_rows(rows)
    out: list[str] = []
    for b in meta.get("batches", []):
        name = b.get("batch", "")
        seg = [r for r in rows if r.get("batch") == name]
        refs = media_keys_of_rows(seg)
        files = batch_media_files(group_dir / "batches" / name)
        missing = {fn for fn in refs if not media_present(fn, archived, extra)}
        orphans = files - all_refs
        note = b.get("note", "")
        mn = b.get("media_note", "")
        if note and mn:
            note = f"{note}；{mn}"
        elif mn:
            note = mn
        out.append(f"| {name} | {b.get('imported', '')} | {b.get('range_local', '')} | "
                   f"{len(seg)} | {b.get('skipped_covered', 0)} | {len(refs)} | "
                   f"{len(missing)} | {len(files)} | {len(orphans)} | {note} |")
    return out


def write_status_md(group_dir: Path, meta: dict, names_rev: dict[str, str],
                    rows: list[dict], ocr: dict[str, str]) -> None:
    lines = [
        f"# {meta['group']} · 归档状态",
        "",
        f"- 群号：{meta.get('peer_uid', '')}",
        f"- 消息总数：{len(rows)}",
        f"- 覆盖时间轴（本地时间 UTC+8）：{meta.get('covered_text', '')}",
        f"- 黑名单丢弃（累计）：{sum(b.get('dropped_blacklist', 0) for b in meta.get('batches', []))} 条",
        f"- OCR 缓存：{len(ocr)} 张图片",
        f"- 最后更新：{meta.get('updated', '')}",
        "",
        "## 批次明细",
        "",
        "> 「引用」= 这批记录提到几个媒体文件；「缺文件」= 提到但本地没有（R37 默认照留，"
        "要清掉跑 `archive.py prune-missing`）；「孤儿」= 文件在、没人引用（重复导入的重叠段留下的）。"
        "所有列都是**按当前档案现算**的，回滚/对账之后自动是对的。"
        "整份导出没带媒体文件的批次（R44）引用为 0 —— 那两步对它自动跳过。",
        "",
        "| 批次 | 导入时间 | 导出范围 | 现有消息 | 跳过(已覆盖) | 引用 | 缺文件 | 文件 | 孤儿 | 说明 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    lines += media_summary(group_dir, meta, rows)
    lines += ["", "## 用户名映射（长期固定）", "", "| 代号 | 用户 |", "| --- | --- |"]
    for lab in sorted(names_rev, key=lambda x: (len(x), x)):
        lines.append(f"| {lab} | {names_rev[lab]} |")
    Path(group_dir / "状态.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ══════════════════════════════ 导入 ══════════════════════════════

def resolve_group_dir(group: str | None, chat_name: str, peer_uid: str) -> Path:
    if group:
        return ROOT / group
    # 已存在同群（peer_uid 相同）的目录则复用
    for d in ROOT.iterdir():
        if not d.is_dir() or d.name.startswith("_"):
            continue
        m = load_json(d / "_meta.json", {})
        if m.get("peer_uid") and m.get("peer_uid") == peer_uid:
            return d
    return ROOT / chat_name


def pick_input(src: Path) -> Path:
    if src.is_dir():
        return cj.pick_default_input(str(src))
    if not src.is_file():
        raise SystemExit(f"输入不存在: {src}")
    return src


def cmd_import(args) -> int:
    if getattr(args, "ocr", False):
        # R40：解析聊天记录时不允许触发 OCR。开关挂在 import 上，等于"每次导入都可能顺手
        # 把几百张图跑一遍"（2026-09-20 就是这么越界的）。OCR 统一走 analyze.py ocr。
        raise SystemExit(
            "import 不再做 OCR（R40：解析聊天记录时不允许触发 OCR）。\n"
            "  先正常导入，然后**手动、限定范围**地识别：\n"
            "    python archive.py import --from <导出目录> [--group 群名]\n"
            "    python analyze.py ocr --group <群名> --batch 2-4      # 或 --since/--until/--limit\n"
            "  图片本体已由 import 一并搬进 <群>\\batches\\<批次>\\resources\\，不会丢。")
    src = Path(args.from_dir).expanduser().resolve()
    json_fp = pick_input(src)
    export_dir = json_fp.parent
    cleaner = cj.Cleaner(json_fp, keep_system=args.keep_system,
                         blacklist=cj.load_blacklist(args.blacklist or cj.DEFAULT_BLACKLIST))
    if any(cleaner.blacklist.values()):
        log(f"[黑名单] 规则：text {len(cleaner.blacklist['text'])} 条 / "
            f"who {len(cleaner.blacklist['who'])} 条")

    group_dir = resolve_group_dir(args.group, cleaner.chat_name, cleaner.peer_uid)
    group_dir.mkdir(parents=True, exist_ok=True)
    batches_dir = group_dir / "batches"
    batches_dir.mkdir(exist_ok=True)

    # 本群已归档的媒体：引用到它们时不算「被删」（文件早在上一批搬进档案了）
    known_media = archived_media_names(group_dir)
    if known_media:
        cleaner.extra_media = known_media
        log(f"[媒体] 本群档案里已有 {len(known_media)} 个媒体文件"
            f"（引用到旧的，按存在处理）")

    meta_fp = group_dir / "_meta.json"
    names_fp = group_dir / "names.tsv"
    index_fp = group_dir / "消息.tsv"
    ocr_fp = group_dir / "ocr_cache.json"

    meta = load_json(meta_fp, {})
    meta.setdefault("group", group_dir.name)
    meta["peer_uid"] = cleaner.peer_uid or meta.get("peer_uid", "")
    existing_names = load_names(names_fp)
    if existing_names:
        log(f"[映射] 载入既有映射 {len(existing_names)} 个用户名（字母保持不变）")
    elif args.names_from:
        # 首次归档时，沿用历史上一次性清洗结果里的映射，保证字母不漂移
        nf = Path(args.names_from).expanduser()
        got: dict[str, str] = {}
        if nf.is_file() and nf.suffix == ".tsv" and nf.name == "names.tsv":
            got = load_names(nf)
        elif nf.is_file():
            got = parse_names_from_md(nf)
        if got:
            existing_names = got
            log(f"[映射] 从 {nf.name} 继承旧映射 {len(got)} 个用户名")
    ocr = load_ocr(ocr_fp)
    rows = load_index(index_fp)
    ranges = covered_ranges(meta)
    bootstrap = None
    if args.covered_until and not meta.get("batches"):        # 首次归档：exporter 缓存里重新塞回来的早期时间段已被人工处理过，
        # 直接把它标成“已覆盖”，避免把早期消息/文件重新入库。
        bootstrap = (0, to_ms(args.covered_until))
        ranges = merge_ranges(ranges + [bootstrap])
        log(f"[引导] 视 {fmt_local(bootstrap[1])} 之前为已处理时间段（不入库）")

    # 导出自身声明的范围，用于「新增区间」判断
    stat = cleaner.raw.get("statistics") or {}
    tr = stat.get("timeRange") or {}
    exp_start = to_ms(tr.get("start") or 0) if tr.get("start") else 0
    exp_end = to_ms(tr.get("end") or 0) if tr.get("end") else 0

    # 解析（沿用长期映射）
    items = cleaner.parse_timeline(existing_names)
    if not items:
        raise SystemExit("这次导出没有解析出任何消息")
    parse_start, parse_end = items[0].ts, items[-1].ts
    log(f"[解析] 保留 {cleaner.stats['kept']}/{cleaner.stats['total']} 条，"
        f"范围 {fmt_local(parse_start)} ~ {fmt_local(parse_end)}")
    cleaner.media_warn()

    # 时间轴去重：落在已覆盖区间内的整条丢弃（不复活任何文件）
    new_items, skipped = [], 0
    skipped_media = 0
    for it in items:
        if in_ranges(it.ts, ranges):
            skipped += 1
            skipped_media += len(it.media)
            continue
        new_items.append(it)
    if not new_items:
        log(f"[去重] 本次导出全部落在已覆盖时间轴内，无新增消息（跳过 {skipped} 条）")
    if skipped:
        log(f"[去重] 落在已覆盖时间轴内、整条丢弃 {skipped} 条"
            f"（连同其 {skipped_media} 个媒体引用，不看文件是否存在）")

    if args.dry_run:
        log("[dry-run] 不写任何文件。预览前 30 条新增：")
        for it in new_items[:30]:
            log(f"  [{it.time_local}] {cleaner.label_of_name(it.sender_name)}: "
                f"{it.text[:60]} {it.media_marks()}")
        log(f"[dry-run] 新增 {len(new_items)} 条 / 跳过 {skipped} 条 / "
            f"媒体引用 {cleaner.stats['media_ok'] + cleaner.stats['media_missing']} 个"
            f"（缺文件 {cleaner.stats['media_missing']} 个，按 R37 照留）")
        if cleaner.stats.get("media_unexported"):
            log(f"[dry-run] 另：本次导出没带媒体文件（{cleaner.stats['media_unexported']} 个 "
                f"image/file 元素无 localPath）→ 按 R44 不登记媒体引用，第③④步自动跳过")
        return 0

    # 批次目录
    n = len(meta.get("batches", [])) + 1
    stamp = time.strftime("%Y%m%d_%H%M%S")
    batch_name = f"batch-{n:03d}_{stamp}"
    batch_dir = batches_dir / batch_name
    (batch_dir / "resources").mkdir(parents=True, exist_ok=True)
    shutil.copy2(json_fp, batch_dir / json_fp.name)

    # 媒体归档：只搬「真正要进档案的」文件 —— 白名单 = 本批新记录的引用
    #                                        ∪ 旧记录里还缺文件的引用（后来补导的图能补上）
    # 绝不能整目录搬：resources/ 是所有群共用的（2026-09-20 踩过）。
    # 也绝不能把「落在已覆盖时间轴内、整条丢掉」的那批消息算进来：那会让重叠段的
    # 图片以「孤儿文件」的形式留在新批次里（R41 实测 9 个）。
    want = media_keys_of_items(new_items)
    already_before = archived_media_names(group_dir)
    if rows:
        repair = {fn for fn in media_keys_of_rows(rows) if fn not in already_before}
        if repair - want:
            log(f"[媒体] 档案里有 {len(repair - want)} 个旧引用仍缺文件，这次导出里如果有就一并补上")
        want |= repair
    archived = archive_media(export_dir / "resources", batch_dir,
                             keep=want, group_dir=group_dir)
    batch_files = sum(1 for sub in MEDIA_DIRS
                      for _ in (batch_dir / "resources" / sub).glob("*") if _.is_file())
    log(f"[媒体] 从 /export 搬入本批次 {len(archived)} 个（本批次共 {batch_files} 个文件）")

    # R37：不再因为「本地没文件」丢消息 —— 引用照记，缺的只记数；
    # 要不要按缺文件回删，是你的手动动作（archive.py prune-missing）。
    kept_items = list(new_items)
    kept_media = sum(len(it.media) for it in kept_items)
    archived_now = archived_media_names(group_dir)
    missing_media = sum(1 for it in kept_items for kind, fn in it.media
                        if not media_present(fn, archived_now))
    if missing_media:
        log(f"[媒体] 本批 {missing_media}/{kept_media} 个引用本地没有文件 —— "
            f"照留（R37）。要清掉：archive.py prune-missing --group \"{group_dir.name}\" "
            f"--batch {batch_name.split('_')[0].replace('batch-', '')}")
    log(f"[媒体] 本批次落库媒体引用 {kept_media} 个（新搬入文件 {len(archived)} 个）")
    log(f"[新增] 写入 {len(kept_items)} 条消息")

    # 落盘
    for it in kept_items:
        rows.append({
            "time": it.time_local, "ts": it.ts,
            "label": cleaner.label_of_name(it.sender_name), "text": it.text,
            "media": ",".join(f"{k}:{fn}" for k, fn in it.media), "batch": batch_name,
        })
    rows.sort(key=lambda r: r["ts"])
    save_index(index_fp, rows)
    save_names(names_fp, cleaner.name_map())

    new_range = (min(i.ts for i in kept_items), max(i.ts for i in kept_items)) if kept_items else None
    if new_range:
        ranges = merge_ranges(ranges + [new_range])
    meta["covered_ranges"] = [{"start_ms": a, "end_ms": b,
                               "start": fmt_local(a), "end": fmt_local(b)} for a, b in ranges]
    meta["covered_text"] = "；".join(f"{fmt_local(a)} ~ {fmt_local(b)}" for a, b in ranges)
    meta["batches"] = meta.get("batches", []) + [{
        "batch": batch_name,
        "imported": time.strftime("%Y-%m-%d %H:%M:%S"),
        "json": json_fp.name,
        "range_local": f"{fmt_local(parse_start)} ~ {fmt_local(parse_end)}",
        "export_range": f"{fmt_local(exp_start)} ~ {fmt_local(exp_end)}",
        "added": len(kept_items),
        "skipped_covered": skipped,
        "missing_media": missing_media,
        "dropped_blacklist": cleaner.stats.get("dropped_blacklist", 0),
        "kept_media": kept_media,
        "archived_media": len(archived),
        "media_unexported": cleaner.stats.get("media_unexported", 0),
        "media_note": (
            f"本次导出没带媒体文件（{cleaner.stats['media_unexported']} 个 image/file 元素无 "
            f"localPath）→ 按 R44 不登记引用，第③④步自动跳过"
            if cleaner.stats.get("media_unexported") else ""),
        "note": args.note or "",
    }]
    meta["total"] = len(rows)
    meta["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_json(meta_fp, meta)

    names_rev = load_names_reverse(names_fp)
    save_ocr(ocr_fp, ocr)
    write_record_md(group_dir, meta, names_rev, rows, ocr)
    write_status_md(group_dir, meta, names_rev, rows, ocr)
    write_range_file(group_dir, meta, rows)

    if cleaner.stats.get("dropped_blacklist"):
        log(f"[黑名单] 本次命中丢弃 {cleaner.stats['dropped_blacklist']} 条")
        for h in cleaner.blacklist_hits[:5]:
            log(f"        {h['hit']} | {h['who']} | {h['text'][:70]}")
    log(f"[OK] {group_dir}")
    log(f"     记录.md / 消息.tsv / 状态.md / 导出范围.txt；覆盖时间轴：{meta['covered_text']}")
    log(f"     下次导出请选 ≥ {meta['covered_ranges'][-1]['end'] if meta.get('covered_ranges') else '?'}")
    return 0


def cmd_prune(args) -> int:
    """按当前黑名单回删已有档案里的消息（改 blacklist.txt 之后的补救手段）。"""
    gd = ROOT / args.group
    index_fp = gd / "消息.tsv"
    if not index_fp.is_file():
        raise SystemExit(f"没有档案：{index_fp}")
    bl = cj.load_blacklist(args.blacklist or cj.DEFAULT_BLACKLIST)
    if not any(bl.values()):
        raise SystemExit(f"黑名单是空的：{args.blacklist or cj.DEFAULT_BLACKLIST}")
    meta_fp = gd / "_meta.json"
    meta = load_json(meta_fp, {})
    rows = load_index(index_fp)
    names_rev = load_names_reverse(gd / "names.tsv")
    keep, hits = [], []
    for r in rows:
        sender = names_rev.get(r["label"], "")
        why = cj.blacklist_hit(r["text"], sender, [], bl)
        if why:
            hits.append((r["time"], r["label"], why, r["text"][:80]))
        else:
            keep.append(r)
    print(f"{gd.name}：{len(rows)} 条 → 命中黑名单 {len(hits)} 条 → 保留 {len(keep)} 条")
    for h in hits[:20]:
        print(f"  {h[0]} {h[1]} [{h[2]}] {h[3]}")
    if len(hits) > 20:
        print(f"  …另有 {len(hits) - 20} 条")
    if args.dry_run:
        log("[dry-run] 没有改动任何文件。")
        return 0
    if not hits:
        log("没有命中的消息，档案不用改。")
        return 0
    save_index(index_fp, keep)
    ocr = load_ocr(gd / "ocr_cache.json")
    meta["total"] = len(keep)
    meta["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    meta["pruned"] = (meta.get("pruned") or []) + [{
        "at": meta["updated"], "removed": len(hits),
        "rules": sorted({h[2] for h in hits}),
        "note": args.note or "黑名单回删",
    }]
    save_json(meta_fp, meta)
    write_record_md(gd, meta, names_rev, keep, ocr)
    write_status_md(gd, meta, names_rev, keep, ocr)
    write_range_file(gd, meta, keep)
    log(f"[OK] 已回删 {len(hits)} 条；消息.tsv / 记录.md / 状态.md / 导出范围.txt 已重写")
    log("     ⚠ 切批结果已过期：要继续提炼请 `python analyze.py run --group ... --resplit`；")
    log("       已进过知识库的批次请 `python kb.py add --group ... --rebuild`。")
    return 0


def select_batches(meta: dict, specs: list[str] | None) -> list[str]:
    """批次选择器 -> 批次目录名列表。'2' / '2-4' / 'batch-002' / 完整名 / 逗号分隔；缺省=全部。

    按 `_meta.json` 台账里的**导入顺序**编号（1 起），所以 `--batch 2-4` 就是
    "第 2 到第 4 次导入的那些批次"，不用去记 `batch-003_20260920_152507` 这种带时间戳的名字。
    """
    names = [b["batch"] for b in meta.get("batches", [])]
    if not specs:
        return names
    out: list[str] = []
    for spec in specs:
        for tok in str(spec).replace("，", ",").split(","):
            tok = tok.strip()
            if not tok:
                continue
            m = re.fullmatch(r"(\d+)\s*[-~到]\s*(\d+)", tok)
            if m:
                a, b = int(m.group(1)), int(m.group(2))
                a, b = min(a, b), max(a, b)
                if not (1 <= a and b <= len(names)):
                    raise SystemExit(f"批次范围 {tok} 超出 1~{len(names)}")
                out += names[a - 1:b]
                continue
            m = re.fullmatch(r"(?:batch-)?0*(\d+)", tok)
            if m:
                i = int(m.group(1))
                if not (1 <= i <= len(names)):
                    raise SystemExit(f"批次号 {tok} 超出 1~{len(names)}"
                                     + ("（这个档案还没有批次）" if not names else ""))
                out.append(names[i - 1])
                continue
            hit = [n for n in names if n == tok or n.startswith(tok)]
            if len(hit) == 1:
                out.append(hit[0])
                continue
            raise SystemExit(f"认不出的批次选择器：{tok}（用 2 / 2-4 / batch-002 这种写法）\n"
                             f"  现有批次：{('、'.join(names)) if names else '（无）'}")
    return sorted(set(out))


def row_in_scope(r: dict, batch_set: set[str] | None, since_ms: int, until_ms: int) -> bool:
    """这条记录是否落在本次处理范围内（批次 + 时间窗）。"""
    if batch_set is not None and r.get("batch") not in batch_set:
        return False
    if since_ms and r["ts"] < since_ms:
        return False
    if until_ms and r["ts"] > until_ms:
        return False
    return True


def _report_orphans(gd: Path, meta: dict, rows: list[dict],
                    sel_set: set[str] | None) -> None:
    """报告孤儿文件（在批次目录里、**全档案**没有任何记录引用），**不删**。

    口径用全档案的引用集合：同一张图可能归档在 A 批、被 B 批的记录引用（跨批共享），
    只按"本批自己的记录"算会把它误报成孤儿。实测求职群按错口径报 10 个、按对口径报 5 个。

    "删文件"是不可逆动作，交给用户；这里只把数量点出来，免得白白占地方还查不到原因。
    """
    all_refs = media_keys_of_rows(rows)
    orphans: dict[str, set[str]] = {}
    for b in meta.get("batches", []):
        name = b.get("batch", "")
        if sel_set is not None and name not in sel_set:
            continue
        rest = batch_media_files(gd / "batches" / name) - all_refs
        if rest:
            orphans[name] = rest
    if orphans:
        print(f"  ⚠ 另有 {sum(len(v) for v in orphans.values())} 个文件在批次目录里、"
              f"整个档案都没有任何记录引用（孤儿，重复导入的重叠段会留下）：")
        for name, rest in orphans.items():
            print(f"     {name}: {len(rest)} 个")
        print("     本次**不动文件**；确认无用就自己删，或留着也无害。")


def mark_media_settled(meta: dict, batches: list[str],
                       refs_removed: int, rows_removed: int) -> None:
    """往 meta 里记一笔"这些批次的图我确认过了"（R42）——只管改 dict，写盘由调用方做。

    给 `analyze.py run` 的顺序闸门看：导入之后、删图对账之前不允许往下做归纳 ——
    先提炼再回删图，就得重切批 + 重提炼 + 重建知识库（已实测返工两次）。
    **即使一张都没删也要记**：那也是一次"这些图我都留着"的决定。
    """
    meta["media_settled"] = (meta.get("media_settled") or []) + [{
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "batches": sorted(batches), "refs_removed": refs_removed, "rows_removed": rows_removed,
    }]


def pending_media_batches(meta: dict, rows: list[dict]) -> dict[str, int]:
    """{批次名: 该批媒体引用数} —— 有图、但还没做过删图对账的批次。

    没图可看的批次不算（这正是你说的"中间两步没有导出图片就可以忽略"）。
    """
    settled = {n for e in (meta.get("media_settled") or []) for n in (e.get("batches") or [])}
    n_refs: dict[str, int] = {}
    for r in rows:
        n = len([p for p in r["media"].split(",") if p.strip()])
        if n:
            n_refs[r.get("batch", "")] = n_refs.get(r.get("batch", ""), 0) + n
    return {b: n for b, n in sorted(n_refs.items()) if b and b not in settled}


def cmd_prune_missing(args) -> int:
    """按范围给媒体对账：**缺文件的引用摘掉，摘完什么都不剩的条目整条移除**（R38）。

    和旧行为（import 时自动丟）的区别：**什么时候做、对哪几批做，由你决定**。
    R37 之后 import 默认全留，所以这个命令是唯一的"按文件不存在回删"入口。

    覆盖时间轴**不动**：时间轴记的是"这段时间已经处理过"，不是"这段时间还有内容"；
    缩回去会让那些消息在下次导出时重新进来（R06 后半条）。
    """
    gd = ROOT / args.group
    index_fp = gd / "消息.tsv"
    if not index_fp.is_file():
        raise SystemExit(f"没有档案：{index_fp}")
    meta_fp = gd / "_meta.json"
    meta = load_json(meta_fp, {})
    rows = load_index(index_fp)
    sel = select_batches(meta, args.batch)
    sel_set = set(sel) if args.batch else None
    since = to_ms(args.since) if args.since else 0
    until = to_ms(args.until) if args.until else 0
    if since and until and until < since:
        raise SystemExit("--until 比 --since 早，检查一下")
    export_dir = Path(args.export).expanduser() if args.export else EXPORT_DIR
    archived = archived_media_names(gd)
    extra = export_media_names(export_dir)
    log(f"[对账] 本群已归档媒体 {len(archived)} 个；导出目录另有 {len(extra)} 个"
        f"（{export_dir / 'resources'}）")

    keep: list[dict] = []
    scanned = 0
    hit_refs: list[tuple] = []
    hit_rows: list[tuple] = []
    for r in rows:
        if not row_in_scope(r, sel_set, since, until):
            keep.append(r)
            continue
        scanned += 1
        good, bad = [], []
        for p in r["media"].split(","):
            if not p.strip():
                continue
            kind, _, fn = p.partition(":")
            (good if media_present(fn, archived, extra) else bad).append(p)
        if bad:
            hit_refs.append((r["time"], r["label"], bad, r["text"][:60]))
        r = dict(r, media=",".join(good))
        if not r["text"] and not r["media"]:
            hit_rows.append((r["time"], r["label"], r.get("batch", "")))
            continue
        keep.append(r)

    scope_txt = ("、".join(sel)) if args.batch else "全部批次"
    if args.since or args.until:
        scope_txt += f"；时间窗 {args.since or '不限'} ~ {args.until or '不限'}"
    print(f"{gd.name}：档案 {len(rows)} 条 → 本次扫描 {scanned} 条（{scope_txt}）")
    n_refs = sum(len(x[2]) for x in hit_refs)
    if hit_refs:
        print(f"  ① 缺文件的媒体引用 {n_refs} 个 → 摘掉（{len(hit_refs)} 条记录受影响）：")
        for t, lab, bad, txt in hit_refs[:12]:
            print(f"     {t} {lab}  摘掉 {'、'.join(bad)}   | {txt}")
        if len(hit_refs) > 12:
            print(f"     …另有 {len(hit_refs) - 12} 条")
    if hit_rows:
        print(f"  ② 摘完只剩空壳、整条移除 {len(hit_rows)} 条：")
        for t, lab, bn in hit_rows[:12]:
            print(f"     {t} {lab}  [{bn}]")
        if len(hit_rows) > 12:
            print(f"     …另有 {len(hit_rows) - 12} 条")
    empty = not hit_refs and not hit_rows
    if empty:
        print("  没有缺文件的引用，档案不用改。")
    _report_orphans(gd, meta, keep, sel_set)
    if args.dry_run:
        log("[dry-run] 没有改动任何文件（也不会记「已确认」，那只认你真跑一次）。")
        return 0
    if empty:
        # R42：一张都没删也要留个印 —— "这一批的图我都留着"同样是一次确认，
        # 不记的话 analyze.py 的顺序闸门会一直拦着。
        mark_media_settled(meta, sel, 0, 0)
        save_json(meta_fp, meta)
        log(f"[确认] 已记下「{len(sel)} 个批次的图确认过了（删 0 个）」→ 可以往下做归纳了")
        return 0
    save_index(index_fp, keep)
    meta["total"] = len(keep)
    meta["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    meta["pruned_missing"] = (meta.get("pruned_missing") or []) + [{
        "at": meta["updated"], "batches": sel if args.batch else [],
        "scope_rows": scanned, "refs_removed": n_refs, "rows_removed": len(hit_rows),
        "since": args.since or "", "until": args.until or "",
        "note": args.note or "",
    }]
    mark_media_settled(meta, sel, n_refs, len(hit_rows))
    save_json(meta_fp, meta)
    names_rev = load_names_reverse(gd / "names.tsv")
    ocr = load_ocr(gd / "ocr_cache.json")
    write_record_md(gd, meta, names_rev, keep, ocr)
    write_status_md(gd, meta, names_rev, keep, ocr)
    write_range_file(gd, meta, keep)
    log(f"[OK] {len(rows)} 条 → {len(keep)} 条（摘掉引用 {n_refs} 个 / 移除条目 {len(hit_rows)} 条）")
    log("     消息.tsv / _meta.json / 记录.md / 状态.md / 导出范围.txt 已重写；"
        "覆盖时间轴**没动**（这些消息不会因重导而复活）")
    log("     ⚠ 切批结果已过期：要继续提炼请 `python analyze.py run --group ... --resplit`；")
    log("       已进过知识库的批次请 `python kb.py add --group ... --rebuild`。")
    return 0


def cmd_rebuild(args) -> int:
    """重写派生文件（记录.md / 状态.md / 导出范围.txt），可选按批次**回滚**一次导入。

    `--drop-batch <批次名>`：把某次导入整体撤销 —— 删掉那一批的消息行、批次目录与批次账，
    并把覆盖时间轴缩回去。用在哪：导错了、被别的群抢了媒体文件、或导入参数写错导致
    整批消息（含媒体）被判成"已被删"而丢弃时，先回滚再重导，而不是手工改 消息.tsv。

    注意：这会改变归档内容，覆盖时间轴跟着缩小，所以那段时间的消息**可以重新导入**。
    """
    gd = ROOT / args.group
    index_fp = gd / "消息.tsv"
    meta_fp = gd / "_meta.json"
    if not index_fp.is_file():
        raise SystemExit(f"没有档案：{index_fp}")
    rows = load_index(index_fp)
    meta = load_json(meta_fp, {})

    drops = list(args.drop_batch or [])
    if drops:
        batch_names = {b["batch"] for b in meta.get("batches", [])}
        unknown = [d for d in drops if d not in batch_names]
        if unknown:
            raise SystemExit(f"这些批次不在台账里：{unknown}\n现有：{sorted(batch_names)}")
        gone = [r for r in rows if r.get("batch") in drops]
        kept = [r for r in rows if r.get("batch") not in drops]
        print(f"{gd.name}：{len(rows)} 条 → 撤掉 {drops} 的 {len(gone)} 条 → 保留 {len(kept)} 条")
        # 覆盖时间轴：把被撤批次占用的时间段整个减掉（用那批消息的真实首尾）
        cut = []
        for b in meta.get("batches", []):
            if b["batch"] in drops:
                seg = [r for r in gone if r.get("batch") == b["batch"]]
                if seg:
                    cut.append((min(r["ts"] for r in seg), max(r["ts"] for r in seg)))
        ranges = [(x["start_ms"], x["end_ms"]) for x in meta.get("covered_ranges", [])]
        ranges = subtract_ranges(ranges, cut)
        if args.dry_run:
            print(f"  [dry-run] 会删批次目录：{drops}")
            print(f"  [dry-run] 覆盖时间轴会变成：" +
                  "；".join(f"{fmt_local(a)} ~ {fmt_local(b)}" for a, b in ranges))
            return 0
        for b in meta.get("batches", []):
            if b["batch"] in drops:
                d = gd / "batches" / b["batch"]
                if d.is_dir():
                    shutil.rmtree(d)
                    print(f"  已删批次目录 {d.name}")
        meta["batches"] = [b for b in meta.get("batches", []) if b["batch"] not in drops]
        meta["covered_ranges"] = [{"start_ms": a, "end_ms": b,
                                   "start": fmt_local(a), "end": fmt_local(b)} for a, b in ranges]
        meta["covered_text"] = "；".join(f"{fmt_local(a)} ~ {fmt_local(b)}" for a, b in ranges)
        meta["total"] = len(kept)
        meta["rebuilt"] = (meta.get("rebuilt") or []) + [{
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "dropped_batches": drops, "removed": len(gone),
            "note": args.note or "回滚一次导入",
        }]
        rows = kept
    elif args.dry_run:
        print(f"{gd.name}：{len(rows)} 条（只重算派生文件）")
        return 0

    meta["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_index(index_fp, rows)
    save_json(meta_fp, meta)
    names_rev = load_names_reverse(gd / "names.tsv")
    ocr = load_ocr(gd / "ocr_cache.json")
    write_record_md(gd, meta, names_rev, rows, ocr)
    write_status_md(gd, meta, names_rev, rows, ocr)
    write_range_file(gd, meta, rows)
    log(f"[OK] 消息.tsv / _meta.json / 记录.md / 状态.md / 导出范围.txt 已重写")
    log(f"     现在 {len(rows)} 条；覆盖：{meta.get('covered_text') or '（空）'}")
    log(f"     下次导出请选 ≥ {meta['covered_ranges'][-1]['end'] if meta.get('covered_ranges') else '?'}")
    return 0


def subtract_ranges(base: list[tuple[int, int]],
                    cut: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """从 base 里减掉 cut 覆盖的时间段（区间减法，结果是"当前真正有内容"的区间）。"""
    out = list(base)
    for ca, cb in cut:
        nxt: list[tuple[int, int]] = []
        for a, b in out:
            if cb < a or ca > b:          # 不相交
                nxt.append((a, b))
                continue
            if a < ca:                    # 左侧残余
                nxt.append((a, min(ca - 1, b)))
            if b > cb:                    # 右侧残余
                nxt.append((max(cb + 1, a), b))
        out = nxt
    return merge_ranges(out)


def cmd_range(args) -> int:
    """重写「导出范围.txt」（只读 消息.tsv/_meta.json，不改档案）。"""
    groups = [args.group] if args.group else [
        p.name for p in sorted(ROOT.iterdir()) if (p / "_meta.json").is_file()]
    for g in groups:
        gd = ROOT / g
        meta = load_json(gd / "_meta.json", {})
        if not meta:
            log(f"[跳过] {g}：没有 _meta.json")
            continue
        rows = load_index(gd / "消息.tsv")
        fp = write_range_file(gd, meta, rows)
        rng = meta.get("covered_ranges") or []
        print(f"{g}：{len(rows)} 条；{rng[0]['start'] if rng else '?'} ~ "
              f"{rng[-1]['end'] if rng else '?'} → {fp.name}")
    return 0


def parse_names_from_md(fp: Path) -> dict[str, str]:
    """从旧的一次性清洗 md 里继承「字母 -> 名字」映射。"""
    out: dict[str, str] = {}
    try:
        text = Path(fp).read_text(encoding="utf-8")
    except Exception:
        return out
    for m in re.finditer(r"^\|\s*([A-Z][0-9]*)\s*\|\s*(.+?)\s*\|\s*$", text, re.M):
        out[m.group(2)] = m.group(1)
    return out


def cmd_ocr(args) -> int:
    """【已停用】旧的一口价 OCR（全群 + 只留 50 字摘要）。

    R39/R40 之后 OCR 只有一条手动路径：`analyze.py ocr`，而且必须限定范围
    （既省时间，也避免"顺手把整个群几百张图都跑了"——实测就是这么越界的）。
    """
    raise SystemExit(
        "archive.py ocr 已停用（R39/R40：OCR 必须手动且限定范围）。改用：\n"
        f"    python analyze.py ocr --group \"{args.group}\" --batch 2-4\n"
        "    也可用 --since/--until 限定时间窗、--limit 限定张数、--dry-run 先看几张。\n"
        "  （analyze.py 那版会把**全文**存进 ocr_full.json，50 字摘要回填 记录.md）")


def cmd_list(args) -> int:
    print(f"档案根目录：{ROOT}")
    for d in sorted(ROOT.iterdir()):
        if not d.is_dir() or d.name.startswith(("_", ".")):
            continue
        if not (d / "_meta.json").is_file():
            continue  # 只列真正的群档案（跳过 prompts/ 之类）
        meta = load_json(d / "_meta.json", {})
        rows = load_index(d / "消息.tsv")
        ocr = load_ocr(d / "ocr_cache.json")
        print(f"\n■ {d.name}")
        print(f"   消息 {len(rows)} 条 / 批次 {len(meta.get('batches', []))} / OCR {len(ocr)} 张")
        print(f"   覆盖：{meta.get('covered_text', '（空）')}")
        rngs = meta.get("covered_ranges") or []
        if rngs:
            print(f"   ★ 下次导出从 {rngs[-1]['end']} 往后（详见 导出范围.txt）")
        n_img = len(list((d / 'batches').glob('*/resources/images/*')))
        n_file = len(list((d / 'batches').glob('*/resources/files/*')))
        print(f"   媒体：images {n_img} / files {n_file}")
        refs = media_keys_of_rows(rows)
        archived = archived_media_names(d)
        missing = {fn for fn in refs if not media_present(fn, archived, export_media_names(EXPORT_DIR))}
        orphans: set[str] = set()
        for b in meta.get("batches", []):
            orphans |= batch_media_files(d / "batches" / b.get("batch", "")) - refs
        line = f"   对账：记录引用 {len(refs)} 个 / 缺文件 {len(missing)} 个 / 孤儿文件 {len(orphans)} 个"
        pend = pending_media_batches(meta, rows)
        if pend:
            line += (f"   ← ⏸ 有 {len(pend)} 个批次还没做「删图对账」（R42 顺序闸门会拦住归纳）："
                     + "、".join(f"batch-{int(b.split('-')[1].split('_')[0]):03d}"
                                for b in pend))
        else:
            line += ("   ← 跑 prune-missing --dry-run 看明细" if missing or orphans else " ✓")
        print(line)
    return 0


def cmd_check(args) -> int:
    src = Path(args.from_dir).expanduser()
    json_fp = pick_input(src)
    return cj.cmd_check(json_fp)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="QQ 群聊长期归档工具")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_imp = sub.add_parser("import", help="把一次新导出并入档案（去重后追加）")
    p_imp.add_argument("--from", dest="from_dir", default=str(EXPORT_DIR),
                       help="导出目录或 json（默认 QQChatExporter 的 exports 目录）")
    p_imp.add_argument("--group", help="群档案目录名（默认按 chatInfo.name 新建或按群号复用）")
    p_imp.add_argument("--ocr", action="store_true",
                       help="【已停用】OCR 必须手动、必须限范围，见 analyze.py ocr")
    p_imp.add_argument("--dry-run", action="store_true", help="只预览，不写任何文件")
    p_imp.add_argument("--keep-system", action="store_true", help="保留系统消息")
    p_imp.add_argument("--names-from", help="首次归档时沿用旧映射（clean md 或 names.tsv）")
    p_imp.add_argument("--blacklist", help=f"黑名单文件（默认 {cj.DEFAULT_BLACKLIST.name}）")
    p_imp.add_argument("--covered-until", help="引导用：把该本地时间之前视为【已处理时间段】"
                                            "（例如上一次人工清洗结束时刻 '2026-09-16 18:17:45'），"
                                            "这段内的消息不会入库")
    p_imp.add_argument("--note", help="给这次批次写个备注")
    p_imp.set_defaults(func=cmd_import)

    p_pm = sub.add_parser("prune-missing",
                          help="按范围对账媒体：缺文件的引用摘掉，摘完空的条目移除")
    p_pm.add_argument("--group", required=True)
    p_pm.add_argument("--batch", action="append",
                      help="限定批次：2 / 2-4 / batch-002（可重复；缺省=全部批次）")
    p_pm.add_argument("--since", help="只处理该本地时间之后的记录（'YYYY-MM-DD HH:MM:SS' 或日期）")
    p_pm.add_argument("--until", help="只处理该本地时间之前的记录")
    p_pm.add_argument("--export", help=f"顺带在这里的 resources/ 里找文件（默认 {EXPORT_DIR}）")
    p_pm.add_argument("--dry-run", action="store_true", help="只报告会摘掉/移除什么，不写文件")
    p_pm.add_argument("--note", help="写进 _meta.json 的备注")
    p_pm.set_defaults(func=cmd_prune_missing)

    p_ocr = sub.add_parser("ocr", help="【已停用】改用 analyze.py ocr --group 群名 --batch 2-4")
    p_ocr.add_argument("--group", required=True)
    p_ocr.set_defaults(func=cmd_ocr)

    p_list = sub.add_parser("list", help="列出档案状态")
    p_list.set_defaults(func=cmd_list)

    p_prune = sub.add_parser("prune", help="按黑名单回删已有档案里的消息")
    p_prune.add_argument("--group", required=True)
    p_prune.add_argument("--blacklist", help=f"黑名单文件（默认 {cj.DEFAULT_BLACKLIST.name}）")
    p_prune.add_argument("--dry-run", action="store_true", help="只列出会删哪些，不改文件")
    p_prune.add_argument("--note", help="写进 _meta.json 的备注")
    p_prune.set_defaults(func=cmd_prune)

    p_range = sub.add_parser("range", help="重写「导出范围.txt」（只看不改档案）")
    p_range.add_argument("--group", help="只处理某个群；缺省=全部")
    p_range.set_defaults(func=cmd_range)

    p_reb = sub.add_parser("rebuild", help="重写派生文件；--drop-batch 回滚一次导入")
    p_reb.add_argument("--group", required=True)
    p_reb.add_argument("--drop-batch", action="append",
                       help="撤掉某个批次（批次目录名，可重复）；覆盖时间轴会缩回去")
    p_reb.add_argument("--dry-run", action="store_true", help="只报告会怎么改")
    p_reb.add_argument("--note", help="写进 _meta.json 的备注")
    p_reb.set_defaults(func=cmd_rebuild)

    p_check = sub.add_parser("check", help="环境体检")
    p_check.add_argument("--from", dest="from_dir", default=str(EXPORT_DIR))
    p_check.set_defaults(func=cmd_check)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    cj.force_utf8_console()
    raise SystemExit(main())
