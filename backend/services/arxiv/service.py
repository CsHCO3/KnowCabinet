"""arXiv 服务 — 搜索与下载。"""

from typing import Any

from backend.services.arxiv.search import (
    search_arxiv,
    download_paper_from_source,
    ArxivSearchParams,
    filter_recent_papers,
)


class ArxivService:
    def search(
        self,
        query: str = "",
        raw_query: str = "",
        title: str = "",
        author: str = "",
        category: str = "",
        abstract: str = "",
        date_from: str = "",
        date_to: str = "",
        start: int = 0,
        max_results: int = 10,
        sort_by: str = "relevance",
        sort_order: str = "descending",
        days_back: int = 0,
    ) -> list[dict[str, Any]]:
        params = ArxivSearchParams(
            raw_query=raw_query,
            query=query,
            title=title,
            author=author,
            category=category,
            abstract=abstract,
            date_from=date_from,
            date_to=date_to,
            start=start,
            max_results=max_results,
            sort_by=sort_by,
            sort_order=sort_order,
        )
        papers = search_arxiv(params)
        if days_back > 0:
            papers = filter_recent_papers(papers, days_back)
        return [p.to_dict() for p in papers]

    def download(self, source: str, output_dir: str, title_hint: str = "") -> str:
        return download_paper_from_source(source, output_dir, title_hint=title_hint)
