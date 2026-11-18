"""重排抽象。

Phase 0.1 pass-through — 直接返回 BM25 分数。
Phase 0.2 cosine      — bge-m3 计算 query 与 doc 的余弦相似度重排（默认本地 FlagEmbedding）。
升级时只改 _rerank_impl，对外接口不变。
"""
import logging

import numpy as np

logger = logging.getLogger(__name__)


class Reranker:
    def __init__(self, backend: str = "pass-through", embed_fn=None):
        self.backend = backend
        self.embed_fn = embed_fn  # 注入用（测试）；None 时用默认 bge-m3 embed_dense

    def rerank(self, query, results, top_k=10):
        if not results:
            return []
        reranked = self._rerank_impl(query, results)
        for i, r in enumerate(reranked, start=1):
            r.rank = i
        return reranked[:top_k]

    def _rerank_impl(self, query, results):
        if self.backend == "pass-through":
            return results
        if self.backend == "cosine":
            return self._rerank_cosine(query, results)
        logger.warning(f"未知 backend '{self.backend}'，使用 pass-through")
        return results

    # ── Phase 0.2: cosine ─────────────────────────────────

    def _default_embed(self):
        from ragsearch.engine.bge_embed import embed_dense
        return embed_dense

    def _rerank_cosine(self, query, results):
        """用嵌入向量余弦相似度重排，替换 BM25 分数。"""
        if not results:
            return []
        embed = self.embed_fn or self._default_embed()
        # 用 title + snippet 代表文档，控制 token 成本
        texts = [query] + [f"{r.title}\n{r.snippet}" for r in results]
        vecs = embed(texts)  # (N, dim)
        q = vecs[0]
        docs = vecs[1:]
        q_norm = q / (np.linalg.norm(q) + 1e-9)
        docs_norm = docs / (np.linalg.norm(docs, axis=1, keepdims=True) + 1e-9)
        scores = docs_norm @ q_norm
        order = np.argsort(-scores)
        reranked = []
        for idx in order:
            r = results[idx]
            r.score = round(float(scores[idx]), 4)
            reranked.append(r)
        return reranked
