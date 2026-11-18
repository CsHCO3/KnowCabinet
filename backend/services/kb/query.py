"""统一检索：确定性过滤 → 分层召回（FTS + 向量）→ 级内归一化/秩融合 → 配额合并 → 证据双轨。

02 §9 流程（M5 起并入向量支路）：
  ① 确定性过滤（topic/tags/org/source_type/year/tier/stage/status）
  ② 分派召回：best_tier=0 → kb_fts 仅元信息列；1-3 → kb_fts 全列
              + deep 模式：query 嵌入一次 → 候选 doc 集内 chunk KNN → chunk→doc 折叠
  ③ 每 tier 内按支路打分（norm=min-max / rrf=倒数秩），同 doc 折叠
  ④ 按 §9.2 配额合并（hit_tier = best_tier，同 doc 只留一条）
  ⑤ evidence 标注（D44：quote_from / quote_tier / quote_kind / verbatim_allowed）
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from backend.services.kb.db import PROJECT_ROOT, connect
from backend.services.kb import ftx
from backend.services.kb import rerank as rerank_mod
from backend.services.kb.catalog import excluded_kinds
from backend.services.kb.chunkq import partition_toc

# D33（2026-10-14 复核裁定）：browse 采用 V4 = 6/2/1/1。
# D35（2026-10-14 M5 重校，D33 护栏②「扩到 50 条后必须重校一次」）：
#   deep 由 1/2/3/4 重校为 1/1/1/7 —— deep 语义是"细节优先"，50 条评测实测：
#   整体 recall 0.8333→0.8933、v1 子集 0.8722→0.8889、e029 0.50→1.00、平均档数 3.18 不变。
# V4 是 top_k=10 的绝对配额；quota_for() 按 top_k 等比缩放。
# D49（2026-10-19，随 D44 口径改造）：deep 的"细节优先"语义不变，但细节在密度序里
# 是 tier=1（原文），故 1/1/1/7 重排为 1/7/1/1——实际召回的还是同一批"原文"文档。
# browse 的 6 个名额给 tier=0（线索）语义不变；balanced 保持对称配额。
_QUOTA = {
    "browse": {0: 6, 1: 2, 2: 1, 3: 1},
    "balanced": {0: 2, 1: 3, 2: 3, 3: 2},
    "deep": {0: 1, 1: 7, 2: 1, 3: 1},
}
MODES = tuple(_QUOTA)

# D52（2026-10-19，应急修 MRR 回退）：档位先验权重。
#   背景：旧实现"每档各自 min-max + 每档保底名额"，导致每一档的最佳命中都被拉成 1.000，
#   弱档噪音（如一条词法撞车的群聊提炼稿）与真答案并列第一，排序退化成按档位排。
#   现改为：同一支路**跨档统一尺度**归一化（见 _norm_bounds），再乘本表权重。
#   L0 只有元数据，标题/摘要常强命中但正文不存在——给 0.30 压到有正文的文档之后。
_TIER_PRIOR = {0: 0.30, 1: 1.00, 2: 1.00, 3: 1.00}

# 软配额：超过某档名额的第 n 条，分数乘 `decay ** n`（不是直接丢掉）。
_QUOTA_DECAY = 0.90

# M5 裁定项（02 §9.2/§9.3）：
#   norm       = 级内 min-max，两支路取 max（M3 行为）
#   rrf        = 倒数秩融合（实测 recall 无提升 + 跨语言崩到 0.0 → 不采用，仅留实验口）
#   interleave = 两支路并列轮转
#   reserve    = 每档留 1 个向量保底名额（**M5 采用**：跨语言 0.80 且 v1 不回退）
FUSIONS = ("norm", "rrf", "interleave", "reserve")
DEFAULT_FUSION = "reserve"
RRF_K = 60

# 向量支路默认只在 deep 生效（02 §11.1：仅 deep 需要进程内常驻 bge）。
# browse/balanced 保持纯 FTS，满足 04 §10「kb-ask browse <1s」预算（首帧不加载模型）。
VECTOR_MODES = ("deep",)
VECTOR_TOP_CHUNKS = 200      # 全库 KNN 取回块数（vec0 无 doc_id 过滤，应用层再收敛）
VECTOR_CHUNKS_PER_DOC = 5    # 每个 doc 回传的命中块上限（取证用）
# chunk→doc 折叠规则（02 §9.3 "向量：cosine 原值；同 doc 折叠取 max"）
#   max      = 单块最高分（M4 实现，默认）
#   top3sum  = 前 3 块求和（"折叠加权"：多块证据 = 更可信，跨语言实测显著更好）
VECTOR_FOLDS = ("max", "top3sum")
VECTOR_FOLD = "max"
_TOP_SUM_CHUNKS = 3
_QUERY_VECTOR_CACHE: dict[tuple[str, str], object] = {}
_QUERY_VECTOR_CACHE_MAX = 256

# D44（2026-10-19）：档位改为信息密度序，"能不能引原话"由 evidence.quote_from 表达。
# 回链标记（[L1:路径#块号]）的解析与校验只保留在 kb/promote.py 一处。

# 同 tier 多 kind 时的取证优先级（对齐 service.fetch_paper：原文优先于译文）
_TIER_KIND_PREFERENCE = {
    0: ("meta",),
    1: ("paper", "cn_paper", "paper_ocr", "cn_paper_ocr", "chat_raw", "原文留存",
        "note", "web_note", "subtitle"),
    2: ("condensation",),
    3: ("chat_distill", "tieba_distill", "analysis", "synthesis"),
}

_TAG_BONUS_PER_HIT = 0.5
_TAG_BONUS_CAP = 2.0
_CANDIDATE_LIMIT = 500
_CONTENT_MAX_CHARS = 400_000


# ── 工具 ────────────────────────────────────────────────────────────────────


def quota_for(mode: str, top_k: int) -> dict[int, int]:
    """§9.2 配额表；top_k≠10 时按比例缩放，每档至少 1 个名额。"""
    base = _QUOTA[mode]
    if top_k == 10:
        return dict(base)
    scale = top_k / 10.0
    # D52：基准为 0 的档位保持 0（不再"每档至少 1"）——deep 不给 L0 空壳留主榜席位。
    return {tier: (0 if count == 0 else max(1, int(round(count * scale))))
            for tier, count in base.items()}


def _resolve_path(raw: str) -> Path:
    """相对路径统一按项目根解析（kb.db 里存的是仓库相对 POSIX 路径）。"""
    path = Path(raw)
    return path if path.is_absolute() else PROJECT_ROOT / path


_abs_path = _resolve_path


def _json_list(raw: str | None) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return [str(item) for item in value] if isinstance(value, list) else []


def _normalize(values: list[float],
               bounds: tuple[float, float] | None = None) -> list[float]:
    """min-max 归一化；全部相等时统一给 1.0（避免除零并保留"该档最佳"语义）。

    D52：可传入 `bounds=(low, high)` 做**跨档统一尺度**归一化——避免"每档各自
    归一化"把某一档唯一的弱命中拉成 1.0，与真正高分命中并列。
    """
    if not values:
        return []
    if bounds is not None:
        low, high = bounds
    else:
        low, high = min(values), max(values)
    if high <= low:
        return [1.0 for _ in values]
    return [max(0.0, min(1.0, (value - low) / (high - low))) for value in values]


# ── doc_id 歧义解析（02 §7 存储 ID 歧义处理）───────────────────────────────


def resolve_doc_id(conn, doc_id: str) -> tuple[str | None, list[str]]:
    """裸 id 唯一命中则直接使用；多 topic 命中返回候选清单，绝不静默合并（02 §7）。

    **M8 修订（DoD④ 现场发现）**：库里有 13 篇论文被"双登记"——`papers/<id>` 的 L0 空壳
    与 CBAC 的 L3 全文各占一行，标题相同但 topic 不同，于是 `10.1103_xxx` 既是某行的
    **精确存储 ID**，又存在 `papers:10.1103_xxx` 前缀行。原实现在这种情况下**一律报
    `ambiguous_doc_id`**，导致 `kb-ask` 返回的 doc_id 无法再喂给 `kb-fetch`——证据双轨的
    主链路（ask → fetch → 引原文）对这批文档直接断掉（M8 DoD④ 实测 2/9 组命中此坑）。

    现行规则（仍是"绝不静默合并"，只是把"精确命中"当成最强证据）：
      ① **精确命中**（某行 doc_id 与输入全等）→ 用该行，并把其余同名前缀候选作为
         `alt_candidates` **知情项**返回（不阻断、不合并）；
      ② 无精确命中、仅 `%:id` 前缀命中且多条 → 保持原行为，返回候选清单让调用方
         改用带 topic 前缀的存储 ID；
      ③ 全无命中 → (None, [])。

    这样既修好了 ask→fetch，又保留了 02 §7 想防的真问题：输入只是个"裸名"、而各 topic
    下存在**不同**文档共用该名时（库里无精确行），依旧要求调用方显式指定 topic。
    """
    candidates = [
        row["doc_id"]
        for row in conn.execute(
            "SELECT doc_id FROM documents WHERE doc_id = ? OR doc_id LIKE ? ORDER BY doc_id",
            (doc_id, f"%:{doc_id}"),
        )
    ]
    if doc_id in candidates:
        return doc_id, [item for item in candidates if item != doc_id]
    if len(candidates) == 1:
        return candidates[0], []
    if not candidates:
        return None, []
    return None, candidates


# ── 召回：FTS 支路 ──────────────────────────────────────────────────────────


def _load_docs(conn, doc_ids: list[str]) -> dict[str, dict]:
    if not doc_ids:
        return {}
    placeholders = ",".join("?" * len(doc_ids))
    rows = conn.execute(
        f"""
        SELECT doc_id, topic, source_type, title, abstract, authors, year, venue,
               citations, doi, source_uri, stage, best_tier, tags, org_marks, status
        FROM documents WHERE doc_id IN ({placeholders})
        """,
        doc_ids,
    ).fetchall()
    return {row["doc_id"]: dict(row) for row in rows}


def _load_versions(conn, doc_ids: list[str]) -> dict[str, list[dict]]:
    if not doc_ids:
        return {}
    placeholders = ",".join("?" * len(doc_ids))
    rows = conn.execute(
        f"""
        SELECT version_id, doc_id, tier, kind, path, content_hash, char_count
        FROM versions WHERE doc_id IN ({placeholders})
        ORDER BY doc_id, tier DESC, kind
        """,
        doc_ids,
    ).fetchall()
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        item = dict(row)
        item["available"] = _resolve_path(row["path"]).exists()
        grouped.setdefault(row["doc_id"], []).append(item)
    return grouped


def _apply_tag_bonus(hits: list[dict], docs: dict[str, dict], query: str) -> None:
    """§9.3：命中查询词的 tag 每个 +0.5，上限 +2。仅作用于 FTS 支路（尺度问题）。"""
    query_tokens = {token for token in ftx.tokens(query).split() if token}
    if not query_tokens:
        return
    for hit in hits:
        doc = docs.get(hit["doc_id"]) or {}
        tags = set(_json_list(doc.get("tags")))
        matched = len(query_tokens & tags)
        if matched:
            hit["score"] += min(matched * _TAG_BONUS_PER_HIT, _TAG_BONUS_CAP)


def _recall(conn, query: str, filters: dict, range_min: int, range_max: int) -> tuple[list[dict], int]:
    """FTS 支路：按 tier 分派召回——0 档走元信息列，1-3 档走全列。"""
    collected: list[dict] = []
    candidates = 0
    if range_min <= 0 <= range_max:
        rows = ftx.match(
            conn, query, {**filters, "tier_min": 0, "tier_max": 0},
            limit=_CANDIDATE_LIMIT, meta_only=True,
        )
        candidates += len(rows)
        for row in rows:
            collected.append({**row, "tier": 0})
    if range_max >= 1:
        rows = ftx.match(
            conn, query, {**filters, "tier_min": max(range_min, 1), "tier_max": range_max},
            limit=_CANDIDATE_LIMIT, meta_only=False,
        )
        candidates += len(rows)
        for row in rows:
            collected.append({**row, "tier": None})  # 真实档位稍后由 best_tier 填
    return collected, candidates


# ── 召回：向量支路（M5）─────────────────────────────────────────────────────


def _default_embed_fn(texts: list[str]):
    from ragsearch.engine.bge_embed import embed_dense

    return embed_dense(texts)


def _embed_query(query: str, embed_fn=None):
    """query 嵌入一次（02 §9.1）。默认路径按 (model, query) 进程内缓存。

    缓存的意义：kb-eval 每条 query 会 ask 两次（正式 + 解配额诊断），
    且 50 条评测集里同一 query 只嵌一次，模型常驻 + query 级缓存 = 不重复算。
    注入 embed_fn（单测）时不走缓存，避免假向量污染真实缓存。
    """
    from backend.services.kb.vector import MODEL_ID, _embed_texts

    key = None
    if embed_fn is None:
        key = (MODEL_ID, query)
        cached = _QUERY_VECTOR_CACHE.get(key)
        if cached is not None:
            return cached
        fn = _default_embed_fn
    else:
        fn = embed_fn
    vector = _embed_texts(fn, [query])[0]
    if key is not None:
        if len(_QUERY_VECTOR_CACHE) >= _QUERY_VECTOR_CACHE_MAX:
            _QUERY_VECTOR_CACHE.clear()
        _QUERY_VECTOR_CACHE[key] = vector
    return vector


def _candidate_doc_ids(conn, filters: dict, range_min: int, range_max: int) -> set[str]:
    """确定性过滤的候选 doc 集（向量支路的收敛范围，02 §9.1 ②）。"""
    where, args = ftx.filter_clause({**filters, "tier_min": range_min, "tier_max": range_max})
    rows = conn.execute(f"SELECT d.doc_id FROM documents d WHERE {where}", args).fetchall()
    return {row[0] for row in rows}


def _fold_chunks(chunks: list[dict], rule: str) -> float:
    """chunk 分 → doc 分（同 doc 折叠）。"""
    ordered = sorted((chunk["score"] for chunk in chunks), reverse=True)
    if rule == "top3sum":
        return float(sum(ordered[:_TOP_SUM_CHUNKS]))
    return float(ordered[0])


def _vector_recall(conn, query_vector, filters: dict, range_min: int, range_max: int, *,
                   top_chunks: int = VECTOR_TOP_CHUNKS, fold: str = VECTOR_FOLD,
                   embed_seconds: float = 0.0, toc_filter: bool = True
                   ) -> tuple[list[dict], dict]:
    """候选 doc 集内 chunk KNN → chunk→doc 折叠（记录命中块，供取证回源）。

    vec0 的 KNN 不支持按 doc_id 过滤（M0 实测结论），故：全库取回 top_chunks 块，
    再用确定性过滤的候选 doc 集在应用层收敛。
    """
    stats = {
        "enabled": True, "allowed_docs": 0, "knn_chunks": 0, "kept_chunks": 0,
        "docs": 0, "top_chunks": int(top_chunks), "fold": fold, "toc_filter": bool(toc_filter),
        "embed_seconds": round(float(embed_seconds), 3), "error": None,
    }
    allowed = _candidate_doc_ids(conn, filters, range_min, range_max)
    stats["allowed_docs"] = len(allowed)
    if not allowed:
        return [], stats
    from backend.services.kb.vector import container_for

    try:
        container = container_for(conn)
    except Exception as exc:  # noqa: BLE001 — 容器不可用时降级为纯 FTS，不炸检索
        stats["error"] = f"{type(exc).__name__}: {exc}"
        return [], stats
    if container.count() == 0:
        stats["error"] = "kb_vec 为空（先跑 kb-embed）"
        return [], stats

    rows = container.knn(query_vector, int(top_chunks))
    stats["knn_chunks"] = len(rows)
    if not rows:
        return [], stats
    score_by_chunk = {int(row["chunk_id"]): float(row["score"]) for row in rows}
    chunk_ids = list(score_by_chunk)
    placeholders = ",".join("?" * len(chunk_ids))
    chunk_rows = conn.execute(
        f"""
        SELECT c.chunk_id, c.doc_id, c.version_id, c.chunk_index, v.tier, v.kind
        FROM chunks c JOIN versions v ON v.version_id = c.version_id
        WHERE c.chunk_id IN ({placeholders})
        """,
        chunk_ids,
    ).fetchall()
    kept = [row for row in chunk_rows if row["doc_id"] in allowed]
    stats["kept_chunks"] = len(kept)
    if not kept:
        return [], stats

    kept_ids = [int(row["chunk_id"]) for row in kept]
    text_by_chunk = {
        int(row[0]): row[1]
        for row in conn.execute(
            f"SELECT chunk_id, text FROM chunks WHERE chunk_id IN "
            f"({','.join('?' * len(kept_ids))})",
            kept_ids,
        )
    }
    by_doc: dict[str, list[dict]] = {}
    for row in kept:
        chunk_id = int(row["chunk_id"])
        by_doc.setdefault(row["doc_id"], []).append({
            "chunk_id": chunk_id,
            "version_id": int(row["version_id"]),
            "tier": int(row["tier"]),
            "kind": row["kind"],
            "chunk_index": int(row["chunk_index"]),
            "score": round(score_by_chunk[chunk_id], 6),
            "text": text_by_chunk.get(chunk_id, ""),
        })
    hits: list[dict] = []
    toc_dropped = 0
    for doc_id, chunks in by_doc.items():
        chunks.sort(key=lambda item: (-item["score"], item["chunk_id"]))
        # M8-3：目录残渣块不参与折叠打分、不作为证据块（每篇至少留 1 块，不打死文档）
        kept_chunks, dropped_chunks = (
            partition_toc(chunks) if toc_filter else (chunks, []))
        toc_dropped += len(dropped_chunks)
        hits.append({
            "doc_id": doc_id,
            "score": _fold_chunks(kept_chunks, fold),
            "tier": None,
            "chunks": kept_chunks[:VECTOR_CHUNKS_PER_DOC],
            "n_chunks": len(kept_chunks),
            "toc_dropped": len(dropped_chunks),
        })
    hits.sort(key=lambda item: (-item["score"], item["doc_id"]))
    stats["docs"] = len(hits)
    stats["toc_chunks_dropped"] = toc_dropped
    return hits, stats


# ── 级内打分与折叠 ──────────────────────────────────────────────────────────


def _arm_scores(hits: list[dict], fusion: str,
                bounds: tuple[float, float] | None = None) -> list[float]:
    """按支路给分：norm=min-max；rrf=1/(K+rank)。输入已按原始分降序排好。"""
    if fusion == "rrf":
        return [1.0 / (RRF_K + rank) for rank in range(1, len(hits) + 1)]
    return _normalize([hit["score"] for hit in hits], bounds)


def _norm_bounds(groups: dict[int, list[dict]]) -> tuple[float, float]:
    """跨档取一条支路的原始分极值，供统一尺度归一化使用。"""
    values = [float(hit["score"]) for hits in groups.values() for hit in hits]
    if not values:
        return 0.0, 0.0
    return min(values), max(values)


def _merge_tier(tier: int, fts_hits: list[dict], vec_hits: list[dict],
                fusion: str,
                fts_bounds: tuple[float, float] | None = None,
                vec_bounds: tuple[float, float] | None = None) -> list[dict]:
    """同一 tier 内合并两条支路；同 doc 折叠（norm 取 max / rrf 求和）。"""
    entries: dict[str, dict] = {}
    for arm, hits, bounds in (("fts", fts_hits, fts_bounds), ("vector", vec_hits, vec_bounds)):
        ordered = sorted(hits, key=lambda hit: (-hit["score"], hit["doc_id"]))
        scores = _arm_scores(ordered, fusion, bounds)
        for hit, arm_score in zip(ordered, scores):
            entry = entries.get(hit["doc_id"])
            if entry is None:
                entry = {
                    "doc_id": hit["doc_id"], "tier": tier,
                    "score": 0.0, "raw_score": 0.0,
                    "fts_score": None, "vector_score": None,
                    "sources": [], "snippet": "", "chunks": [], "n_chunks": 0,
                    "_arms": {},
                }
                entries[hit["doc_id"]] = entry
            entry["_arms"][arm] = (arm_score, float(hit["score"]))
            if fusion == "rrf":
                entry["score"] += arm_score
            else:
                entry["score"] = max(entry["score"], arm_score)
            if arm not in entry["sources"]:
                entry["sources"].append(arm)
            if arm == "fts":
                entry["fts_score"] = round(float(hit["score"]), 6)
                if not entry["snippet"]:
                    entry["snippet"] = hit.get("snippet") or ""
            else:
                entry["vector_score"] = round(float(hit["score"]), 6)
                entry["chunks"] = hit.get("chunks") or []
                entry["n_chunks"] = int(hit.get("n_chunks") or 0)
                if not entry["snippet"] and entry["chunks"]:
                    entry["snippet"] = (entry["chunks"][0]["text"] or "")[:200]
    for entry in entries.values():
        arms = entry.pop("_arms")
        winner = max(arms.items(), key=lambda item: (item[1][0], item[0]))[1]
        entry["raw_score"] = round(winner[1], 6)
        # D52 档位先验：统一尺度后仍要压掉"只有元数据"的空壳，避免它的标题/摘要强命中
        # 把有正文的文档挤下去（实测 L0 曾以 1.000 排第 4）。
        entry["score"] = round(entry["score"] * _TIER_PRIOR.get(tier, 1.0), 6)
    return sorted(entries.values(), key=lambda item: (-item["score"], item["doc_id"]))


def _interleave_select(merged: list[dict], fts_hits: list[dict], vec_hits: list[dict],
                       slots: int) -> list[dict]:
    """两支路并列（"co-equal arms"）：FTS / 向量各按自己的名次轮转取名额。

    动机（M5 实测）：FTS 支路的候选池可达数百条（中文 query 会命中大量中文文档），
    若纯按分数取前 N，向量支路的命中（例如"中文 query → 英文论文"）会被整档挤掉；
    级内 min-max 还会把余弦的细小差距放大成排名差。轮转取名额保证两条支路
    在每档都有代表，同时仍用级内归一化分数做展示排序。
    """
    if slots <= 0 or not merged:
        return []
    by_doc = {entry["doc_id"]: entry for entry in merged}
    ranked = [
        [hit["doc_id"] for hit in sorted(fts_hits, key=lambda h: (-h["score"], h["doc_id"]))],
        [hit["doc_id"] for hit in sorted(vec_hits, key=lambda h: (-h["score"], h["doc_id"]))],
    ]
    chosen: list[dict] = []
    seen: set[str] = set()
    pointers = [0, 0]
    while len(chosen) < slots:
        progressed = False
        for arm in (0, 1):
            while pointers[arm] < len(ranked[arm]) and ranked[arm][pointers[arm]] in seen:
                pointers[arm] += 1
            if pointers[arm] < len(ranked[arm]):
                doc_id = ranked[arm][pointers[arm]]
                pointers[arm] += 1
                if doc_id not in seen and doc_id in by_doc:
                    seen.add(doc_id)
                    chosen.append(by_doc[doc_id])
                    progressed = True
                    if len(chosen) >= slots:
                        break
        if not progressed:
            break
    if len(chosen) < slots:
        for entry in sorted(merged, key=lambda item: (-item["score"], item["doc_id"])):
            if entry["doc_id"] in seen:
                continue
            chosen.append(entry)
            seen.add(entry["doc_id"])
            if len(chosen) >= slots:
                break
    return chosen


def _reserve_select(merged: list[dict], fts_hits: list[dict], vec_hits: list[dict],
                    slots: int) -> list[dict]:
    """向量保底（04 §9 跨语言硬门槛）：每档保留 1 个名额给向量支路最佳块。

    与 interleave 的区别：FTS 仍是主排序（保住 M3 召回），只让出**一个**名额；
    当档位只有 1 个名额时不夺（避免把该档唯一名额让给向量）。
    """
    if slots <= 0 or not merged:
        return []
    by_doc = {entry["doc_id"]: entry for entry in merged}
    ordered = sorted(merged, key=lambda item: (-item["score"], item["doc_id"]))
    reserve = 1 if slots >= 2 and vec_hits else 0
    keep = max(0, slots - reserve)
    chosen: list[dict] = []
    seen: set[str] = set()
    for entry in ordered:
        if len(chosen) >= keep:
            break
        chosen.append(entry)
        seen.add(entry["doc_id"])
    if reserve:
        for hit in sorted(vec_hits, key=lambda h: (-h["score"], h["doc_id"])):
            if hit["doc_id"] in seen or hit["doc_id"] not in by_doc:
                continue
            chosen.append(by_doc[hit["doc_id"]])
            seen.add(hit["doc_id"])
            break
    # 向量保底名额未被用掉（向量最佳已在选中列表）时，按分数补满
    if len(chosen) < slots:
        for entry in ordered:
            if len(chosen) >= slots:
                break
            if entry["doc_id"] in seen:
                continue
            chosen.append(entry)
            seen.add(entry["doc_id"])
    return chosen


def _select_tier(tier: int, fts_hits: list[dict], vec_hits: list[dict], fusion: str,
                 slots: int,
                 fts_bounds: tuple[float, float] | None = None,
                 vec_bounds: tuple[float, float] | None = None) -> list[dict]:
    merged = _merge_tier(tier, fts_hits, vec_hits, fusion, fts_bounds, vec_bounds)
    if fusion == "interleave":
        return _interleave_select(merged, fts_hits, vec_hits, slots)
    if fusion == "reserve":
        return _reserve_select(merged, fts_hits, vec_hits, slots)
    return merged[:max(0, slots)]


# ── 主入口 ──────────────────────────────────────────────────────────────────


def ask(
    query: str,
    *,
    topic: str | None = None,
    tags: list[str] | None = None,
    org: list[str] | None = None,
    source_types: list[str] | None = None,
    year: str | None = None,
    tier_min: int | None = None,
    tier_max: int | None = None,
    stage: str | None = None,
    mode: str = "balanced",
    top_k: int = 10,
    quota: dict | None = None,
    use_vector: bool | None = None,
    embed_fn=None,
    fusion: str = DEFAULT_FUSION,
    vector_top_chunks: int = VECTOR_TOP_CHUNKS,
    fold: str = VECTOR_FOLD,
    toc_filter: bool = True,
    exclude_kinds: list[str] | None = None,
    rerank: bool | None = None,
    rerank_scorer=None,
) -> dict:
    """统一检索入口。

    `quota` 默认取 §9.2 配额表；显式传入可覆盖（**仅供评测/诊断**）。
    `use_vector=None` → 按 mode 决定（deep 默认开，02 §11.1）。
    `embed_fn` 供单测注入确定性向量；`fusion`/`fold` 供裁定实验（M5）。
    `toc_filter`（M8-3）：是否在 chunk→doc 折叠前剔除"目录残渣块"（`chunkq.partition_toc`），
    默认开启；显式 False 供"开/关对照"实验与单测注入固定语料时使用。
    `exclude_kinds`（D53）：整篇排除的 version kind；`None` = 读 `knowledgebase/_meta/config/scope.json`
    （默认排除 `web_note`），显式传 `[]` = 不排除（诊断用）。
    `rerank`（D55）：是否做交叉编码器重排；`None` = 模型可用就用（`KB_RERANK=0` 可全局关）。
    开启时召回池放大到 `top_k × rerank.RERANK_POOL`，重排后再截 `top_k`。
    `rerank_scorer` 供单测注入打分函数。
    """
    if mode not in _QUOTA:
        raise ValueError(f"unknown mode: {mode} (expect one of {', '.join(MODES)})")
    if top_k < 1:
        raise ValueError("top_k must be >= 1")
    if fusion not in FUSIONS:
        raise ValueError(f"unknown fusion: {fusion} (expect one of {', '.join(FUSIONS)})")
    if fold not in VECTOR_FOLDS:
        raise ValueError(f"unknown fold: {fold} (expect one of {', '.join(VECTOR_FOLDS)})")
    vector_enabled = (mode in VECTOR_MODES) if use_vector is None else bool(use_vector)

    # D55：先判断重排**用不用得了**，再决定召回池——反过来会导致降级时池子白放大
    if rerank_scorer is not None:          # 单测注入打分函数：显式启用，不查模型是否存在
        use_rerank = True
    else:
        use_rerank = (rerank_mod.available() if rerank is None
                      else (bool(rerank) and rerank_mod.available()))
    if rerank is True and not use_rerank:
        raise RuntimeError("重排模型不可用，请准备权重或在 .envs 配置 RERANK_MODEL_DIR")
    pool_k = top_k * rerank_mod.RERANK_POOL if use_rerank else top_k

    filters = {
        "topic": topic, "tags": tags or [], "org": org or [],
        "source_types": source_types or [], "year": year,
        "tier_min": tier_min, "tier_max": tier_max, "stage": stage,
        "exclude_kinds": list(exclude_kinds) if exclude_kinds is not None
        else excluded_kinds(),
    }
    range_min = 0 if tier_min is None else int(tier_min)
    range_max = 3 if tier_max is None else int(tier_max)

    vector_stats: dict = {"enabled": vector_enabled, "error": None}
    conn = connect()
    try:
        hits, candidates = _recall(conn, query, filters, range_min, range_max)

        vector_hits: list[dict] = []
        if vector_enabled:
            try:
                started = time.perf_counter()
                query_vector = _embed_query(query, embed_fn)
                embed_seconds = time.perf_counter() - started
                vector_hits, vector_stats = _vector_recall(
                    conn, query_vector, filters, range_min, range_max,
                    top_chunks=vector_top_chunks, fold=fold, embed_seconds=embed_seconds,
                    toc_filter=toc_filter,
                )
            except Exception as exc:  # noqa: BLE001 — 向量支路失败降级为纯 FTS
                vector_stats = {"enabled": True, "error": f"{type(exc).__name__}: {exc}"}
                vector_hits = []

        doc_ids = sorted({hit["doc_id"] for hit in hits}
                         | {hit["doc_id"] for hit in vector_hits})
        if not doc_ids:
            result = _result(query, mode, filters, [], candidates, None, vector_stats, fusion)
            result["stats"]["rerank"] = {
                "enabled": False, "model": rerank_mod.MODEL_DIR.name, "pool": 0,
                "returned": 0, "seconds": 0.0, "error": "no candidates",
            }
            return result

        docs = _load_docs(conn, doc_ids)
        for hit in hits:
            if hit["tier"] is None:
                hit["tier"] = int((docs.get(hit["doc_id"]) or {}).get("best_tier") or 0)
        for hit in vector_hits:
            hit["tier"] = int((docs.get(hit["doc_id"]) or {}).get("best_tier") or 0)
        _apply_tag_bonus(hits, docs, query)

        # 支路内先折叠到 doc 级（FTS 理论上可重复；向量已折叠）
        fts_best: dict[str, dict] = {}
        for hit in hits:
            current = fts_best.get(hit["doc_id"])
            if current is None or hit["score"] > current["score"]:
                fts_best[hit["doc_id"]] = hit
        fts_by_tier: dict[int, list[dict]] = {}
        for hit in fts_best.values():
            fts_by_tier.setdefault(hit["tier"], []).append(hit)
        vec_by_tier: dict[int, list[dict]] = {}
        for hit in vector_hits:
            vec_by_tier.setdefault(hit["tier"], []).append(hit)

        quota_table = quota if quota is not None else quota_for(mode, pool_k)
        # D52：跨档统一尺度——两条支路各用全库候选的极值归一化一次，再分档合并。
        fts_bounds = _norm_bounds(fts_by_tier)
        vec_bounds = _norm_bounds(vec_by_tier)
        # ① **选谁**：沿用分档配额 + 级内归一化——保证每档都有代表，
        #    也保证"某一档唯一的好命中"不会被别的档挤掉（召回不受影响）。
        selected: list[dict] = []
        for tier in sorted(set(fts_by_tier) | set(vec_by_tier)):
            selected.extend(_select_tier(tier, fts_by_tier.get(tier, []),
                                        vec_by_tier.get(tier, []), fusion,
                                        quota_table.get(tier, 0)))
        # ② **怎么排**：用跨档统一尺度重新打分，消掉"每档最佳都=1.000"的并列假象。
        _rescore_cross_tier(selected, fts_bounds, vec_bounds)
        selected.sort(key=lambda item: (-item["score"], -item["tier"], item["doc_id"]))
        selected = selected[:pool_k]

        versions = _load_versions(conn, [hit["doc_id"] for hit in selected])
        groups = [
            _group(hit, docs.get(hit["doc_id"]) or {}, versions.get(hit["doc_id"], []))
            for hit in selected
        ]
        # ③ **D55 重排**：把 (查询, 候选) 成对喂给 cross-encoder 重新排序，再截 top_k。
        if use_rerank:
            groups, rerank_stats = rerank_mod.rerank_groups(
                query, groups, top_k, scorer=rerank_scorer)
        else:
            pool_size = len(groups)
            groups = groups[:top_k]
            rerank_stats = {
                "enabled": False, "model": rerank_mod.MODEL_DIR.name,
                "pool": pool_size, "returned": len(groups),
                "seconds": 0.0, "error": "disabled",
            }
        by_tier: dict[str, int] = {}
        for group in groups:
            by_tier[str(group["hit_tier"])] = by_tier.get(str(group["hit_tier"]), 0) + 1
        result = _result(query, mode, filters, groups, candidates, by_tier, vector_stats, fusion)
        result["stats"]["rerank"] = rerank_stats
        return result
    finally:
        conn.close()


def _rescore_cross_tier(entries: list[dict],
                        fts_bounds: tuple[float, float],
                        vec_bounds: tuple[float, float]) -> None:
    """D52：把已入选的条目改用**跨档统一尺度**重算展示分。

    选择阶段仍用级内归一化（保护召回），这里只改排序用的分数：
    每条取两支路在全局尺度下的较高者，再乘档位先验（L0 空壳 ×0.30）。
    """
    for entry in entries:
        tier = int(entry["tier"])
        arm_scores: list[float] = []
        if entry.get("fts_score") is not None:
            arm_scores.append(_normalize([float(entry["fts_score"])], fts_bounds)[0])
        if entry.get("vector_score") is not None:
            arm_scores.append(_normalize([float(entry["vector_score"])], vec_bounds)[0])
        base = max(arm_scores) if arm_scores else 0.0
        entry["score"] = round(base * _TIER_PRIOR.get(tier, 1.0), 6)


def _preferred_kind(pool: list[dict], tier: int) -> str:
    """同档多 kind 时按 `_TIER_KIND_PREFERENCE` 择优；都不匹配则按 kind 名排序取首个。"""
    if not pool:
        return ""
    for kind in _TIER_KIND_PREFERENCE.get(tier, ()):
        for version in pool:
            if version["kind"] == kind:
                return str(version["kind"])
    return str(sorted(pool, key=lambda version: version["kind"])[0]["kind"])


def _group(hit: dict, doc: dict, versions: list[dict]) -> dict:
    """组装一条命中。

    D44（2026-10-19）：`quote_from` 把「能不能引原话」从档位里拆出来——
      raw     = 该文档存在 tier=1（原文）版本，可 `kb-fetch --tier 1` 取原话；
      derived = 只有轻整理/提炼版，只能转述，不能当原话引；
      none    = 只有元数据。
    """
    legacy_l2 = (doc.get("status") or "") == "legacy_l2"
    chunks = hit.get("chunks") or []
    best_tier = int(doc.get("best_tier") or 0)
    raw_versions = [
        version for version in versions
        if version["tier"] == 1 and version.get("available", True)
    ]
    if raw_versions:
        quote_from, quote_tier = "raw", 1
        quote_kind = _preferred_kind(raw_versions, 1)
    elif any(version["tier"] >= 2 for version in versions):
        quote_from, quote_tier = "derived", best_tier
        quote_kind = _preferred_kind(
            [version for version in versions if version["tier"] >= 2], best_tier)
    else:
        # 只有 meta（或什么都没有）：没有正文可引
        quote_from, quote_tier = "none", best_tier
        quote_kind = ""
    return {
        "doc_id": hit["doc_id"],
        "title": doc.get("title") or "",
        "topic": doc.get("topic") or "",
        "source_type": doc.get("source_type") or "",
        "best_tier": best_tier,
        "hit_tier": hit["tier"],
        "score": round(hit["score"], 6),
        "raw_score": round(hit.get("raw_score", 0.0), 6),
        "fts_score": hit.get("fts_score"),
        "vector_score": hit.get("vector_score"),
        "sources": list(hit.get("sources") or []),
        "stage": doc.get("stage") or "",
        "tags": _json_list(doc.get("tags")),
        "org_marks": _json_list(doc.get("org_marks")),
        "versions": [
            {
                "kind": version["kind"], "tier": version["tier"],
                "path": version["path"], "available": version["available"],
            }
            for version in versions
        ],
        "snippet": hit.get("snippet") or "",
        # M5 段落级取证：命中块（含版本/序号），quote_tier 的块可直接喂 kb-fetch --chunk-ids
        "matched_chunks": chunks,
        "quote_chunk_ids": [
            chunk["chunk_id"] for chunk in chunks if chunk.get("tier") == quote_tier
        ],
        "evidence": {
            "quote_from": quote_from,
            "quote_tier": quote_tier,
            "quote_kind": quote_kind,
            "verbatim_allowed": quote_from == "raw",
            "l0_only": best_tier == 0,
            # D27：存量无回链 L2 的豁免标记（M5 起不再新增，kb_promote 强制回链）
            "legacy_l2": legacy_l2,
        },
    }


def _result(query: str, mode: str, filters: dict, groups: list[dict],
            candidates: int, by_tier: dict | None = None,
            vector_stats: dict | None = None, fusion: str = DEFAULT_FUSION) -> dict:
    stats = {
        "candidates": candidates,
        "returned": len(groups),
        "by_tier": by_tier if by_tier is not None else {},
        "fusion": fusion,
    }
    if vector_stats is not None:
        stats["vector"] = vector_stats
    return {
        "query": query,
        "mode": mode,
        "filters": {key: value for key, value in filters.items() if value not in (None, [], "")},
        "groups": groups,
        "stats": stats,
    }


# ── 取原文 / 段落级取证 ─────────────────────────────────────────────────────


def _pick_version(versions: list[dict], tier: int | None) -> dict | None:
    """选版本：指定 tier 时按 kind 优先级择优（原文优先于译文），否则取最高档。"""
    if not versions:
        return None
    if tier is not None:
        pool = [version for version in versions if version["tier"] == tier]
        if not pool:
            return None
    else:
        pool = versions
    best_tier = max(version["tier"] for version in pool)
    pool = [version for version in pool if version["tier"] == best_tier]
    for kind in _TIER_KIND_PREFERENCE.get(best_tier, ()):
        for version in pool:
            if version["kind"] == kind:
                return version
    return sorted(pool, key=lambda version: version["kind"])[0]


def _parse_chunk_ids(chunk_ids) -> tuple[list[int], list]:
    wanted: list[int] = []
    bad: list = []
    for raw in chunk_ids:
        try:
            wanted.append(int(str(raw).strip()))
        except (TypeError, ValueError):
            bad.append(raw)
    return wanted, bad


def _fetch_chunks(conn, doc_id: str, doc: dict, tier: int | None,
                  chunk_ids, context: int) -> dict:
    """段落级取证（M5）：返回命中块 ± context 上下文与块定位，不给整篇。"""
    wanted, bad = _parse_chunk_ids(chunk_ids)
    if bad:
        return {"error": "invalid_chunk_id", "doc_id": doc_id, "chunk_ids": bad,
                "hint": "chunk_id 必须是整数（kb-ask 的 matched_chunks[].chunk_id）"}
    if not wanted:
        return {"error": "invalid_chunk_id", "doc_id": doc_id, "chunk_ids": []}
    context = max(0, int(context))

    placeholders = ",".join("?" * len(wanted))
    rows = conn.execute(
        f"""
        SELECT c.chunk_id, c.version_id, c.chunk_index, c.text, c.model_id,
               v.tier, v.kind, v.path
        FROM chunks c JOIN versions v ON v.version_id = c.version_id
        WHERE c.doc_id = ? AND c.chunk_id IN ({placeholders})
        ORDER BY v.tier, c.chunk_index
        """,
        [doc_id, *wanted],
    ).fetchall()
    missing = [chunk_id for chunk_id in wanted
               if chunk_id not in {int(row["chunk_id"]) for row in rows}]
    if missing:
        return {
            "error": "chunk_not_found", "doc_id": doc_id, "missing": missing,
            "hint": "chunk_id 必须属于该 doc；用 kb-ask 的 matched_chunks[].chunk_id",
        }

    chunk_tiers = {int(row["chunk_id"]): int(row["tier"]) for row in rows}
    if tier is not None:
        rows = [row for row in rows if int(row["tier"]) == tier]
        if not rows:
            return {
                "error": "chunk_not_in_tier", "doc_id": doc_id,
                "requested_tier": tier, "chunk_tiers": chunk_tiers,
                "available_tiers": sorted(set(chunk_tiers.values())),
                "hint": f"这些块不在 L{tier}；去掉 --tier 或改用列出的档位",
            }

    segments: list[dict] = []
    for row in rows:
        version_id = int(row["version_id"])
        index = int(row["chunk_index"])
        total = int(conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE version_id=?", (version_id,)
        ).fetchone()[0])
        neighbors = conn.execute(
            "SELECT chunk_id, chunk_index, text FROM chunks WHERE version_id=? "
            "AND chunk_index BETWEEN ? AND ? ORDER BY chunk_index",
            (version_id, index - context, index + context),
        ).fetchall()
        before = [
            {"chunk_id": int(item["chunk_id"]), "chunk_index": int(item["chunk_index"]),
             "text": item["text"]}
            for item in neighbors if int(item["chunk_index"]) < index
        ]
        after = [
            {"chunk_id": int(item["chunk_id"]), "chunk_index": int(item["chunk_index"]),
             "text": item["text"]}
            for item in neighbors if int(item["chunk_index"]) > index
        ]
        segments.append({
            "chunk_id": int(row["chunk_id"]),
            "version_id": version_id,
            "tier": int(row["tier"]),
            "kind": row["kind"],
            "path": row["path"],
            "chunk_index": index,
            "total_chunks": total,
            "text": row["text"],
            "char_count": len(row["text"]),
            "context_before": before,
            "context_after": after,
            "prev_chunk_id": before[-1]["chunk_id"] if before else None,
            "next_chunk_id": after[0]["chunk_id"] if after else None,
            "embedded": bool(row["model_id"]),
        })

    first = segments[0]
    return {
        "doc_id": doc_id,
        "tier": int(first["tier"]),
        "kind": first["kind"],
        "path": first["path"],
        "title": (doc or {}).get("title") or "",
        "abstract": (doc or {}).get("abstract") or "",
        "topic": (doc or {}).get("topic") or "",
        "source_type": (doc or {}).get("source_type") or "",
        "best_tier": int((doc or {}).get("best_tier") or 0),
        "content": "",
        "char_count": 0,
        "meta_only": False,
        "segments": segments,
        "segment_count": len(segments),
        "context": context,
        "chunk_note": "段落取证：只回命中块 ±context 上下文（不给整篇）；"
                      "chunk_index 为块在该版本内的序号，可用 prev/next_chunk_id 续读",
    }


def _with_alt(payload: dict, candidates: list[str]) -> dict:
    """M8：精确命中的同时把"同名前缀登记"作为知情项塞进返回体（不阻断、不合并）。"""
    if candidates and isinstance(payload, dict) and "error" not in payload:
        payload["alt_candidates"] = candidates
        payload["alt_note"] = ("同名 doc_id 在其它 topic 下另有登记（同一篇论文的空壳/全文"
                               "双登记）；本条已按存储 ID 精确命中，未做任何合并")
    return payload


def fetch(doc_id: str, tier: int | None = None, chunk_ids: list | None = None,
          *, context: int = 1) -> dict:
    """取某文档某一档的正文；L0 只有元数据时明确标记 `仅元数据`。

    `chunk_ids` 非空 → 段落级取证（M5 归位）：返回命中块 ±context 上下文 + 块定位。
    """
    conn = connect()
    try:
        resolved, candidates = resolve_doc_id(conn, doc_id)
        if resolved is None:
            if candidates:
                return {
                    "error": "ambiguous_doc_id", "input": doc_id, "candidates": candidates,
                    "hint": "请用带 topic 前缀的存储 ID 重试",
                }
            return {"error": "not_found", "input": doc_id, "candidates": []}
        docs = _load_docs(conn, [resolved])
        doc = docs.get(resolved)
        versions = _load_versions(conn, [resolved]).get(resolved, [])
        if chunk_ids:
            return _with_alt(
                _fetch_chunks(conn, resolved, doc or {}, tier, list(chunk_ids), context),
                candidates)
        version = _pick_version(versions, tier)
        if version is None:
            return {
                "error": "tier_not_available", "doc_id": resolved,
                "requested_tier": tier,
                "available_tiers": sorted({item["tier"] for item in versions}),
            }
        path = _resolve_path(version["path"])
        meta_only = version["kind"] == "meta" or not version["available"]
        content = ""
        if not meta_only:
            try:
                content = path.read_text(encoding="utf-8", errors="replace")[:_CONTENT_MAX_CHARS]
            except OSError as exc:
                return {"error": "read_failed", "doc_id": resolved, "path": version["path"],
                        "detail": str(exc)}
        payload = {
            "doc_id": resolved,
            "tier": version["tier"],
            "kind": version["kind"],
            "path": version["path"],
            "title": (doc or {}).get("title") or "",
            "abstract": (doc or {}).get("abstract") or "",
            "topic": (doc or {}).get("topic") or "",
            "source_type": (doc or {}).get("source_type") or "",
            "best_tier": int((doc or {}).get("best_tier") or 0),
            "content": content,
            "char_count": len(content),
            "meta_only": meta_only,
            "chunks": [],
            "segments": [],
            "chunk_note": "需要段落级取证时用 --chunk-ids（kb-ask 的 matched_chunks[].chunk_id）",
        }
        if meta_only:
            payload["note"] = "仅元数据：该条目无正文，只能断言其存在性，禁止把 abstract 当结论引用"
        # M8：精确命中时同名前缀行不阻断，但必须让调用方知情（该论文被多 topic 双登记）
        return _with_alt(payload, candidates)
    finally:
        conn.close()
