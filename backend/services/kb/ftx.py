"""FTS5 读写：写入侧 refresh_doc + 查询侧 build_match/match（M3）。

查询侧铁律（M1 复核 F1 实证）：**必须走与写入端同一套 jieba 分词**。
实测 `MATCH '判决树'` 原始连续串命中 0 条、分词后 `'判决 树'` 命中 10 条。
"""
from __future__ import annotations

import json
import re
import sqlite3

import jieba

# FTS5 bm25() 列权重，列序 = title_t / abstract_t / tags_t / body_t（02 §9.3）
BM25_WEIGHTS = "3.0, 1.5, 2.0, 1.0"
# 分词规则版本：参与 refresh_doc 输入的指纹（catalog._fts_fingerprint）。
# 改 jieba 用法 / tokens() 规则时 +1，让全部条目指纹失效、下次 kb-scan 全量重算。
TOKENIZER_VERSION = "1"
# 未精炼（best_tier=0）只允许命中元信息列，不许命中 body（02 §9.1 ②）
META_COLUMNS = "{title_t abstract_t tags_t}"
# snippet 默认取 body_t（列序号 4）
SNIPPET_COLUMN = 4
# 可检索状态（02 §5 DDL + D27）：legacy_l2 = 存量无回链 L2，仍可检索，
# 但证据双轨按"豁免"处理（must_quote_l3=false）。deprecated/excluded 不参与检索。
SEARCHABLE_STATUSES = ("active", "legacy_l2")
_KEEP_CHAR = re.compile(r"[0-9A-Za-z\u4e00-\u9fff]")
# 可直接拼进 SQL 的可检索状态字面量（值来自上面的常量，无外部输入）
SEARCHABLE_SQL = "(" + ", ".join(f"'{value}'" for value in SEARCHABLE_STATUSES) + ")"


def tokens(text: str) -> str:
    """jieba 搜索引擎模式分词，用空格连接给 unicode61。"""
    jieba.setLogLevel(60)
    return " ".join(token for token in jieba.lcut_for_search(text or "") if token.strip())


def _query_tokens(query: str) -> list[str]:
    """切出可安全交给 FTS5 的词；纯符号词直接丢弃（否则 MATCH 会语法报错）。"""
    return [t for t in tokens(query).split() if _KEEP_CHAR.search(t)]


def _fts_quote(token: str) -> str:
    """FTS5 字符串字面量转义：内部双引号翻倍。"""
    return '"' + token.replace('"', '""') + '"'


def build_match(query: str) -> str | None:
    """query → FTS5 MATCH 表达式：短语优先 + 单词 OR 兜底（02 §6.2）。

    返回 None 表示 query 无有效词，调用方应直接返回空结果而不是把空串送进 MATCH。
    """
    parts = _query_tokens(query)
    if not parts:
        return None
    quoted = [_fts_quote(token) for token in parts]
    if len(quoted) == 1:
        return quoted[0]
    phrase = '"' + " ".join(parts).replace('"', '""') + '"'
    return " OR ".join([phrase, *quoted])


def scope_to_meta_columns(expr: str) -> str:
    """把 MATCH 表达式限定到元信息列，用于 best_tier=0 的候选分派。"""
    return f"{META_COLUMNS} : ({expr})"


def refresh_doc(conn: sqlite3.Connection, doc_id: str, override_body: str | None = None) -> None:
    """以 documents 当前值为准刷新一条 FTS 记录。"""
    row = conn.execute(
        "SELECT doc_id, title, abstract, tags, best_tier FROM documents WHERE doc_id=?",
        (doc_id,),
    ).fetchone()
    if row is None:
        conn.execute("DELETE FROM kb_fts WHERE doc_id=?", (doc_id,))
        return
    try:
        tags = json.loads(row["tags"] or "[]")
    except json.JSONDecodeError:
        tags = []
    body = override_body if override_body is not None else ""
    if not body and int(row["best_tier"]) == 0:
        body = row["abstract"] or ""
    conn.execute("DELETE FROM kb_fts WHERE doc_id=?", (doc_id,))
    conn.execute(
        "INSERT INTO kb_fts(doc_id,title_t,abstract_t,tags_t,body_t) VALUES (?,?,?,?,?)",
        (
            row["doc_id"], tokens(row["title"]), tokens(row["abstract"]),
            tokens(" ".join(tags)), tokens(body),
        ),
    )


