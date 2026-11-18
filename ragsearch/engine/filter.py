"""按 frontmatter 字段过滤检索结果（P0.3）。

在 BM25 候选之后应用：section 子串匹配、tags 任一命中、year 精确、authors 子串匹配。
空过滤条件即不约束；匹配均大小写不敏感。
"""


def filter_results(results, section=None, tags=None, year=None, authors=None, source_type=None):
    """对 SearchResult 列表过滤，返回新列表。所有条件默认放行。

    section/authors 子串、tags 任一命中、year/source_type 精确（均大小写不敏感）。
    """
    if not results:
        return results
    tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()] if tags else []

    def _tags_hit(r):
        doc_tags = {t.lower() for t in r.tags}
        return any(t.lower() in doc_tags for t in tag_list)

    out = []
    for r in results:
        if section and section.lower() not in (r.section or "").lower():
            continue
        if tag_list and not _tags_hit(r):
            continue
        if year and (r.year or "") != str(year):
            continue
        if authors and authors.lower() not in (r.authors or "").lower():
            continue
        if source_type and source_type.lower() != (r.source_type or "").lower():
            continue
        out.append(r)
    return out


def group_results(results):
    """按 doc_id 分组折叠（P1.4）。

    同论文多个版本（paper.md / cn_paper.md / condensation.md）折叠为一条：
    [{doc_id, title, score(组内最高), versions: [SearchResult]}]
    """
    groups = {}
    for r in results:
        if r.doc_id not in groups:
            groups[r.doc_id] = {"doc_id": r.doc_id, "title": r.title,
                                "score": r.score, "versions": []}
        g = groups[r.doc_id]
        g["versions"].append(r)
        g["score"] = max(g["score"], r.score)
    return list(groups.values())
