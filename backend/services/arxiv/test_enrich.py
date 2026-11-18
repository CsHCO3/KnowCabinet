#!/usr/bin/env python3
"""OpenAlex enrich 模块测试。

用法:
    python testfuncs/test_arxiv_enrich.py
"""
import sys
import time
from pathlib import Path

# ── 路径引导 ──
_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from backend.services.arxiv.search import ArxivSearchParams, search_arxiv
from backend.services.arxiv.enrich import enrich_paper, batch_enrich


def test_single_papers():
    """测试已知论文的 enrich 功能（验证匹配准确性）。"""
    print("=" * 60)
    print("测试 1: 单篇 enrich（已知论文）")
    print("=" * 60)

    from backend.services.arxiv.search import ArxivPaper
    test_cases = [
        ("1706.03762", "Attention is all you need",
         ["Ashish Vaswani", "Noam Shazeer"], "2017"),
        ("1512.03385", "Deep residual learning for image recognition",
         ["Kaiming He", "Xiangyu Zhang"], "2015"),
        ("1810.04805", "BERT: pre-training of deep bidirectional transformers",
         ["Jacob Devlin", "Ming-Wei Chang"], "2018"),
        ("2005.11401", "Rethinking attention with performers",
         ["Krzysztof Choromanski"], "2020"),
        ("2106.09685", "LoRA: low-rank adaptation of large language models",
         ["Edward Hu"], "2021"),
        ("2203.02155", "PaLM: scaling language modeling with pathways",
         ["Aakanksha Chowdhery"], "2022"),
    ]

    passed = 0
    for arxiv_id, title, authors, year_str in test_cases:
        paper = ArxivPaper(
            arxiv_id=arxiv_id, title=title, authors=authors,
            published=year_str,
        )
        try:
            r = enrich_paper(paper)
            status = r["enrich_status"]
            cit = r["cited_by_count"]
            venue = r["venue"][:30] if r["venue"] else "(no venue)"
            if status in ("exact_arxiv_link", "author_year_match"):
                flag = "PASS"
                passed += 1
            else:
                flag = "WARN"
            print(f"  [{flag}] {arxiv_id} | {status:20s} | cit:{cit:>6d} | {venue}")
        except Exception as e:
            print(f"  [FAIL] {arxiv_id} | exception: {e}")
        time.sleep(0.5)

    print(f"  单篇测试: {passed}/{len(test_cases)} 通过\n")
    return passed


def test_batch_enrich():
    """测试批量 enrich。"""
    print("=" * 60)
    print("测试 2: 批量 enrich")
    print("=" * 60)

    from backend.services.arxiv.search import ArxivPaper
    papers = [
        ArxivPaper(arxiv_id="1706.03762", title="Attention is all you need",
                   authors=["Ashish Vaswani"], published="2017"),
        ArxivPaper(arxiv_id="1512.03385", title="Deep residual learning",
                   authors=["Kaiming He"], published="2015"),
        ArxivPaper(arxiv_id="1810.04805", title="BERT pre-training",
                   authors=["Jacob Devlin"], published="2018"),
    ]

    results = batch_enrich(papers, workers=3, show_progress=True)
    enriched = [r for r in results if r and r["enrich_status"] not in ("no_match", "error")]
    print(f"  批量: {len(enriched)}/{len(papers)} 成功丰富")
    for p, r in zip(papers, results):
        if r:
            print(f"    {p.arxiv_id}: {r['enrich_status']} cit={r['cited_by_count']}")
    print()
    return len(enriched)


def test_search_then_enrich():
    """测试真实场景: arXiv 搜索 → OpenAlex enrich。"""
    print("=" * 60)
    print("测试 3: 真实搜索 → enrich（小规模）")
    print("=" * 60)

    # 搜索一小批 arXiv 论文
    params = ArxivSearchParams(
        raw_query='all:"large language model" AND (cat:cs.CL OR cat:cs.AI)',
        max_results=10,
        sort_by="submittedDate",
        sort_order="descending",
    )
    print("  搜索 arXiv ...")
    papers = search_arxiv(params)
    print(f"  找到 {len(papers)} 篇论文")

    # 逐个 enrich
    print("  OpenAlex enrich ...")
    for i, p in enumerate(papers):
        r = enrich_paper(p)
        status = r["enrich_status"]
        cit = r["cited_by_count"]
        venue = r["venue"][:25] if r["venue"] else "-"
        print(f"  [{i+1:2d}] {p.arxiv_id:<12s} {status:<20s} cit:{cit:>5d}  {venue}")
        time.sleep(0.3)

    print()
    return len(papers)


if __name__ == "__main__":
    total = 0
    total += test_single_papers()
    total += test_batch_enrich()
    total += test_search_then_enrich()
    print(f"\n所有测试完成。共 {total} 个 enrich 调用。")
