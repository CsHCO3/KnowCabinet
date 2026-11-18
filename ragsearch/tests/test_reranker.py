from ragsearch.engine.document import SearchResult
from ragsearch.engine.reranker import Reranker


def _res(score, rank):
    return SearchResult(filename=f"f{rank}.md", doc_id=f"d{rank}", title=f"t{rank}",
                        snippet="s", score=score, rank=rank)


def test_pass_through_preserves_order_and_renumbers():
    results = [_res(5.0, 1), _res(3.0, 2), _res(1.0, 3)]
    out = Reranker("pass-through").rerank("q", results, top_k=2)
    assert [r.filename for r in out] == ["f1.md", "f2.md"]
    assert [r.rank for r in out] == [1, 2]


def test_empty_returns_empty():
    assert Reranker("pass-through").rerank("q", [], top_k=5) == []


def test_unknown_backend_falls_back_to_pass_through():
    out = Reranker("nope").rerank("q", [_res(1.0, 1)], top_k=1)
    assert len(out) == 1


def _res_named(filename, title, score, rank):
    return SearchResult(filename=filename, doc_id="d", title=title, snippet="",
                        score=score, rank=rank)


def test_cosine_reranks_by_similarity_with_injected_embed():
    # embed_fn 注入假向量：query=[1,0,0]，doc3 与 query 夹角最小 → 余弦最高
    def fake_embed(texts):
        import numpy as np
        return np.array([
            [1.0, 0.0, 0.0],   # query
            [0.8, 0.6, 0.0],   # a.md
            [0.2, 0.98, 0.0],  # b.md
            [0.95, 0.31, 0.0], # c.md → 最相似
        ])

    results = [_res_named("a.md", "A", 5.0, 1),
               _res_named("b.md", "B", 3.0, 2),
               _res_named("c.md", "C", 1.0, 3)]
    out = Reranker("cosine", embed_fn=fake_embed).rerank("q", results, top_k=3)
    assert [r.filename for r in out] == ["c.md", "a.md", "b.md"]
    assert out[0].score > out[1].score > out[2].score
    assert [r.rank for r in out] == [1, 2, 3]


def test_cosine_empty_returns_empty():
    assert Reranker("cosine").rerank("q", [], top_k=5) == []
