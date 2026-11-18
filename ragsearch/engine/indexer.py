"""双索引加权 BM25 索引器 + 持久化。

支持：标题/正文双索引归一化加权、frontmatter 全字段、include/exclude glob、
tag_boost（查询词命中 frontmatter tags 加分）、format_version=3 持久化。
"""
import fnmatch
import hashlib
import json
import math
import os
import pickle

import jieba
from rank_bm25 import BM25Okapi

from ragsearch.config import (BODY_WEIGHT, BM25_TOP_K, HIGHLIGHT, SNIPPET_CONTEXT,
                              SNIPPET_MAX, TITLE_WEIGHT)
from ragsearch.engine.document import (DocEntry, SearchResult, doc_id_from_path,
                                       extract_title, normalize_meta, parse_frontmatter)
from ragsearch.engine.snippet import make_snippet

FORMAT_VERSION = 4


class _BM25OkapiFixed(BM25Okapi):
    """修正 rank_bm25 的 idf：原式 log(N-n+0.5)-log(n+0.5) 在词出现恰好一半文档时 idf=0
    （小库/临时文件夹尤其致命，所有分数归零）。改为标准式 log(1+(N-n+0.5)/(n+0.5))，
    对 n<N 恒为正。"""

    def _calc_idf(self, nd):
        idf_sum = 0
        for word, freq in nd.items():
            idf = math.log(1 + (self.corpus_size - freq + 0.5) / (freq + 0.5))
            self.idf[word] = idf
            idf_sum += idf
        self.average_idf = idf_sum / len(self.idf) if self.idf else 0


