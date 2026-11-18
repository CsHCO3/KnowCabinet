"""只读扫描真相源，并把目录层 upsert 到 kb.db。"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import jieba

from backend.services.kb.db import META_DIR, PROJECT_ROOT, connect, now
from backend.services.kb.ftx import SEARCHABLE_SQL, TOKENIZER_VERSION, refresh_doc
from backend.services.kb.invariants import assert_invariants


@dataclass
class Report:
    scanned: int
    docs_upserted: int
    versions_upserted: int
    fts_upserted: int
    suspected_dupes: dict
    seconds: float
    pruned: dict = field(default_factory=dict)
    fts_skipped: int = 0

    def to_dict(self) -> dict:
        return {
            "scanned": self.scanned,
            "docs_upserted": self.docs_upserted,
            "versions_upserted": self.versions_upserted,
            "fts_upserted": self.fts_upserted,
            "fts_skipped": self.fts_skipped,
            "suspected_dupes": self.suspected_dupes,
            "seconds": round(self.seconds, 3),
            "pruned": {
                key: value for key, value in self.pruned.items() if key != "removed_doc_ids"
            },
        }


_STAGE_RANK = {"found": 0, "downloaded": 1, "converted": 2, "translated": 3, "condensed": 4}

# ── D44（2026-10-19）分级口径：tier 是【信息密度】序，不是取证保真度 ──────────
#   0=线索（只有元数据） 1=原文（逐字记录） 2=轻整理（一次加工） 3=提炼（二次加工）
#   kind→tier 的唯一事实源是 knowledgebase/_meta/config/tiers.json；未登记的 kind 直接报错（D45）。
TIERS_PATH = META_DIR / "config" / "tiers.json"
# 检索范围配置（D53）：哪些 kind 整篇不参与检索
SCOPE_PATH = META_DIR / "config" / "scope.json"
_SCOPE_CACHE: dict = {"mtime": None, "kinds": []}
_TIERS_CACHE: dict = {"mtime": None, "kinds": {}}

# 管理类文件不入库（D46）
_INDEX_STEM = "_索引"
# registry 下保留原名的特殊产物；其余按所属库归为 note / web_note
_REGISTRY_SPECIAL_KINDS = frozenset(
    {"paper", "cn_paper", "paper_ocr", "cn_paper_ocr", "condensation", "原文留存"}
)

_BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")
# arXiv 分区码形态（M4 从 org_marks 分流到 documents.arxiv_cats）
_ARXIV_CAT_RE = re.compile(
    r"^(?:cs|math|stat|eess|econ|physics|nlin|gr-qc|quant-ph|hep-[a-z]+|"
    r"astro-ph|cond-mat|q-bio|q-fin)\.[A-Za-z0-9.-]+$",
    re.IGNORECASE,
)
# 按库定规则：浓缩层（<分区>/dense/<id>.md）kind=condensation 且 doc_id 取文件名主干，
# 不按"父目录/文件名"拼接（否则 doc_id 会变成 dense/<id>）。
_KB_RULES: dict[str, tuple[str, str]] = {
    "papers-dense": ("condensation", "stem"),
    "CBAC-dense": ("condensation", "stem"),
    "bilibili-dense": ("condensation", "stem"),
}
# 无正文题录（L0 线索）与正文目录的对应关系：题录是**扁平 json 文件**，
# 一篇没下载的论文不配占用一个文件夹（库主 2026-10-20 裁定）。正文侧只用来判断 stage。
_META_ROOTS = (
    ("papers",
     PROJECT_ROOT / "knowledgebase" / "default" / "papers_meta",
     PROJECT_ROOT / "knowledgebase" / "default" / "papers"),
    ("cbac",
     PROJECT_ROOT / "knowledgebase" / "CBAC" / "papers_meta",
     PROJECT_ROOT / "knowledgebase" / "CBAC" / "papers"),
)

# D27 存量豁免名单（冻结文件）：`knowledgebase/_meta/config/legacy_l2.json` 列出的 doc_id 在入库/重建后
# 仍标 status=legacy_l2（可检索、免 L2 回链）。文件是真相，代码不硬编码 doc_id，
# 这样 kb-rebuild 重建派生层后豁免状态仍可复现（I2 口径同步为 active+legacy_l2）。
LEGACY_L2_PATH = META_DIR / "config" / "legacy_l2.json"
_LEGACY_L2_CACHE: dict = {"mtime": None, "ids": frozenset()}


def legacy_l2_ids() -> frozenset[str]:
    """读冻结名单（按 mtime 缓存）；文件缺失/损坏时返回空集（保守：不做豁免）。"""
    try:
        stat = LEGACY_L2_PATH.stat()
    except OSError:
        _LEGACY_L2_CACHE.update(mtime=None, ids=frozenset())
        return frozenset()
    if _LEGACY_L2_CACHE["mtime"] != stat.st_mtime_ns:
        try:
            payload = json.loads(LEGACY_L2_PATH.read_text(encoding="utf-8"))
            ids = frozenset(
                str(value) for value in (payload.get("doc_ids") or []) if str(value).strip()
            )
        except (OSError, json.JSONDecodeError, AttributeError, TypeError):
            ids = frozenset()
        _LEGACY_L2_CACHE.update(mtime=stat.st_mtime_ns, ids=ids)
    return _LEGACY_L2_CACHE["ids"]


def _deleted_topics(config: dict) -> set[str]:
    return {
        topic for topic, value in config.get("topics", {}).items()
        if value.get("status") == "deleted"
    }


class UnknownKindError(ValueError):
    """出现未登记的 version kind —— D45 要求中止扫描，禁止静默兜底成 L1。"""


def load_tiers() -> dict[str, int]:
    """读 `knowledgebase/_meta/config/tiers.json`（按 mtime 缓存）；缺失或越界直接抛错。"""
    try:
        stat = TIERS_PATH.stat()
    except OSError as exc:
        raise UnknownKindError(f"分级表缺失：{TIERS_PATH}") from exc
    if _TIERS_CACHE["mtime"] != stat.st_mtime_ns:
        try:
            payload = json.loads(TIERS_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise UnknownKindError(f"分级表不是合法 JSON：{TIERS_PATH}") from exc
        kinds: dict[str, int] = {}
        for kind, tier in (payload.get("kinds") or {}).items():
            value = int(tier)
            if not 0 <= value <= 3:
                raise UnknownKindError(f"tiers.json 中 {kind!r} 的档位越界：{tier}")
            kinds[str(kind)] = value
        _TIERS_CACHE.update(mtime=stat.st_mtime_ns, kinds=kinds)
    return _TIERS_CACHE["kinds"]


def excluded_kinds() -> list[str]:
    """读 `knowledgebase/_meta/config/scope.json` 的 `exclude_kinds`（按 mtime 缓存）：整篇移出检索范围。

    D53（2026-10-19）：网页爬取类（`web_note`）用户一直是单独落盘 + 临时 BM25 用，
    不与主库混检，故检索期默认排除；**数据、版本、切块、向量全部保留**，加回只需改配置。
    """
    try:
        stat = SCOPE_PATH.stat()
    except OSError:
        return []
    if _SCOPE_CACHE["mtime"] != stat.st_mtime_ns:
        try:
            payload = json.loads(SCOPE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {}
        kinds = [str(kind) for kind in (payload.get("exclude_kinds") or [])]
        _SCOPE_CACHE.update(mtime=stat.st_mtime_ns, kinds=kinds)
    return _SCOPE_CACHE["kinds"]


def derive_tier(kind: str) -> int:
    """按 `knowledgebase/_meta/config/tiers.json` 把 version kind 映射为 tier（信息密度序）。"""
    kinds = load_tiers()
    try:
        return kinds[kind]
    except KeyError as exc:
        raise UnknownKindError(
            f"未登记的 version kind: {kind!r}；请先在 knowledgebase/_meta/config/tiers.json 声明档位（D45）"
        ) from exc


def _registry_kind(stem: str, kb_name: str) -> str:
    """registry 下 md 的 kind：特殊产物保留原名，其余按库归为 note / web_note。"""
    if stem in _REGISTRY_SPECIAL_KINDS:
        return stem
    if kb_name in {"runoob", "newapi"}:
        return "web_note"
    return "note"


def _external_kind(stem: str, root: Path) -> str:
    """`external` 根下 md 的 kind：群聊原文 / 群聊提炼 / 贴吧知识点。"""
    if stem == "原文留存":
        return "chat_raw"
    if "tieba" in root.as_posix().lower():
        return "tieba_distill"
    return "chat_distill"


def _put_body(bodies: dict[str, tuple[int, str]], doc_id: str, tier: int, body: str) -> None:
    """多档并存时正文取【最低的非零档】（原文词面最全，召回最稳）；0 档只作兜底。"""
    current = bodies.get(doc_id)
    if current is None:
        bodies[doc_id] = (tier, body)
        return
    current_tier = current[0]
    if current_tier == 0 and tier > 0:
        bodies[doc_id] = (tier, body)
    elif tier > 0 and tier < current_tier:
        bodies[doc_id] = (tier, body)


def derive_stage(status: dict | None, has_md: bool = False) -> str:
    """从 metadata.status 按最深完成态推导；无 metadata 时 md 即 converted。"""
    if status and isinstance(status, dict):
        if status.get("condense"):
            return "condensed"
        if status.get("translate"):
            return "translated"
        if status.get("md") or status.get("ocr"):
            return "converted"
        if status.get("pdf"):
            return "downloaded"
    return "converted" if has_md else "found"


def _sha16(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _fts_fingerprint(doc_row, body: str) -> str:
    """refresh_doc 写入 FTS 的全部输入的指纹（ftx.TOKENIZER_VERSION 换分词规则时整体失效）。

    字段集必须与 ftx.refresh_doc 的输入逐一对齐：doc_id/title/abstract/tags(库内
    json 原串)/best_tier/body；body 由调用方按 refresh_doc 的取法解析好传入。
    """
    payload = "\x1f".join([
        TOKENIZER_VERSION,
        str(doc_row["doc_id"]),
        str(doc_row["title"] or ""),
        str(doc_row["abstract"] or ""),
        str(doc_row["tags"] or ""),
        str(int(doc_row["best_tier"] or 0)),
        _sha16(body.encode("utf-8")),
    ])
    return _sha16(payload.encode("utf-8"))


def _read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _project_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _load_topics() -> tuple[dict[str, str], dict]:
    path = META_DIR / "config" / "topics.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    kb_map: dict[str, str] = {}
    for topic, value in config.get("topics", {}).items():
        for kb in value.get("kbs", []):
            kb_map[kb] = topic
    return kb_map, config


def _extract_bvid(*parts: str) -> str | None:
    match = _BV_RE.search("/".join(parts))
    return match.group(0) if match else None


def _registry_raw_id(rel_parts: tuple[str, ...], meta_id: str, stem: str) -> str:
    """正文载体用父目录身份；普通 docs/web md 用相对路径去扩展。"""
    if meta_id:
        return meta_id
    if stem in {"paper", "cn_paper", "paper_ocr", "cn_paper_ocr", "condensation", "project"}:
        return rel_parts[-2] if len(rel_parts) >= 2 else stem
    return "/".join((*rel_parts[:-1], stem))


def _global_doc_id(conn, topic: str, raw_id: str) -> str:
    """doc_id 是全局主键；跨 topic 撞名时用 topic:raw 保住“跨 topic 双行”。"""
    row = conn.execute("SELECT topic FROM documents WHERE doc_id=?", (raw_id,)).fetchone()
    if row is None or row["topic"] == topic:
        return raw_id
    candidate = f"{topic}:{raw_id}"
    row = conn.execute("SELECT topic FROM documents WHERE doc_id=?", (candidate,)).fetchone()
    if row is None or row["topic"] == topic:
        return candidate
    return f"{topic}__{raw_id}"


def _json_list(value: list[str]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _split_org_marks(values: list[str]) -> tuple[list[str], list[str]]:
    """把 `cs.CR`/`cs.DC` 等 arXiv 分区码从 org_marks 分流到 arxiv_cats。

    02 v1.7 / D33 前置整改（M3 复核授权）：md 源文件不动，只在 ETL 派生层分流；
    `--org` 过滤随之只剩真正的组织标记。
    """
    org_marks: list[str] = []
    arxiv_cats: list[str] = []
    for raw in values:
        value = str(raw).strip()
        if not value:
            continue
        if _ARXIV_CAT_RE.match(value):
            arxiv_cats.append(value)
        else:
            org_marks.append(value)
    return org_marks, arxiv_cats


def _write_document(
    conn,
    doc_id: str,
    *,
    topic: str,
    source_type: str,
    title: str,
    abstract: str,
    authors: str,
    year: str,
    venue: str,
    citations: int | None,
    doi: str,
    source_uri: str,
    stage: str,
    tags: list[str],
    org_marks: list[str] | None = None,
    arxiv_cats: list[str] | None = None,
) -> None:
    stamp = now()
    status = "legacy_l2" if doc_id in legacy_l2_ids() else "active"
    conn.execute(
        """
        INSERT INTO documents(
          doc_id, topic, source_type, title, abstract, authors, year, venue,
          citations, doi, source_uri, stage, best_tier, tags, org_marks, arxiv_cats,
          status, first_seen_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,(SELECT COALESCE(MAX(tier),0) FROM versions WHERE doc_id=?),
                  ?,?,?, ?, ?, ?)
        ON CONFLICT(doc_id) DO UPDATE SET
          topic=CASE WHEN excluded.topic != '' THEN excluded.topic ELSE documents.topic END,
          source_type=CASE WHEN excluded.source_type != '' THEN excluded.source_type ELSE documents.source_type END,
          title=CASE WHEN excluded.title != '' THEN excluded.title ELSE documents.title END,
          abstract=CASE WHEN excluded.abstract != '' THEN excluded.abstract ELSE documents.abstract END,
          authors=CASE WHEN excluded.authors != '' THEN excluded.authors ELSE documents.authors END,
          year=CASE WHEN excluded.year != '' THEN excluded.year ELSE documents.year END,
          venue=CASE WHEN excluded.venue != '' THEN excluded.venue ELSE documents.venue END,
          citations=COALESCE(excluded.citations, documents.citations),
          doi=CASE WHEN excluded.doi != '' THEN excluded.doi ELSE documents.doi END,
          source_uri=CASE WHEN excluded.source_uri != '' THEN excluded.source_uri ELSE documents.source_uri END,
          stage=CASE WHEN ? >= CASE documents.stage
                        WHEN 'downloaded' THEN 1 WHEN 'converted' THEN 2
                        WHEN 'translated' THEN 3 WHEN 'condensed' THEN 4
                        ELSE 0 END
                     THEN excluded.stage ELSE documents.stage END,
          tags=CASE WHEN json_array_length(excluded.tags) > 0 THEN excluded.tags ELSE documents.tags END,
          org_marks=CASE WHEN ? AND json_array_length(excluded.org_marks) > 0
                         THEN excluded.org_marks ELSE documents.org_marks END,
          arxiv_cats=CASE WHEN ? AND json_array_length(excluded.arxiv_cats) > 0
                          THEN excluded.arxiv_cats ELSE documents.arxiv_cats END,
          updated_at=?
        """,
        (
            doc_id, topic, source_type, title, abstract, authors, year, venue,
            citations, doi, source_uri, stage, doc_id, _json_list(tags),
            _json_list(org_marks or []), _json_list(arxiv_cats or []), status, stamp, stamp,
            _STAGE_RANK[stage], int(org_marks is not None), int(arxiv_cats is not None), stamp,
        ),
    )


def _write_version(
    conn,
    doc_id: str,
    *,
    kind: str,
    path: str,
    content_hash: str | None,
    char_count: int | None,
) -> None:
    tier = derive_tier(kind)
    conn.execute(
        """
        INSERT INTO versions(doc_id, tier, kind, path, content_hash, char_count, updated_at)
        VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(doc_id, kind) DO UPDATE SET
          tier=excluded.tier, path=excluded.path,
          content_hash=excluded.content_hash,
          char_count=excluded.char_count, updated_at=excluded.updated_at
        """,
        (doc_id, tier, kind, path, content_hash, char_count, now()),
    )
    conn.execute(
        "UPDATE documents SET best_tier=(SELECT COALESCE(MAX(tier),0) FROM versions WHERE doc_id=?) "
        "WHERE doc_id=?",
        (doc_id, doc_id),
    )


def _normalize_metadata_authors(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(v).strip() for v in value if str(v).strip())
    return str(value).strip()


def _scan_registry_md(
    conn, kb_map: dict[str, str], touched: set[str], versions: set[tuple[str, str]],
    bodies: dict[str, str], deleted_topics: set[str] = frozenset(),
) -> int:
    from ragsearch.config import load_registry
    from ragsearch.engine.document import parse_frontmatter, parse_tags

    count = 0
    for kb_name, root_value in load_registry().items():
        root = Path(root_value)
        if not root.exists():
            continue
        topic = kb_map.get(kb_name, "_inbox")
        if topic in deleted_topics:
            continue
        fallback_type = "web" if kb_name in {"runoob", "newapi"} else "note"
        rule = _KB_RULES.get(kb_name)
        for path in sorted(root.rglob("*.md")):
            if path.stem == _INDEX_STEM:
                continue  # D46：管理类文件（索引）不入库
            count += 1
            raw_bytes = _read_bytes(path)
            content = raw_bytes.decode("utf-8", errors="replace")
            meta, body = parse_frontmatter(content)
            rel_parts = path.relative_to(root).parts
            if rule and rule[1] == "stem":
                raw_id = path.stem
            else:
                raw_id = _registry_raw_id(rel_parts, str(meta.get("id") or "").strip(), path.stem)
            doc_id = _global_doc_id(conn, topic, raw_id)
            title = str(meta.get("title") or "").strip()
            if not title:
                title = next((line[2:].strip() for line in body.splitlines() if line.startswith("# ")), path.stem)
            source_type = str(meta.get("source_type") or "").strip() or fallback_type
            tags = parse_tags(meta.get("tags"))
            org_marks, arxiv_cats = _split_org_marks(parse_tags(meta.get("org_marks")))
            stage = ("condensed" if (rule and rule[0] == "condensation") or path.stem == "condensation"
                     else ("converted" if not meta else "converted"))
            _write_document(
                conn, doc_id, topic=topic, source_type=source_type, title=title,
                abstract=str(meta.get("abstract") or "").strip(),
                authors=str(meta.get("authors") or "").strip(),
                year=str(meta.get("year") or "").strip(), venue="",
                citations=None, doi=str(meta.get("doi") or "").strip(),
                source_uri=str(meta.get("source") or "").strip(), stage=stage, tags=tags,
                org_marks=org_marks, arxiv_cats=arxiv_cats,
            )
            kind = rule[0] if rule else _registry_kind(path.stem, kb_name)
            version_path = _project_path(path)
            _write_version(
                conn, doc_id, kind=kind, path=version_path,
                content_hash=_sha16(raw_bytes), char_count=len(body),
            )
            touched.add(doc_id)
            versions.add((doc_id, kind))
            _put_body(bodies, doc_id, derive_tier(kind), body)
    return count


def _scan_metadata(
    conn, touched: set[str], versions: set[tuple[str, str]], bodies: dict[str, str],
    *, deleted_topics: set[str] = frozenset(),
) -> int:
    from ragsearch.engine.document import parse_tags

    count = 0
    for topic, root, bodies_root in _META_ROOTS:
        if topic in deleted_topics:
            continue
        if not root.exists():
            continue
        for meta_path in sorted(root.glob("*.json")):
            count += 1
            raw_bytes = _read_bytes(meta_path)
            try:
                meta = json.loads(raw_bytes.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            raw_id = meta_path.stem
            doc_id = _global_doc_id(conn, topic, raw_id)
            front = meta.get("frontmatter") or {}
            status = meta.get("status") or {}
            body_dir = bodies_root / raw_id
            md_exists = any(
                (body_dir / name).exists()
                for name in ("paper.md", "cn_paper.md", "paper_ocr.md", "cn_paper_ocr.md", "condensation.md")
            )
            tags = parse_tags(meta.get("tags"))
            venue = meta.get("venue")
            if isinstance(venue, dict):
                venue = venue.get("display_name") or venue.get("name") or ""
            citations = meta.get("citations")
            if isinstance(citations, float) and citations.is_integer():
                citations = int(citations)
            elif not isinstance(citations, int):
                citations = None
            _write_document(
                conn, doc_id, topic=topic, source_type=str(meta.get("type") or "paper"),
                title=str(meta.get("title") or "").strip(),
                abstract=str(meta.get("abstract") or "").strip(),
                authors=_normalize_metadata_authors(front.get("authors")),
                year=str(front.get("year") or "").strip(), venue=str(venue or "").strip(),
                citations=citations, doi=str(meta.get("doi") or "").strip(),
                source_uri="", stage=derive_stage(status, md_exists), tags=tags,
            )
            _write_version(
                conn, doc_id, kind="meta", path=_project_path(meta_path),
                content_hash=_sha16(raw_bytes), char_count=len(raw_bytes.decode("utf-8", errors="replace")),
            )
            touched.add(doc_id)
            versions.add((doc_id, "meta"))
            _put_body(bodies, doc_id, 0, str(meta.get("abstract") or ""))
    return count


def _scan_bili_meta(conn, touched: set[str], versions: set[tuple[str, str]], bodies: dict[str, str]) -> int:
    """B站视频 L0 线索：community/bilibili/meta/{bvid}.json（仅元数据，待调研，stage=found）。

    与 _scan_videos 的 analysis 版本共享 doc_id（同视频=同一文档多版本，best_tier 取 max）。
    """
    from ragsearch.engine.document import parse_tags

    root = PROJECT_ROOT / "knowledgebase" / "community" / "bilibili" / "meta"
    count = 0
    if not root.exists():
        return 0
    for meta_path in sorted(root.glob("*.json")):
        raw_bytes = _read_bytes(meta_path)
        try:
            meta = json.loads(raw_bytes.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        bvid = str(meta.get("bvid") or meta_path.stem).strip()
        if not bvid:
            continue
        count += 1
        doc_id = _global_doc_id(conn, "videos", bvid)
        _write_document(
            conn, doc_id, topic="videos", source_type="video",
            title=str(meta.get("title") or "").strip(),
            abstract=str(meta.get("abstract") or "").strip(),
            authors=str(meta.get("authors") or "").strip(),
            year=str(meta.get("year") or "").strip(),
            venue="B站", citations=None, doi="",
            source_uri=f"https://www.bilibili.com/video/{bvid}",
            stage="found", tags=parse_tags(meta.get("tags")),
        )
        _write_version(
            conn, doc_id, kind="meta", path=_project_path(meta_path),
            content_hash=_sha16(raw_bytes),
            char_count=len(raw_bytes.decode("utf-8", errors="replace")),
        )
        touched.add(doc_id)
        versions.add((doc_id, "meta"))
        _put_body(bodies, doc_id, 0, str(meta.get("abstract") or ""))
    return count


def _scan_videos(conn, touched: set[str], versions: set[tuple[str, str]], bodies: dict[str, str]) -> int:
    from ragsearch.engine.document import parse_frontmatter, parse_tags

    root = PROJECT_ROOT / "knowledgebase" / "community" / "bilibili" / "videos"
    count = 0
    for path in sorted(root.rglob("*.md")):
        bvid = _extract_bvid(str(path.relative_to(root)))
        if not bvid:
            continue
        count += 1
        raw_bytes = _read_bytes(path)
        content = raw_bytes.decode("utf-8", errors="replace")
        meta, body = parse_frontmatter(content)
        doc_id = _global_doc_id(conn, "videos", bvid)
        title = str(meta.get("title") or "").strip()
        if not title:
            title = next((line[2:].strip() for line in body.splitlines() if line.startswith("# ")), path.stem)
        _write_document(
            conn, doc_id, topic="videos", source_type="video", title=title,
            abstract=str(meta.get("abstract") or "").strip(),
            authors=str(meta.get("authors") or "").strip(),
            year=str(meta.get("year") or "").strip(), venue="B站", citations=None,
            doi="", source_uri=f"https://www.bilibili.com/video/{bvid}",
            stage="converted", tags=parse_tags(meta.get("tags")),
        )
        _write_version(
            conn, doc_id, kind="analysis", path=_project_path(path),
            content_hash=_sha16(raw_bytes), char_count=len(body),
        )
        touched.add(doc_id)
        versions.add((doc_id, "analysis"))
        _put_body(bodies, doc_id, 3, body)
    return count


def _scan_external(conn, config: dict, touched: set[str], versions: set[tuple[str, str]], bodies: dict[str, str]) -> int:
    from ragsearch.engine.document import parse_frontmatter

    count = 0
    for topic, value in config.get("topics", {}).items():
        for raw_path in value.get("external", []):
            root = Path(raw_path)
            if not root.exists():
                continue
            # data/videos has a dedicated ETL rule and must not be scanned twice.
            if root.resolve() == (PROJECT_ROOT / "data" / "videos").resolve():
                continue
            for path in sorted(root.glob("*.md")):
                if path.stem == _INDEX_STEM:
                    continue  # D46：管理类文件（索引）不入库
                count += 1
                raw_bytes = _read_bytes(path)
                content = raw_bytes.decode("utf-8", errors="replace")
                meta, body = parse_frontmatter(content)
                raw_id = f"{topic}-{path.stem}"
                doc_id = _global_doc_id(conn, topic, raw_id)
                kind = _external_kind(path.stem, root)
                title = next((line[2:].strip() for line in body.splitlines() if line.startswith("# ")), path.stem)
                source_type = "chat"
                stage = "converted"
                _write_document(
                    conn, doc_id, topic=topic, source_type=source_type, title=title,
                    abstract="", authors=root.name or "群聊", year="", venue="",
                    citations=None, doi="", source_uri=_project_path(path), stage=stage,
                    tags=[],
                )
                _write_version(
                    conn, doc_id, kind=kind, path=str(path),
                    content_hash=_sha16(raw_bytes), char_count=len(body),
                )
                touched.add(doc_id)
                versions.add((doc_id, kind))
                _put_body(bodies, doc_id, derive_tier(kind), body)
    return count


def _natural_id(doc_id: str) -> str:
    if doc_id.startswith("__"):
        return doc_id
    marker = doc_id.find(":")
    return doc_id[marker + 1:] if marker >= 0 else doc_id


def _is_exact_identifier(raw_id: str) -> bool:
    normalized = raw_id.lower()
    return bool(re.match(r"^10\.\d{4,9}/?", normalized) or re.match(r"^\d{4}\.\d{4,5}(v\d+)?$", normalized))


def _suspected_dupes(conn) -> dict:
    rows = conn.execute(
        "SELECT doc_id, doi, title FROM documents WHERE status IN " + SEARCHABLE_SQL
    ).fetchall()
    exact_groups: dict[str, list[str]] = {}
    title_groups: dict[str, list[str]] = {}
    for row in rows:
        raw_id = _natural_id(row["doc_id"])
        if row["doi"]:
            exact_groups.setdefault("doi:" + row["doi"].strip().lower(), []).append(row["doc_id"])
        if _is_exact_identifier(raw_id):
            exact_groups.setdefault("id:" + raw_id.lower(), []).append(row["doc_id"])
        if row["title"].strip():
            normalized = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", row["title"].lower())
            if len(normalized) >= 12:
                title_groups.setdefault("title:" + normalized, []).append(row["doc_id"])

    def pairs_from(groups: dict[str, list[str]]) -> set[tuple[str, str]]:
        pairs: set[tuple[str, str]] = set()
        for doc_ids in groups.values():
            if len(doc_ids) < 2:
                continue
            for i, left in enumerate(doc_ids):
                for right in doc_ids[i + 1:]:
                    pairs.add(tuple(sorted((left, right))))
        return pairs

    exact_pairs = pairs_from(exact_groups)
    approx_pairs = pairs_from(title_groups) - exact_pairs
    approx_sorted = sorted(approx_pairs)
    return {
        "exact": [list(pair) for pair in sorted(exact_pairs)],
        "approx": [list(pair) for pair in approx_sorted[:20]],
        "exact_count": len(exact_pairs),
        "approx_count": len(approx_pairs),
    }


def _path_root(path: str) -> str:
    """取版本路径的前 3 段作为"根"（如 `knowledgebase/full/docs`），用于误删保护。"""
    parts = [part for part in str(path).replace("\\", "/").split("/") if part]
    return "/".join(parts[:3])


def _prune_stale(conn, touched: set[str], versions: set[tuple[str, str]]) -> dict:
    """删除本次扫描中已不存在的 version（含块/向量）与零版本文档。

    只处理 `touched`（本次扫描到的 doc），未扫描到的文档一律不碰——避免某个根目录
    临时不可用时被误删。kind 改名（如 chat_distill→tieba_distill）也靠这里收尾。
    """
    wanted: dict[str, set[str]] = {}
    for doc_id, kind in versions:
        wanted.setdefault(doc_id, set()).add(kind)

    removed_versions = 0
    removed_chunks: list[int] = []

    # ① 已下线的 kind（如 D46 的计划外索引 `index`）整版清除。
    #    判据：kind 不在 tiers.json 里 —— 与 D45 同源，未登记的 kind 不应留在库里。
    known_kinds = set(load_tiers())
    for row in conn.execute("SELECT version_id, doc_id, kind FROM versions").fetchall():
        if row["kind"] in known_kinds:
            continue
        for chunk in conn.execute(
            "SELECT chunk_id FROM chunks WHERE version_id=?", (row["version_id"],)
        ):
            removed_chunks.append(int(chunk["chunk_id"]))
        conn.execute("DELETE FROM versions WHERE version_id=?", (row["version_id"],))
        removed_versions += 1

    # ② 本次扫描到、但已不再产出的 version（kind 改名等）
    for doc_id in sorted(touched):
        keep = wanted.get(doc_id, set())
        rows = conn.execute(
            "SELECT version_id, kind FROM versions WHERE doc_id=?", (doc_id,)
        ).fetchall()
        for row in rows:
            if row["kind"] in keep:
                continue
            for chunk in conn.execute(
                "SELECT chunk_id FROM chunks WHERE version_id=?", (row["version_id"],)
            ):
                removed_chunks.append(int(chunk["chunk_id"]))
            conn.execute("DELETE FROM versions WHERE version_id=?", (row["version_id"],))
            removed_versions += 1
    for chunk_id in removed_chunks:
        conn.execute("DELETE FROM kb_vec WHERE chunk_id=?", (chunk_id,))

    orphan_rows = conn.execute(
        "SELECT doc_id FROM documents WHERE doc_id NOT IN (SELECT DISTINCT doc_id FROM versions)"
    ).fetchall()
    removed_doc_ids = {str(row["doc_id"]) for row in orphan_rows}
    for doc_id in sorted(removed_doc_ids):
        conn.execute("DELETE FROM kb_fts WHERE doc_id=?", (doc_id,))
        conn.execute("DELETE FROM documents WHERE doc_id=?", (doc_id,))

    # ③ 源文件已被删除的文档（2026-10-20 补：此前只按 touched 收尾，
    #    文件删了库里永远留着）。安全阀：要求①同一根目录本次扫到过文档（证明目录可读）、
    #    ②该根目录本身还存在，避免某个盘/目录临时不可用时被整片误删。
    touched_roots: set[str] = set()
    for doc_id, _kind in versions:
        row = conn.execute(
            "SELECT path FROM versions WHERE doc_id=? LIMIT 1", (doc_id,)
        ).fetchone()
        if row and row["path"]:
            touched_roots.add(_path_root(str(row["path"])))

    missing_doc_ids: set[str] = set()
    for row in conn.execute("SELECT doc_id FROM documents").fetchall():
        doc_id = str(row["doc_id"])
        if doc_id in touched or doc_id in removed_doc_ids:
            continue
        paths = [str(r["path"]) for r in conn.execute(
            "SELECT path FROM versions WHERE doc_id=?", (doc_id,))]
        if not paths or any((PROJECT_ROOT / p).exists() for p in paths if p):
            continue
        root = _path_root(paths[0])
        if root not in touched_roots or not (PROJECT_ROOT / root).is_dir():
            continue
        for chunk in conn.execute(
            "SELECT c.chunk_id FROM chunks c JOIN versions v ON v.version_id=c.version_id "
            "WHERE v.doc_id=?", (doc_id,)
        ):
            removed_chunks.append(int(chunk["chunk_id"]))
            conn.execute("DELETE FROM kb_vec WHERE chunk_id=?", (int(chunk["chunk_id"]),))
        conn.execute("DELETE FROM chunks WHERE version_id IN "
                     "(SELECT version_id FROM versions WHERE doc_id=?)", (doc_id,))
        removed_versions += conn.execute(
            "DELETE FROM versions WHERE doc_id=?", (doc_id,)).rowcount
        conn.execute("DELETE FROM kb_fts WHERE doc_id=?", (doc_id,))
        conn.execute("DELETE FROM documents WHERE doc_id=?", (doc_id,))
        missing_doc_ids.add(doc_id)
    removed_doc_ids |= missing_doc_ids

    for doc_id in sorted(touched):
        if doc_id in removed_doc_ids:
            continue
        conn.execute(
            "UPDATE documents SET best_tier=(SELECT COALESCE(MAX(tier),0) FROM versions "
            "WHERE doc_id=?) WHERE doc_id=?", (doc_id, doc_id),
        )
    return {
        "removed_versions": removed_versions,
        "removed_chunks": len(removed_chunks),
        "removed_docs": len(removed_doc_ids),
        "removed_doc_ids": removed_doc_ids,
    }


def scan_all(force: bool = False, source: str = "all") -> Report:
    """全量扫描；force=True 时忽略内容指纹，强制重算全部条目的 FTS。"""
    full = force
    started = time.perf_counter()
    jieba.setLogLevel(60)
    conn = connect()
    kb_map, topic_config = _load_topics()
    touched: set[str] = set()
    versions: set[tuple[str, str]] = set()
    bodies: dict[str, tuple[int, str]] = {}
    scanned = 0
    try:
        deleted_topics = _deleted_topics(topic_config)
        if source in {"all", "registry"}:
            scanned += _scan_registry_md(conn, kb_map, touched, versions, bodies, deleted_topics)
        if source in {"all", "metadata"}:
            scanned += _scan_metadata(conn, touched, versions, bodies, deleted_topics=deleted_topics)
        if source in {"all", "videos"}:
            scanned += _scan_bili_meta(conn, touched, versions, bodies)
            scanned += _scan_videos(conn, touched, versions, bodies)
        if source in {"all", "external"}:
            scanned += _scan_external(conn, topic_config, touched, versions, bodies)
        conn.commit()

        pruned = _prune_stale(conn, touched, versions)
        refreshed = 0
        skipped = 0
        for doc_id in sorted(touched - pruned["removed_doc_ids"]):
            body = bodies[doc_id][1] if doc_id in bodies else None
            row = conn.execute("SELECT doc_id,title,abstract,tags,best_tier,fts_hash "
                               "FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
            if row is None:
                continue
            fp = _fts_fingerprint(row, body or (row["abstract"] if int(row["best_tier"]) == 0 else ""))
            if not full and row["fts_hash"] == fp:
                skipped += 1
                continue
            refresh_doc(conn, doc_id, override_body=body)
            conn.execute("UPDATE documents SET fts_hash=? WHERE doc_id=?", (fp, doc_id))
            refreshed += 1
        conn.commit()
        assert_invariants(conn)
        report = Report(
            scanned=scanned,
            docs_upserted=len(touched) - pruned["removed_docs"],
            versions_upserted=len(versions),
            fts_upserted=refreshed,
            suspected_dupes=_suspected_dupes(conn),
            seconds=time.perf_counter() - started,
            pruned=pruned,
            fts_skipped=skipped,
        )
        return report
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
