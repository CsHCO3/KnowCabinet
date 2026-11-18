"""arXiv search utility.

This module provides a small, dependency-free wrapper around the arXiv API.
It can be used as a library or as a command-line tool.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import time
import textwrap
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Iterable


ARXIV_API_URL = "https://export.arxiv.org/api/query"
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
URL_PATTERN = re.compile(r"https?://[^\s)>\]]+")
ARXIV_MIN_REQUEST_INTERVAL_SECONDS = 3.0
_LAST_ARXIV_REQUEST_MONOTONIC = 0.0

# Common acceptance signals seen in arXiv journal_ref/comment fields.
ACCEPTANCE_KEYWORDS = (
    "accepted",
    "to appear",
    "proceedings",
    "published in",
    "published at",
    "neurips",
    "icml",
    "iclr",
    "acl",
    "emnlp",
    "cvpr",
    "eccv",
    "iccv",
    "aaai",
    "kdd",
    "nature",
    "science",
)

NEGATIVE_ACCEPTANCE_KEYWORDS = (
    "under review",
    "submitted to",
    "submission",
    "preprint",
)

CODE_DATA_HOST_KEYWORDS = ("github.com", "gitlab.com", "huggingface.co", "kaggle.com", "zenodo.org")
CODE_DATA_TEXT_KEYWORDS = ("code", "github", "repository", "repo", "dataset", "data")


@dataclass
class ArxivSearchParams:
    """Search conditions for arXiv."""

    raw_query: str = ""
    query: str = ""
    title: str = ""
    author: str = ""
    category: str = ""
    abstract: str = ""
    date_from: str = ""   # YYYYMMDD, 例如 20240101
    date_to: str = ""     # YYYYMMDD, 例如 20240630
    start: int = 0
    max_results: int = 10
    sort_by: str = "relevance"
    sort_order: str = "descending"

    def build_search_query(self) -> str:
        """Convert the search conditions into an arXiv query string."""

        if self.raw_query.strip():
            return self.raw_query.strip()

        parts: list[str] = []
        if self.query.strip():
            parts.append(f'all:"{self.query.strip()}"')
        if self.title.strip():
            parts.append(f'ti:"{self.title.strip()}"')
        if self.author.strip():
            parts.append(f'au:"{self.author.strip()}"')
        if self.category.strip():
            parts.append(f'cat:{self.category.strip()}')
        if self.abstract.strip():
            parts.append(f'abs:"{self.abstract.strip()}"')

        if self.date_from or self.date_to:
            df = self.date_from or "00000101"
            dt = self.date_to or "99991231"
            parts.append(f'submittedDate:[{df} TO {dt}]')

        if not parts:
            raise ValueError("At least one search condition must be provided.")

        return " AND ".join(parts)


@dataclass
class ArxivPaper:
    """Normalized arXiv result item."""

    arxiv_id: str
    title: str
    authors: list[str] = field(default_factory=list)
    summary: str = ""
    published: str = ""
    updated: str = ""
    primary_category: str = ""
    categories: list[str] = field(default_factory=list)
    entry_url: str = ""
    pdf_url: str = ""
    comment: str = ""
    journal_ref: str = ""
    doi: str = ""
    code_data_links: list[str] = field(default_factory=list)
    has_code_or_data: bool = False
    is_accepted: bool = False
    acceptance_note: str = ""
    page_count: int | None = None

    def to_dict(self) -> dict:
        return {
            "arxiv_id": self.arxiv_id,
            "title": self.title,
            "authors": self.authors,
            "summary": self.summary,
            "published": self.published,
            "updated": self.updated,
            "primary_category": self.primary_category,
            "categories": self.categories,
            "entry_url": self.entry_url,
            "pdf_url": self.pdf_url,
            "comment": self.comment,
            "journal_ref": self.journal_ref,
            "doi": self.doi,
            "code_data_links": self.code_data_links,
            "has_code_or_data": self.has_code_or_data,
            "is_accepted": self.is_accepted,
            "acceptance_note": self.acceptance_note,
            "page_count": self.page_count,
        }


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _extract_arxiv_id(entry_id: str) -> str:
    match = re.search(r"/abs/([^/?#]+)", entry_id)
    return match.group(1) if match else _clean_text(entry_id)


def _get_text(element: ET.Element, path: str) -> str:
    found = element.find(path, ATOM_NS)
    return _clean_text(found.text) if found is not None and found.text else ""


def _throttled_urlopen(request: urllib.request.Request | str, timeout: int = 20):
    """Open a URL while respecting arXiv's minimum request interval."""

    global _LAST_ARXIV_REQUEST_MONOTONIC

    now = time.monotonic()
    elapsed = now - _LAST_ARXIV_REQUEST_MONOTONIC
    if _LAST_ARXIV_REQUEST_MONOTONIC > 0 and elapsed < ARXIV_MIN_REQUEST_INTERVAL_SECONDS:
        time.sleep(ARXIV_MIN_REQUEST_INTERVAL_SECONDS - elapsed)

    response = urllib.request.urlopen(request, timeout=timeout)
    _LAST_ARXIV_REQUEST_MONOTONIC = time.monotonic()
    return response


