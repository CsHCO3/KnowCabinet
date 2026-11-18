"""qq CLI：只读体检默认开放；导出等写操作需显式确认。"""

from __future__ import annotations

import argparse
import json
import sys


def _utf8():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and (stream.encoding or "").lower() != "utf-8":
            stream.reconfigure(encoding="utf-8", errors="replace")


def _print_result(result, raw: bool = True):
    _utf8()
    if raw:
        print(result.output, end="" if result.output.endswith("\n") else "\n")
        print(f"[exit={result.exit_code}{' timeout' if result.timeout else ''}]")
    else:
        print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))


def cmd_status(args):
    from backend.services.community.qq.service import QQPipelineService
    result = QQPipelineService().export_status()
    _print_result(result, raw=not args.json)
    return result.exit_code


def cmd_dry_run(args):
    from backend.services.community.qq.service import QQPipelineService
    result = QQPipelineService().export_dry_run(only=args.only, overlap=args.overlap)
    _print_result(result, raw=not args.json)
    return result.exit_code


def cmd_export(args):
    if not args.confirm:
        print("这是写操作：会调用 QCE 导出并可能写入 QQ 归档。请加 --confirm。")
        return 2
    from backend.services.community.qq.service import QQPipelineService
    result = QQPipelineService().export(
        launch=args.launch,
        import_to_archive=not args.no_import,
        only=args.only,
        overlap=args.overlap,
        full=args.full,
    )
    return result.exit_code


def cmd_archive_list(args):
    from backend.services.community.qq.service import QQPipelineService
    result = QQPipelineService().archive_list()
    _print_result(result, raw=not args.json)
    return result.exit_code


def cmd_plan(args):
    from backend.services.community.qq.service import QQPipelineService
    result = QQPipelineService().analyze_plan(args.group)
    _print_result(result, raw=not args.json)
    return result.exit_code


def cmd_analyze_status(args):
    from backend.services.community.qq.service import QQPipelineService
    result = QQPipelineService().analyze_status(args.group)
    _print_result(result, raw=not args.json)
    return result.exit_code


def cmd_kb_check(args):
    from backend.services.community.qq.service import QQPipelineService
    result = QQPipelineService().kb_check(args.group)
    _print_result(result, raw=not args.json)
    return result.exit_code


