from ragsearch.engine.snippet import make_snippet


def test_single_hit_highlight():
    body = "今天讨论向量数据库设计。" + "填充内容。" * 20
    snip = make_snippet(body, ["向量数据库"])
    assert "**向量数据库**" in snip


def test_multi_cluster_ellipsis():
    body = ("无关开头填充。" * 5 + "目标词第一次出现。" + "中间填充内容。" * 20
            + "目标词第二次出现。" + "结尾填充内容。" * 20)
    snip = make_snippet(body, ["目标词"], context=30)
    assert "…" in snip  # 两簇相距远，有省略号


def test_no_hit_fallback():
    body = "完全无关的内容。" * 10
    snip = make_snippet(body, ["数据库"])
    assert snip.startswith("完全无关")


def test_empty_tokens_returns_prefix():
    body = "abcdefghij"
    assert make_snippet(body, []) == body


def test_two_far_clusters_concatenated():
    """两个相距很远的命中簇应被拼接在一个片段中，各自出现高亮词。"""
    body = ("无关开头。" * 10
            + "向量数据库出现。"
            + "中间填充。" * 50
            + "RAG出现。"
            + "无关结尾。" * 10)
    snip = make_snippet(body, ["向量数据库", "RAG"], context=30, max_len=400)
    assert "**向量数据库**" in snip
    assert "**RAG**" in snip
    assert "…" in snip
