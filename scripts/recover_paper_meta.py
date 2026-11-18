"""从 paper_searches JSON 恢复被 fill-meta 改坏的论文元数据（source/is_oa/eprint_id 等）。

用法：python scripts/recover_paper_meta.py [--dry-run]
- 扫描 data/paper_searches/*.json（兼容 dict/list 顶层），按论文 id 建原始元数据查找表
- 对 full/papers 下缺 source/is_oa/eprint_id 的论文，从查找表补回（增量，不覆盖已有好字段）
"""
import glob
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEARCH_DIR = ROOT / "data" / "paper_searches"
PAPERS_DIR = ROOT / "knowledgebase" / "full" / "papers"

DRY = "--dry-run" in sys.argv


def iter_records():
    """遍历所有 paper_searches JSON，yield 每条 PaperRecord dict。"""
    for f in sorted(SEARCH_DIR.glob("*.json")):
        if f.name.startswith("_"):
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(d, dict):
            results = d.get("results") or []
            yield from results
        elif isinstance(d, list):
            for entry in d:
                if isinstance(entry, dict):
                    results = entry.get("results") or []
                    yield from results


def build_lookup():
    """论文 id (arxiv/doi/eprint) → 原始元数据。"""
    lookup = {}
    for r in iter_records():
        if not isinstance(r, dict):
            continue
        keys = []
        if r.get("arxiv_id"):
            keys.append(r["arxiv_id"].lower())
        if r.get("doi"):
            keys.append(r["doi"].lower())
        if r.get("eprint_id"):
            keys.append(r["eprint_id"].lower())
        if r.get("source"):
            keys.append(r["source"].split("/")[-1].lower())
        for k in keys:
            if k and k not in lookup:
                lookup[k] = r
    return lookup


def main():
    lookup = build_lookup()
    print(f"paper_searches 建立查找表: {len(lookup)} 个 id")

    restored, fixed_title = 0, 0
    for d in sorted(PAPERS_DIR.iterdir()):
        if not d.is_dir():
            continue
        mpath = d / "metadata.json"
        if not mpath.exists():
            continue
        try:
            meta = json.loads(mpath.read_text(encoding="utf-8"))
        except Exception:
            continue
        pid = d.name.lower()
        rec = None
        for key in (pid, pid.replace("_", "/"), pid.replace("-", "/")):
            if key in lookup:
                rec = lookup[key]
                break
        if not rec:
            continue
        missing = []
        for field in ("source", "is_oa", "oa_url", "venue", "venue_type", "abstract", "year", "doi"):
            cur = meta.get(field)
            orig = rec.get(field)
            if (cur in (None, "", False) or (field == "year" and not cur)) and orig not in (None, "", False):
                missing.append((field, orig))
        # 恢复 title：当前 title 为空或明显是垃圾提取（专利局/极短/含怪字符）
        cur_title = meta.get("title", "")
        rec_title = rec.get("title", "")
        title_bad = (not cur_title or len(cur_title.strip()) < 4
                     or "国家知识产权局" in cur_title
                     or "专利" in cur_title)
        if title_bad and rec_title and rec_title != cur_title:
            fixed_title += 1
            print(f"  [{pid}] 修正 title: {cur_title[:20]!r} → {rec_title[:40]!r}")
            if not DRY:
                meta["title"] = rec_title
        # 补 source：从 arxiv_id/doi 派生
        if not meta.get("source"):
            if rec.get("arxiv_id"):
                meta["source"] = f"https://arxiv.org/abs/{rec['arxiv_id']}"
                missing.append(("source(派生)", f"arxiv {rec['arxiv_id']}"))
            elif rec.get("doi"):
                meta["source"] = f"https://doi.org/{rec['doi']}"
                missing.append(("source(派生)", f"doi {rec['doi']}"))
        if not missing and not title_bad:
            continue
        restored += 1
        print(f"  [{pid}] 恢复 {[f for f,_ in missing] or ['title']}")
        if DRY:
            continue
        changed = False
        for field, orig in missing:
            if field.startswith("source") or field not in meta or meta[field] in (None, "", False):
                if field != "source(派生)":
                    meta[field] = orig
                changed = True
        mpath.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{'[dry-run] 将处理' if DRY else '已处理'} {restored} 篇（含修正 title {fixed_title} 篇）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