def _extract_urls(text: str) -> list[str]:
    if not text:
        return []
    return [match.rstrip(".,;)") for match in URL_PATTERN.findall(text)]


def _extract_code_data_links(comment: str) -> list[str]:
    links = _extract_urls(comment)
    selected: list[str] = []
    for link in links:
        lower_link = link.lower()
        if any(keyword in lower_link for keyword in CODE_DATA_HOST_KEYWORDS) or any(
            keyword in lower_link for keyword in CODE_DATA_TEXT_KEYWORDS
        ):
            selected.append(link)
    return selected


def _infer_acceptance_signal(journal_ref: str, comment: str) -> tuple[bool, str]:
    journal_ref_clean = _clean_text(journal_ref)
    text = f"{journal_ref_clean} {_clean_text(comment)}".lower()
    if not text:
        return False, ""

    if any(keyword in text for keyword in NEGATIVE_ACCEPTANCE_KEYWORDS):
        return False, ""

    if journal_ref_clean:
        return True, journal_ref_clean

    if any(keyword in text for keyword in ACCEPTANCE_KEYWORDS):
        return True, _clean_text(comment)

    return False, ""


def _resolve_pdf_url(source: str) -> tuple[str, str]:
    """Resolve source string into (pdf_url, arxiv_id)."""

    source = _clean_text(source)
    if not source:
        raise ValueError("Paper source is empty.")

    lower_source = source.lower()
    if lower_source.startswith("http://") or lower_source.startswith("https://"):
        if "/pdf/" in source:
            arxiv_id = re.sub(r"\.pdf$", "", _extract_arxiv_id(source.replace("/pdf/", "/abs/")), flags=re.IGNORECASE)
            pdf_url = source if source.lower().endswith(".pdf") else f"{source}.pdf"
            return pdf_url, arxiv_id
        arxiv_id = _extract_arxiv_id(source)
        if arxiv_id:
            return f"https://arxiv.org/pdf/{arxiv_id}.pdf", arxiv_id
        raise ValueError("Could not resolve arXiv ID from the provided URL.")

    arxiv_id = source.replace("arXiv:", "").strip()
    if not arxiv_id:
        raise ValueError("Could not resolve arXiv ID from the provided source.")
    return f"https://arxiv.org/pdf/{arxiv_id}.pdf", arxiv_id


def estimate_pdf_page_count(pdf_bytes: bytes) -> int | None:
    """Estimate page count by counting '/Type /Page' objects in PDF bytes."""

    count = len(re.findall(rb"/Type\s*/Page\b", pdf_bytes))
    return count if count > 0 else None


def fetch_pdf_page_count(pdf_url: str, timeout: int = 30) -> int | None:
    """Download PDF and infer its page count.

    This is intentionally kept as a dedicated test helper and is not used by the
    normal search flow, because it requires downloading the full PDF.
    """

    request = urllib.request.Request(pdf_url, headers={"User-Agent": "Mozilla/5.0 (arXiv page counter)"})
    with _throttled_urlopen(request, timeout=timeout) as response:
        return estimate_pdf_page_count(response.read())


def build_materials_ai_search_params() -> ArxivSearchParams:
    """Build the preset query used for the materials science + AI search test."""

    return ArxivSearchParams(
        raw_query='all:"materials science" AND all:"artificial intelligence" AND (all:review OR all:survey OR all:perspective)',
        max_results=50,
        sort_by="submittedDate",
        sort_order="descending",
    )


def _build_request_url(params: ArxivSearchParams) -> str:
    query_string = params.build_search_query()
    query_params = {
        "search_query": query_string,
        "start": str(max(0, params.start)),
        "max_results": str(max(1, params.max_results)),
        "sortBy": params.sort_by,
        "sortOrder": params.sort_order,
    }
    return f"{ARXIV_API_URL}?{urllib.parse.urlencode(query_params)}"


