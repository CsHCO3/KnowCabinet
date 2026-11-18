"""P0 冒烟测试：模块导入 + 多源检索 + 去重合并 + 字段完整。

用法:
    python tests/smoke_test.py [keyword]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import rank, search
from record import PaperRecord, normalize_doi, normalize_title
from sources import arxiv, crossref, openalex, semantic_scholar
from venue_score import get_ccf_level, get_ccf_score


def test_normalize():
    assert normalize_doi("https://doi.org/10.1109/JIOT.2022.3160737") == "10.1109/jiot.2022.3160737"
    assert normalize_doi(" 10.1109/x.Y ") == "10.1109/x.y"
    assert normalize_title("Attention Is All You Need!") == "attentionisallyouneed"
    print("[OK] 归一化")


def test_ccf():
    assert get_ccf_level("Advances in Neural Information Processing Systems") == "A"
    assert get_ccf_level("NeurIPS 2023") == "A"
    assert get_ccf_level("IEEE Internet of Things Journal") == ""
    assert get_ccf_score("ICML") == 100
    print("[OK] CCF 匹配")


def test_crossref_single():
    r = crossref.resolve_doi("10.1109/JIOT.2022.3160737")
    assert r and r["title"], "crossref resolve_doi 未找到"
    assert r["venue"], "crossref venue 为空"
    assert r["citations"] > 0, "crossref 引用数为0"
    print(f"[OK] crossref DOI 单查: {r['title'][:40]} ｜ venue={r['venue'][:30]} ｜ cit={r['citations']}")


def test_openalex_single():
    r = openalex.resolve_doi("10.1109/JIOT.2022.3160737")
    assert r and r["title"], "openalex resolve_doi 未找到"
    print(f"[OK] openalex DOI 单查: {r['title'][:40]} ｜ is_oa={r['is_oa']} ｜ cit={r['citations']}")


def test_arxiv():
    print("\n=== arXiv 检索（best-effort） ===")
    recs = arxiv.search_records("attention is all you need", max_results=5)
    if not recs:
        print("  [warn] arXiv 返回空（可能被限流，属预期降权）")
        return
    print(f"  返回 {len(recs)} 条，首条: {recs[0]['title'][:50]} ｜ oa_url={recs[0]['oa_url'][:50]}")
    assert recs[0]["oa_url"].endswith(".pdf") or "/pdf/" in recs[0]["oa_url"]
    print("[OK] arXiv 检索（直连+降权）")


def test_s2():
    print("\n=== S2 DOI 丰富 ===")
    r = semantic_scholar.resolve_doi("10.1109/JIOT.2022.3160737")
    assert r and r["title"], "S2 resolve_doi 未找到"
    print(f"  title={r['title'][:45]} ｜ cit={r['citations']} ｜ oa={bool(r['oa_url'])} ｜ tldr={r['tldr'][:40]}")
    print("[OK] S2 DOI 丰富")


def test_download():
    print("\n=== PDF 下载三态 ===")
    from download import download_paper_pdf
    from record import PaperRecord
    import tempfile, os

    # 一篇确定有开放版的：arxiv 预印本（arXiv PDF 直连）
    rec = PaperRecord(title="Attention is all you need", arxiv_id="1706.03762",
                      oa_url="https://arxiv.org/pdf/1706.03762", is_oa=True, doi="")
    outdir = tempfile.mkdtemp(prefix="p1_dl_")
    status, info = download_paper_pdf(rec, outdir)
    print(f"  arXiv 开放版: {status} -> {os.path.basename(info) if info else ''}")
    assert status == "downloaded", "arXiv 开放版应能直连下载"

    # 付费墙样例：IEEE IoT 2022 无开放版
    rec2 = PaperRecord(title="Identification and Resolution for Industrial Internet",
                       doi="10.1109/JIOT.2022.3160737", is_oa=False)
    status2, _ = download_paper_pdf(rec2, outdir)
    print(f"  付费墙样例: {status2}")
    assert status2 == "paywall", "无开放链接应标记 paywall"
    print("[OK] PDF 下载三态")


def test_pipeline(keyword: str):
    print(f"\n=== pipeline.search('{keyword}') ===")
    recs = search(keyword, rows=20, enrich_s2=True)
    print(f"合并去重后: {len(recs)} 条")
    assert len(recs) >= 5, "结果太少"

    recs = rank(recs)
    has_venue = sum(1 for r in recs if r.venue)
    has_cit = sum(1 for r in recs if r.citations > 0)
    has_doi = sum(1 for r in recs if r.doi)
    has_tldr = sum(1 for r in recs if r.tldr)
    print(f"  venue 非空: {has_venue}/{len(recs)} ｜ 引用>0: {has_cit}/{len(recs)} ｜ DOI: {has_doi}/{len(recs)} ｜ tldr: {has_tldr}/{len(recs)}")
    for r in recs[:5]:
        print(f"  [{r.year or '?'}] {r.title[:50]} ｜ {r.venue[:28] or '(no venue)'} ｜ cit={r.citations} ｜ rank={r.rank_score}")
    assert has_cit >= len(recs) // 2, "引用量覆盖不足"
    print("[OK] pipeline 检索+合并+排序+丰富")


if __name__ == "__main__":
    test_normalize()
    test_ccf()
    test_crossref_single()
    test_openalex_single()
    test_arxiv()
    test_s2()
    test_download()
    test_pipeline(sys.argv[1] if len(sys.argv) > 1 else "industrial internet identification")
    print("\n=== 冒烟测试全部通过 ===")
