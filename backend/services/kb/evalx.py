"""检索质量评测：recall@10 / MRR@10，并对旧 `search_all` 基线逐条对比。

02 §12 / 04 §9：基线每条 query 调 `backend.services.ragsearch.service.search_all(query, top_k=10)`，
一次采集落 `knowledgebase/_meta/eval/eval_baseline.json` 复用；**禁止**通过加白名单让评测变绿。

M8 增补 `alt_accept_ids`（M5 复核裁定 P2①）：可选字段，列"可接受的替代命中"
（典型场景＝跨语言条目：英文目标未召回，但命中了同主题中文文档）。语义收紧的三条纪律：
  ① **不改 `expect_doc_ids`、不删旧期望**——alt 只是附加口径，主指标 recall 仍按原期望算；
  ② alt 只单列成 `alt_hit` 诊断，**不并入 recall@10 主指标**，避免"靠扩标签刷分"；
  ③ 另给 `recall@10_with_alt`（expect∪alt 当可接受集）作为**口径说明**用，与主指标并列呈现。
"""
from __future__ import annotations

import json
from pathlib import Path

from backend.services.kb.db import META_DIR, PROJECT_ROOT, now
from backend.services.kb.query import DEFAULT_FUSION, VECTOR_FOLD, ask

EVAL_DIR = META_DIR / "eval"
EVAL_V1 = EVAL_DIR / "eval_v1.jsonl"
EVAL_V2 = EVAL_DIR / "eval_v2.jsonl"
# M5 起默认评测集是 50 条 eval_v2（30 条 v1 原样保留 + 20 条新增）；缺失时回退 v1。
DEFAULT_SET = EVAL_V2 if EVAL_V2.exists() else EVAL_V1
BASELINE_CACHE = EVAL_DIR / "eval_baseline.json"


def natural_id(doc_id: str) -> str:
    """去掉跨 topic 撞名时的 `topic:` 前缀，便于与旧系统 doc_id 对齐。"""
    if not doc_id or doc_id.startswith("__"):
        return doc_id
    marker = doc_id.find(":")
    return doc_id[marker + 1:] if marker >= 0 else doc_id