def build_arxiv_web_search_url(query: str, months_back: int = 6, size: int = 50) -> str:
    """Build an arXiv website search URL with a date range filter."""

    today = dt.date.today()
    # Approximate 6 months as 183 days to keep standard-library only.
    from_date = today - dt.timedelta(days=max(1, months_back) * 30 + 3)
    query_encoded = urllib.parse.quote(query, safe="()")
    return (
        "https://arxiv.org/search/?"
        f"query={query_encoded}"
        "&searchtype=all"
        "&abstracts=show"
        "&order=-announced_date_first"
        f"&size={max(1, size)}"
        "&date-filter_by=date_range"
        f"&date-from_date={from_date.isoformat()}"
        f"&date-to_date={today.isoformat()}"
        "&date-date_type=submitted_date"
    )


def run_embedded_materials_ai_test(timeout: int = 20) -> int:
    """Run the embedded website test requested by the user."""

    test_query = '"materials science" AND "artificial intelligence" AND (review OR survey OR perspective)'
    url = build_arxiv_web_search_url(test_query, months_back=6, size=50)
    print(f"Test URL: {url}")

    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (arXiv test helper)"})
    try:
        with _throttled_urlopen(request, timeout=timeout) as response:
            print(f"HTTP status: {response.status}")
            return 0
    except Exception as error:  # pragma: no cover - network dependent
        print(f"Test failed: {error}")
        return 1


def filter_recent_papers(papers: Iterable[ArxivPaper], days_back: int) -> list[ArxivPaper]:
    """Keep papers whose published date is within the last N days."""

    if days_back <= 0:
        return list(papers)

    cutoff = dt.datetime.utcnow() - dt.timedelta(days=days_back)
    filtered: list[ArxivPaper] = []
    for paper in papers:
        try:
            published_at = dt.datetime.fromisoformat(paper.published.replace("Z", "+00:00"))
            if published_at.replace(tzinfo=None) >= cutoff:
                filtered.append(paper)
        except Exception:
            continue
    return filtered


def run_embedded_materials_ai_search_test(timeout: int = 20) -> int:
    """Run built-in API search and print paper list with abstracts."""

    params = build_materials_ai_search_params()
    try:
        papers = search_arxiv(params, timeout=timeout)
        papers = filter_recent_papers(papers, days_back=183)
    except Exception as error:  # pragma: no cover - network dependent
        print(f"Search failed: {error}")
        return 1

    print("Embedded API search test")
    print("Query: \"materials science\" AND \"artificial intelligence\" AND (review OR survey OR perspective)")
    print("Time window: last ~6 months")
    print(f"Matched papers: {len(papers)}")
    print()
    print_search_results(papers)
    return 0 if papers else 1


def get_embedded_materials_ai_papers(timeout: int = 20) -> list[ArxivPaper]:
    """Return papers for the built-in materials+AI query in the recent window."""

    params = build_materials_ai_search_params()
    papers = search_arxiv(params, timeout=timeout)
    return filter_recent_papers(papers, days_back=183)


def fetch_arxiv_feed(params: ArxivSearchParams, timeout: int = 20) -> str:
    """Fetch raw Atom XML from the arXiv API."""

    url = _build_request_url(params)
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (arXiv search helper)"})
    with _throttled_urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8")


