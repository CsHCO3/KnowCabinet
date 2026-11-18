"""文档数据模型 + frontmatter 解析。"""
import os
import re
from dataclasses import dataclass, field

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def parse_frontmatter(content: str) -> tuple:
    """解析 YAML frontmatter，返回 (meta, body)。无 frontmatter 则 meta 为空。"""
    m = _FRONTMATTER_RE.match(content)
    if not m:
        return {}, content
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip().strip("\"'")
    return meta, content[m.end():]


def parse_tags(raw) -> list:
    """把 tags 值规范化为 list[str]：支持 `[a, b]`、`a, b`、`a b`、单值。"""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    s = str(raw).strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    parts = re.split(r"[,\s、]+", s)
    return [p for p in (x.strip() for x in parts) if p]


def normalize_meta(meta: dict) -> dict:
    """把 frontmatter 规范成统一字段（缺失给空值）。"""
    meta = meta or {}
    return {
        "id": (meta.get("id") or "").strip(),
        "title": (meta.get("title") or "").strip(),
        "source": (meta.get("source") or "").strip(),
        "section": (meta.get("section") or "").strip(),
        "authors": (meta.get("authors") or "").strip(),
        "year": str(meta.get("year") or "").strip(),
        "tags": parse_tags(meta.get("tags")),
        "source_type": (meta.get("source_type") or "").strip(),
    }


def doc_id_from_path(filename: str, meta_id: str = "") -> str:
    """doc_id：frontmatter id 优先；否则取路径首段目录名；无目录则用文件名去扩展名。

    与 infosystem 约定一致：论文 doc_id = 目录名 {arxiv_id}；视频 = {bvid}。
    """
    if meta_id:
        return meta_id
    parts = filename.replace("\\", "/").split("/")
    if len(parts) >= 2 and parts[0]:
        return parts[0]
    return os.path.splitext(parts[-1])[0]


def extract_title(body: str, meta: dict = None, filename: str = "") -> str:
    """标题优先级：frontmatter title → 第一个 '# ' 标题 → 文件名。"""
    if meta and meta.get("title"):
        return meta["title"]
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s.lstrip("# ").strip()
    return filename


@dataclass
class DocEntry:
    """单篇文档（正文已去掉 frontmatter）。"""
    filename: str       # 相对路径（= path）
    doc_id: str         # 文档身份（目录名 / frontmatter id）
    title: str
    section: str
    source: str
    authors: str
    year: str
    tags: list
    body: str
    char_count: int
    source_type: str = ""


@dataclass
class SearchResult:
    """一条搜索结果。"""
    filename: str       # 相对路径（= path，可回查全文）
    doc_id: str
    title: str
    snippet: str
    score: float
    rank: int
    section: str = ""
    source: str = ""
    authors: str = ""
    year: str = ""
    tags: list = field(default_factory=list)
    source_type: str = ""
