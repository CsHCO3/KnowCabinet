# -*- coding: utf-8 -*-
"""论文全文分块器：md → 段落 chunk（供 bge-m3 向量化段落检索）。

- 剥离 frontmatter，读 title/section/tags/source_type/source/authors/year 注入每块 metadata
- 按 markdown 标题(#/##/###)分节；节超长再按空行段落、最后按窗口+重叠切分
- 剔除末尾 References/参考文献 段（默认开）
- 清理 markdown 图片引用 ![](...) 等噪声
- 返回 chunk 列表：{doc_id, title, section, source, authors, year, tags, source_type, chunk_index, text}
"""
from __future__ import annotations

import re

from ragsearch.engine.document import extract_title, parse_frontmatter, parse_tags

_HEADER_RE = re.compile(r"^#{1,3}\s+(.+)$")
_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_REF_HEAD_RE = re.compile(
    r"^#{1,3}\s*(references|参考文献|引用文献)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_MIN_CHUNK = 24          # 太短的块丢弃（公式碎片/纯图注）
_MAX_CHARS = 500         # 单块目标字符数
_OVERLAP = 80            # 窗口切分重叠


def _clean_section(text: str) -> str:
    text = _IMG_RE.sub(" ", text)
    lines = [ln.rstrip() for ln in text.splitlines()]
    # 压缩连续空行，去空行首尾
    out = []
    blank = 0
    for ln in lines:
        if not ln.strip():
            blank += 1
            if blank > 1:
                continue
        else:
            blank = 0
        out.append(ln)
    return "\n".join(out).strip()


def _strip_refs(body: str) -> str:
    m = _REF_HEAD_RE.search(body)
    if m:
        return body[: m.start()]
    return body


def _split_by_headers(body: str) -> list[tuple[str, str]]:
    """按标题切分 → [(section, text)]。无标题的正文归为 '' 一节。"""
    lines = body.splitlines()
    sections: list[tuple[str, list[str]]] = []
    cur_section, cur_lines = "", []
    for ln in lines:
        hm = _HEADER_RE.match(ln)
        if hm and cur_lines and hm.group(1).strip() not in (cur_section,):
            sections.append((cur_section, cur_lines))
            cur_section, cur_lines = hm.group(1).strip(), []
            continue
        if hm and not cur_lines:
            cur_section = hm.group(1).strip()
            continue
        cur_lines.append(ln)
    if cur_lines:
        sections.append((cur_section, cur_lines))
    return [(s, _clean_section("\n".join(ls))) for s, ls in sections if _clean_section("\n".join(ls))]


def _split_long(text: str, max_chars: int, overlap: int) -> list[str]:
    """超长文本：先按空行段落聚块，末尾用窗口补切。"""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paras:
        return []
    chunks: list[str] = []
    buf = ""
    for p in paras:
        if len(p) > max_chars:                      # 单段超长：直接窗口切
            if buf.strip():
                chunks.append(buf.strip())
                buf = ""
            chunks.extend(_window_split(p, max_chars, overlap))
            continue
        if len(buf) + len(p) <= max_chars or not buf:
            buf += ("\n\n" if buf else "") + p
        else:
            chunks.append(buf.strip())
            buf = p
    if buf.strip():
        chunks.append(buf.strip())
    return chunks


def _window_split(text: str, max_chars: int, overlap: int) -> list[str]:
    out = []
    i = 0
    n = len(text)
    while i < n:
        seg = text[i : i + max_chars]
        if not seg.strip():
            break
        out.append(seg.strip())
        if i + max_chars >= n:
            break
        i += max_chars - overlap
    return out


def chunk_md(content: str, doc_id: str, *, filename: str = "",
             max_chars: int = _MAX_CHARS, overlap: int = _OVERLAP,
             strip_refs: bool = True) -> list[dict]:
    """一篇 md → 段落 chunk 列表（按文本切，不含 frontmatter）。"""
    meta, body = parse_frontmatter(content)
    if strip_refs:
        body = _strip_refs(body)
    title = extract_title(body, meta, filename)
    tags = parse_tags(meta.get("tags"))
    base = {
        "doc_id": doc_id,
        "title": title,
        "section": (meta.get("section") or "").strip(),
        "source": (meta.get("source") or "").strip(),
        "authors": (meta.get("authors") or "").strip(),
        "year": str(meta.get("year") or "").strip(),
        "tags": tags,
        "source_type": (meta.get("source_type") or "").strip() or "paper",
    }
    chunks: list[dict] = []
    idx = 0
    for section, text in _split_by_headers(body):
        pieces = _split_long(text, max_chars, overlap) if len(text) > max_chars else ([text] if text else [])
        for piece in pieces:
            if len(piece) < _MIN_CHUNK:
                continue
            c = dict(base)
            c["section"] = section or base["section"]
            c["chunk_index"] = idx
            c["text"] = piece
            chunks.append(c)
            idx += 1
    return chunks