def load_set(path: str | Path | None = None) -> list[dict]:
    """读评测集；缺 id/query/expect_doc_ids 的行直接报错（不静默跳过）。"""
    target = Path(path) if path else DEFAULT_SET
    entries: list[dict] = []
    for lineno, line in enumerate(target.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{target}:{lineno} JSON 解析失败: {exc}") from exc
        for key in ("id", "query", "expect_doc_ids"):
            if key not in item:
                raise ValueError(f"{target}:{lineno} 缺少字段 {key}")
        if not isinstance(item["expect_doc_ids"], list):
            raise ValueError(f"{target}:{lineno} expect_doc_ids 必须是数组")
        alt = item.get("alt_accept_ids")
        if alt is not None:
            if not isinstance(alt, list) or not all(isinstance(x, str) for x in alt):
                raise ValueError(f"{target}:{lineno} alt_accept_ids 必须是字符串数组")
            dup = sorted(set(alt) & set(item["expect_doc_ids"]))
            if dup:
                raise ValueError(
                    f"{target}:{lineno} alt_accept_ids 与 expect_doc_ids 重复（{dup}）"
                    "——alt 只放'替代'命中，不得重复主期望")
        entries.append(item)
    if not entries:
        raise ValueError(f"{target}: 评测集为空")
    return entries


def evaluate_set_quality(entries: list[dict]) -> dict:
    """结构校验：条数、mode 合法、L0 反例条数、跨语言条数、期望集非空、alt 条目数。"""
    modes = {"browse", "balanced", "deep"}
    issues: list[str] = []
    l0_cases = 0
    cross_cases = 0
    alt_cases = 0
    for item in entries:
        mode = item.get("mode", "balanced")
        if mode not in modes:
            issues.append(f"{item['id']}: 非法 mode {mode}")
        if not item["expect_doc_ids"]:
            issues.append(f"{item['id']}: expect_doc_ids 为空")
        if item.get("tier_expect") == "L0":
            l0_cases += 1
        if item.get("cross_lingual"):
            cross_cases += 1
            if mode != "deep":
                # 跨语言靠 chunk 向量（中文 query → 英文正文块）；L0/L1 无块，故必须 deep。
                issues.append(f"{item['id']}: 跨语言条目的 mode 应为 deep（实测 mode={mode}）")
        if item.get("alt_accept_ids"):
            alt_cases += 1
    return {
        "n": len(entries),
        "l0_cases": l0_cases,
        "cross_lingual_cases": cross_cases,
        "alt_accept_cases": alt_cases,
        "sources": _counts(item.get("source", "unknown") for item in entries),
        "modes": _counts(item.get("mode", "balanced") for item in entries),
        "issues": issues,
    }


def _counts(values) -> dict:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


# ── 基线采集 ────────────────────────────────────────────────────────────────


def collect_baseline(entries: list[dict], top_k: int = 10, refresh: bool = False) -> dict[str, list[str]]:
    """采旧系统结果并缓存；缓存已覆盖的 query 直接复用，只补采缺失的。"""
    queries: dict[str, list[str]] = {}
    if BASELINE_CACHE.exists() and not refresh:
        try:
            cached = json.loads(BASELINE_CACHE.read_text(encoding="utf-8"))
            queries = {str(k): list(v) for k, v in (cached.get("queries") or {}).items()}
        except (json.JSONDecodeError, AttributeError, TypeError):
            queries = {}

    missing = [item["query"] for item in entries if item["query"] not in queries]
    missing = list(dict.fromkeys(missing))
    if not missing:
        return queries

    from backend.services.ragsearch.service import search_all

    for query in missing:
        try:
            results = search_all(query, top_k=top_k)
        except Exception as exc:  # noqa: BLE001 — 基线失败不等于新系统失败，记录后继续
            print(f"  [baseline] {query!r} 失败: {type(exc).__name__}: {exc}")
            queries[query] = []
            continue
        queries[query] = [natural_id(str(r.get("doc_id") or "")) for r in results]

    BASELINE_CACHE.write_text(
        json.dumps({"generated_at": now(), "top_k": top_k, "queries": queries},
                   ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="",
    )
    return queries


# ── 指标 ────────────────────────────────────────────────────────────────────


def recall_at_k(returned: list[str], expected: list[str], k: int = 10) -> float:
    if not expected:
        return 0.0
    got = {natural_id(item) for item in returned[:k]}
    want = {natural_id(item) for item in expected}
    return len(got & want) / len(want)


def reciprocal_rank(returned: list[str], expected: list[str], k: int = 10) -> float:
    want = {natural_id(item) for item in expected}
    for index, item in enumerate(returned[:k], 1):
        if natural_id(item) in want:
            return 1.0 / index
    return 0.0


def hit_ids(returned: list[str], wanted: list[str], k: int = 10) -> list[str]:
    """top-k 里命中的 `wanted` 子集（按 wanted 原始顺序返回，便于报告对齐）。"""
    got = {natural_id(item) for item in returned[:k]}
    return [item for item in wanted if natural_id(item) in got]


# ── 主流程 ──────────────────────────────────────────────────────────────────


def run(set_path: str | Path | None = None, top_k: int = 10, *, refresh_baseline: bool = False,
        write_report: bool = True, use_vector=None, fusion: str = DEFAULT_FUSION,
        fold: str = VECTOR_FOLD, toc_filter: bool = True) -> dict:
    """评测主流程。

    `use_vector=None` → 按 mode 默认（deep 开向量）；显式 False/True 用于
    "加向量支路前/后"对照实验与回归（不改变默认行为）。`fusion` 供裁定实验。
    `fusion` 缺省跟随 query.DEFAULT_FUSION（M5 采用 reserve），不写死。
    `fold`（M8）：chunk→doc 折叠规则，供 max / top3sum 对照实验；缺省跟随
    query.VECTOR_FOLD，不写死。
    `toc_filter`（M8-3）：是否剔除目录残渣块，缺省 True；显式 False 供开/关对照。
    """
    entries = load_set(set_path)
    quality = evaluate_set_quality(entries)
    baseline = collect_baseline(entries, top_k=top_k, refresh=refresh_baseline)

    per_query: list[dict] = []
    new_recalls: list[float] = []
    base_recalls: list[float] = []
    new_rrs: list[float] = []
    base_rrs: list[float] = []
    relaxed_recalls: list[float] = []
    # M8 口径说明：expect∪alt 当可接受集时的 recall（**不进主指标**，仅作诊断并列）
    with_alt_recalls: list[float] = []
    alt_hits_total = 0
    alt_cases_total = 0
    # 诊断：解除 §9.2 分档配额后的召回，用于量化"配额换多样性"的代价（不改默认行为）
    relaxed_quota = {0: top_k, 1: top_k, 2: top_k, 3: top_k}

    for item in entries:
        query = item["query"]
        expect = item["expect_doc_ids"]
        mode = item.get("mode", "balanced")
        ask_kwargs = {
            "topic": item.get("topic"), "tags": item.get("tags"), "org": item.get("org"),
            "source_types": item.get("source_types"), "year": item.get("year"),
            "tier_min": item.get("tier_min"), "tier_max": item.get("tier_max"),
        }
        result = ask(query, mode=mode, top_k=top_k, use_vector=use_vector, fusion=fusion,
                     fold=fold, toc_filter=toc_filter, **ask_kwargs)
        new_ids = [group["doc_id"] for group in result["groups"]]
        base_ids = baseline.get(query, [])

        relaxed = ask(query, mode=mode, top_k=top_k, quota=relaxed_quota,
                      use_vector=use_vector, fusion=fusion, fold=fold,
                      toc_filter=toc_filter, **ask_kwargs)
        relaxed_ids = [group["doc_id"] for group in relaxed["groups"]]

        new_recall = recall_at_k(new_ids, expect, top_k)
        base_recall = recall_at_k(base_ids, expect, top_k)
        relaxed_recalls.append(recall_at_k(relaxed_ids, expect, top_k))
        new_recalls.append(new_recall)
        base_recalls.append(base_recall)
        new_rrs.append(reciprocal_rank(new_ids, expect, top_k))
        base_rrs.append(reciprocal_rank(base_ids, expect, top_k))

        # M8：alt_accept_ids 单列诊断（不并入 recall@10）
        alt_ids = item.get("alt_accept_ids") or []
        alt_hit_ids = hit_ids(new_ids, alt_ids, top_k) if alt_ids else []
        if alt_ids:
            alt_cases_total += 1
            if alt_hit_ids:
                alt_hits_total += 1
        with_alt_recalls.append(recall_at_k(new_ids, list(expect) + list(alt_ids), top_k))

        missing = sorted({natural_id(e) for e in expect} - {natural_id(i) for i in new_ids})
        # D44：密度序下"细节"是 tier=1（原文）；旧口径数的 tier=3 已变成"提炼"。
        detail_count = sum(1 for group in result["groups"] if group.get("hit_tier") == 1)
        source_counts: dict[str, int] = {}
        for group in result["groups"]:
            for arm in group.get("sources") or []:
                source_counts[arm] = source_counts.get(arm, 0) + 1
        per_query.append({
            "id": item["id"], "query": query, "mode": mode,
            "source": item.get("source", "unknown"),
            "tier_expect": item.get("tier_expect"),
            "cross_lingual": bool(item.get("cross_lingual")),
            "expect": expect, "new_ids": new_ids, "baseline_ids": base_ids,
            "new_recall": round(new_recall, 4), "baseline_recall": round(base_recall, 4),
            "new_rr": round(new_rrs[-1], 4), "baseline_rr": round(base_rrs[-1], 4),
            "relaxed_recall": round(relaxed_recalls[-1], 4),
            "recall_with_alt": round(with_alt_recalls[-1], 4),
            "alt_accept_ids": alt_ids,
            "alt_hit": bool(alt_hit_ids),
            "alt_hit_ids": alt_hit_ids,
            "detail_in_top10": detail_count,
            "hit_sources": source_counts,
            "missing": missing,
        })

    def mean(values: list[float]) -> float:
        return round(sum(values) / len(values), 4) if values else 0.0

    l0_items = [item for item in per_query if item.get("tier_expect") == "L0"]
    deep_items = [item for item in per_query if item.get("mode") == "deep"]
    cross_items = [item for item in per_query if item.get("cross_lingual")]
    report = {
        "n": len(entries),
        "top_k": top_k,
        "generated_at": now(),
        "eval_set": str(set_path or DEFAULT_SET),
        "quality": quality,
        "recall@10": mean(new_recalls),
        "mrr@10": mean(new_rrs),
        "baseline": {
            "recall@10": mean(base_recalls),
            "mrr@10": mean(base_rrs),
        },
        "l0_cases": {
            "n": len(l0_items),
            "new_hit": sum(1 for item in l0_items if item["new_recall"] > 0),
            "baseline_hit": sum(1 for item in l0_items if item["baseline_recall"] > 0),
        },
        "regressions": [
            item["id"] for item in per_query if item["new_recall"] < item["baseline_recall"]
        ],
        "diagnostics": {
            "relaxed_quota_recall@10": mean(relaxed_recalls),
            "use_vector": "auto(deep)" if use_vector is None else bool(use_vector),
            "fusion": fusion,
            "fold": fold,
            "toc_filter": bool(toc_filter),
            "alt_accept": {
                "n": alt_cases_total,
                "n_alt_hit": alt_hits_total,
                "recall@10_with_alt": mean(with_alt_recalls),
                "note": ("M8 口径：alt_accept_ids 是可选'可接受替代命中'，"
                         "仅单列 alt_hit 诊断，不并入 recall@10 主指标；"
                         "recall@10_with_alt 把 expect∪alt 当可接受集，仅供口径说明"),
            },
            "cross_lingual": {
                "n": len(cross_items),
                "recall@10": mean([item["new_recall"] for item in cross_items]),
                "baseline_recall@10": mean([item["baseline_recall"] for item in cross_items]),
            },
            "deep_detail_gate": {
                "n": len(deep_items),
                "min_detail_in_top10": min((item["detail_in_top10"] for item in deep_items),
                                          default=0),
                "queries_with_detail_ge_4": sum(1 for item in deep_items
                                                if item["detail_in_top10"] >= 4),
                "note": "D44：deep 的'细节'在密度序里是 tier=1（原文），闸门改数 tier=1",
            },
            "note": "解除 §9.2 分档配额（各档放开到 top_k）后的召回，用于量化配额换多样性的代价",
        },
        "per_query": per_query,
    }
    report["pass"] = (
        report["recall@10"] >= report["baseline"]["recall@10"]
        and (not l0_items or report["l0_cases"]["new_hit"] == report["l0_cases"]["n"])
    )
    if write_report:
        stamp = report["generated_at"].replace(":", "").replace("-", "")[:15]
        path = EVAL_DIR / "reports" / f"eval_report_{stamp}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8", newline="")
        report["report_path"] = path.as_posix()
    return report


def render_report(report: dict) -> str:
    lines = [
        f'评测 {report["n"]} 条 / top_k={report["top_k"]}',
        f'  新系统 recall@10 = {report["recall@10"]}   MRR@10 = {report["mrr@10"]}',
        f'  旧基线 recall@10 = {report["baseline"]["recall@10"]}   '
        f'MRR@10 = {report["baseline"]["mrr@10"]}',
    ]
    l0 = report.get("l0_cases") or {}
    if l0.get("n"):
        lines.append(f'  L0 反例 {l0["new_hit"]}/{l0["n"]} 命中（旧基线 {l0["baseline_hit"]}/{l0["n"]}）')
    regressions = report.get("regressions") or []
    lines.append(f'  劣于基线的条目: {len(regressions)}' + (f' {regressions}' if regressions else ''))
    diag = report.get("diagnostics") or {}
    if diag.get("relaxed_quota_recall@10") is not None:
        lines.append(f'  [诊断] 解除分档配额后 recall@10 = {diag["relaxed_quota_recall@10"]}'
                     f'（当前 {report["recall@10"]}）——差值即"配额换多样性"的召回代价')
    cross = diag.get("cross_lingual") or {}
    if cross.get("n"):
        lines.append(f'  [跨语言] {cross["n"]} 条 recall@10 = {cross["recall@10"]}'
                     f'（旧基线 {cross.get("baseline_recall@10")}；门槛 ≥0.5）')
    gate = diag.get("deep_detail_gate") or {}
    if gate.get("n"):
        lines.append(f'  [deep 细节门] {gate["n"]} 条深模查询中 '
                     f'{gate["queries_with_detail_ge_4"]} 条 top10 含原文(tier=1) ≥4'
                     f'（最少 {gate["min_detail_in_top10"]}）')
    alt = diag.get("alt_accept") or {}
    if alt.get("n"):
        alt_items = [item for item in report["per_query"] if item.get("alt_accept_ids")]
        hit_ids = [item["id"] for item in alt_items if item.get("alt_hit")]
        lines.append(f'  [替代命中 alt] {alt["n"]} 条标注 alt_accept_ids，'
                     f'{alt["n_alt_hit"]} 条 alt_hit'
                     + (f'（{",".join(hit_ids)}）' if hit_ids else '')
                     + f'；expect∪alt 口径 recall@10 = {alt["recall@10_with_alt"]}'
                     f'（主指标仍为 {report["recall@10"]}，alt 不并入）')
    lines.append("  逐条:")
    for item in report["per_query"]:
        flag = " " if item["new_recall"] >= item["baseline_recall"] else "!"
        detail = f' 原文={item["detail_in_top10"]}' if item.get("mode") == "deep" else ""
        cross = " 跨语言" if item.get("cross_lingual") else ""
        alt = ""
        if item.get("alt_accept_ids"):
            alt = f' alt={"HIT" if item.get("alt_hit") else "miss"}'
        lines.append(
            f'   {flag} {item["id"]} [{item["mode"]}]{cross} {item["query"][:40]:<42} '
            f'new={item["new_recall"]:.2f} base={item["baseline_recall"]:.2f} '
            f'relax={item.get("relaxed_recall", 0):.2f}{detail}{alt} '
            f'miss={item["missing"][:3]}'
        )
    lines.append(f'  判定: {"PASS" if report.get("pass") else "FAIL"}')
    return "\n".join(lines)
