"""ragsearch 知识库服务 — infosystem 管线的统一入口。

薄封装：不启动 HTTP，直接 import ragsearch 引擎。供后端管线（archive/condense）
与接口 AI 使用。函数签名即对外契约（详见 文档以及其它代码/(4)RAG与信息整合设计/知识库接口契约.md；
数据组织与扩展指南见 文档以及其它代码/(4)RAG与信息整合设计/知识库与检索系统设计.md）。

返回的 SearchResult 为 dict（`__dict__` 转出），可 JSON 序列化。

    SearchResult: {filename, doc_id, title, snippet, score, rank,
                   section, source, authors, year, tags, source_type}
    GroupResult:  {doc_id, title, score, version_count, versions: [SearchResult]}
"""

from __future__ import annotations

import os
import shutil
import threading
import threading

import ragsearch.config as rc
from ragsearch.engine.dedup import dedup_results
from ragsearch.engine.filter import filter_results, group_results
from ragsearch.engine.indexer import DocIndexer
from ragsearch.engine.reranker import Reranker


def load_registry() -> dict[str, str]:
    """重载 registry.json，返回 {kb: 绝对目录}。"""
    rc.KNOWLEDGE_BASES = rc.load_registry()
    rc.KB_SETTINGS = rc.load_kb_settings()
    return dict(rc.KNOWLEDGE_BASES)


_INDEX_CACHE: dict[str, tuple[object, DocIndexer]] = {}
_INDEX_LOCK = threading.RLock()


def _manifest_path(name: str) -> str:
    return os.path.join(rc.INDEX_ROOT, name, "manifest.json")


def _manifest_key(name: str):
    """索引 manifest 文件签名——文件没变即可安全复用内存中的索引。

    读不到（刚 rebuild / 首次运行）返回 None，调用方按「需要 ensure_index」处理。
    """
    try:
        st = os.stat(_manifest_path(name))
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _index_dirty(idx: DocIndexer) -> bool:
    """KB 源文档是否已变化。任何异常都保守认为脏。"""
    try:
        return bool(idx.is_dirty())
    except Exception:  # noqa: BLE001
        return True


def _get_indexer(name: str) -> DocIndexer:
    """取可用的 DocIndexer（进程内缓存）。

    命中时**完全跳过** manifest 全量重算与 bm25_index.pkl 反序列化。
    原先每次检索都新建 indexer + ensure_index()：单个 KB 的 pkl 有 7–18MB，
    一次 search_all() 跨 12 个库 => 每个请求约 13 秒纯加载开销（实测）。
    """
    key = _manifest_key(name)
    with _INDEX_LOCK:
        hit = _INDEX_CACHE.get(name)
        if hit is not None and key is not None:
            cached_key, idx = hit
            if cached_key == key and not _index_dirty(idx):
                return idx
        idx = _make_indexer(name)
        idx.ensure_index()
        _INDEX_CACHE[name] = (_manifest_key(name), idx)
        return idx


def _clear_indexer_cache(name: str | None = None) -> None:
    """清缓存（rebuild / 归档刷新后调用，避免继续用旧索引）。"""
    with _INDEX_LOCK:
        if name is None:
            _INDEX_CACHE.clear()
        else:
            _INDEX_CACHE.pop(name, None)


def _make_indexer(name: str) -> DocIndexer:
    settings = rc.KB_SETTINGS.get(name, {})
    return DocIndexer(
        docs_dir=rc.KNOWLEDGE_BASES[name],
        index_dir=os.path.join(rc.INDEX_ROOT, name),
        title_weight=settings.get("title_weight", rc.TITLE_WEIGHT),
        body_weight=settings.get("body_weight", rc.BODY_WEIGHT),
        tag_boost=settings.get("tag_boost", 0.0),
        include=settings.get("include"),
        exclude=settings.get("exclude"),
    )


def rebuild_kb(name: str) -> dict:
    """强制重建某库索引。目录不存在返回 error 而非抛错。

    返回 {"status": "ok"|"error", "kb_name", "doc_count", "error"}。
    """
    if name not in rc.KNOWLEDGE_BASES:
        return {"status": "error", "kb_name": name, "doc_count": 0, "error": f"未登记: {name}"}
    path = rc.KNOWLEDGE_BASES[name]
    if not os.path.isdir(path):
        return {"status": "error", "kb_name": name, "doc_count": 0, "error": f"目录不存在: {path}"}
    index_dir = os.path.join(rc.INDEX_ROOT, name)
    with _INDEX_LOCK:
        if os.path.isdir(index_dir):
            shutil.rmtree(index_dir)
        _clear_indexer_cache(name)
        try:
            idx = _make_indexer(name)
            idx.ensure_index()
            _INDEX_CACHE[name] = (_manifest_key(name), idx)
            return {"status": "ok", "kb_name": name, "doc_count": len(idx.docs), "error": None}
        except Exception as e:  # noqa: BLE001
            return {"status": "error", "kb_name": name, "doc_count": 0, "error": str(e)}


def _result_dict(r) -> dict:
    """把 SearchResult 转 dict，并把 numpy 标量（score）归一为 Python float，保证 JSON 可序列化。"""
    d = r.__dict__
    return {**d, "score": float(d.get("score", 0.0))}


def _search_single(name: str, query: str, top_k: int, **kw) -> list:
    if name not in rc.KNOWLEDGE_BASES:
        return []
    try:
        idx = _get_indexer(name)
    except Exception:  # noqa: BLE001 — 目录缺失/索引失败则该库跳过
        return []
    raw = idx.search(query, top_k=top_k * 5)
    raw = filter_results(raw, **kw)
    results = Reranker(rc.RERANK_BACKEND).rerank(query, raw, top_k=top_k)
    return dedup_results(results)


