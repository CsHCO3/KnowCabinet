"""论文浓缩服务 — 读全文 md → 单条目结构化浓缩，归档到 knowledgebase/papers/{arxiv_id}/。

浓缩条目为 markdown（贡献/方法/结果/局限/关键词），面向检索与速读；
后续可接入 ragsearch 等知识库做 RAG 检索。
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from backend.config.settings import Settings
from backend.pipelines._prompts import load_prompt

_PAPERS_ROOT = Path(__file__).resolve().parent.parent.parent.parent / "knowledgebase" / "full" / "papers"
_ARXIV_ID_RE = re.compile(r"(\d{4}\.\d{4,5})(?:v\d+)?")


def _extract_arxiv_id(name: str) -> str | None:
    m = _ARXIV_ID_RE.search(name)
    return m.group(1) if m else None


def _build_condense_meta(result: str, arxiv_id: str) -> dict:
    """构建浓缩条目的 frontmatter meta（论文元数据 + 浓缩关键词 + section/tags/source_type）。"""
    from backend.services.archive.service import (
        _build_section_and_tags, _extract_keywords_from_text, _paper_meta_lookup,
    )
    meta = dict(_paper_meta_lookup().get(arxiv_id, {}))
    kws = _extract_keywords_from_text(result)
    if kws:
        meta["tags"] = kws
    section, tags = _build_section_and_tags(meta)
    meta["section"] = section
    meta["tags"] = tags
    meta["source_type"] = "paper"
    return meta


def _build_condense_frontmatter(result: str, arxiv_id: str) -> str:
    """为 condensation.md 生成 frontmatter（论文元数据 + 浓缩关键词 + source_type）。"""
    from backend.services.archive.service import _fallback_title, _frontmatter
    return _frontmatter(_build_condense_meta(result, arxiv_id),
                        _fallback_title(result, arxiv_id), arxiv_id)


def condense_paper(md_path: str, model_key: str = "deepseek-chat",
                   arxiv_id: str | None = None) -> dict:
    """浓缩单篇论文 md，归档到 knowledgebase/papers/{arxiv_id}/。"""
    md_file = Path(md_path)
    if not md_file.exists():
        return {"ok": False, "file": str(md_path), "error": "文件不存在"}
    content = md_file.read_text(encoding="utf-8", errors="replace")
    if not content.strip():
        return {"ok": False, "file": str(md_path), "error": "md 为空"}

    settings = Settings()
    llm = settings.get_llm_client(model_key=model_key)
    sys_prompt, user_prompt = load_prompt("paper_condense", paper_md=content[:200_000])
    try:
        result = llm.call(
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=2048,
            temperature=0.2,
        )
    except Exception as e:
        return {"ok": False, "file": str(md_path), "error": str(e)}
    if not result or not result.strip():
        return {"ok": False, "file": str(md_path), "error": "LLM 返回空"}

    arxiv_id = arxiv_id or _extract_arxiv_id(md_file.stem) or md_file.stem[:20]
    out_dir = _PAPERS_ROOT / arxiv_id
    out_dir.mkdir(parents=True, exist_ok=True)
    # 源 md 按规范名复制进论文目录（避免 2210.04644v1_*.md 这类源名副本）
    from backend.services.archive.service import _classify_md
    kind, _ = _classify_md(md_file.stem)
    canon = {"paper": "paper.md", "translate": "cn_paper.md", "ocr": "paper_ocr.md",
             "translate_ocr": "cn_paper_ocr.md"}.get(kind, "paper.md")
    if not (out_dir / canon).exists():
        try:
            shutil.copy2(str(md_file), out_dir / canon)
        except Exception:
            pass
    cond_path = out_dir / "condensation.md"
    meta = _build_condense_meta(result, arxiv_id)
    from backend.services.archive.service import _fallback_title, _frontmatter, promote_to_dense
    cond_path.write_text(_frontmatter(meta, _fallback_title(result, arxiv_id), arxiv_id)
                         + result.strip() + "\n", encoding="utf-8")
    promote_to_dense("paper", arxiv_id, result, meta)  # 晋升 L1 高级知识库（幂等覆盖）
    return {"ok": True, "arxiv_id": arxiv_id, "out_dir": str(out_dir),
            "condensation_path": str(cond_path), "chars": len(result)}


def condense_directory(md_dir: str, model_key: str = "deepseek-chat") -> list[dict]:
    """浓缩目录下所有 .md。"""
    d = Path(md_dir)
    if not d.is_dir():
        return [{"ok": False, "file": str(md_dir), "error": "目录不存在"}]
    mds = sorted(d.glob("*.md"))
    results = []
    for md in mds:
        results.append(condense_paper(str(md), model_key))
    # 浓缩后重建 papers 索引（失败不阻塞浓缩）
    try:
        from backend.services.ragsearch.service import rebuild_kb
        rebuild_kb("papers")
    except Exception:
        pass
    return results