def cmd_verify(args):
    from backend.services.community.qq.service import QQPipelineService
    result = QQPipelineService().verify(args.group or None, data_root=args.data_root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["all_ok"] else 1


def cmd_prune_missing(args):
    from backend.services.community.qq.service import QQPipelineService
    if args.execute and not args.confirm:
        print("prune-missing 真跑会写归档状态；请加 --confirm。")
        return 2
    result = QQPipelineService().prune_missing(
        args.group,
        batch=args.batch,
        dry_run=not args.execute,
        note=args.note,
    )
    _print_result(result, raw=not args.json)
    return result.exit_code


def cmd_analyze_run(args):
    from backend.services.community.qq.service import QQPipelineService
    if args.execute and not args.confirm:
        print("analyze run 会调用 LLM；请加 --confirm。")
        return 2
    result = QQPipelineService().analyze_run(
        args.group,
        limit=args.limit,
        only=args.only,
        dry_run=not args.execute,
        model=args.model,
        prompt=args.prompt,
        no_media_check=args.no_media_check,
    )
    _print_result(result, raw=not args.json)
    return result.exit_code


def cmd_kb_add(args):
    from backend.services.community.qq.service import QQPipelineService
    if args.execute and not args.confirm:
        print("kb add 会调用 LLM；请加 --confirm。")
        return 2
    result = QQPipelineService().kb_add(
        args.group,
        dry_run=not args.execute,
        model=args.model,
        per_call=args.per_call,
        batches=args.batches,
        rebuild=args.rebuild,
    )
    _print_result(result, raw=not args.json)
    return result.exit_code


def register_parser(sp: argparse._SubParsersAction) -> None:
    parser = sp.add_parser("qq", help="QQ 导出、归档体检与知识管线封装")
    sub = parser.add_subparsers(dest="action", required=True)

    status = sub.add_parser("export-status", help="QCE/NapCat 状态与每群下次导出窗口（只读）")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    dry = sub.add_parser("export-dry-run", help="显示导出计划，不创建任务（只读）")
    dry.add_argument("--only")
    dry.add_argument("--overlap", type=float)
    dry.add_argument("--json", action="store_true")
    dry.set_defaults(func=cmd_dry_run)

    export = sub.add_parser("export", help="真跑 QCE 导出并可选入库；必须加 --confirm")
    export.add_argument("--confirm", action="store_true")
    export.add_argument("--launch", action="store_true", help="QQ 没开时拉起并快速登录")
    export.add_argument("--no-import", action="store_true", help="只导出，不调 archive import")
    export.add_argument("--only")
    export.add_argument("--overlap", type=float)
    export.add_argument("--full", action="store_true")
    export.set_defaults(func=cmd_export)

    archive = sub.add_parser("archive-list", help="读取归档台账（只读）")
    archive.add_argument("--json", action="store_true")
    archive.set_defaults(func=cmd_archive_list)

    plan = sub.add_parser("plan", help="切批预览（只读）")
    plan.add_argument("group")
    plan.add_argument("--json", action="store_true")
    plan.set_defaults(func=cmd_plan)

    analyze = sub.add_parser("analyze-status", help="提炼进度")
    analyze.add_argument("group")
    analyze.add_argument("--json", action="store_true")
    analyze.set_defaults(func=cmd_analyze_status)

    kb = sub.add_parser("kb-check", help="知识库来源记号对齐检查")
    kb.add_argument("group")
    kb.add_argument("--json", action="store_true")
    kb.set_defaults(func=cmd_kb_check)

    verify = sub.add_parser("verify", help="运行集成接口五条判据")
    verify.add_argument("--group", action="append", help="可重复；缺省检查两个已配置群")
    verify.add_argument("--data-root", help="临时指定档案数据根；用于迁移对比验收")
    verify.set_defaults(func=cmd_verify)

    prune = sub.add_parser("prune-missing", help="媒体对账；默认 dry-run")
    prune.add_argument("group")
    prune.add_argument("--batch")
    prune.add_argument("--note")
    prune.add_argument("--execute", action="store_true", help="默认只 dry-run；真跑还需 --confirm")
    prune.add_argument("--confirm", action="store_true")
    prune.add_argument("--json", action="store_true")
    prune.set_defaults(func=cmd_prune_missing)

    run = sub.add_parser("analyze-run", help="逐批提炼；默认 dry-run，真跑会花钱")
    run.add_argument("group")
    run.add_argument("--limit", type=int)
    run.add_argument("--only")
    run.add_argument("--model")
    run.add_argument("--prompt")
    run.add_argument("--no-media-check", action="store_true", help="明确跳过对账闸门；不建议")
    run.add_argument("--execute", action="store_true", help="默认只生成输入；真跑还需 --confirm")
    run.add_argument("--confirm", action="store_true")
    run.add_argument("--json", action="store_true")
    run.set_defaults(func=cmd_analyze_run)

    kbadd = sub.add_parser("kb-add", help="跨批合成；默认 dry-run，真跑会花钱")
    kbadd.add_argument("group")
    kbadd.add_argument("--model")
    kbadd.add_argument("--per-call", type=int)
    kbadd.add_argument("--batches")
    kbadd.add_argument("--rebuild", action="store_true", help="重建而非追加；先自动快照")
    kbadd.add_argument("--execute", action="store_true", help="默认 dry-run；真跑还需 --confirm")
    kbadd.add_argument("--confirm", action="store_true")
    kbadd.add_argument("--json", action="store_true")
    kbadd.set_defaults(func=cmd_kb_add)