def upsert_doc(conn: sqlite3.Connection, doc_id: str, override_body: str | None = None) -> None:
    """refresh_doc 的别名（02 §6.2 契约名）。"""
    refresh_doc(conn, doc_id, override_body=override_body)


def delete_doc(conn: sqlite3.Connection, doc_id: str) -> None:
    conn.execute("DELETE FROM kb_fts WHERE doc_id=?", (doc_id,))


def filter_clause(filters: dict) -> tuple[str, list]:
    """把过滤字典编译成 WHERE 片段（只做确定性过滤，不参与打分，02 §9.1 ①）。

    状态语义（M5 起）：缺省 = 可检索状态集合 `SEARCHABLE_STATUSES`（active + legacy_l2）；
    显式传 `status`（字符串或序列）时按显式值过滤，便于体检/审计单查某一状态。
    """
    filters = filters or {}
    clauses: list[str] = []
    args: list = []

    status = filters.get("status")
    if isinstance(status, (list, tuple, set, frozenset)):
        values = [str(value) for value in status]
        if values:
            clauses.append(f"d.status IN ({','.join('?' * len(values))})")
            args.extend(values)
        else:
            clauses.append("d.status = ?")
            args.append("active")
    elif status:
        clauses.append("d.status = ?")
        args.append(str(status))
    else:
        clauses.append(f"d.status IN ({','.join('?' * len(SEARCHABLE_STATUSES))})")
        args.extend(SEARCHABLE_STATUSES)

    topic = filters.get("topic")
    if topic:
        clauses.append("d.topic = ?")
        args.append(topic)

    for column, key in (("d.tags", "tags"), ("d.org_marks", "org")):
        values = filters.get(key) or []
        if values:
            placeholders = ",".join("?" * len(values))
            clauses.append(
                f"EXISTS (SELECT 1 FROM json_each({column}) je WHERE je.value IN ({placeholders}))"
            )
            args.extend(values)

    source_types = filters.get("source_types") or []
    if source_types:
        placeholders = ",".join("?" * len(source_types))
        clauses.append(f"d.source_type IN ({placeholders})")
        args.extend(source_types)

    if filters.get("year"):
        clauses.append("d.year = ?")
        args.append(str(filters["year"]))
    if filters.get("stage"):
        clauses.append("d.stage = ?")
        args.append(filters["stage"])
    if filters.get("tier_min") is not None:
        clauses.append("d.best_tier >= ?")
        args.append(int(filters["tier_min"]))
    if filters.get("tier_max") is not None:
        clauses.append("d.best_tier <= ?")
        args.append(int(filters["tier_max"]))

    # D53：整篇排除（按 version kind 判——documents 表没有 kind 列）
    exclude_kinds = filters.get("exclude_kinds") or []
    if exclude_kinds:
        placeholders = ",".join("?" * len(exclude_kinds))
        clauses.append(
            "NOT EXISTS (SELECT 1 FROM versions v "
            f"WHERE v.doc_id = d.doc_id AND v.kind IN ({placeholders}))"
        )
        args.extend(str(kind) for kind in exclude_kinds)
    return " AND ".join(clauses), args


def match(
    conn: sqlite3.Connection,
    query: str,
    filters: dict | None = None,
    *,
    limit: int = 200,
    meta_only: bool = False,
) -> list[dict]:
    """FTS5 召回。meta_only=True 时只允许命中元信息列（best_tier=0 分支）。"""
    expr = build_match(query)
    if expr is None:
        return []
    where, args = filter_clause(filters)
    scope = scope_to_meta_columns(expr) if meta_only else f"({expr})"
    sql = f"""
        SELECT kb_fts.doc_id AS doc_id,
               -bm25(kb_fts, {BM25_WEIGHTS}) AS score,
               snippet(kb_fts, {SNIPPET_COLUMN}, '[', ']', '…', 16) AS snippet
        FROM kb_fts JOIN documents d ON d.doc_id = kb_fts.doc_id
        WHERE kb_fts MATCH ? AND {where}
        ORDER BY score DESC, doc_id ASC
        LIMIT ?
    """
    rows = conn.execute(sql, [scope, *args, int(limit)]).fetchall()
    return [
        {"doc_id": row["doc_id"], "score": float(row["score"] or 0.0), "snippet": row["snippet"] or ""}
        for row in rows
    ]
