"""检索结果去重：前 N 字指纹。

多通道/混合检索（BM25 + 向量、跨库）可能返回同一文档的不同 chunk 或重复命中。
策略（借鉴 lzf）：以 title 前 N 字（无 title 则 snippet 前 N 字）为指纹 key，
结果按分数降序到达时，保留首个出现的、跳过重复。
"""


def dedup_results(results, fingerprint_chars: int = 50):
    """对已按分数降序的 results 去重，返回新的去重后列表。"""
    if not results:
        return results
    seen = set()
    out = []
    for r in results:
        base = (r.title or r.snippet or "").strip()
        key = base[:fingerprint_chars]
        if not key:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out
