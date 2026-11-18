"""PDF 本地提取工具。

从 remote/utils.py 迁移。
"""

import re
from pathlib import Path

from pypdf import PdfReader


def extract_pdf_content(pdf_path: str, output_txt_path: str, output_urls_path: str) -> None:
    """提取 PDF 全部文本和网址。"""
    reader = PdfReader(pdf_path)
    all_text: list[str] = []
    all_urls: set[str] = set()
    url_pattern = r"https?://[^\s<>\"{}|\\^`\[\]]+"

    for page_num, page in enumerate(reader.pages, 1):
        text = page.extract_text()
        all_text.append(f"\n{'='*50}\n第 {page_num} 页\n{'='*50}\n{text}")
        urls = re.findall(url_pattern, text)
        all_urls.update(urls)

    Path(output_txt_path).write_text("".join(all_text), encoding="utf-8")
    lines = [f"共找到 {len(all_urls)} 个唯一网址\n"]
    for idx, url in enumerate(sorted(all_urls), 1):
        lines.append(f"{idx}. {url}\n")
    Path(output_urls_path).write_text("".join(lines), encoding="utf-8")
