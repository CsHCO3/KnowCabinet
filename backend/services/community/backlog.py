"""备选调研方向（backlog）：来源无关的手动/自动项目与教程清单。

存储：
- knowledgebase/community/backlog/backlog.jsonl 机器台账，幂等合并
- knowledgebase/community/backlog/00-备选调研方向.md 人读视图

贴吧、QQ、浏览器、剪贴板、手动命令都只是不同的 source；入库后不做来源隔离。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from backend.path_setup import PROJECT_ROOT


BACKLOG_DIR = PROJECT_ROOT / "knowledgebase" / "community" / "backlog"
JSONL_PATH = BACKLOG_DIR / "backlog.jsonl"
MD_PATH = BACKLOG_DIR / "00-备选调研方向.md"

# triage（施工单 05）：报告写到 data/ 一侧，不污染知识库目录。
TRIAGE_DIR = PROJECT_ROOT / "data" / "community" / "backlog"
KB_ROOT = PROJECT_ROOT / "knowledgebase"
# URL 后面如果还是路径/域名字符，说明命中的只是更长 URL 的前缀，不算真命中
# （比如条目是 github.com/x，正文里只有 github.com/x/y，不能算"已在库"）。
_URL_TAIL = re.compile(r"[A-Za-z0-9\-._~/]")


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    url = re.sub(r"^<|>$", "", url)
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return url
    host = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    tracked = {"fr", "share_medium", "share_from", "share_token", "utm_source", "utm_medium", "utm_campaign"}
    # 只保留常见业务参数太复杂；这里删除明显的分享追踪参数即可。
    from urllib.parse import parse_qsl, urlencode
    kept = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k.lower() not in tracked]
    query = urlencode(kept)
    return f"{parsed.scheme}://{host}{path}" + (f"?{query}" if query else "")


def _key(title: str, url: str, note: str) -> str:
    normalized = re.sub(r"\s+", " ", f"{title}\n{url}\n{note}".lower()).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _note_lines(text: str) -> list[str]:
    """把备注拆成"行"：去掉列表符号/空行，逐行比较才能真去重。"""
    out: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip().lstrip("-").lstrip("•").strip()
        if line:
            out.append(line)
    return out


def _norm_line(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip().lower()


def merge_note(old_note: str, new_note: str) -> str:
    """按行合并备注（P-29）。

    旧实现拿"整段 note"跟"新 note"比：存进去的是多行拼接串，永远不等于新传进来的
    单段文本，于是每次 update 都追加一次，界面里同一句话叠到 6 遍。改为按行归一化去重。
    """
    merged: list[str] = []
    seen: set[str] = set()
    for line in _note_lines(old_note) + _note_lines(new_note):
        key = _norm_line(line)
        if key and key not in seen:
            seen.add(key)
            merged.append(line)
    return "\n- ".join(merged)


def _ensure_dir():
    BACKLOG_DIR.mkdir(parents=True, exist_ok=True)


def load_items() -> list[dict]:
    if not JSONL_PATH.exists():
        return []
    items: list[dict] = []
    with JSONL_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError:
                    # 不让一条坏行毁掉整个 backlog；保留到人工检查文件。
                    (BACKLOG_DIR / "bad_lines.jsonl").open("a", encoding="utf-8").write(line)
    return items


def save_items(items: list[dict]) -> None:
    _ensure_dir()
    tmp = JSONL_PATH.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        for item in items:
            f.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
    tmp.replace(JSONL_PATH)
    render_markdown(items)


def find_existing(items: list[dict], *, title: str, url: str, note: str) -> dict | None:
    normalized_url = _normalize_url(url)
    if normalized_url:
        for item in items:
            if _normalize_url(item.get("url", "")) == normalized_url:
                return item
    key = _key(title, url, note)
    for item in items:
        if item.get("key") == key:
            return item
    return None


def add_item(
    *,
    title: str,
    url: str = "",
    note: str = "",
    source: str = "manual",
    source_ref: str = "",
    tags: list[str] | None = None,
    status: str = "todo",
    kind: str = "project",
    local_path: str = "",
    update: bool = False,
) -> tuple[dict, bool]:
    """新增一条；URL 相同时默认拒绝，update=True 则合并 note/tags。"""
    _ensure_dir()
    title = (title or url or "").strip() or "未命名备选项目"
    note = note.strip()
    url = url.strip()
    tags = sorted({t.strip() for t in (tags or []) if t.strip()})
    items = load_items()
    old = find_existing(items, title=title, url=url, note=note)
    ts = _now()

    if old and not update:
        return old, False

    if old:
        old["note"] = merge_note(old.get("note", ""), note)
        old["tags"] = sorted(set(old.get("tags", [])) | set(tags))
        old["source_refs"] = sorted(set(old.get("source_refs", [])) | ({source_ref} if source_ref else set()))
        old["status"] = status if status != "todo" else old.get("status", "todo")
        old["local_path"] = local_path or old.get("local_path", "")
        old["updated_at"] = ts
        save_items(items)
        return old, False

    item = {
        "id": f"B{datetime.now().strftime('%Y%m%d')}-{hashlib.sha1(f'{title}{url}{ts}'.encode()).hexdigest()[:6].upper()}",
        "title": title,
        "url": url,
        "note": note,
        "source": source,
        "source_refs": [source_ref] if source_ref else [],
        "tags": tags,
        "status": status,
        "kind": kind,
        "local_path": local_path,
        "key": _key(title, url, note),
        "created_at": ts,
        "updated_at": ts,
    }
    items.append(item)
    save_items(items)
    return item, True


def update_status(item_id: str, status: str, local_path: str = "") -> dict | None:
    items = load_items()
    for item in items:
        if item.get("id") == item_id:
            item["status"] = status
            if local_path:
                item["local_path"] = local_path
            item["updated_at"] = _now()
            save_items(items)
            return item
    return None


def _match(item: dict, *, status: str = "", source: str = "", tag: str = "", query: str = "") -> bool:
    if status and item.get("status") != status:
        return False
    if source and item.get("source") != source:
        return False
    if tag and tag not in item.get("tags", []):
        return False
    if query:
        hay = " ".join(str(item.get(k, "")) for k in ("title", "url", "note", "local_path")).lower()
        if query.lower() not in hay:
            return False
    return True


def render_markdown(items: list[dict]) -> None:
    _ensure_dir()
    lines = [
        "# 00-备选调研方向",
        "",
        "> 多来源合并的待学习项目/教程/工具清单。来源不限于贴吧；手动命令、剪贴板、QQ、浏览器推荐都可写入。",
        "",
    ]
    for item in sorted(items, key=lambda x: (x.get("status", "todo"), x.get("updated_at", "")), reverse=True):
        lines.append(f"## {item['id']} · {item['title']}")
        lines.append("")
        lines.append(f"- 状态：{item.get('status', 'todo')} / 类型：{item.get('kind', 'project')}")
        lines.append(f"- 来源：{item.get('source', 'manual')}")
        if item.get("source_refs"):
            lines.append("- 来源引用：" + "；".join(item["source_refs"]))
        if item.get("url"):
            lines.append(f"- 资源：<{item['url']}>")
        if item.get("local_path"):
            lines.append(f"- 本地：`{item['local_path']}`")
        if item.get("tags"):
            lines.append("- 标签：" + "、".join(item["tags"]))
        if item.get("note"):
            lines.append("- 备注：")
            for note in str(item["note"]).split("\n- "):
                lines.append(f"  - {note}" if not note.startswith("- ") else f"  {note}")
        lines.append(f"- 创建：{item.get('created_at', '')} / 更新：{item.get('updated_at', '')}")
        lines.append("")
    tmp = MD_PATH.with_suffix(".md.tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    tmp.replace(MD_PATH)


def extract_candidates(path: Path) -> list[dict]:
    """从 txt/md/json/html 里抽取 URL；支持 `URL | 标题 | 备注` 与普通含链接文本。"""
    text = path.read_text(encoding="utf-8-sig", errors="replace") if path != Path("-") else sys.stdin.read()
    candidates = []
    for raw_line in text.splitlines():
        line = raw_line.strip().lstrip("-*> ").strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        url = next((p for p in parts if re.match(r"https?://", p)), "")
        rest = [p for p in parts if p != url]
        title = rest[0] if rest else ""
        note = " | ".join(rest[1:]) if len(rest) > 1 else ""
        if not url:
            urls = re.findall(r"https?://[^\s<>）)\]]+", line)
            if not urls:
                # 允许手动先记一个还没有链接的想法/项目名。
                title = parts[0] if parts else line
                note = " | ".join(parts[1:]) if len(parts) > 1 else ""
                candidates.append({"title": title, "url": "", "note": note})
                continue
            url = urls[0]
            title = re.sub(re.escape(url), "", line).strip(" -|：:")
        candidates.append({"title": title or url, "url": url, "note": note})
    return candidates


# ---- triage：backlog 的消化出口（施工单 05，只读判重，--apply 才写） ----


def _parse_created(ts: str) -> datetime | None:
    """created_at 兼容解析：ISO 带时区 / 带时间 / 纯日期；失败返回 None（未知龄期）。"""
    t = (ts or "").strip()
    if not t:
        return None
    dt = None
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
            try:
                dt = datetime.strptime(t, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


def _age_days(ts: str) -> int | None:
    dt = _parse_created(ts)
    if dt is None:
        return None
    return max(0, (datetime.now(dt.tzinfo) - dt).days)


def _url_core(url: str) -> tuple[str, str]:
    """判重键：host（小写）+ path（去尾斜杠），query 一律忽略。非 http(s) 返回空键。"""
    parsed = urlparse(_normalize_url(url))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ("", "")
    return (parsed.netloc.lower(), parsed.path.rstrip("/"))


def _load_kb_source_uris() -> set[tuple[str, str]]:
    db_path = PROJECT_ROOT / "data" / "kb" / "kb.db"
    out: set[tuple[str, str]] = set()
    if not db_path.exists():
        return out
    conn = sqlite3.connect(db_path)
    try:
        for (uri,) in conn.execute("select source_uri from documents where source_uri != ''"):
            core = _url_core(uri)
            if core[0]:
                out.add(core)
    finally:
        conn.close()
    return out


def _dense_texts() -> list[tuple[str, str]]:
    """只读 dense 层 md（各 topic 的 dense/ 目录），不扫全库——
    贴吧帖存档和 backlog 自己的人读视图里本来就有这些 URL，扫全库会全军误判。"""
    out: list[tuple[str, str]] = []
    if not KB_ROOT.exists():
        return out
    for f in sorted(KB_ROOT.glob("**/dense/*.md")):
        try:
            out.append((f.relative_to(PROJECT_ROOT).as_posix(), f.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue
    return out


def _url_variants(url: str) -> list[str]:
    """正文 grep 的候选串：归一化形式 + http/https 互换 + 带/不带尾斜杠。"""
    core = _url_core(url)
    if not core[0]:
        normalized = _normalize_url(url)
        return [normalized] if normalized else []
    scheme = "http" if _normalize_url(url).startswith("http://") else "https"
    other = "https" if scheme == "http" else "http"
    bases = {f"{s}://{core[0]}{core[1]}" for s in (scheme, other)}
    return sorted(bases | {b + "/" for b in bases})


def _url_in_text(variants: list[str], text: str) -> bool:
    for cand in variants:
        idx = text.find(cand)
        while idx != -1:
            end = idx + len(cand)
            nxt = text[end] if end < len(text) else ""
            if not nxt or not _URL_TAIL.match(nxt):
                return True
            idx = text.find(cand, idx + 1)
    return False


def _local_path_of(item: dict) -> str:
    """已有本地副本判据：结构化字段 local_path 优先，兼容 note 行 `本地：路径`。"""
    local = (item.get("local_path") or "").strip()
    if local:
        return local
    for line in _note_lines(item.get("note", "")):
        m = re.match(r"本地[:：]\s*(.+)$", line)
        if m:
            return m.group(1).strip().strip("`")
    return ""


def _load_bili_repo_map() -> dict[tuple[str, str], list[str]]:
    """bili.db repo_urls 是 apply_kb 落库时记的仓库链接，但不在施工单判重定义
    （source_uri + dense 正文）里，triage 只拿它做"疑似已在库"提示，不改状态。"""
    db_path = PROJECT_ROOT / "data" / "bili.db"
    out: dict[tuple[str, str], list[str]] = {}
    if not db_path.exists():
        return out
    try:
        conn = sqlite3.connect(db_path)
        try:
            for bvid, raw in conn.execute("select bvid, repo_urls from videos"):
                try:
                    urls = json.loads(raw) if raw else []
                except json.JSONDecodeError:
                    continue
                if not isinstance(urls, list):
                    continue
                for u in urls:
                    core = _url_core(str(u))
                    if core[0]:
                        out.setdefault(core, []).append(bvid)
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    return out


def _triage_classify(items: list[dict]) -> list[dict]:
    kb_uris = _load_kb_source_uris()
    dense = _dense_texts()
    bili_repo = _load_bili_repo_map()
    records: list[dict] = []
    for item in items:
        core = _url_core(item.get("url", ""))
        in_kb = ""
        if core[0] and core in kb_uris:
            in_kb = "documents.source_uri"
        if not in_kb and core[0]:
            variants = _url_variants(item.get("url", ""))
            hits = [rel for rel, text in dense if _url_in_text(variants, text)]
            if hits:
                in_kb = "dense 正文：" + "、".join(hits)
        local = _local_path_of(item)
        records.append({
            "item": item,
            "in_kb": in_kb,
            "local": local,
            "local_exists": Path(local).exists() if local else None,
            "age": _age_days(item.get("created_at", "")),
            "bili_bv": bili_repo.get(core, []) if core[0] else [],
        })
    return records


def _triage_line(rec: dict) -> str:
    it = rec["item"]
    line = f"- [{it.get('source', 'manual')}] {it.get('title', '')}"
    if it.get("url"):
        line += f" · {it['url']}"
    line += f" · {it.get('status', '')} · 创建于 {(it.get('created_at', '') or '')[:10] or '未知'}"
    line += f" · {it.get('id', '')}"
    extras = []
    if rec["in_kb"]:
        extras.append(f"已在库（{rec['in_kb']}）→ 建议置 done")
    if rec["local"]:
        suffix = "" if rec["local_exists"] else "（路径已不存在）"
        extras.append(f"本地：`{rec['local']}`{suffix}")
    if rec["bili_bv"]:
        extras.append("bili.db repo_urls：" + "、".join(rec["bili_bv"]))
    if extras:
        line += "（" + "；".join(extras) + "）"
    return line


def _over90(records: list[dict]) -> list[dict]:
    return [r for r in records if r["item"].get("status") == "todo" and r["age"] is not None and r["age"] > 90]


def _write_triage_report(records: list[dict], applied: list[dict], apply_mode: bool) -> Path:
    TRIAGE_DIR.mkdir(parents=True, exist_ok=True)
    items = [r["item"] for r in records]
    todo = [r for r in records if r["item"].get("status") == "todo"]
    in_kb = [r for r in records if r["in_kb"]]
    local = [r for r in records if r["local"]]
    over90 = _over90(records)
    bili = [r for r in records if r["bili_bv"]]

    def _counts(pairs) -> str:
        return " · ".join(f"{k or '（空）'} {v}" for k, v in sorted(pairs)) or "（无）"

    age_counts = Counter()
    for r in records:
        if r["age"] is None:
            age_counts["未知龄期"] += 1
        elif r["age"] > 90:
            age_counts[">90 天"] += 1
        elif r["age"] > 30:
            age_counts["31–90 天"] += 1
        else:
            age_counts["≤30 天"] += 1

    if apply_mode:
        mode = f"--apply（已置 done {len(applied)} 条）"
    else:
        mode = "dry-run（台账未改动）"
    lines = [
        "# backlog triage",
        "",
        f"- 生成时间：{_now()}",
        f"- 台账：knowledgebase/community/backlog/backlog.jsonl（共 {len(items)} 条）",
        "- 判重范围（不联网）：data/kb/kb.db documents.source_uri ＋ knowledgebase/**/dense/*.md 正文",
        f"- 本次执行：{mode}",
        "",
        "## 分组统计",
        "",
        f"- 按 status：{_counts(Counter(str(i.get('status', '')) for i in items).items())}",
        f"- 按 source：{_counts(Counter(str(i.get('source', '')) for i in items).items())}",
        f"- 按龄期：{_counts(age_counts.items())}",
        "",
        f"## 待办（{len(todo)}）",
        "",
    ]
    lines += [_triage_line(r) for r in todo] or ["（无）"]
    lines += ["", f"## 已在库（{len(in_kb)}）", ""]
    lines += [_triage_line(r) for r in in_kb] or [
        "（无——没有任何条目的 URL 命中 documents.source_uri 或 dense 正文）"
    ]
    lines += ["", f"## 有本地副本（{len(local)}）", ""]
    lines += [_triage_line(r) for r in local] or ["（无）"]
    lines += [
        "",
        f"## 超 90 天（{len(over90)}）",
        "",
        "> 这些 todo 已经放了一个季度以上：要么安排做，要么手动 `backlog status <id> rejected`。",
        "",
    ]
    lines += [_triage_line(r) for r in over90] or ["（无）"]
    if bili:
        lines += [
            "",
            f"## 疑似已在库（bili.db repo_urls 命中；不在施工单判重定义内，上面未计入，--apply 也不动它们）（{len(bili)}）",
            "",
        ]
        lines += [_triage_line(r) for r in bili]
    if applied:
        lines += ["", "## 本次 --apply 置 done", ""]
        for it in applied:
            lines.append(f"- {it['id']} {it.get('title', '')} · {it.get('url', '')}")
    tmp = TRIAGE_DIR / f"triage_{datetime.now().strftime('%Y%m%d')}.md.tmp"
    report = tmp.with_suffix("")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    tmp.replace(report)
    return report


def register_parser(sp: argparse._SubParsersAction) -> None:
    p = sp.add_parser("backlog", aliases=["research-backlog"], help="备选调研方向：手动记录/导入/查看")
    sub = p.add_subparsers(dest="action", required=True)

    add = sub.add_parser("add", help="随手添加：URL、标题或短描述")
    add.add_argument("what", help="URL、标题或短描述；也可写 `标题 | URL | 备注`")
    add.add_argument("--url", default="")
    add.add_argument("--note", default="")
    add.add_argument("--source", default="manual")
    add.add_argument("--source-ref", default="")
    add.add_argument("--tags", default="项目,备选调研", help="逗号分隔")
    add.add_argument("--status", default="todo", choices=["todo", "learning", "done", "rejected"])
    add.add_argument("--kind", default="project", choices=["project", "tutorial", "tool", "idea"])
    add.add_argument("--local-path", default="")
    add.add_argument("--update", action="store_true", help="URL 已存在时合并备注/标签")
    add.set_defaults(func=cmd_add)

    imp = sub.add_parser("import", help="从 txt/md/json/html 抽取 URL 批量添加；`-` 表示 stdin")
    imp.add_argument("path")
    imp.add_argument("--source", default="manual-import")
    imp.add_argument("--source-ref", default="")
    imp.add_argument("--tags", default="项目,备选调研")
    imp.add_argument("--update", action="store_true")
    imp.set_defaults(func=cmd_import)

    lst = sub.add_parser("list", help="查看清单")
    lst.add_argument("--status", default="")
    lst.add_argument("--source", default="")
    lst.add_argument("--tag", default="")
    lst.add_argument("--query", default="")
    lst.add_argument("--json", action="store_true")
    lst.set_defaults(func=cmd_list)

    done = sub.add_parser("status", help="改状态，例如 done/learning/rejected")
    done.add_argument("item_id")
    done.add_argument("value", choices=["todo", "learning", "done", "rejected"])
    done.add_argument("--local-path", default="")
    done.set_defaults(func=cmd_status)

    tri = sub.add_parser("triage", help="消化报告：分组统计/判重/超90天/本地副本；默认 dry-run")
    tri.add_argument(
        "--apply",
        action="store_true",
        help="显式开关：只把「URL 已在知识库」的 todo/learning 条目置 done，其余状态一律不动",
    )
    tri.set_defaults(func=cmd_triage)


def _utf8():
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure") and (s.encoding or "").lower() != "utf-8":
            s.reconfigure(encoding="utf-8", errors="replace")


def cmd_add(args: argparse.Namespace) -> int:
    _utf8()
    title, url, note = args.what, args.url, args.note
    if "|" in args.what:
        parts = [x.strip() for x in args.what.split("|")]
        if len(parts) >= 2:
            if re.match(r"https?://", parts[0]):
                url, title = parts[0], parts[1] if len(parts) > 1 else url
                note = parts[2] if len(parts) > 2 else note
            else:
                title = parts[0]
                if re.match(r"https?://", parts[1]):
                    url = parts[1]
                note = parts[2] if len(parts) > 2 else note
    elif re.match(r"https?://", args.what):
        url = args.what
        title = urlparse(url).netloc + urlparse(url).path

    item, created = add_item(
        title=title, url=url, note=note, source=args.source, source_ref=args.source_ref,
        tags=[x.strip() for x in args.tags.split(",") if x.strip()], status=args.status,
        kind=args.kind, local_path=args.local_path, update=args.update,
    )
    print(("已新增 " if created else "已存在并更新 ") + item["id"] + "：" + item["title"])
    print("清单：" + str(MD_PATH))
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    _utf8()
    path = Path(args.path)
    candidates = extract_candidates(path)
    created_count = updated_count = 0
    for c in candidates:
        item, created = add_item(
            title=c["title"], url=c["url"], note=c["note"], source=args.source,
            source_ref=args.source_ref or str(path),
            tags=[x.strip() for x in args.tags.split(",") if x.strip()],
            update=args.update,
        )
        created_count += int(created)
        updated_count += int(not created)
    print(f"抽取 {len(candidates)} 条：新增 {created_count}，合并/已存在 {updated_count}")
    print("清单：" + str(MD_PATH))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    _utf8()
    items = [
        x for x in load_items()
        if _match(x, status=args.status, source=args.source, tag=args.tag, query=args.query)
    ]
    if args.json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
    else:
        for x in items:
            print(f"{x['id']} [{x.get('status')}/{x.get('source')}] {x['title']}")
            if x.get("url"):
                print(f"  {x['url']}")
            if x.get("local_path"):
                print(f"  本地：{x['local_path']}")
        print(f"共 {len(items)} 条")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    _utf8()
    item = update_status(args.item_id, args.value, args.local_path)
    if not item:
        print(f"找不到 {args.item_id}")
        return 2
    print(f"已更新 {item['id']} -> {item['status']}")
    return 0


def cmd_triage(args: argparse.Namespace) -> int:
    _utf8()
    applied: list[dict] = []
    if args.apply:
        pre = _triage_classify(load_items())
        targets = [r for r in pre if r["in_kb"] and r["item"].get("status") in ("todo", "learning")]
        skipped = [r for r in pre if r["in_kb"] and r["item"].get("status") not in ("todo", "learning")]
        for r in targets:
            updated = update_status(r["item"]["id"], "done")
            if updated:
                applied.append(updated)
        for r in skipped:
            it = r["item"]
            print(f"跳过（status={it.get('status')}，--apply 只自动处理 todo/learning）：{it.get('id')} {it.get('title', '')}")

    records = _triage_classify(load_items())
    report = _write_triage_report(records, applied, args.apply)
    todo_n = sum(1 for r in records if r["item"].get("status") == "todo")
    print(
        f"backlog triage：共 {len(records)} 条 ｜ 待办 {todo_n}"
        f" ｜ 已在库 {sum(1 for r in records if r['in_kb'])}"
        f" ｜ 本地副本 {sum(1 for r in records if r['local'])}"
        f" ｜ 超 90 天 {len(_over90(records))}"
    )
    if args.apply:
        print(f"--apply：已把 {len(applied)} 条「已在库」条目置 done")
    else:
        print("dry-run：台账未改动（加 --apply 仅把「已在库」的 todo/learning 条目置 done）")
    print(f"报告：{report}")
    return 0
