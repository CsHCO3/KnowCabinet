import pytest
from ragsearch.engine.document import DocEntry, SearchResult, parse_frontmatter, extract_title
from ragsearch.engine.indexer import DocIndexer


def _write(tmp_path, name, content):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


BODY = "数据库 数据库 介绍 用途 说明 内容 文章 语义 检索 示例 详细 说明。\n" * 3


def test_build_and_title_weighting(tmp_path):
    # a.md 标题含查询词，b.md 正文与 a 完全相同但标题不含 —— 排名差异只来自标题
    _write(tmp_path, "a.md", "# 数据库\n\n" + BODY)
    _write(tmp_path, "b.md", "# 随便\n\n" + BODY)
    idx = DocIndexer(docs_dir=str(tmp_path), index_dir=str(tmp_path / "idx"))
    assert idx.build_index() == 2
    results = idx.search("数据库", top_k=5)
    assert len(results) == 2
    assert results[0].filename == "a.md"
    assert results[1].filename == "b.md"


def test_no_match_returns_empty(tmp_path):
    _write(tmp_path, "a.md", "# 数据库\n\n" + BODY)
    idx = DocIndexer(docs_dir=str(tmp_path), index_dir=str(tmp_path / "idx"))
    idx.build_index()
    assert idx.search("不存在的词xyz", top_k=5) == []


def test_small_kb_idf_not_zero(tmp_path):
    # 修复 rank_bm25：词出现在恰好一半文档时 idf=0 → 所有分数归零（小库/临时文件夹致命）
    _write(tmp_path, "a.md", "# FastAPI\n\nFastAPI 是 Web 框架。\n")
    _write(tmp_path, "b.md", "# 依赖注入\n\n依赖注入是设计模式。\n")
    idx = DocIndexer(docs_dir=str(tmp_path), index_dir=str(tmp_path / "idx"))
    idx.build_index()
    results = idx.search("FastAPI", top_k=5)
    assert len(results) == 1
    assert results[0].filename == "a.md"
    assert results[0].score > 0


def test_empty_dir_build_and_search(tmp_path):
    # 空知识库（如清空待导入的视频 KB）：构建/搜索/持久化/重载都不该崩
    empty = tmp_path / "empty"
    empty.mkdir()
    idx = DocIndexer(docs_dir=str(empty), index_dir=str(tmp_path / "idx"))
    assert idx.build_index() == 0
    assert idx.search("任何", top_k=5) == []
    idx.save()
    idx2 = DocIndexer(docs_dir=str(empty), index_dir=str(tmp_path / "idx"))
    idx2.ensure_index()
    assert idx2.search("任何", top_k=5) == []


def test_save_load_roundtrip(tmp_path):
    _write(tmp_path, "a.md", "# 数据库\n\n" + BODY)
    idx = DocIndexer(docs_dir=str(tmp_path), index_dir=str(tmp_path / "idx"))
    idx.build_index()
    idx.save()
    idx2 = DocIndexer(docs_dir=str(tmp_path), index_dir=str(tmp_path / "idx"))
    assert idx2.load() is True
    assert len(idx2.docs) == 1
    assert len(idx2.search("数据库", top_k=5)) == 1


def test_old_format_forces_rebuild(tmp_path):
    _write(tmp_path, "a.md", "# 数据库\n\n" + BODY)
    idx = DocIndexer(docs_dir=str(tmp_path), index_dir=str(tmp_path / "idx"))
    idx.build_index()
    idx.save()
    # 篡改 format_version 模拟旧索引
    import pickle
    import os
    with open(os.path.join(str(tmp_path), "idx", "bm25_index.pkl"), "rb") as fh:
        state = pickle.load(fh)
    state["format_version"] = 1
    with open(os.path.join(str(tmp_path), "idx", "bm25_index.pkl"), "wb") as fh:
        pickle.dump(state, fh)
    idx2 = DocIndexer(docs_dir=str(tmp_path), index_dir=str(tmp_path / "idx"))
    assert idx2.load() is False  # 旧格式 → 需重建


def test_manifest_dirty_detects_change(tmp_path):
    _write(tmp_path, "a.md", "# 数据库\n\n" + BODY)
    idx = DocIndexer(docs_dir=str(tmp_path), index_dir=str(tmp_path / "idx"))
    idx.build_index()
    idx.save()
    _write(tmp_path, "b.md", "# 新文件\n内容")
    idx2 = DocIndexer(docs_dir=str(tmp_path), index_dir=str(tmp_path / "idx"))
    assert idx2.load() is False  # 有变更 → 需重建


def test_results_carry_frontmatter_and_doc_id(tmp_path):
    _write(tmp_path, "2210.04644/paper.md", """---
title: 区块链共识
source: https://arxiv.org/abs/2210.04644
authors: 张三, 李四
year: 2022
section: 区块链
tags: [共识, 轻量级]
---

# 区块链共识

共识算法 介绍 区块链 论文 内容。""")
    idx = DocIndexer(docs_dir=str(tmp_path), index_dir=str(tmp_path / "idx"))
    idx.build_index()
    results = idx.search("共识", top_k=5)
    r = results[0]
    assert r.doc_id == "2210.04644"
    assert r.filename == "2210.04644/paper.md"
    assert r.year == "2022"
    assert r.tags == ["共识", "轻量级"]
    assert r.authors == "张三, 李四"
    assert r.section == "区块链"


def test_include_exclude_glob(tmp_path):
    _write(tmp_path, "a/paper.md", "# A\n\n共识 内容。")
    _write(tmp_path, "b/cn_paper.md", "# B\n\n共识 内容。")
    _write(tmp_path, "c/condensation.md", "# C\n\n共识 内容。")
    # 黑名单
    idx = DocIndexer(docs_dir=str(tmp_path), index_dir=str(tmp_path / "idx"),
                     exclude=["a/*"])
    assert idx.build_index() == 2
    assert sorted(d.filename for d in idx.docs) == ["b/cn_paper.md", "c/condensation.md"]
    # 白名单
    idx2 = DocIndexer(docs_dir=str(tmp_path), index_dir=str(tmp_path / "idx"),
                      include=["c/*"])
    assert idx2.build_index() == 1
    assert idx2.docs[0].filename == "c/condensation.md"


def test_tag_boost(tmp_path):
    body = "共识 介绍 内容 说明 文章 讨论。\n" * 3
    _write(tmp_path, "a.md", "# 共识\n\n" + body)   # 标题命中
    _write(tmp_path, "b.md", """---
tags: [共识]
---

# 其它

""" + body)  # 命中 tags（正文同 a）
    idx = DocIndexer(docs_dir=str(tmp_path), index_dir=str(tmp_path / "idx"), tag_boost=0.5)
    idx.build_index()
    results = idx.search("共识", top_k=5)
    assert results[0].filename == "b.md"  # tag_boost 把命中 tags 的顶上去
