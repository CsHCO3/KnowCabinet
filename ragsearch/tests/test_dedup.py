from ragsearch.engine.dedup import dedup_results
from ragsearch.engine.document import SearchResult


def _res(title, snippet, score, rank):
    return SearchResult(filename="x.md", doc_id="d", title=title, snippet=snippet,
                        score=score, rank=rank)


def test_dedups_by_title_prefix():
    # 两个超长标题共享前 50 字 → 指纹相同 → 保留高分那个
    long1 = "数据库介绍" * 20       # 100 字
    long2 = "数据库介绍" * 20 + "详细版"
    results = [
        _res(long1, "s", 0.9, 1),
        _res(long2, "s", 0.8, 2),
        _res("不同文档", "s", 0.7, 3),
    ]
    out = dedup_results(results)
    assert len(out) == 2
    assert out[0].score == 0.9
    assert out[1].title == "不同文档"


def test_short_but_different_titles_not_deduped():
    results = [_res("向量数据库介绍", "s", 0.9, 1), _res("向量数据库介绍详细版", "s", 0.8, 2)]
    assert len(dedup_results(results)) == 2


def test_dedups_by_snippet_when_no_title():
    body = "这是一段很长很长的内容" * 10
    results = [
        _res("", body, 0.9, 1),
        _res("", body + "后面不同", 0.8, 2),
    ]
    out = dedup_results(results)
    assert len(out) == 1


def test_keeps_distinct_titles():
    results = [_res("A", "s", 0.9, 1), _res("B", "s", 0.8, 2)]
    out = dedup_results(results)
    assert len(out) == 2


def test_empty():
    assert dedup_results([]) == []