def search_kb(name: str, query: str, top_k: int = 10, *,
              section: str | None = None, tags: str | None = None,
              year: str | None = None, authors: str | None = None,
              source_type: str | None = None, group_by_doc: bool = False) -> list:
    """单库搜索。返回 list[dict]；group_by_doc=True 时返回分组结构。"""
    kw = dict(section=section, tags=tags, year=year, authors=authors, source_type=source_type)
    results = _search_single(name, query, top_k, **kw)
    if group_by_doc:
        groups = group_results(results)
        return [dict(g, score=float(g["score"]), version_count=len(g["versions"]),
                     versions=[_result_dict(r) for r in g["versions"]]) for g in groups]
    return [_result_dict(r) for r in results]


def search_all(query: str, top_k: int = 10, *,
               section: str | None = None, tags: str | None = None,
               year: str | None = None, authors: str | None = None,
               source_type: str | None = None, group_by_doc: bool = False) -> list:
    """跨库搜索。filename 带 [kb] 前缀。"""
    kw = dict(section=section, tags=tags, year=year, authors=authors, source_type=source_type)
    flat = []
    for name in rc.KNOWLEDGE_BASES:
        for r in _search_single(name, query, top_k, **kw):
            r.filename = f"[{name}] {r.filename}"
            flat.append(r)
    flat.sort(key=lambda r: r.score, reverse=True)
    flat = dedup_results(flat)[:top_k]
    if group_by_doc:
        groups = group_results(flat)
        return [dict(g, score=float(g["score"]), version_count=len(g["versions"]),
                     versions=[_result_dict(r) for r in g["versions"]]) for g in groups]
    return [_result_dict(r) for r in flat]


def get_status() -> dict:
    """所有已登记 KB 的状态：{kb: {doc_count, docs_dir, index_ready}}。"""
    out: dict = {}
    for name, path in rc.KNOWLEDGE_BASES.items():
        try:
            idx = _make_indexer(name)
            idx.ensure_index()
            out[name] = {"doc_count": len(idx.docs), "docs_dir": path, "index_ready": True}
        except Exception:  # noqa: BLE001
            out[name] = {"doc_count": 0, "docs_dir": path, "index_ready": False}
    return out


def search_folder(folder: str, query: str, top_k: int = 10, *,
                  section: str | None = None, tags: str | None = None,
                  year: str | None = None, authors: str | None = None,
                  source_type: str | None = None, group_by_doc: bool = False) -> list:
    """在任意文件夹里立刻做 BM25 检索（不向量化、不进知识库、不登记 registry）。

    文件夹格式要求：内含 .md 文件（递归、UTF-8）；带 YAML frontmatter 可选
    （title/source/section/authors/year/tags/source_type → 支持过滤/分组），
    不带也能搜（标题回退 `# ` 行）。
    临时索引缓存在 ragsearch/index/_tmp/{hash}/，文件变化按 manifest 自动重建。
    返回与 search_kb 相同的 dict 列表。
    """
    import hashlib
    import os

    folder = os.path.abspath(folder)
    if not os.path.isdir(folder):
        return []
    h = hashlib.md5(folder.encode("utf-8")).hexdigest()[:12]
    idx_dir = os.path.join(rc.INDEX_ROOT, "_tmp", h)
    try:
        idx = DocIndexer(docs_dir=folder, index_dir=idx_dir)
        idx.ensure_index()
    except Exception:  # noqa: BLE001 — 目录无 md 或索引失败 → 空结果
        return []
    raw = idx.search(query, top_k=top_k * 5)
    raw = filter_results(raw, section=section, tags=tags, year=year,
                         authors=authors, source_type=source_type)
    results = Reranker(rc.RERANK_BACKEND).rerank(query, raw, top_k=top_k)
    results = dedup_results(results)
    if group_by_doc:
        groups = group_results(results)
        return [dict(g, score=float(g["score"]), version_count=len(g["versions"]),
                     versions=[_result_dict(r) for r in g["versions"]]) for g in groups]
    return [_result_dict(r) for r in results]


def fetch_paper(arxiv_id: str) -> dict:
    """取单篇论文全文：dense 浓缩优先，回退 full 最优完整版。

    返回 {"id", "source"(dense|full|pdf|None), "content", "unparsed"(仅PDF未解析), "missing"}。
    """
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent.parent.parent
    # 分区优先（2026-10-20）：浓缩稿在 <分区>/dense/<id>.md，正文在 <分区>/papers/<id>/
    for part in ("default", "CBAC"):
        dense = root / "knowledgebase" / part / "dense" / f"{arxiv_id}.md"
        if dense.exists():
            return {"id": arxiv_id, "source": "dense",
                    "content": dense.read_text(encoding="utf-8", errors="replace"),
                    "unparsed": False, "missing": False}
    full_dir = None
    for part in ("default", "CBAC"):
        candidate = root / "knowledgebase" / part / "papers" / arxiv_id
        if candidate.is_dir():
            full_dir = candidate
            break
    if full_dir is None:
        full_dir = root / "knowledgebase" / "default" / "papers" / arxiv_id
    for name in ("paper.md", "cn_paper.md", "paper_ocr.md", "cn_paper_ocr.md"):
        p = full_dir / name
        if p.exists():
            return {"id": arxiv_id, "source": "full",
                    "content": p.read_text(encoding="utf-8", errors="replace"),
                    "unparsed": False, "missing": False}
    if (root / "data" / "papers" / arxiv_id / "paper.pdf").exists():
        return {"id": arxiv_id, "source": "pdf", "content": "", "unparsed": True, "missing": False}
    return {"id": arxiv_id, "source": None, "content": "", "unparsed": False, "missing": True}
