"""贴吧楼层 → 原子知识点；不保留全帖流水。"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import unquote


THEMES = [
    "00-备选调研方向",
    "01-模型与Agent行为",
    "02-上下文压缩与长程工作",
    "03-会话加载与运行架构",
    "04-插件工具与工作流",
    "05-角色卡与AI创作",
    "06-风险故障与限制",
]

THEMES_TEXT = "\n".join(THEMES)

PROJECT_RE = re.compile(r"github\.com|gitee\.com|gitlab\.com|huggingface\.co|插件|开源|仓库|教程|手册|工具", re.I)
PROJECT_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:github\.com|gitee\.com|gitlab\.com|huggingface\.co)/[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)?",
    re.I,
)
NOISE_RE = re.compile(
    r"^(围观|前排|沙发|路过|支持|谢谢|感谢|哈哈|哈哈哈|好的|确实|同问|帮顶|顶|6+|666+)[\s！!。.~]*$",
    re.I,
)
TECH_RE = re.compile(
    r"DSH|DeepSeek|Claude|OpenAI|Gemini|Manus|Agent|API|token|上下文|压缩|会话|内存|OOM|插件|工具|模型|部署|命令|报错|"
    r"版本|RC\b|Alpha\b|缓存|命中|价格|额度|计费|GitHub|开源|教程|手册|prompt|提示词|工作流|SillyTavern|角色卡|写作|"
    r"小说|代码|编程|Rust|TypeScript|TS\b|cordis|形式化|数据库|世界书|正则",
    re.I,
)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _unwrap_links(text: str) -> str:
    """贴吧的 `mo/q/checkurl?url=<百分号编码的真链接>&urlrefer=...` 还原成真链接。

    2026-10-20 修：旧实现把 group(1)（真链接本体）当 query string 丢给 parse_qs，
    永远取不到 `url` 键，等于没还原；这里直接 unquote。
    """
    def repl(match: re.Match) -> str:
        try:
            return unquote(match.group(1))
        except Exception:
            return match.group(0)
    return re.sub(
        r"https?://tieba\.baidu\.com/mo/q/checkurl\?url=([^&\s]+)(?:&urlrefer=[^&\s]*)?",
        repl,
        text,
    )


def _safe_name(value: str) -> str:
    return re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", value).strip("._")[:80]


def render_kb_markdown(state: dict) -> str:
    """知识点 → `knowledgebase` 可索引的 md（`kb-scan --source external` 的真相源）。"""
    title = (state.get("title") or "").strip() or f"贴吧帖子 {state.get('tid', '')}"
    entries = state.get("entries") or []
    by_marker = {
        value.get("marker"): value
        for value in (state.get("evidence") or {}).values()
        if isinstance(value, dict)
    }
    lines = [
        f"# 贴吧知识：{title}",
        "",
        f"- 来源：{state.get('url', '')}",
        f"- 吧：{state.get('forum_name', '')}",
        f"- 更新：{state.get('updated_at') or _now()}",
        f"- 知识点：{len(entries)} 条",
        "",
    ]
    for entry in entries:
        lines.append(f"## {entry.get('object') or entry.get('id')}")
        lines.append("")
        lines.append(f"- **{entry.get('kind', 'fact')}**：{entry.get('statement', '')}")
        if entry.get("resource_url"):
            lines.append(f"- 资源：<{entry['resource_url']}>")
        lines.append("- 来源：" + "、".join(entry.get("source_markers") or []))
        for marker in entry.get("source_markers") or []:
            text = ((by_marker.get(marker) or {}).get("text") or "").strip()
            if text:
                lines.append(f"- 证据[{marker}]：{text}")
        lines.append("")
    return "\n".join(lines)


def write_kb_markdown(state: dict) -> Path | None:
    """写 `knowledgebase/community/tieba/knowledge/tieba-thread-{tid}.md`，供 kb-scan 索引。"""
    tid = str(state.get("tid") or "").strip()
    if not tid or not state.get("entries"):
        return None
    from backend.path_setup import PROJECT_ROOT

    path = PROJECT_ROOT / "knowledgebase" / "community" / "tieba" / "knowledge" / f"tieba-thread-{tid}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(render_kb_markdown(state), encoding="utf-8", newline="\n")
    tmp.replace(path)
    return path


def parse_json_text(text: str) -> dict:
    text = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError(f"LLM did not return JSON: {text[:200]}")
    return json.loads(text[start : end + 1])


class TiebaKnowledgeExtractor:
    def __init__(self, llm, knowledge_dir: Path):
        self.llm = llm
        self.knowledge_dir = Path(knowledge_dir)
        self.knowledge_dir.mkdir(parents=True, exist_ok=True)

    def flatten(self, doc: dict) -> list[dict]:
        tid = str(doc.get("tid") or "")
        rows: list[dict] = []

        def add(item: dict, suffix: str = "", parent_floor: int | None = None):
            pid = str(item.get("pid") or item.get("id") or "")
            floor = int(item.get("floor") or 0)
            text = _unwrap_links((item.get("content_text") or "").strip())
            images = item.get("images") or []
            if parent_floor is None:
                row_id, marker = f"post:{pid}", f"#{tid}#F{floor:03d}"
            else:
                row_id, marker = f"comment:{pid}", f"#{tid}#F{parent_floor:03d}{suffix}"
            payload = f"{pid}\n{marker}\n{text}\n" + "\n".join(images)
            rows.append({
                "row_id": row_id,
                "marker": marker,
                "pid": pid,
                "floor": floor,
                "parent_floor": parent_floor,
                "author": item.get("author") or "",
                "pub_time": item.get("pub_time") or "",
                "text": text,
                "image_count": len(images),
                "hash": _sha16(payload),
            })

        for post in [doc] + (doc.get("replies") or []):
            add(post)
            for i, comment in enumerate(post.get("comments") or [], 1):
                comment = dict(comment)
                comment["tid"] = doc.get("tid")
                comment["floor"] = post.get("floor")
                add(comment, f"-C{i:02d}", int(post.get("floor") or 0))
        return rows

    @staticmethod
    def _candidate(row: dict) -> bool:
        """粗筛流水；LLM 仍会做第二层筛选。"""
        text = row.get("text", "")
        if row.get("parent_floor") is None:     # 主帖默认送给模型判断
            return True
        if PROJECT_RE.search(text):
            return True
        if NOISE_RE.search(text.strip()):
            return False
        if TECH_RE.search(text):
            return True
        # 有一定长度但没命中的讨论送给模型兜底，短灌水直接丢。
        return len(text) >= 80

    @staticmethod
    def render(row: dict) -> str:
        """不渲染图片；截图默认不看。"""
        who = f" | 作者:{row['author']}" if row.get("author") else ""
        when = f" | {row['pub_time']}" if row.get("pub_time") else ""
        kind = "楼中楼" if row.get("parent_floor") is not None else "楼层"
        text = row.get("text") or "(无文字)"
        image_note = f" | 图片:{row.get('image_count', 0)}张" if row.get("image_count") else ""
        return f"ROW_ID={row['row_id']} | {row['marker']} | {kind}{who}{when}{image_note}\n  {text}"

    @staticmethod
    def split_rows(rows: list[dict], max_chars: int) -> list[list[dict]]:
        batches, current, size = [], [], 0
        for row in rows:
            n = len(TiebaKnowledgeExtractor.render(row))
            if current and size + n > max_chars:
                batches.append(current)
                current, size = [], 0
            current.append(row)
            size += n
        if current:
            batches.append(current)
        return batches

    @staticmethod
    def _pick_project_url(text: str, object_name: str = "", statement: str = "") -> str:
        """模型偶尔不填直链：从被引用原文里确定性回收项目 URL。

        命中条件（按优先级）：URL 末段 slug 出现在 object/statement 里 → 唯一一个 URL。
        多个 URL 且都对不上就不猜，留空（避免给条目挂错仓库）。
        """
        urls: list[str] = []
        for url in PROJECT_URL_RE.findall(_unwrap_links(text or "")):
            url = url.rstrip(".,;:，。；）)")
            if url and url not in urls:
                urls.append(url)
        if not urls:
            return ""
        hay = (object_name or "").lower()
        # object 的英文片段（companion/kit/DSHBox…）在 URL 里出现得越多越可信。
        tokens = [t for t in re.split(r"[^0-9a-z]+", (object_name or "").lower()) if len(t) >= 3]
        best, best_score = "", 0
        for url in urls:
            slug = url.rstrip("/").split("/")[-1].lower()
            if slug and slug in hay:
                return url
            score = sum(1 for token in tokens if token in url.lower())
            if score > best_score:
                best, best_score = url, score
        if best_score:
            return best
        # 对不上就不猜：一条记录里并列提到多个项目时，宁可留空也不挂错仓库。
        return ""

    @staticmethod
    def _url_fits(url: str, object_name: str = "", statement: str = "") -> bool:
        """URL 是否真的对应这条知识点提到的项目（防一栋楼里多个项目串台）。

        只拿 object 比对，不看 statement：statement 里常并列提到同楼的其他项目。
        """
        hay = (object_name or "").lower()
        slug = url.rstrip("/").split("/")[-1].lower()
        if slug and slug in hay:
            return True
        tokens = [t for t in re.split(r"[^0-9a-z]+", (object_name or "").lower()) if len(t) >= 3]
        if not tokens:
            return True  # 中文/短名对象无法判定，信任模型给的直链
        return any(token in url.lower() for token in tokens)

    def _validate_entry(self, raw: dict, allowed: dict[str, dict]) -> dict | None:
        theme = raw.get("theme")
        statement = (raw.get("statement") or "").strip()
        row_ids = raw.get("source_row_ids") or []
        if theme not in THEMES or not statement or not row_ids:
            return None
        kind = raw.get("kind") or "fact"
        if kind not in {"fact", "claim", "method", "opinion", "risk", "project", "tutorial"}:
            kind = "fact"
        valid_ids = []
        for row_id in row_ids:
            if row_id in allowed and row_id not in valid_ids:
                valid_ids.append(row_id)
        if not valid_ids:
            return None
        resource_url = (raw.get("resource_url") or "").strip()
        all_text = "\n".join(row["text"] for row in allowed.values())
        if resource_url and resource_url not in all_text:
            resource_url = ""
        cited_text = "\n".join(allowed[row_id]["text"] for row_id in valid_ids)
        object_name = raw.get("object") or ""
        if resource_url and not self._url_fits(resource_url, object_name, statement):
            # 模型把同一楼里别的项目链接挂到了这条上：按确定性规则重挑，挑不到就留空。
            resource_url = self._pick_project_url(cited_text, object_name, statement)
        if not resource_url:
            resource_url = self._pick_project_url(cited_text, object_name, statement)
        return {
            "theme": theme,
            "kind": kind,
            "object": (raw.get("object") or "未命名").strip()[:100],
            "statement": statement[:800],
            "source_row_ids": valid_ids,
            "source_markers": [allowed[x]["marker"] for x in valid_ids],
            "resource_url": resource_url,
        }

    @staticmethod
    def _evidence_from(rows: list[dict]) -> dict[str, dict]:
        return {
            row["row_id"]: {
                "marker": row["marker"],
                "author": row["author"],
                "pub_time": row["pub_time"],
                "text": row["text"],
                "image_count": row.get("image_count", 0),
            }
            for row in rows
        }

    def _write(self, path: Path, state: dict):
        state["updated_at"] = _now()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
        tmp.replace(path)

    @staticmethod
    def _merge_same_resource(state: dict):
        """同一仓库/工具只留一条；版本和插件进展并入主条目。"""
        entries = state.get("entries", [])
        groups: dict[str, list[dict]] = {}
        for entry in entries:
            url = entry.get("resource_url", "").rstrip("/")
            if url:
                groups.setdefault(url.lower(), []).append(entry)

        removed = set()
        for same in groups.values():
            if len(same) < 2:
                continue
            primary = min(same, key=lambda x: x.get("id", ""))
            for entry in same:
                if entry is primary:
                    continue
                detail = f"{entry['object']}：{entry['statement']}"
                if detail not in primary["statement"]:
                    primary["statement"] = primary["statement"].rstrip("。") + "。进展/细节：" + detail + "。"
                primary["source_row_ids"] = list(dict.fromkeys(primary.get("source_row_ids", []) + entry.get("source_row_ids", [])))
                markers = dict.fromkeys(primary.get("source_markers", []) + entry.get("source_markers", []))
                primary["source_markers"] = list(markers)
                primary["updated_at"] = _now()
                removed.add(id(entry))
        if removed:
            state["entries"] = [entry for entry in entries if id(entry) not in removed]
            state["entry_seq"] = len(state["entries"])
            for i, entry in enumerate(state["entries"], 1):
                entry["id"] = f"E{i:03d}"

    def extract(
        self,
        doc: dict,
        *,
        llm,
        update: bool = True,
        max_chars: int = 15000,
        load_prompt: Callable[[str], str],
    ) -> dict:
        tid = str(doc.get("tid") or "")
        path = self.knowledge_dir / f"thread_{tid}.json"
        old_state = json.loads(path.read_text(encoding="utf-8")) if update and path.exists() else None

        all_rows = self.flatten(doc)
        candidates = [row for row in all_rows if self._candidate(row)]
        current_hashes = {row["row_id"]: {"hash": row["hash"], "marker": row["marker"]} for row in all_rows}

        if old_state:
            old_hashes = old_state.get("row_hashes", {})
            added = [row for row in candidates if row["row_id"] not in old_hashes]
            changed = [
                row for row in candidates
                if row["row_id"] in old_hashes and old_hashes[row["row_id"]]["hash"] != row["hash"]
            ]
            touched = added + [row for row in changed if row not in added]
            state = old_state
            mode = "no_change" if not touched else "increment"
        else:
            touched = candidates
            state = {
                "tid": tid,
                "title": doc.get("title", ""),
                "url": f"https://tieba.baidu.com/p/{tid}",
                "forum_name": doc.get("forum_name", ""),
                "created_at": _now(),
                "entries": [],
                "entry_seq": 0,
                "evidence": {},
                "llm_calls": 0,
                "extractions": [],
            }
            mode = "baseline"

        old_entries = state.get("entries", [])
        old_evidence = state.get("evidence", {})
        new_count = updated_count = llm_calls = 0
        considered = len(candidates)

        if mode == "no_change":
            pass
        elif mode == "baseline":
            evidence: dict[str, dict] = {}
            baseline_new = 0
            for chunk_index, rows in enumerate(self.split_rows(touched, max_chars), 1):
                system = load_prompt(
                    "atomic_extract",
                    sub_dir="tieba",
                    themes=THEMES_TEXT,
                    thread_url=f"https://tieba.baidu.com/p/{tid}",
                )
                user = "\n\n".join(self.render(row) for row in rows)
                parsed = parse_json_text(llm.call(
                    [{"role": "system", "content": system}, {"role": "user", "content": user}],
                    max_tokens=3000,
                ))
                llm_calls += 1
                allowed = {row["row_id"]: row for row in rows}
                for raw in parsed.get("entries") or []:
                    entry = self._validate_entry(raw, allowed)
                    if not entry:
                        continue
                    fp = _sha16(entry["theme"] + entry["object"] + entry["statement"])
                    if any(_sha16(x["theme"] + x["object"] + x["statement"]) == fp for x in old_entries):
                        continue
                    state["entry_seq"] += 1
                    entry.update({
                        "id": f"E{state['entry_seq']:03d}",
                        "created_at": _now(),
                        "updated_at": _now(),
                    })
                    old_entries.append(entry)
                    baseline_new += 1
                    evidence.update(self._evidence_from([allowed[x] for x in entry["source_row_ids"]]))
            state["entries"] = old_entries
            state["evidence"] = {**old_evidence, **evidence}
            new_count = baseline_new
        else:
            if touched:
                old_lines = []
                for entry in old_entries:
                    old_lines.append(
                        f"{entry['id']} | {entry['theme']} | {entry['object']} | {entry['statement']} | "
                        f"sources:{','.join(entry['source_row_ids'])}"
                    )
                system = load_prompt(
                    "atomic_update",
                    sub_dir="tieba",
                    themes=THEMES_TEXT,
                    thread_url=f"https://tieba.baidu.com/p/{tid}",
                )
                user = "OLD_ENTRIES:\n" + "\n".join(old_lines) + "\n\nNEW_ROWS:\n" + "\n\n".join(
                    self.render(row) for row in touched
                )
                parsed = parse_json_text(llm.call(
                    [{"role": "system", "content": system}, {"role": "user", "content": user}],
                    max_tokens=2500,
                ))
                llm_calls = 1
                allowed = {row["row_id"]: row for row in touched}
                by_id = {entry["id"]: entry for entry in old_entries}
                new_evidence = {}
                for op in parsed.get("operations") or []:
                    if not isinstance(op, dict):
                        continue
                    if op.get("op") == "add":
                        entry = self._validate_entry(op, allowed)
                        if not entry:
                            continue
                        fp = _sha16(entry["theme"] + entry["object"] + entry["statement"])
                        if any(_sha16(x["theme"] + x["object"] + x["statement"]) == fp for x in old_entries):
                            continue
                        state["entry_seq"] += 1
                        entry.update({
                            "id": f"E{state['entry_seq']:03d}",
                            "created_at": _now(),
                            "updated_at": _now(),
                        })
                        old_entries.append(entry)
                        new_count += 1
                        new_evidence.update(self._evidence_from([allowed[x] for x in entry["source_row_ids"]]))
                    elif op.get("op") == "update" and op.get("target_id") in by_id:
                        entry = self._validate_entry(op, allowed)
                        target = by_id[op["target_id"]]
                        if not entry:
                            continue
                        merged_sources = list(dict.fromkeys(target.get("source_row_ids", []) + entry["source_row_ids"]))
                        merged_markers = []
                        evidence = {**old_evidence, **new_evidence}
                        for row_id in merged_sources:
                            if row_id in evidence:
                                merged_markers.append(evidence[row_id]["marker"])
                            elif row_id in allowed:
                                merged_markers.append(allowed[row_id]["marker"])
                        target.update({
                            "theme": entry["theme"],
                            "kind": entry["kind"],
                            "object": entry["object"],
                            "statement": entry["statement"],
                            "resource_url": entry.get("resource_url") or target.get("resource_url", ""),
                            "source_row_ids": merged_sources,
                            "source_markers": merged_markers,
                            "updated_at": _now(),
                        })
                        updated_count += 1
                        new_evidence.update(self._evidence_from([allowed[x] for x in entry["source_row_ids"]]))
                state["evidence"] = {**old_evidence, **new_evidence}

        state.update({
            "tid": tid,
            "title": doc.get("title", state.get("title", "")),
            "url": f"https://tieba.baidu.com/p/{tid}",
            "forum_name": doc.get("forum_name", state.get("forum_name", "")),
            "entries": old_entries,
            "entry_seq": state.get("entry_seq", len(old_entries)),
            "row_hashes": current_hashes,
            "llm_calls": state.get("llm_calls", 0) + llm_calls,
        })
        self._merge_same_resource(state)
        report = {
            "mode": mode,
            "fetched_records": len(all_rows),
            "candidate_records": considered,
            "discarded_records": len(all_rows) - considered,
            "touched_records": len(touched),
            "llm_calls": llm_calls,
            "new_entries": new_count,
            "updated_entries": updated_count,
            "total_entries": len(old_entries),
            "evidence_count": len(state.get("evidence", {})),
        }
        state["last_report"] = report
        state["extractions"] = (state.get("extractions") or [])[-19:] + [{
            "time": _now(),
            **report,
        }]
        self._write(path, state)

        # 知识点同时落一份 md 到 knowledgebase（kb-scan --source external 的真相源）。
        kb_md = None
        try:
            kb_md = write_kb_markdown(state)
        except Exception:
            # md 渲染失败不应回滚已入库的 JSON 知识点。
            kb_md = None

        # 项目/教程同步进全局 backlog；URL 已存在时由 backlog 幂等合并。
        for entry in old_entries:
            if entry.get("theme") == "00-备选调研方向" and entry.get("resource_url"):
                try:
                    from backend.services.community.backlog import add_item
                    add_item(
                        title=entry["object"],
                        url=entry["resource_url"],
                        note=entry["statement"],
                        source="tieba",
                        source_ref=state["url"],
                        tags=["项目", "备选调研"],
                        update=True,
                    )
                except Exception:
                    # backlog 不应阻断知识点保存；报告仍会说明主结果。
                    pass

        return {"path": str(path), "kb_markdown": str(kb_md) if kb_md else "", **report}
