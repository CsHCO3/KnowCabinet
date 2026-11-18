"""kb-* CLI 实现；backend/main.py 只保留参数绑定。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from backend.services.kb.catalog import scan_all
from backend.services.kb.db import DB_PATH, connect
from backend.services.kb.evalx import render_report, run
from backend.services.kb.invariants import assert_invariants
from backend.services.kb.inventory import gap, recent, render_compact, render_gap, snapshot
from backend.services.kb.promote import promote
from backend.services.kb.query import (
    DEFAULT_FUSION, VECTOR_FOLD, ask, fetch,
)
from backend.services.kb.tags import clean_apply, clean_dryrun, draft_vocab, stats
from backend.services.kb.vector import MODEL_ID, status as vector_status, sync as vector_sync


def _csv(value: str | None) -> list[str]:
    """`a,b` / `a, b` → ['a','b']；空值给空列表。"""
    if not value:
        return []
    return [item.strip() for item in value.replace("，", ",").split(",") if item.strip()]


def _utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and (stream.encoding or "").lower() != "utf-8":
            stream.reconfigure(encoding="utf-8")


def _emit(value) -> None:
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def cmd_kb_scan(args: argparse.Namespace) -> int:
    _utf8_stdio()
    try:
        report = scan_all(force=args.force, source=args.source).to_dict()
        _emit(report)
        return 0
    except Exception as exc:
        print(f"kb-scan failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


def _render_recent(data: dict) -> str:
    items = data.get("items", [])
    if not items:
        return f"[{data.get('topic', '_all')} recent] {data.get('days', 7)} 天内无更新"
    lines = [f"[{data.get('topic', '_all')} recent] {len(items)} 条更新"]
    for row in items[:10]:
        lines.append(f"  L{row['best_tier']} {row['title']} ({row['topic']})")
    return "\n".join(lines)


def cmd_kb_inventory(args: argparse.Namespace) -> int:
    _utf8_stdio()
    try:
        if args.view == "gap":
            data = gap(args.topic)
            compact = render_gap(data)
        elif args.view == "recent":
            data = recent(args.days, args.topic)
            compact = _render_recent(data)
        else:
            data = snapshot(args.topic, args.tag)
            compact = render_compact(data)
        if args.pretty:
            print(compact)
        else:
            _emit(data)
        return 0
    except Exception as exc:
        print(f"kb-inventory failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


def cmd_kb_rebuild(args: argparse.Namespace) -> int:
    _utf8_stdio()
    try:
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(DB_PATH) + suffix)
            if path.exists():
                path.unlink()
        scan_report = scan_all(force=True).to_dict()
        embed_report = {} if args.keep_vector else vector_sync()
        conn = connect()
        try:
            counts = {
                "documents": conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
                "versions": conn.execute("SELECT COUNT(*) FROM versions").fetchone()[0],
                "kb_fts": conn.execute("SELECT COUNT(*) FROM kb_fts").fetchone()[0],
            }
            chunk_count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            invariant_report = assert_invariants(conn)
        finally:
            conn.close()
        _emit({
            "dropped": True,
            "scan_report": scan_report,
            "embed_report": embed_report,
            "row_counts": counts,
            "chunk_count": chunk_count,
            "invariants": invariant_report,
        })
        return 0
    except Exception as exc:
        print(f"kb-rebuild failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


def _render_ask(result: dict) -> str:
    stats = result.get("stats", {})
    by_tier = stats.get("by_tier", {})
    tier_text = " ".join(f"L{tier}:{by_tier[tier]}" for tier in sorted(by_tier))
    header = (
        f'[{result["mode"]}] "{result["query"]}" 候选 {stats.get("candidates", 0)} '
        f'→ 返回 {stats.get("returned", 0)}' + (f" ({tier_text})" if tier_text else "")
    )
    filters = result.get("filters")
    lines = [header]
    if filters:
        lines.append(f"  过滤: {json.dumps(filters, ensure_ascii=False)}")
    if not result.get("groups"):
        lines.append("  无命中（考虑放宽过滤、换 mode 或换关键词）")
        return "\n".join(lines)
    for index, group in enumerate(result["groups"], 1):
        evidence = group.get("evidence", {})
        if evidence.get("l0_only"):
            flag = "仅元数据·禁止引用 abstract"
        elif evidence.get("quote_from") == "raw":
            flag = f"可引原话（tier={evidence.get('quote_tier')} · {evidence.get('quote_kind')}）"
        elif evidence.get("quote_from") == "derived":
            flag = (f"仅可转述·不可当原话引（最高档 {group['best_tier']}"
                    f" · {evidence.get('quote_kind')}）")
        else:
            flag = "无正文版本"
        if evidence.get("legacy_l2"):
            flag += " ｜ 存量豁免 D27"
        lines.append(
            f'  {index}. L{group["hit_tier"]} {group["score"]:.3f} '
            f'[{group["topic"]}/{group["source_type"]}] {group["title"] or group["doc_id"]}'
        )
        lines.append(f'       doc_id={group["doc_id"]}')
        kinds = " ".join(
            f'{v["kind"]}(L{v["tier"]}{"" if v["available"] else "缺"})' for v in group["versions"]
        )
        lines.append(f'       版本: {kinds}')
        sources = "+".join(group.get("sources") or [])
        if sources:
            lines.append(f'       支路: {sources}'
                         f' (fts={group.get("fts_score")} vec={group.get("vector_score")})')
        if group.get("tags"):
            lines.append(f'       标签: {", ".join(group["tags"])}')
        if group.get("org_marks"):
            lines.append(f'       项目线: {", ".join(group["org_marks"])}')
        lines.append(f'       证据: {flag}')
        quote_ids = group.get("quote_chunk_ids") or []
        if quote_ids:
            quote_tier = evidence.get("quote_tier", 1)
            lines.append(f'       可取证块: {", ".join(str(cid) for cid in quote_ids[:5])}'
                         f'（kb-fetch {group["doc_id"]} --tier {quote_tier} '
                         f'--chunk-ids {quote_ids[0]}）')
        snip = (group.get("snippet") or "").strip()
        if snip:
            lines.append(f'       片段: {snip[:200]}')
    return "\n".join(lines)


def cmd_kb_ask(args: argparse.Namespace) -> int:
    _utf8_stdio()
    try:
        use_vector = None
        if args.no_vector:
            use_vector = False
        elif args.vector:
            use_vector = True
        result = ask(
            args.query,
            topic=args.topic,
            tags=_csv(args.tags),
            org=_csv(args.org),
            source_types=_csv(args.source_type),
            year=args.year,
            tier_min=args.tier_min,
            tier_max=args.tier_max,
            stage=args.stage,
            mode=args.mode,
            top_k=args.top_k,
            use_vector=use_vector,
            fusion=args.fusion,
            fold=args.fold,
            toc_filter=getattr(args, "toc_filter", True),
        )
        print(_render_ask(result) if args.pretty else json.dumps(result, ensure_ascii=False,
                                                                separators=(",", ":")))
        return 0
    except ValueError as exc:
        print(f"kb-ask: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"kb-ask failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


def _render_fetch(result: dict) -> str:
    lines = [
        f'[{result["doc_id"]}] L{result["tier"]} {result["kind"]} '
        f'({result["topic"]}/{result["source_type"]})',
        f'  标题: {result["title"]}',
        f'  路径: {result["path"]}',
    ]
    if result.get("note"):
        lines.append(f'  注意: {result["note"]}')
    if result.get("abstract"):
        lines.append(f'  摘要: {result["abstract"][:500]}')
    if result.get("segments"):
        lines.append(f'  段落取证: {result["segment_count"]} 段 / ±{result["context"]} 上下文')
        for segment in result["segments"]:
            lines.append("-" * 70)
            lines.append(
                f'[chunk {segment["chunk_id"]}] {segment["path"]} '
                f'#{segment["chunk_index"]}/{segment["total_chunks"]} '
                f'(L{segment["tier"]}/{segment["kind"]}, {segment["char_count"]} 字)'
            )
            if segment.get("context_before"):
                head = segment["context_before"][0]
                lines.append(f'  ↑ 上文块 #{head["chunk_index"]} (id={head["chunk_id"]}): '
                             f'{head["text"][:120]}…')
            lines.append(segment["text"])
            if segment.get("context_after"):
                tail = segment["context_after"][-1]
                lines.append(f'  ↓ 下文块 #{tail["chunk_index"]} (id={tail["chunk_id"]}): '
                             f'{tail["text"][:120]}…')
        return "\n".join(lines)
    lines.append("-" * 70)
    lines.append(result["content"])
    return "\n".join(lines)


def cmd_kb_fetch(args: argparse.Namespace) -> int:
    _utf8_stdio()
    try:
        result = fetch(args.doc_id, tier=args.tier, chunk_ids=_csv(args.chunk_ids),
                       context=args.context)
        if "error" in result:
            print(f"kb-fetch: {result['error']} ({json.dumps(result, ensure_ascii=False)})",
                  file=sys.stderr)
            return 1
        if args.pretty:
            print(_render_fetch(result))
        else:
            _emit(result)
        return 0
    except Exception as exc:
        print(f"kb-fetch failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


def cmd_kb_eval(args: argparse.Namespace) -> int:
    _utf8_stdio()
    try:
        report = run(args.set, top_k=args.top_k, refresh_baseline=args.refresh_baseline)
        if args.pretty:
            print(render_report(report))
        else:
            _emit({key: value for key, value in report.items() if key != "per_query"}
                  | {"per_query": report["per_query"]})
        return 0 if report.get("pass") else 1
    except Exception as exc:
        print(f"kb-eval failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


def cmd_kb_tags(args: argparse.Namespace) -> int:
    _utf8_stdio()
    try:
        if args.action == "stats":
            data = stats(args.topic)
        elif args.action == "draft":
            data = draft_vocab(args.topic, top_k=args.top_k, min_count=args.min_count)
        elif args.apply:
            data = clean_apply(args.topic)
        else:
            data = clean_dryrun(args.topic)
        if args.pretty:
            if args.action == "stats":
                print(f"[{args.topic}] {data['documents']} 条 / {data['md_files_scanned']} md / "
                      f"{data['db_distinct_tags']} DB tags / {len(data['issues'])} 待处理")
            elif args.action == "draft":
                print(f"[{args.topic}] draft={data['status']} knowledge={data['knowledge_candidates']} "
                      f"org={data['org_candidates']} path={data['path']}")
            elif args.apply:
                # clean_apply 的返回结构与 dry-run 不同（written/written_count/timestamp）
                print(f"[{args.topic}] apply=True files={data['written_count']} "
                      f"backups={data['written_count']} stamp={data['timestamp']}")
            else:
                print(f"[{args.topic}] apply=False files={data['files']} "
                      f"replace={data['replace_plans']} unmapped={len(data['unmapped_tags'])}")
        else:
            _emit(data)
        return 0
    except Exception as exc:
        print(f"kb-tags failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


def _render_batch(data: dict) -> str:
    if "error" in data:
        return f"[kb-batch] ERROR {data['error']}: {data.get('message', '')}"
    if data.get("kind") == "batches":
        lines = [f"[kb-batch list] {data['count']} 批 "
                 f"(open={data['by_status'].get('open', 0)} "
                 f"confirmed={data['by_status'].get('confirmed', 0)})"]
        for item in data["items"]:
            lines.append(f'  {item["round_id"]} {item["status"]} topic={item.get("topic")} '
                         f'items={item["item_count"]} {item.get("confirmed_at") or ""}')
        return "\n".join(lines)
    lines = [f'[kb-batch {data["round_id"]}] status={data["status"]} '
             f'topic={data.get("topic")} items={data["item_count"]}']
    if data.get("added") is not None:
        lines.append(f'  新增 {len(data["added"])} 条: {", ".join(data["added"])}')
    if data.get("unknown_doc"):
        lines.append(f'  未入库(拒绝): {", ".join(data["unknown_doc"])}')
    if data.get("tags_applied") is not None:
        lines.append(f'  已确认，topic={data.get("topic_applied")} '
                     f'tags={", ".join(data.get("tags_applied") or []) or "(无)"} '
                     f'写入 {data.get("written_docs", 0)} 条')
    return "\n".join(lines)


def cmd_kb_batch(args: argparse.Namespace) -> int:
    _utf8_stdio()
    from backend.services.kb.batch import add_items, confirm, list_batches, open_batch

    try:
        if args.action == "open":
            data = open_batch(args.round_id, args.topic)
        elif args.action == "add":
            data = add_items(args.round_id, args.doc_ids)
        elif args.action == "confirm":
            data = confirm(args.round_id, args.topic, _csv(args.tags))
        else:
            data = list_batches(args.status)
        if args.pretty:
            print(_render_batch(data))
        else:
            _emit(data)
        return 1 if "error" in data else 0
    except Exception as exc:
        print(f"kb-batch failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


def cmd_kb_tag(args: argparse.Namespace) -> int:
    """M6：kb_tag 的 CLI 等价物（只写 kb.db；md 归 kb-tags clean）。"""
    _utf8_stdio()
    from backend.services.kb.tags import tag_docs

    try:
        report = tag_docs(_csv(args.doc_ids), add=_csv(args.add), remove=_csv(args.remove))
        if args.pretty:
            if report.get("errors"):
                print(f'[kb-tag] 错误: {report["errors"]}')
            print(f'[kb-tag] 更新 {len(report["updated"])} 条 / 未找到 {len(report["not_found"])} / '
                  f'歧义 {len(report["ambiguous"])} / md 写入 {report.get("md_written", 0)}')
            for change in report.get("changed", []):
                print(f'  {change["doc_id"]}: {change["before"]} → {change["after"]}')
            for item in report.get("noop", []):
                print(f'  {item["doc_id"]}: 无变化 {item["tags"]}')
            for item in report.get("ambiguous", []):
                print(f'  歧义 {item["input"]} → 候选 {item["candidates"]}')
            for doc_id in report.get("not_found", []):
                print(f'  未找到 {doc_id}')
        else:
            _emit(report)
        if report.get("not_found"):
            return 1
        return 1 if report.get("errors") or report.get("ambiguous") else 0
    except Exception as exc:
        print(f"kb-tag failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


def cmd_kb_promote(args: argparse.Namespace) -> int:
    _utf8_stdio()
    try:
        report = promote(args.doc_ids, to_tier=args.to_tier, apply=args.apply,
                         strict=not args.no_strict)
        if args.pretty:
            lines = [
                f'[kb-promote] to_tier=L{report["to_tier"]} apply={report["apply"]} '
                f'ok={report["ok"]} 通过={len(report["promoted"])} '
                f'豁免={len(report["exempt"])} 拒绝={len(report["rejected"])}'
            ]
            for detail in report["details"]:
                tag = "OK" if detail["ok"] else "REJECT"
                extra = ""
                if detail.get("exempt"):
                    extra = " (D27 存量豁免，保持 legacy_l2)"
                elif detail.get("reason"):
                    extra = f' ({detail["reason"]})'
                lines.append(f'  [{tag}] {detail["doc_id"]}{extra}')
                for link in (detail.get("backlinks") or [])[:3]:
                    lines.append(f'         回链 {link["link"]} → {link["l3_path"]}')
                for broken in (detail.get("broken") or [])[:3]:
                    lines.append(f'         坏链 {broken.get("link") or broken.get("version")}'
                                 f' → {broken.get("reason")}')
            if report["written"]:
                lines.append(f'  已写入 status=active: {", ".join(report["written"])}')
            print("\n".join(lines))
        else:
            _emit(report)
        return 0 if report["ok"] else 1
    except ValueError as exc:
        print(f"kb-promote: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"kb-promote failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


def cmd_kb_embed(args: argparse.Namespace) -> int:
    _utf8_stdio()
    try:
        if args.status:
            data = vector_status(args.topic)
            if args.pretty:
                chunks = data["chunks"]
                print(
                    f"[kb-embed {data['topic'] or 'all'}] model={data['model_id']} "
                    f"versions={data['scanned_versions']} chunks={chunks['total']} "
                    f"current={chunks['current']} pending={chunks['pending']} stale={chunks['stale']}"
                )
            else:
                _emit(data)
            return 0
        report = vector_sync(args.topic, dry_run=args.dry_run,
                             reembed_stale=args.reembed_stale)
        if args.pretty:
            print(
                f"[kb-embed {args.topic or 'all'}] scanned={report['scanned_versions']} "
                f"skipped={report['skipped']} updated={report['updated']} "
                f"embedded={report['embedded']} deleted={report['deleted']} "
                f"stale={report['stale']} pending={report['pending']} "
                f"seconds={report['seconds']}"
            )
        else:
            _emit(report)
        return 0
    except Exception as exc:
        print(f"kb-embed failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


def register_kb_parsers(sp) -> None:
    """在 main.py 的 subparsers 后追加本命令族。"""
    kb_scan = sp.add_parser("kb-scan", help="扫描真相源并重建/更新私域知识库目录层")
    kb_scan.add_argument("--source", choices=["all", "registry", "metadata", "videos", "external"],
                         default="all", help="只扫描指定来源（默认 all）")
    kb_scan.add_argument("--force", action="store_true", help="忽略内容指纹，强制重算全部条目的 FTS")
    kb_scan.set_defaults(func=cmd_kb_scan)

    kb_inv = sp.add_parser("kb-inventory", help="知识库库存体检")
    kb_inv.add_argument("--topic", default=None, help="按 topic 过滤，如 cbac")
    kb_inv.add_argument("--tag", default=None, help="按原始 tag 过滤（M2 后为 canonical）")
    kb_inv.add_argument("--view", choices=["snapshot", "gap", "recent"], default="snapshot")
    kb_inv.add_argument("--days", type=int, default=7, help="recent 视图天数")
    kb_inv.add_argument("--pretty", action="store_true", help="输出紧凑人类可读文本")
    kb_inv.set_defaults(func=cmd_kb_inventory)

    kb_rebuild = sp.add_parser("kb-rebuild", help="删除并完整重建 kb.db")
    kb_rebuild.add_argument("--keep-vector", action="store_true", help="预留：M4 后保留向量容器")
    kb_rebuild.set_defaults(func=cmd_kb_rebuild)

    kb_ask = sp.add_parser("kb-ask", help="统一检索（确定性过滤 → 分层召回 → 配额合并）")
    kb_ask.add_argument("query", help="查询串（中文需经 jieba 分词，命令内部自动处理）")
    kb_ask.add_argument("--topic", default=None, help="主题，如 cbac / papers / ai-agent")
    kb_ask.add_argument("--tags", default=None, help="标签，逗号分隔，任一命中")
    kb_ask.add_argument("--org", default=None, help="项目线/组织标记，逗号分隔，任一命中（D32）")
    kb_ask.add_argument("--source-type", default=None, help="来源类型，逗号分隔")
    kb_ask.add_argument("--year", default=None, help="精确年份")
    kb_ask.add_argument("--tier-min", type=int, default=None, help="最低精炼档 0-3")
    kb_ask.add_argument("--tier-max", type=int, default=None, help="最高精炼档 0-3")
    kb_ask.add_argument("--stage", default=None, help="处理进度 found|downloaded|converted|translated|condensed")
    kb_ask.add_argument("--mode", choices=["browse", "balanced", "deep"], default="balanced",
                        help="配额模式：browse 我有什么 / balanced 默认 / deep 细节优先")
    kb_ask.add_argument("--top-k", type=int, default=10, help="返回条数")
    kb_ask.add_argument("--fusion", choices=["norm", "rrf", "interleave", "reserve"],
                        default=DEFAULT_FUSION,
                        help="支路融合：reserve=每档留 1 个向量保底名额（默认，M5 采用）/ "
                             "norm=级内 min-max 取 max（M3）/ rrf=倒数秩融合 / "
                             "interleave=两支路并列轮转")
    kb_ask.add_argument("--fold", choices=["max", "top3sum"], default=VECTOR_FOLD,
                        help="chunk→doc 折叠：max=最高块（默认）/ top3sum=前 3 块求和")
    kb_ask.add_argument("--toc-filter", dest="toc_filter", action="store_true", default=True,
                        help="剔除 PDF 转换出的目录残渣块（默认开，M8）")
    kb_ask.add_argument("--no-toc-filter", dest="toc_filter", action="store_false",
                        help="关闭目录残渣过滤（开/关对照用）")
    kb_ask.add_argument("--vector", action="store_true",
                        help="强制开启向量支路（默认仅 deep 模式开）")
    kb_ask.add_argument("--no-vector", action="store_true",
                        help="关闭向量支路（纯 FTS 对照）")
    kb_ask.add_argument("--pretty", action="store_true", help="输出紧凑人类可读版")
    kb_ask.set_defaults(func=cmd_kb_ask)

    kb_fetch = sp.add_parser("kb-fetch", help="取某文档某一档正文 / 段落级取证（证据回源）")
    kb_fetch.add_argument("doc_id", help="存储 ID（跨 topic 撞名时为 topic:id）或唯一裸 id")
    kb_fetch.add_argument("--tier", type=int, default=None, help="指定精炼档 0-3，缺省取最高档")
    kb_fetch.add_argument("--chunk-ids", default=None,
                          help="段落号（逗号分隔，来自 kb-ask 的 matched_chunks[].chunk_id）；"
                               "给出则只回命中块 ±context，不给整篇")
    kb_fetch.add_argument("--context", type=int, default=1, help="命中块前后各带几块（默认 1）")
    kb_fetch.add_argument("--pretty", action="store_true", help="输出头部 + 正文")
    kb_fetch.set_defaults(func=cmd_kb_fetch)

    kb_eval = sp.add_parser("kb-eval", help="检索质量评测（recall@10 / MRR@10 对旧基线）")
    kb_eval.add_argument("--set", default=None, help="评测集路径（默认 knowledgebase/_meta/eval/eval_v1.jsonl）")
    kb_eval.add_argument("--top-k", type=int, default=10, help="评测截断")
    kb_eval.add_argument("--refresh-baseline", action="store_true", help="强制重采旧系统基线")
    kb_eval.add_argument("--pretty", action="store_true", help="输出逐条对比表")
    kb_eval.set_defaults(func=cmd_kb_eval)

    kb_promote = sp.add_parser("kb-promote", help="L2/L3 晋升校验（D27：L2 必须带回链）")
    kb_promote.add_argument("doc_ids", nargs="+", help="一个或多个存储 ID")
    kb_promote.add_argument("--to-tier", type=int, choices=[2, 3], default=2,
                            help="目标档位：2=精炼层（须回链）/ 3=原文层（须有 L3 版本）")
    kb_promote.add_argument("--apply", action="store_true",
                            help="通过后落库（把 status 置 active）；缺省只校验")
    kb_promote.add_argument("--no-strict", action="store_true",
                            help="允许部分通过时写入通过者（默认任一失败即不写）")
    kb_promote.add_argument("--pretty", action="store_true", help="输出紧凑人类可读摘要")
    kb_promote.set_defaults(func=cmd_kb_promote)

    kb_embed = sp.add_parser("kb-embed", help="chunk manifest 增量向量（M4）")
    kb_embed.add_argument("--topic", default=None, help="只同步指定 topic（缺省全库）")
    kb_embed.add_argument("--dry-run", action="store_true", help="只计算增量计划，不嵌入、不写库")
    kb_embed.add_argument("--status", action="store_true", help="只读盘点 chunks/容器状态")
    kb_embed.add_argument("--reembed-stale", action="store_true",
                          help="重算 model_id 不符的旧块（默认惰性保留）")
    kb_embed.add_argument("--pretty", action="store_true", help="输出紧凑人类可读摘要")
    kb_embed.set_defaults(func=cmd_kb_embed)

    kb_tags = sp.add_parser("kb-tags", help="知识库受控词表统计、草案生成与安全清洗")
    tags_sub = kb_tags.add_subparsers(dest="action", required=True)
    for action in ("stats", "draft", "clean"):
        sub = tags_sub.add_parser(action)
        sub.add_argument("--topic", required=True, help="主题，如 cbac")
        if action == "draft":
            sub.add_argument("--top-k", type=int, default=60, help="知识 tag 草案最多候选数")
            sub.add_argument("--min-count", type=int, default=2, help="知识 tag 草案最低频次")
        if action == "clean":
            sub.add_argument("--apply", action="store_true", help="执行写回；缺省 dry-run")
        sub.add_argument("--pretty", action="store_true", help="输出紧凑人类可读摘要")
        sub.set_defaults(action=action)
    kb_tags.set_defaults(func=cmd_kb_tags)

    kb_batch = sp.add_parser("kb-batch", help="批次（候选区）生命周期：open/add/confirm/list（M6）")
    batch_sub = kb_batch.add_subparsers(dest="action", required=True)
    batch_open = batch_sub.add_parser("open", help="开一个批次（round_id 缺省自动生成）")
    batch_open.add_argument("--round-id", default=None, help="批次号（缺省自动生成）")
    batch_open.add_argument("--topic", default=None, help="预留的主归属主题")

    batch_add = batch_sub.add_parser("add", help="把 doc_id 加入候选区")
    batch_add.add_argument("round_id", help="批次号")
    batch_add.add_argument("doc_ids", nargs="+", help="一个或多个存储 ID")

    batch_confirm = batch_sub.add_parser("confirm", help="一次性确认：写 topic + 按 topic 打 tags")
    batch_confirm.add_argument("round_id", help="批次号")
    batch_confirm.add_argument("--topic", required=True, help="主归属主题（必填，D24）")
    batch_confirm.add_argument("--tags", default=None, help="本批统一标签，逗号分隔")

    batch_list = batch_sub.add_parser("list", help="列批次")
    batch_list.add_argument("--status", choices=["open", "confirmed", "cancelled"], default=None)

    for sub in (batch_open, batch_add, batch_confirm, batch_list):
        sub.add_argument("--pretty", action="store_true", help="输出紧凑人类可读摘要")
        sub.set_defaults(func=cmd_kb_batch)
    kb_batch.set_defaults(func=cmd_kb_batch)

    kb_tag = sp.add_parser("kb-tag", help="打标（M6：只写 kb.db 词表，不改 md 文件）")
    kb_tag.add_argument("doc_ids", help="存储 ID，逗号分隔")
    kb_tag.add_argument("--add", default=None, help="要加的标签，逗号分隔（按 topic 词表归一）")
    kb_tag.add_argument("--remove", default=None, help="要删的标签，逗号分隔")
    kb_tag.add_argument("--pretty", action="store_true", help="输出紧凑人类可读摘要")
    kb_tag.set_defaults(func=cmd_kb_tag)