def parse_arxiv_feed(xml_text: str) -> list[ArxivPaper]:
    """Parse arXiv Atom XML into structured paper records."""

    root = ET.fromstring(xml_text)
    papers: list[ArxivPaper] = []

    for entry in root.findall("atom:entry", ATOM_NS):
        arxiv_id = _extract_arxiv_id(_get_text(entry, "atom:id"))
        title = _clean_text(_get_text(entry, "atom:title"))
        summary = _get_text(entry, "atom:summary")
        published = _get_text(entry, "atom:published")
        updated = _get_text(entry, "atom:updated")
        comment = _get_text(entry, "arxiv:comment")
        journal_ref = _get_text(entry, "arxiv:journal_ref")
        doi = _get_text(entry, "arxiv:doi")
        primary_category = ""
        primary_category_element = entry.find("arxiv:primary_category", ATOM_NS)
        if primary_category_element is not None:
            primary_category = primary_category_element.attrib.get("term", "")

        authors = [
            _get_text(author, "atom:name")
            for author in entry.findall("atom:author", ATOM_NS)
            if _get_text(author, "atom:name")
        ]
        categories = [category.attrib.get("term", "") for category in entry.findall("atom:category", ATOM_NS)]
        categories = [category for category in categories if category]

        entry_url = ""
        pdf_url = ""
        for link in entry.findall("atom:link", ATOM_NS):
            href = link.attrib.get("href", "")
            if link.attrib.get("rel") == "alternate" and not entry_url:
                entry_url = href
            if link.attrib.get("title") == "pdf":
                pdf_url = href

        code_data_links = _extract_code_data_links(comment)
        is_accepted, acceptance_note = _infer_acceptance_signal(journal_ref, comment)

        papers.append(
            ArxivPaper(
                arxiv_id=arxiv_id,
                title=title,
                authors=authors,
                summary=summary,
                published=published,
                updated=updated,
                primary_category=primary_category,
                categories=categories,
                entry_url=entry_url,
                pdf_url=pdf_url,
                comment=comment,
                journal_ref=journal_ref,
                doi=doi,
                code_data_links=code_data_links,
                has_code_or_data=bool(code_data_links),
                is_accepted=is_accepted,
                acceptance_note=acceptance_note,
            )
        )

    return papers


def search_arxiv(params: ArxivSearchParams, timeout: int = 20) -> list[ArxivPaper]:
    """Search arXiv and return normalized results."""

    xml_text = fetch_arxiv_feed(params, timeout=timeout)
    return parse_arxiv_feed(xml_text)


def get_download_record(paper: ArxivPaper) -> dict[str, str]:
    """Return the source mapping used when downloading a paper."""

    return {
        "arxiv_id": paper.arxiv_id,
        "title": paper.title,
        "entry_url": paper.entry_url,
        "pdf_url": paper.pdf_url or f"https://arxiv.org/pdf/{paper.arxiv_id}.pdf",
    }


def format_paper(paper: ArxivPaper, index: int) -> str:
    """Format a single search result for console output."""

    author_text = ", ".join(paper.authors) if paper.authors else "Unknown"
    category_text = paper.primary_category or ", ".join(paper.categories) or "N/A"
    published_text = (paper.published or "N/A").split("T")[0]
    updated_text = (paper.updated or "N/A").split("T")[0]
    code_text = "Yes" if paper.has_code_or_data else "No"
    if paper.code_data_links:
        code_text = f"Yes ({paper.code_data_links[0]})"
    acceptance_text = "No"
    if paper.is_accepted:
        acceptance_text = f"Yes ({paper.acceptance_note})" if paper.acceptance_note else "Yes"
    page_count_text = f"{paper.page_count} pages" if paper.page_count else "N/A"
    summary_text = textwrap.fill(_clean_text(paper.summary), width=88, subsequent_indent="    ")

    lines = [
        f"{index}. {paper.title}",
        f"   arXiv ID: {paper.arxiv_id}",
        f"   Authors: {author_text}",
        f"   Published: {published_text}",
        f"   Last Updated: {updated_text}",
        f"   Category: {category_text}",
        f"   Page Count: {page_count_text}",
        f"   Accepted: {acceptance_text}",
        f"   Code/Data: {code_text}",
        f"   Entry: {paper.entry_url or 'N/A'}",
        f"   Download Source: {paper.pdf_url or f'https://arxiv.org/pdf/{paper.arxiv_id}.pdf'}",
    ]
    if paper.pdf_url:
        lines.append(f"   PDF: {paper.pdf_url}")
    if paper.journal_ref:
        lines.append(f"   Journal Ref: {paper.journal_ref}")
    if paper.comment:
        lines.append(f"   Comment: {paper.comment}")
    if summary_text:
        lines.append(f"   Abstract: {summary_text}")
    return "\n".join(lines)


def print_search_results(papers: Iterable[ArxivPaper]) -> None:
    """Print arXiv results to stdout."""

    papers = list(papers)
    if not papers:
        print("No results found.")
        return

    for index, paper in enumerate(papers, 1):
        print(format_paper(paper, index))
        if index < len(papers):
            print()


