"""角色分类：LLM 给候选角色，人工确认后升级 confirmed。

只对已选论文执行；优先使用 condensation.md，其次摘要。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from backend.services.research_project import project_store as store

_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_FULL_PAPERS = _ROOT / "knowledgebase" / "full" / "papers"

ROLE_CHOICES = {"background", "key", "benchmark", "controversial", "gap"}


def _paper_text(paper_id: str) -> str:
    full = _FULL_PAPERS / paper_id
    for name in ("condensation.md", "paper.md", "cn_paper.md"):
        p = full / name
        if p.exists():
            return p.read_text(encoding="utf-8", errors="replace")[:12000]
    meta_path = full / "metadata.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            return meta.get("abstract") or ""
        except Exception:
            return ""
    return ""


def _parse_roles(text: str) -> dict | None:
    """从 LLM 文本解析 {roles, reason, confidence}。"""
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except Exception:
        return None
    roles = data.get("roles") or []
    if isinstance(roles, str):
        roles = [x.strip() for x in roles.split(",") if x.strip()]
    roles = [r.lower() for r in roles if r.lower() in ROLE_CHOICES]
    if not roles:
        return None
    return {
        "roles": roles,
        "reason": data.get("reason", ""),
        "confidence": float(data.get("confidence", 0.6) or 0.6),
    }


def classify_candidates(slug: str, paper_ids: list[str] | None = None,
                        model_key: str = "deepseek-chat") -> dict:
    """为项目论文生成候选角色。返回 {ok, classified[], failed[]}。"""
    if paper_ids is None:
        paper_ids = store.list_project_paper_ids(slug)
    if not paper_ids:
        return {"ok": False, "error": "项目还没有已选论文"}

    from backend.config.settings import Settings
    from backend.pipelines._prompts import load_prompt

    llm = Settings().get_llm_client(model_key=model_key)
    classified, failed = [], []

    for pid in paper_ids:
        text = _paper_text(pid)
        if not text.strip():
            failed.append({"paper_id": pid, "error": "无摘要/浓缩文本"})
            continue
        try:
            sys_prompt, user_prompt = load_prompt(
                "paper_role", sub_dir="review",
                paper_id=pid,
                paper_text=text,
                roles=", ".join(sorted(ROLE_CHOICES)),
            )
            result = llm.call(
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=400,
                temperature=0.2,
            )
            parsed = _parse_roles(result or "")
            if not parsed:
                failed.append({"paper_id": pid, "error": "LLM 输出无法解析"})
                continue
            entry = store.set_paper_role(
                slug, pid, parsed["roles"],
                reason=parsed["reason"],
                confirmed=False,
                confidence=parsed["confidence"],
            )
            classified.append({"paper_id": pid, "roles": parsed["roles"],
                               "confidence": parsed["confidence"]})
        except Exception as e:
            failed.append({"paper_id": pid, "error": str(e)})

    from backend.services.research_project.mirror import mirror_roles
    mirror_roles(slug)
    return {"ok": True, "classified": classified, "failed": failed}


def classify_status(slug: str) -> dict:
    """项目角色分类总览。"""
    proj = store.load_project(slug)
    roles = proj.get("roles", {})
    confirmed = [pid for pid, e in roles.items() if e.get("status") == "confirmed"]
    candidates = [pid for pid, e in roles.items() if e.get("status") == "candidate"]
    return {
        "ok": True,
        "confirmed": confirmed,
        "candidates": candidates,
        "total": len(roles),
    }
