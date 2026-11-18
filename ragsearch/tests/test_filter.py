from ragsearch.engine.document import SearchResult
from ragsearch.engine.filter import filter_results, group_results


def _res(doc_id, filename, title, score, rank, section="", tags=None, year="", authors="",
         source_type=""):
    return SearchResult(filename=filename, doc_id=doc_id, title=title, snippet="s",
                        score=score, rank=rank, section=section, tags=tags or [],
                        year=year, authors=authors, source_type=source_type)


def test_filter_by_section():
    results = [_res("d1", "a.md", "A", 0.9, 1, section="区块链"),
               _res("d2", "b.md", "B", 0.8, 2, section="共识")]
    out = filter_results(results, section="区块链")
    assert [r.filename for r in out] == ["a.md"]


def test_filter_by_tags_any_match():
    results = [_res("d1", "a.md", "A", 0.9, 1, tags=["共识", "轻量级"]),
               _res("d2", "b.md", "B", 0.8, 2, tags=["数据库"])]
    out = filter_results(results, tags="共识")
    assert [r.filename for r in out] == ["a.md"]
    out2 = filter_results(results, tags="轻量级, 其它")
    assert [r.filename for r in out2] == ["a.md"]


def test_filter_by_year_exact():
    results = [_res("d1", "a.md", "A", 0.9, 1, year="2022"),
               _res("d2", "b.md", "B", 0.8, 2, year="2023")]
    assert [r.filename for r in filter_results(results, year="2022")] == ["a.md"]


def test_filter_by_authors_substring():
    results = [_res("d1", "a.md", "A", 0.9, 1, authors="张三, 李四"),
               _res("d2", "b.md", "B", 0.8, 2, authors="王五")]
    assert [r.filename for r in filter_results(results, authors="张三")] == ["a.md"]


def test_filter_by_source_type_exact():
    results = [_res("d1", "a.md", "A", 0.9, 1, source_type="paper"),
               _res("d2", "b.md", "B", 0.8, 2, source_type="web")]
    assert [r.filename for r in filter_results(results, source_type="paper")] == ["a.md"]
    assert [r.filename for r in filter_results(results, source_type="WEB")] == ["b.md"]
    assert len(filter_results(results, source_type="docs")) == 0


def test_filter_no_conditions_passes_all():
    results = [_res("d1", "a.md", "A", 0.9, 1), _res("d2", "b.md", "B", 0.8, 2)]
    assert len(filter_results(results)) == 2


def test_group_by_doc_collapses_versions():
    results = [
        _res("2210.04644", "2210.04644/paper.md", "论文", 0.9, 1),
        _res("2210.04644", "2210.04644/cn_paper.md", "论文（中）", 0.7, 2),
        _res("9999.99999", "9999.99999/paper.md", "另一篇", 0.8, 3),
    ]
    groups = group_results(results)
    assert len(groups) == 2
    g = groups[0]
    assert g["doc_id"] == "2210.04644"
    assert g["score"] == 0.9
    assert len(g["versions"]) == 2


def test_group_empty():
    assert group_results([]) == []