def _safe_filename(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")
    return cleaned or "arxiv_result"


def export_results_json(papers: Iterable[ArxivPaper], file_path: str) -> str:
    """Export papers to a JSON file."""

    data = [paper.to_dict() for paper in papers]
    directory = os.path.dirname(file_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as file_obj:
        json.dump(data, file_obj, ensure_ascii=False, indent=2)
    return file_path


def export_results_markdown(papers: Iterable[ArxivPaper], file_path: str) -> str:
    """Export papers to a Markdown file."""

    papers = list(papers)
    directory = os.path.dirname(file_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    lines: list[str] = ["# arXiv Search Results", ""]
    if not papers:
        lines.extend(["No results found.", ""])
    else:
        for index, paper in enumerate(papers, 1):
            lines.append(f"## {index}. {paper.title}")
            lines.append(f"- arXiv ID: {paper.arxiv_id}")
            lines.append(f"- Authors: {', '.join(paper.authors) if paper.authors else 'Unknown'}")
            lines.append(f"- Published: {(paper.published or 'N/A').split('T')[0]}")
            lines.append(f"- Last Updated: {(paper.updated or 'N/A').split('T')[0]}")
            lines.append(f"- Category: {paper.primary_category or ', '.join(paper.categories) or 'N/A'}")
            lines.append(f"- Page Count: {f'{paper.page_count} pages' if paper.page_count else 'N/A'}")
            lines.append(
                f"- Accepted: {'Yes' if paper.is_accepted else 'No'}"
                f"{f' ({paper.acceptance_note})' if paper.is_accepted and paper.acceptance_note else ''}"
            )
            lines.append(
                f"- Code/Data: {'Yes' if paper.has_code_or_data else 'No'}"
                f"{f' ({paper.code_data_links[0]})' if paper.code_data_links else ''}"
            )
            lines.append(f"- Entry: {paper.entry_url or 'N/A'}")
            lines.append(f"- PDF: {paper.pdf_url or 'N/A'}")
            lines.append(
                f"- Download Source: {paper.pdf_url or f'https://arxiv.org/pdf/{paper.arxiv_id}.pdf'}"
            )
            lines.append(f"- Journal Ref: {paper.journal_ref or 'N/A'}")
            lines.append(f"- Comment: {paper.comment or 'N/A'}")
            lines.append("")
            lines.append("### Abstract")
            lines.append("")
            lines.append(paper.summary or "N/A")
            lines.append("")

    with open(file_path, "w", encoding="utf-8") as file_obj:
        file_obj.write("\n".join(lines))
    return file_path


def download_paper(paper: ArxivPaper, output_dir: str) -> str:
    """Download one paper PDF to local directory."""

    title_hint = paper.title or paper.arxiv_id
    return download_paper_from_source(paper.pdf_url or paper.entry_url or paper.arxiv_id, output_dir, title_hint=title_hint)


def download_paper_from_source(source: str, output_dir: str, title_hint: str = "", timeout: int = 30) -> str:
    """Download a paper by source path (arXiv ID, abs URL, or PDF URL)."""

    os.makedirs(output_dir, exist_ok=True)
    pdf_url, arxiv_id = _resolve_pdf_url(source)
    filename = _safe_filename(f"{arxiv_id}_{title_hint or arxiv_id}") + ".pdf"
    output_path = os.path.join(output_dir, filename)
    request = urllib.request.Request(pdf_url, headers={"User-Agent": "Mozilla/5.0 (arXiv downloader)"})
    with _throttled_urlopen(request, timeout=timeout) as response, open(output_path, "wb") as file_obj:
        file_obj.write(response.read())
    return output_path


def postprocess_search_results(
    papers: list[ArxivPaper],
    *,
    export_dir: str,
    export_basename: str,
    export_json_enabled: bool,
    export_md_enabled: bool,
) -> int:
    """Handle output printing and export for search results."""

    print_search_results(papers)
    exit_code = 0 if papers else 1

    if export_json_enabled:
        json_path = os.path.join(export_dir, f"{export_basename}.json")
        exported = export_results_json(papers, json_path)
        print(f"\nExported JSON: {exported}")
    if export_md_enabled:
        md_path = os.path.join(export_dir, f"{export_basename}.md")
        exported = export_results_markdown(papers, md_path)
        print(f"Exported Markdown: {exported}")

    return exit_code


def build_argument_parser() -> argparse.ArgumentParser:
    """Build the CLI parser."""

    parser = argparse.ArgumentParser(description="Search arXiv papers or download a single PDF.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    search_parser = subparsers.add_parser("search", help="Search arXiv and print/export structured results")
    search_parser.add_argument("--query", default="", help="Free-text search across all fields")
    search_parser.add_argument(
        "--raw-query",
        default="",
        help="Raw arXiv API query expression, for example: all:\"graph\" AND (all:survey OR all:review)",
    )
    search_parser.add_argument("--title", default="", help="Search by title")
    search_parser.add_argument("--author", default="", help="Search by author")
    search_parser.add_argument("--category", default="", help="Search by category, for example cs.CL")
    search_parser.add_argument("--abstract", default="", help="Search in abstract text")
    search_parser.add_argument("--date-from", default="", help="Start date (YYYYMMDD), e.g. 20240101")
    search_parser.add_argument("--date-to", default="", help="End date (YYYYMMDD), e.g. 20240630")
    search_parser.add_argument("--start", type=int, default=0, help="Result offset")
    search_parser.add_argument("--max-results", type=int, default=10, help="Maximum number of results")
    search_parser.add_argument(
        "--sort-by", default="relevance", choices=["relevance", "lastUpdatedDate", "submittedDate"]
    )
    search_parser.add_argument("--sort-order", default="descending", choices=["ascending", "descending"])
    search_parser.add_argument("--days-back", type=int, default=0, help="Optional recent-date filter in days based on published date")
    search_parser.add_argument("--export-json", action="store_true", help="Export results as JSON file")
    search_parser.add_argument("--export-md", action="store_true", help="Export results as Markdown file")
    search_parser.add_argument("--export-dir", default="exports", help="Output directory for exported files")
    search_parser.add_argument("--export-basename", default="arxiv_results", help="Base filename for exported files")
    search_parser.add_argument(
        "--preset",
        choices=["materials-ai"],
        default="",
        help="Use a built-in search preset instead of a custom query",
    )

    download_parser = subparsers.add_parser("download", help="Download one PDF by arXiv ID or URL")
    download_parser.add_argument("source", help="arXiv ID, abs URL, or pdf URL")
    download_parser.add_argument("--output-dir", default="exports/downloads", help="Output directory for downloaded PDFs")
    download_parser.add_argument("--title-hint", default="", help="Optional title hint used in the output filename")

    return parser


def main() -> int:
    """Command-line entry point."""

    parser = build_argument_parser()
    args = parser.parse_args()

    if args.command == "download":
        try:
            output_path = download_paper_from_source(args.source, args.output_dir, title_hint=args.title_hint)
            print(f"Downloaded paper PDF: {output_path}")
            return 0
        except Exception as error:  # pragma: no cover - network dependent
            print(f"Download failed: {error}")
            return 1

    if args.command == "search":
        try:
            if args.preset == "materials-ai":
                papers = get_embedded_materials_ai_papers()
            else:
                params = ArxivSearchParams(
                    raw_query=args.raw_query,
                    query=args.query,
                    title=args.title,
                    author=args.author,
                    category=args.category,
                    abstract=args.abstract,
                    date_from=args.date_from,
                    date_to=args.date_to,
                    start=args.start,
                    max_results=args.max_results,
                    sort_by=args.sort_by,
                    sort_order=args.sort_order,
                )
                papers = search_arxiv(params)
                if args.days_back > 0:
                    papers = filter_recent_papers(papers, args.days_back)
        except ValueError as error:
            print(str(error))
            print("Tip: use `search --preset materials-ai` for the built-in test query.")
            return 2
        except Exception as error:  # pragma: no cover - CLI convenience
            print(f"Search failed: {error}")
            return 1

        if args.preset == "materials-ai":
            print("Embedded API search test")
            print("Query: \"materials science\" AND \"artificial intelligence\" AND (review OR survey OR perspective)")
            print("Time window: last ~6 months")
            print(f"Matched papers: {len(papers)}")
            print()

        return postprocess_search_results(
            papers,
            export_dir=args.export_dir,
            export_basename=args.export_basename,
            export_json_enabled=args.export_json,
            export_md_enabled=args.export_md,
        )

    parser.error("Unknown command")


def search_arxiv_bulk(
    params: ArxivSearchParams,
    max_total: int = 200,
    timeout: int = 30,
) -> list[ArxivPaper]:
    """自动翻页搜索 arXiv，累计到 max_total 篇或 API 返回空。

    arXiv API 单页上限 100，翻页间隔已在 _throttled_urlopen 中自动处理（3s）。
    """
    all_papers: list[ArxivPaper] = []
    page_size = min(100, max_total)

    while len(all_papers) < max_total:
        remaining = max_total - len(all_papers)
        batch_size = min(page_size, remaining)
        params.start = len(all_papers)
        params.max_results = batch_size

        batch = search_arxiv(params, timeout=timeout)
        if not batch:
            break
        all_papers.extend(batch)

    return all_papers


if __name__ == "__main__":
    raise SystemExit(main())