class DocIndexer:
    def __init__(self, docs_dir, index_dir, title_weight=TITLE_WEIGHT,
                 body_weight=BODY_WEIGHT, tag_boost=0.0,
                 include=None, exclude=None):
        self.docs_dir = docs_dir
        self.index_dir = index_dir
        self.title_weight = title_weight
        self.body_weight = body_weight
        self.tag_boost = tag_boost            # 查询词命中 tags 的加分（P2.8）
        self.include = include or []          # glob 白名单（相对路径）
        self.exclude = exclude or []          # glob 黑名单
        self.docs = []
        self._title_tokenized = []
        self._body_tokenized = []
        self.bm25_title = None
        self.bm25_body = None

    # ── 加载 ──────────────────────────────────────────────
    def _load_md_files(self):
        entries = []
        for root, _, files in os.walk(self.docs_dir):
            for fname in files:
                if not fname.endswith(".md"):
                    continue
                fpath = os.path.join(root, fname)
                rel = os.path.relpath(fpath, self.docs_dir).replace("\\", "/")
                if self.include and not any(fnmatch.fnmatch(rel, p) for p in self.include):
                    continue
                if self.exclude and any(fnmatch.fnmatch(rel, p) for p in self.exclude):
                    continue
                with open(fpath, "r", encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
                meta, body = parse_frontmatter(content)
                m = normalize_meta(meta)
                entries.append(DocEntry(
                    filename=rel,
                    doc_id=doc_id_from_path(rel, m["id"]),
                    title=extract_title(body, meta, fname),
                    section=m["section"],
                    source=m["source"],
                    authors=m["authors"],
                    year=m["year"],
                    tags=m["tags"],
                    source_type=m["source_type"],
                    body=body,
                    char_count=len(body),
                ))
        entries.sort(key=lambda d: d.filename)
        return entries

    @staticmethod
    def _tokenize(text):
        return [w.lower() for w in jieba.lcut_for_search(text) if w.strip()]

    # ── 构建 ──────────────────────────────────────────────
    def build_index(self):
        self.docs = self._load_md_files()
        self._title_tokenized = [self._tokenize(d.title) for d in self.docs]
        self._body_tokenized = [self._tokenize(d.body) for d in self.docs]
        if self.docs:
            self.bm25_title = _BM25OkapiFixed(self._title_tokenized)
            self.bm25_body = _BM25OkapiFixed(self._body_tokenized)
        else:
            # 空知识库：不建 BM25（否则 0 文档除零），search 返回空
            self.bm25_title = None
            self.bm25_body = None
        return len(self.docs)

    # ── 搜索 ──────────────────────────────────────────────
    def search(self, query, top_k=BM25_TOP_K):
        if not self.docs:
            return []  # 空知识库
        if self.bm25_title is None or self.bm25_body is None:
            raise RuntimeError("索引未构建")
        q = self._tokenize(query)
        q_set = set(q)
        title_scores = self.bm25_title.get_scores(q)
        body_scores = self.bm25_body.get_scores(q)
        title_max = max(title_scores) if title_scores.size else 0.0
        body_max = max(body_scores) if body_scores.size else 0.0
        combined = []
        for i, doc in enumerate(self.docs):
            norm_t = title_scores[i] / title_max if title_max else 0.0
            norm_b = body_scores[i] / body_max if body_max else 0.0
            score = self.title_weight * norm_t + self.body_weight * norm_b
            if self.tag_boost and doc.tags:
                overlap = len(set(doc.tags) & q_set)
                if overlap:
                    score += self.tag_boost * overlap
            combined.append((i, score))
        combined.sort(key=lambda x: x[1], reverse=True)
        results = []
        rank = 0
        for i, score in combined:
            if score <= 0:
                continue
            rank += 1
            if rank > top_k:
                break
            doc = self.docs[i]
            results.append(SearchResult(
                filename=doc.filename,
                doc_id=doc.doc_id,
                title=doc.title,
                snippet=make_snippet(doc.body, q, SNIPPET_CONTEXT, SNIPPET_MAX, HIGHLIGHT),
                score=round(score, 4),
                rank=rank,
                section=doc.section,
                source=doc.source,
                authors=doc.authors,
                year=doc.year,
                tags=doc.tags,
                source_type=doc.source_type,
            ))
        return results

    # ── 持久化 ────────────────────────────────────────────
    def _compute_manifest(self):
        files = {}
        for root, _, fnames in os.walk(self.docs_dir):
            for fname in fnames:
                if not fname.endswith(".md"):
                    continue
                fpath = os.path.join(root, fname)
                rel = os.path.relpath(fpath, self.docs_dir).replace("\\", "/")
                if self.include and not any(fnmatch.fnmatch(rel, p) for p in self.include):
                    continue
                if self.exclude and any(fnmatch.fnmatch(rel, p) for p in self.exclude):
                    continue
                with open(fpath, "rb") as fh:
                    files[rel] = hashlib.md5(fh.read()).hexdigest()
        return {"files": files}

    def _read_manifest(self):
        mpath = os.path.join(self.index_dir, "manifest.json")
        if not os.path.isfile(mpath):
            return {"files": {}}
        with open(mpath, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def is_dirty(self):
        return self._compute_manifest()["files"] != self._read_manifest()["files"]

    def save(self):
        os.makedirs(self.index_dir, exist_ok=True)
        state = {
            "format_version": FORMAT_VERSION,
            "docs": [{"filename": d.filename, "doc_id": d.doc_id, "title": d.title,
                      "section": d.section, "source": d.source, "authors": d.authors,
                      "year": d.year, "tags": d.tags, "source_type": d.source_type,
                      "body": d.body, "char_count": d.char_count} for d in self.docs],
            "title_tokenized": self._title_tokenized,
            "body_tokenized": self._body_tokenized,
        }
        with open(os.path.join(self.index_dir, "bm25_index.pkl"), "wb") as fh:
            pickle.dump(state, fh)
        with open(os.path.join(self.index_dir, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(self._compute_manifest(), fh, ensure_ascii=False, indent=2)

    def load(self):
        pkl_path = os.path.join(self.index_dir, "bm25_index.pkl")
        if not os.path.isfile(pkl_path):
            return False
        if self.is_dirty():
            return False
        with open(pkl_path, "rb") as fh:
            state = pickle.load(fh)
        if state.get("format_version") != FORMAT_VERSION:
            return False
        self.docs = [DocEntry(**d) for d in state["docs"]]
        self._title_tokenized = state["title_tokenized"]
        self._body_tokenized = state["body_tokenized"]
        if self.docs:
            self.bm25_title = _BM25OkapiFixed(self._title_tokenized)
            self.bm25_body = _BM25OkapiFixed(self._body_tokenized)
        else:
            self.bm25_title = None
            self.bm25_body = None
        return True

    def ensure_index(self):
        if not os.path.isdir(self.docs_dir):
            raise FileNotFoundError(f"文档目录不存在: {self.docs_dir}")
        if not self.load():
            self.build_index()
            self.save()
