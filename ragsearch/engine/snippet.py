"""snippet 生成：查询词命中聚类 + 高亮。"""
import re


def make_snippet(body: str, query_tokens: list, context: int = 120,
                 max_len: int = 300, highlight: bool = True) -> str:
    """找所有命中词位置 → 聚类 → 取命中最多簇 → 上下文窗口 → 高亮。"""
    if not body:
        return ""
    lower = body.lower()
    hits = []  # [(pos, token)]
    seen = set()
    for token in query_tokens:
        if not token or token in seen:
            continue
        seen.add(token)
        start = 0
        while True:
            idx = lower.find(token.lower(), start)
            if idx == -1:
                break
            hits.append((idx, token))
            start = idx + len(token)

    if not hits:
        snippet = body[:max_len].replace("\n", " ").strip()
        return snippet + ("…" if len(body) > max_len else "")

    # 聚类：命中点相距 <= context 则并簇
    hits.sort()
    clusters = []
    for pos, tok in hits:
        if clusters and pos - clusters[-1]["end"] <= context:
            clusters[-1]["hits"].append((pos, tok))
            clusters[-1]["end"] = max(clusters[-1]["end"], pos + len(tok))
        else:
            clusters.append({"start": pos, "end": pos + len(tok), "hits": [(pos, tok)]})

    best = max(clusters, key=lambda c: len(c["hits"]))
    start = max(0, best["start"] - context)
    end = min(len(body), best["end"] + context)
    snippet = body[start:end].replace("\n", " ").strip()
    if start > 0:
        snippet = "…" + snippet
    if end < len(body):
        snippet = snippet + "…"

    # 多簇拼接：若存在与主窗口不重叠的第二簇且拼接后不超过 max_len，则用"…"拼接
    other_clusters = [
        c for c in clusters
        if c is not best and len(c["hits"]) > 0
    ]
    if other_clusters:
        non_overlap = []
        for c in other_clusters:
            c_s = max(0, c["start"] - context)
            c_e = min(len(body), c["end"] + context)
            if c_e <= start or c_s >= end:
                non_overlap.append((c, c_s, c_e))
        if non_overlap:
            second, s2_s, s2_e = max(non_overlap, key=lambda x: len(x[0]["hits"]))
            s2 = body[s2_s:s2_e].replace("\n", " ").strip()
            if s2_s > 0:
                s2 = "…" + s2
            if s2_e < len(body):
                s2 = s2 + "…"
            # 去掉主片段末尾的 … 再用 … 连接
            if snippet.endswith("…"):
                candidate = snippet[:-1] + "…" + s2
            else:
                candidate = snippet + "…" + s2
            if len(candidate) <= max_len:
                snippet = candidate

    if len(snippet) > max_len:
        snippet = snippet[:max_len] + "…"

    if highlight:
        tokens = sorted({t for _, t in hits}, key=len, reverse=True)
        for token in tokens:
            snippet = re.sub(re.escape(token), f"**{token}**", snippet, flags=re.IGNORECASE)
    return snippet
