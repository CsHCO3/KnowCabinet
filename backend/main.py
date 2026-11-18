#!/usr/bin/env python3
"""信息处理系统 — CLI 入口。

用法:
    python -m backend.main bilibili  --url https://www.bilibili.com/video/BVxxx
    python -m backend.main bilibili  --input ./subtitles
    python -m backend.main arxiv     --input ./data/arxiv_papers.json --source-format json
"""

# ── 路径引导：向上查找项目根目录（CLAUDE.md / README.md 为标记）──
import sys
from pathlib import Path
_root = Path(__file__).resolve()
for _p in _root.parents:
    if (_p / "CLAUDE.md").exists() or (_p / "README.md").exists():
        _root = _p; break
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
# ─────────────────────────────────────────────────────

import argparse
import os
import re

from backend.config.settings import Settings
from backend.services.container import ServiceContainer
from backend.services.arxiv.service import ArxivService
from backend.services.bilibili.service import BilibiliService
from backend.services.remote_fs.service import RemoteFSService
from backend.utils.paths import Paths


def _build_container(settings: Settings) -> ServiceContainer:
    c = ServiceContainer()
    c.register("arxiv", lambda: ArxivService())
    c.register("bilibili", lambda: BilibiliService())
    c.register("remote_fs", lambda: RemoteFSService())
    return c


def cmd_bilibili(args: argparse.Namespace) -> int:
    print("✗ `bilibili` 已下线（2026-11-04）。"
          "单视频解析改用 `bili-visual-scan BVxxx`（字幕+分段+画面+浓缩+入库一站跑）。")
    return 1


def cmd_bili_export(args: argparse.Namespace) -> int:
    print("✗ `bili-export` 已下线（2026-11-04）。"
          "收藏夹导出改用 `bili-survey-stage1 --fav-id <id>`（直接出 JSON，无需 CSV 中转）。")
    return 1


def cmd_bili_analyze(args: argparse.Namespace) -> int:
    print("✗ `bili-analyze` 已下线（2026-11-04）。"
          "选片用 `bili-find 关键词`，深度解析用 `bili-visual-scan`。")
    return 1


def cmd_bili_find(args: argparse.Namespace) -> int:
    """在只有元信息的视频库上检索，挑出要深度解析的那几条。"""
    from backend.services.bilibili.find import search

    rows = search(args.query, limit=args.limit, min_view=args.min_view, sort=args.sort)
    if not rows:
        print(f"没有命中：{args.query}")
        print("提示：多个关键词之间是「都要命中」，可以先只留一个词试试")
        return 1
    if args.bvid_only:
        for r in rows:
            print(r["bvid"])
        return 0
    for i, r in enumerate(rows, 1):
        dur = r["duration"]
        print(f"{i:2d}. {r['bvid']}  {r['title'][:44]}")
        print(f"    {r['author'] or '?'} · {r['view']} 播放 · "
              f"{dur // 60}分{dur % 60:02d}秒 · 命中 {'/'.join(r['where'])}")
    print(f"\n共 {len(rows)} 条。挑好后直接跑：")
    print(f"  python -m backend.main bili-visual-scan {' '.join(x['bvid'] for x in rows[:3])}")
    print("或整条管道（全部候选）：")
    print(f'  python -m backend.main bili-find "{args.query}" --limit {args.limit} --bvid-only'
          " | python -m backend.main bili-visual-scan --from-stdin")
    return 0


def cmd_bili_visual_scan(args: argparse.Namespace) -> int:
    """画面要素提取一站跑：字幕→分段→扫帧→看图浓缩 digest.md→落库。"""
    from collections import Counter

    from backend.services.bilibili.visual.paths import VISUAL_ROOT
    from backend.services.bilibili.visual.runner import parse_bvid_entry, read_bvids_from_stdin, run_batch

    def _fmt(ent: tuple[str, int]) -> str:
        bv, pg = ent
        return f"{bv}?p={pg}" if pg > 1 else bv

    entries: list[tuple[str, int]] = []
    for x in list(args.bvids) + (read_bvids_from_stdin() if args.from_stdin else []):
        ent = parse_bvid_entry(x) if isinstance(x, str) else x
        if ent and ent not in entries:
            entries.append(ent)
    if args.list_only:
        print(f"待处理 {len(entries)} 条：" + " ".join(_fmt(e) for e in entries))
        return 0
    if not entries:
        print("需要 --bvids 或 --from-stdin（可配合 bili-find --bvid-only 管道）")
        return 1
    if args.scan_mode == "off" and args.force_scan:
        print("注意：--scan off 优先于 --force-scan，本次一条都不扫")
    if args.scan_mode == "off" and args.asr_fallback:
        print("注意：--scan off 时也不触发 ASR 兜底（保持纯字幕语义），缺字幕就缺着")
    out = run_batch(entries, args.thr, args.model, args.start, args.keep_temps,
                    args.force_scan, args.probe, args.no_index, args.backend_python,
                    args.min_sub_cover, args.scan_mode, not args.no_probe_veto,
                    asr_fallback=args.asr_fallback, asr_model=args.asr_model,
                    asr_compute=args.asr_compute)
    ok = sum(1 for r in out if (r or {}).get("ok"))
    tally = Counter((r or {}).get("status") or ("ok" if (r or {}).get("ok") else "error")
                    for r in out)
    stat = " ".join(f"{k}={v}" for k, v in sorted(tally.items()))
    print(f"\n完成 {ok}/{len(out)}（{stat}）")
    print(f"明细：{VISUAL_ROOT / '_runs.jsonl'}")
    return 0 if ok else 1


def cmd_bili_visual_apply(args: argparse.Namespace) -> int:
    """把 visual/<bvid>/digest.md 落进知识库（bili.db 标注 + dense md + 截图）。"""
    from backend.services.bilibili.visual import apply_kb as AK
    from backend.services.bilibili.visual.paths import VISUAL_ROOT

    bvids = list(args.bvids)
    if args.all:
        bvids = sorted(pp.parent.name for pp in VISUAL_ROOT.glob("*/digest.md"))
    if not bvids:
        print(f"没有可落的：给 --bvids，或用 --all（扫 {VISUAL_ROOT}/<bvid>/digest.md）")
        return 1
    ok = 0
    for bv in bvids:
        try:
            if AK.apply_one(bv, dry=args.dry, force=not args.no_force):
                ok += 1
        except Exception as e:
            print(f"{bv}: 失败 {str(e)[:120]}")
    print(f"\n落库 {ok}/{len(bvids)}")
    return 0



def cmd_bili_info(args: argparse.Namespace) -> int:
    """单视频速查：元信息 + 字幕概况（不下载视频，不调 LLM）。"""
    import json as _json

    from backend.services.bilibili.find import info

    r = info(args.bvid.strip())   # ?p=N 原样透传给字幕层
    if args.json:
        print(_json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r.get("ok") else 1
    print(f"{r['bvid']}  {r['title']}")
    if not r.get("ok"):
        print(f"  ✗ {r.get('error')}")
    print(f"  作者：{r.get('author') or '?'}   时长：{r.get('duration')}s")
    if r.get("page_count", 1) > 1:
        parts = " · ".join(
            f"P{i + 1}「{pp.get('part', '')}」{pp.get('duration', '?')}s"
            for i, pp in enumerate(r.get("pages") or [])
        )
        pt = r.get("page_title") or ""
        print(f"  分P：第 {r.get('page', 1)}P「{pt}」（全部：{parts}）")
    st = r.get("stat") or {}
    print(f"  播放 {st.get('view')} · 点赞 {st.get('like')} · 收藏 {st.get('favorite')}")
    sub = r.get("subtitle") or {}
    if sub:
        print(f"  字幕：{sub['segments']} 段，覆盖 {sub['covers']}s")
        for line in sub.get("header", []):
            print(f"    | {line[:50]}")
    else:
        print("  字幕：无")
    if r.get("desc"):
        print(f"  简介：{r['desc'][:160]}")
    return 0


def cmd_bili_survey_stage1(args: argparse.Namespace) -> int:
    """Stage 1: 作品-作者级信息收集。"""
    from backend.services.bilibili.survey_pipeline import (
        export_favorites_to_json,
        collect_collection_videos,
        collect_author_top_videos,
        collect_from_video_url,
    )

    output_dir = args.output
    results = []

    # 模式1: 收藏夹导出
    if args.fav_id is not None:
        print(f"[Stage 1] 导出收藏夹 {args.fav_id}...")
        path = export_favorites_to_json(args.fav_id, output_dir, max_items=args.max_items)
        print(f"  → {path}")
        results.append(str(path))

    # 模式2: 视频 URL → 合集 + 作者
    if args.video_url:
        print(f"[Stage 1] 从视频收集: {args.video_url}...")
        bundle = collect_from_video_url(
            args.video_url, output_dir,
            author_order=args.order, author_max_pages=args.max_pages,
        )
        if bundle["collection_path"]:
            print(f"  → 合集: {bundle['collection_path']}")
            results.append(str(bundle["collection_path"]))
        else:
            print(f"  → 视频不在任何合集中")
        if bundle["author_path"]:
            print(f"  → 作者 ({bundle['author_name']}): {bundle['author_path']}")
            results.append(str(bundle["author_path"]))
        else:
            print(f"  → 未找到作者信息")

    # 模式3: 作者 MID → 批量视频
    if args.mid is not None:
        print(f"[Stage 1] 收集作者 {args.mid} ({args.order})...")
        path = collect_author_top_videos(args.mid, output_dir, order=args.order, max_pages=args.max_pages)
        print(f"  → {path}")
        results.append(str(path))

    if results:
        print(f"\n[Stage 1] 完成，产出 {len(results)} 个文件")
        for r in results:
            print(f"  {r}")
    else:
        print("[Stage 1] 未执行任何收集操作（请指定 --fav-id / --video-url / --mid）")
    return 0


def cmd_bili_survey_stage2(args: argparse.Namespace) -> int:
    """Stage 2: 简介级收集 + 评分。"""
    from backend.services.bilibili.survey_pipeline import (
        enrich_videos_from_file,
        score_videos_from_file,
    )

    # 1. 先 enrich
    print(f"[Stage 2] 丰富视频信息: {args.input}...")
    enriched_path = enrich_videos_from_file(args.input)
    print(f"  → {enriched_path}")

    # 2. 如果 --enrich-only 则停在这里
    if args.enrich_only:
        print("[Stage 2] 仅丰富信息模式，完成")
        return 0

    # 3. 评分（必须提供 --profile）
    if not args.profile:
        print("[Stage 2] 跳过评分（未提供 --profile）")
        print("  提示: 如需评分，请加上 --profile path/to/video_scorer_profile.txt")
        return 0

    # 创建 LLM 客户端
    from backend.config.settings import Settings
    settings = Settings()
    llm = settings.get_llm_client(model_key=args.model)

    output = args.output if args.output else None
    sp = args.system_prompt if args.system_prompt else None

    print(f"[Stage 2] 评分排序 (model={args.model})...")
    result_path = score_videos_from_file(
        enriched_path,
        output_path=output,
        llm=llm,
        profile_path=args.profile,
        system_prompt_path=sp,
        relevance_weight=args.relevance_weight,
        popularity_weight=args.popularity_weight,
    )
    print(f"  → {result_path}")
    return 0


def cmd_bili_survey_stage3(args: argparse.Namespace) -> int:
    print("✗ `bili-survey-stage3` 已下线（2026-11-04）。"
          "提字幕已并入 `bili-visual-scan`（抓字幕+分段+画面+浓缩）。")
    return 1


def cmd_bili_annotate(args: argparse.Namespace) -> int:
    """给已摘要但未标注的视频补 分类/仓库链接（bili.db 的 category/tags/repo_urls）。"""
    from api.services.bilibili_service import annotate_missing

    print("标注模型: glm-flash（智谱）")
    r = annotate_missing(limit=args.limit)
    print(f"\n标注完成: 成功 {r['annotated']} / 失败 {r['failed']} / 待标注 {r['total']}")
    return 0 if r["failed"] == 0 else 1


def cmd_bili_unfav(args: argparse.Namespace) -> int:
    """把已经解析完成、进了知识库的视频从收藏夹里删掉（默认只演练）。"""
    import json as _json

    from backend.services.bilibili import favorites as F
    from backend.services.bilibili.visual.paths import VISUAL_ROOT

    bvids = [x.strip() for x in args.bvids if x.strip()]
    if args.from_runs:
        p = VISUAL_ROOT / "_runs.jsonl"
        if not p.exists():
            print("没有 _runs.jsonl")
            return 1
        seen: list[str] = []
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                d = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            bv = d.get("bvid") or ""
            if (d.get("status") == "ok" and d.get("kb_ok")
                    and bv and bv not in seen and "_p" not in bv):
                seen.append(bv)
        bvids = seen
    if not bvids:
        print("没有要处理的 BV 号（给 bvids 或 --from-runs）")
        return 1
    print(f"{'演练' if not args.apply else '执行'}：从收藏夹 {args.fav_id} 移除 {len(bvids)} 条")
    res = F.remove_videos(args.fav_id, bvids, dry_run=not args.apply)
    ok = sum(1 for r in res if r["ok"])
    for r in res:
        if not r["ok"]:
            print(f"  ✗ {r['bvid']} aid={r['aid']} {r['msg']}")
    print(f"完成 {ok}/{len(res)}")
    return 0 if ok == len(res) else 1


def cmd_bili_asr(args: argparse.Namespace) -> int:
    """本地 ASR 单跑转写：只产字幕缓存，不跑后续流程（补历史/调试/兜底自检用）。

    与 bili-visual-scan 的 --asr-fallback 共用同一个 transcribe()，不存在第二条转写路径。
    """
    import json as _json

    from backend.services.bilibili import asr
    from backend.services.bilibili.visual.runner import parse_bvid_entry

    entries = []
    for x in args.bvids:
        ent = parse_bvid_entry(x) if isinstance(x, str) else x
        if ent and ent not in entries:
            entries.append(ent)
    if not entries:
        print("需要至少一个 BV 号（支持 BVxxx?p=2 指定分P）")
        return 1
    results = []
    failed = 0
    for bv, page in entries:
        r = asr.transcribe(bv, page, model=args.model, compute=args.compute,
                           force=args.force)
        results.append(r)
        if not r.get("ok"):
            failed += 1
            if not args.json:
                print(f"{bv}  P{page}  ✗ {r.get('reason')}：{r.get('error', '')}")
            continue
        if not args.json:
            tag = "无语音（缓存已写 no_speech）" if r.get("no_speech") else "缓存已写"
            reuse = "（命中缓存未重跑）" if r.get("reused") else ""
            print(f"{bv}  P{page}  segments={r.get('segments')}  chars={r.get('chars')}"
                  f"  用时 {r.get('elapsed_s', 0) / 60:.1f}min  → {tag}{reuse}")
    if args.json:
        print(_json.dumps(results, ensure_ascii=False, indent=1))
    # 无语音算成功（合法结论），退出码只看真失败（INTERFACE §8）
    return 1 if failed else 0


def cmd_bili_status(args: argparse.Namespace) -> int:
    """一条命令看清视频库进度：线索 → 已入库 / 跳过 / 待处理。"""
    import json as _json

    from backend.services.bilibili import status as S

    r = S.build_report(limit=args.limit, pending_limit=args.pending)
    if args.json:
        print(_json.dumps(r, ensure_ascii=False, indent=1))
        return 0
    print(S.format_report(r))
    return 0


def cmd_bili_backfill(args: argparse.Namespace) -> int:
    """批量回刷：把"试过但没进库"的视频用 ASR 兜底重跑（串行、逐条超时、断点续跑）。

    默认不碰 no_speech（无解说的结论）和内容判定类失败，免得白烧 GPU。
    """
    from backend.services.bilibili import backfill

    out = backfill.run(only=args.only or None, retry_failed=args.retry_failed,
                       timeout=args.timeout, no_index=args.no_index,
                       python_exe=args.backend_python, dry=args.dry)
    if args.dry:
        return 0
    ok = sum(1 for r in out if r.get("exit") == 0)
    print(f"\n回刷完成：{ok}/{len(out)} 条子进程退出码 0")
    return 0 if ok == len(out) else 0   # 单条失败不算整批失败，账在 _runs.jsonl


def cmd_bili_coverage(args: argparse.Namespace) -> int:
    """收藏视频终态对账：已入库 / 隔离 / 未入库原因 / 长视频未跑，一条命令看全。"""
    import json as _json
    from pathlib import Path as _Path

    from backend.services.bilibili import coverage

    out = _Path(args.out) if args.out else None
    r = coverage.build(csv_path=_Path(args.csv) if args.csv else coverage.FAV_CSV,
                       out_path=out, include_long=args.all)
    if args.json:
        print(_json.dumps({"total": r["total"], "buckets": r["buckets"]},
                          ensure_ascii=False, indent=1))
        return 0
    print(f"收藏 {r['total']} 条：")
    for k, v in sorted(r["buckets"].items(), key=lambda kv: -kv[1]):
        print(f"  {k}: {v}")
    if out:
        print(f"\n明细 -> {out}")
    return 0


def cmd_bili_author_mine(args: argparse.Namespace) -> int:
    """按分类聚合作者，产出宝藏作者挖掘报告（md + json）。"""
    from backend.services.bilibili.author_mining import mine_authors

    path = mine_authors(args.output or None)
    print(f"报告: {path}")
    print(f"JSON: {path.with_suffix('.json')}")
    return 0


def cmd_bili_author_scan(args: argparse.Namespace) -> int:
    """从收藏锚点扫作者发布的视频：计算机类>阈值 → AI 确认领域 → 注册表。"""
    from backend.services.bilibili.author_scan import scan_authors_from_favorites

    mids = [int(x) for x in args.mids.split(",") if x.strip()] if args.mids else None
    md = scan_authors_from_favorites(
        fav_id=args.fav_id,
        anchors=args.anchors,
        mids=mids,
        threshold=args.threshold,
        max_pages=args.max_pages,
        top_n=args.top,
        batch_size=args.batch,
        output=args.output or None,
    )
    print(f"注册表: {md}")
    return 0


def cmd_arxiv(args: argparse.Namespace) -> int:
    from backend.pipelines.arxiv_value import run_arxiv_value_pipeline

    settings = Settings()

    profile_text = args.user_profile or ""
    if args.user_profile_file:
        pp = Path(args.user_profile_file)
        if pp.exists():
            profile_text = pp.read_text(encoding="utf-8").strip()

    report_path = run_arxiv_value_pipeline(
        input_file=args.input,
        source_format=args.source_format,
        output_dir=args.output,
        workers=args.workers,
        llm=settings.get_llm_client(model_key=args.model),
        user_profile=profile_text,
        item_template=args.item_template,
        batch_template=args.batch_template,
    )
    print(f"报告已生成: {report_path}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    """arXiv 搜索，结果按 arXiv 自身相关性排序。

    --auto-submit: 自动将排名第一的论文下载并提交到远程 PDF 转换服务。
    目前排序完全依赖 arXiv API 的 relevance 排序，不涉及 LLM 打分。
    """
    from backend.services.arxiv.search import search_arxiv, ArxivSearchParams, export_results_json, export_results_markdown, download_paper_from_source
    from backend.utils.paths import Paths

    paths = Paths()

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
    for p in papers:
        print(f"  - {p.title} ({p.arxiv_id})")

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.export_json:
        out_path = str(out_dir / "arxiv_search_results.json")
        export_results_json(papers, out_path)
        print(f"JSON 已导出: {out_path}")
    if args.export_md:
        out_path = str(out_dir / "arxiv_search_results.md")
        export_results_markdown(papers, out_path)
        print(f"Markdown 已导出: {out_path}")

    # ── auto-submit: 下载 top1 并提交远程转换 ──
    if args.auto_submit and papers:
        best = papers[0]
        print(f"\n自动下载并提交: {best.title}")
        paths.pdfs.mkdir(parents=True, exist_ok=True)
        pdf_path = download_paper_from_source(
            source=best.arxiv_id,
            output_dir=str(paths.pdfs),
            title_hint=best.title[:60],
        )
        print(f"  已下载: {pdf_path}")

        from backend.services.pdf_remote.service import RemotePDFService
        svc = RemotePDFService()
        result = svc.submit_batch(file_list=[pdf_path])
        if result.get("success"):
            print(f"  已提交远程转换, PID: {result.get('remote_pid', 'N/A')}")
        else:
            print(f"  提交失败: {result.get('error', 'unknown')}")

    return 0


def cmd_pdf(args: argparse.Namespace) -> int:
    """提交远程 PDF 批量转换任务。

    默认上传 pdfs/ 目录下的所有 PDF；用 --files 指定具体文件。
    """
    from backend.services.pdf_remote.service import RemotePDFService

    svc = RemotePDFService()
    result = svc.submit_batch(
        file_list=args.files,
        translate_requested=getattr(args, "translate", False),
    )

    if result.get("success"):
        print("远程转换任务已提交")
        print(f"  Job ID: {result.get('job_id', 'N/A')}")
        print(f"  远端 PID: {result.get('remote_pid', 'N/A')}")
        print(f"  日志文件: {result.get('remote_log_file', 'N/A')}")
        print(f"  上传文件数: {result.get('uploaded_count', 0)}")
        if getattr(args, "translate", False):
            print("  [已标记] 转换完成后可运行 `python -m backend.main translate --auto` 触发翻译")
        print("稍后运行 `python -m backend.main warmup` 检查并拉取结果")
    else:
        print("提交失败")
        for step in result.get("steps", []):
            if step.get("status") == "failed":
                print(f"  [{step.get('step', '?')}] 失败: {step.get('error', 'unknown')}")
    return 0 if result.get("success") else 1


def cmd_mineru(args: argparse.Namespace) -> int:
    """提交远程 mineru 转换任务(pdf/pptx/docx/xlsx/图片 → Markdown)。

    文件必须显式指定(--files 或 --dir)；产物经 `warmup` 拉回解包。
    """
    import os as _os
    from backend.services.pdf_remote.service import RemotePDFService
    from backend.services.pdf_remote.parser import MINERU_SUPPORTED_EXTS

    files = list(args.files) if args.files else []

    # --dir：扫描本地目录下所有支持格式文件
    if args.dir:
        d = args.dir
        if not _os.path.isdir(d):
            print(f"错误：目录不存在 {d}")
            return 1
        files += [
            _os.path.join(d, name) for name in _os.listdir(d)
            if _os.path.isfile(_os.path.join(d, name))
            and _os.path.splitext(name)[1].lower() in MINERU_SUPPORTED_EXTS
        ]
    files = list(dict.fromkeys(files))  # 去重，保留顺序

    if not files:
        print("错误：请用 --files 指定文件（或 --dir 指定目录）。支持 pdf/pptx/docx/xlsx/图片")
        return 1

    svc = RemotePDFService()
    result = svc.submit_mineru(
        file_list=files,
        method=args.method,
        lang=args.lang,
        backend=args.backend,
        formula=args.formula,
        table=args.table,
    )

    if result.get("success"):
        print("mineru 转换任务已提交")
        print(f"  Job ID: {result.get('job_id', 'N/A')}")
        print(f"  远端 PID: {result.get('remote_pid', 'N/A')}")
        print(f"  日志文件: {result.get('remote_log_file', 'N/A')}")
        print(f"  上传文件数: {result.get('uploaded_count', 0)}")
        print(f"  远端输入: {result.get('remote_input_dir')}")
        print(f"  远端输出: {result.get('remote_output_dir')}")
        print("稍后运行 `python -m backend.main warmup` 检查并拉取结果")
    else:
        print("提交失败")
        for step in result.get("steps", []):
            if step.get("status") == "failed":
                print(f"  [{step.get('step', '?')}] 失败: {step.get('error', 'unknown')}")
    return 0 if result.get("success") else 1


def cmd_reconcile(args: argparse.Namespace) -> int:
    """盘点整理远程任务旧账：归档已转源文件、删除孤儿记录。"""
    from backend.services.pdf_remote.reconcile import run_reconcile

    report = run_reconcile()
    print("远程任务盘点整理完成")
    if report["backup"]:
        print(f"  备份: {report['backup']}")
    print(f"  搬走源文件: {len(report['moved_sources'])}")
    for m in report["moved_sources"]:
        if "error" in m:
            print(f"    [job {m['job_id']}] 搬移失败 {m['from']}: {m['error']}")
        else:
            print(f"    [job {m['job_id']}] {m['from']} → {m['to']}")
    print(f"  删除孤儿记录: {len(report['deleted_orphans'])}")
    for o in report["deleted_orphans"]:
        print(f"    {o['id']} ({', '.join(o['files']) or '无文件'})")
    print(f"  保留记录: {report['kept']}")
    return 0


def cmd_condense(args: argparse.Namespace) -> int:
    """浓缩已转换的论文 md → knowledgebase/papers/{arxiv_id}/condensation.md。

    来源：--survey <task_id>（浓缩该调研的 convert 产物）或 --dir <md目录>。
    """
    from backend.services.condense.service import condense_directory, condense_paper

    model = args.model or "deepseek-chat"
    if args.survey:
        import json
        from backend.path_setup import PROJECT_ROOT as _ROOT
        tasks_path = _ROOT / ".data" / "tasks.json"
        if not tasks_path.exists():
            print(f"任务记录不存在: {tasks_path}")
            return 1
        tasks = json.loads(tasks_path.read_text(encoding="utf-8"))
        task = tasks.get(args.survey)
        if not task:
            print(f"survey 任务不存在: {args.survey}")
            return 1
        job_id = None
        if task.get("result") and task["result"].get("convert_job_id"):
            job_id = task["result"]["convert_job_id"]
        if not job_id:
            for s in task.get("stages", []):
                if s.get("key") == "convert" and s.get("remote_job_id"):
                    job_id = s["remote_job_id"]
                    break
        if not job_id:
            print("该调研没有已转换的产物（无 convert job）")
            return 1
        md_dir = str(_ROOT / "data" / "remote_pdf" / job_id)
        print(f"浓缩目录: {md_dir}")
        results = condense_directory(md_dir, model)
    else:
        if not args.dir:
            print("需要 --survey <task_id> 或 --dir <md目录>")
            return 1
        md_dir = args.dir
        results = condense_directory(md_dir, model)

    for r in results:
        if r.get("ok"):
            print(f"  ✅ {r['arxiv_id']} -> {r['condensation_path']} ({r['chars']} 字)")
        else:
            print(f"  ❌ {r.get('file')}: {r.get('error')}")
    ok = sum(1 for r in results if r.get("ok"))
    print(f"\n浓缩完成: {ok}/{len(results)}")
    return 0 if ok == len(results) else 2


def cmd_archive(args: argparse.Namespace) -> int:
    """归档散乱产物到统一结构（knowledgebase/papers|videos|docs/），补 frontmatter + metadata.json。"""
    from backend.services.archive.service import run_archive

    report = run_archive()
    print("归档完成（复制为主，未删旧文件）")
    print(f"  论文产物: {len(report['papers'])} 项（含 {len(report['pdfs'])} 份 PDF）")
    for p in report["papers"][:8]:
        print(f"    {p}")
    if len(report["papers"]) > 8:
        print(f"    ... 共 {len(report['papers'])} 项")
    print(f"  其它文档: {len(report['docs'])}")
    for d in report["docs"][:5]:
        print(f"    {d}")
    print(f"  B站视频: {report['videos']} 个")
    print(f"  metadata.json: papers={report['meta_papers']} / videos={report['meta_videos']}")
    if report["skipped"]:
        print(f"  跳过(无法识别 arxiv_id): {len(report['skipped'])}")
        for s in report["skipped"][:5]:
            print(f"    {s}")
    return 0


def cmd_rag_ask(args: argparse.Namespace) -> int:
    """已下线（2026-10-21）。检索统一走 `kb-ask` / `kb-fetch`，读 `data/kb/kb.db`。

    留这个壳只为别让老命令静默变成"参数错误"：谁再敲 rag-ask，立刻看到该改用哪条命令。
    旧实现（BM25 + FAISS 双路）的索引在 `ragsearch/index/**`，向量那半层已随本次下线删除。
    """
    print("✗ `rag-ask` 已下线（2026-10-21）。检索现在只有一条路：`kb-ask`。\n")
    print("  查片段：  python -m backend.main kb-ask \"查询词\" --pretty")
    print("           （可选 --topic papers|cbac|dairy|campus|ai-agent|videos… / --mode deep --vector）")
    print("  取原文：  python -m backend.main kb-fetch <doc_id> --pretty")
    print("           只取命中块：--tier 1 --chunk-ids 6106,6221")
    print("  新增内容后入库：kb-scan → kb-embed（不再是 ragsearch-index）")
    print("\n  为什么改：kb.db 是唯一真相索引；旧 FAISS 那层是第二份副本，长期不同步。")
    return 2


def cmd_ragsearch_index(args: argparse.Namespace) -> int:
    """已下线（2026-10-21）：dense 向量已并入 kb.db（kb_vec / sqlite-vec），不再有 FAISS 索引。"""
    print("✗ `ragsearch-index` 已下线（2026-10-21）。向量索引现在在 kb.db 里。\n")
    print("  改法：新内容落进 knowledgebase/ 之后 →")
    print("        python -m backend.main kb-scan      # 重建目录层与块")
    print("        python -m backend.main kb-embed     # 只补新增块的向量（增量）")
    print("\n  为什么改：旧 FAISS 索引是第二份副本，需要手动重建、经常过期（8/19 的索引到 10 月还在用）。")
    return 2


def cmd_fill_meta(args: argparse.Namespace) -> int:
    """补齐论文缺失元信息：解析 arxiv_id/DOI → 写 frontmatter + 同步 metadata.json。

    安全（C 修复）：无 --papers/--doi 时不再静默全库扫描改写；需显式 --all 才扫，
    且可用 --dry-run 只列清单不写。
    """
    from backend.services.archive.service import _FULL_PAPERS, fill_paper_meta
    from backend.services.arxiv.meta_resolver import resolve_paper
    from ragsearch.engine.document import parse_frontmatter

    # --doi 单独解析打印（可反推 arxiv_id 并填充）
    if args.doi and not args.papers:
        meta = resolve_paper(doi=args.doi)
        print("DOI 解析结果:")
        for k in ("title", "authors", "year", "citations", "venue", "ccf_rank", "arxiv_id", "doi"):
            v = meta.get(k)
            if v:
                print(f"  {k}: {v if not isinstance(v, list) else ', '.join(str(x) for x in v[:5])}")
        aid = meta.get("arxiv_id")
        if aid and (_FULL_PAPERS / aid).is_dir():
            print(f"→ 反推 arxiv_id={aid}，填充知识库条目")
            if not args.dry_run:
                print(fill_paper_meta(aid, doi=args.doi))
            else:
                print("  (dry-run) 不写入")
        return 0

    if args.papers:
        targets = [p.strip() for p in args.papers.split(",") if p.strip()]
    elif args.all:
        # 扫描缺 authors/year 的论文（需显式 --all）
        targets = []
        for d in sorted(_FULL_PAPERS.iterdir()):
            if not d.is_dir():
                continue
            md = d / "paper.md"
            if not md.exists():
                continue
            t = md.read_text(encoding="utf-8", errors="replace")
            fm, _ = parse_frontmatter(t)
            if not fm.get("authors") or not fm.get("year"):
                targets.append(d.name)
    else:
        print("用法: fill-meta --papers 1810.01291,2302.08496  或  --doi 10.xxx/yyy")
        print("      批量补齐缺失元数据: fill-meta --all [--dry-run]")
        print("      （无参数默认不扫描；--all 才会全库扫描，--dry-run 只列不改）")
        return 1

    if not targets:
        print("没有需要补齐的论文")
        return 0
    print(f"将处理 {len(targets)} 篇: {', '.join(targets[:5])}{'...' if len(targets) > 5 else ''}")
    if args.dry_run:
        print("(dry-run) 只列出，不写入")
        return 0
    ok = 0
    for aid in targets:
        r = fill_paper_meta(aid)
        if r.get("ok"):
            ok += 1
            filled = r.get("filled", {})
            auth = filled.get("authors", [])
            print(f"✅ {aid}: {str(filled.get('title'))[:40]} | 作者 {len(auth) if isinstance(auth, list) else auth} | "
                  f"年 {filled.get('year')} | 引 {filled.get('citations')}")
        else:
            print(f"❌ {aid}: {r.get('error')}")
    print(f"\n补齐 {ok}/{len(targets)}")
    return 0


def cmd_video_import(args: argparse.Namespace) -> int:
    """批量导入 B站视频：bvid/URL → 采集(bili.db) → 提取字幕+摘要 → 晋升 dense/video。"""
    import re
    import time as _time

    from backend.services.bilibili.collect import get_video_view
    from api.services.bilibili_service import BilibiliService
    from api.services.bili_task_manager import BiliTaskManager

    # ── 收集 bvids（支持 URL / BV 号 / 文件）──
    bvids: list[str] = []
    def _add(x: str) -> None:
        x = x.strip()
        if not x:
            return
        if x.startswith("http"):
            m = re.search(r"(BV[0-9A-Za-z]{10})", x)
            if m:
                x = m.group(1)
        if x.startswith("BV"):
            bvids.append(x)
    for x in (args.bvids or "").split(","):
        _add(x)
    if args.file:
        for line in open(args.file, encoding="utf-8"):
            _add(line.strip())
    bvids = list(dict.fromkeys(bvids))
    if not bvids:
        print('用法: video-import --bvids "BVxxx,BVyyy" | --file <bvids文件> [--no-extract]')
        return 1

    # ── 1. 采集 → bili.db ──
    svc = BilibiliService()
    videos = []
    for bvid in bvids:
        try:
            v = get_video_view(bvid)
            if v:
                owner = v.get("owner", {}) or {}
                videos.append({"bvid": bvid, "title": v.get("title", bvid),
                               "author": owner.get("name", ""),
                               "author_mid": owner.get("mid", 0),
                               "duration": v.get("duration", 0),
                               "view": (v.get("stat") or {}).get("view", 0),
                               "description": v.get("desc", ""), "pic": v.get("pic", "")})
            else:
                videos.append({"bvid": bvid, "title": bvid})
        except Exception as e:
            print(f"  ❌ {bvid}: 采集失败 {e}")
    if videos:
        res = svc.commit_videos(videos)
        print(f"采集 {len(bvids)} 个: {res.get('inserted')} 新增 / {res.get('skipped')} 已存在")

    if args.no_extract:
        print("（--no-extract，仅入库；后续可在 /videos 页提取字幕+摘要）")
        return 0

    # ── 2. 提取字幕 + 摘要（后台）→ 等待完成 ──
    task_id = svc.extract_and_summarize(bvids)
    print(f"提取+摘要任务: {task_id}")
    deadline = _time.time() + args.timeout
    final = "timeout"
    while _time.time() < deadline:
        _time.sleep(3)
        st = BiliTaskManager.get_status(task_id) or {}
        status = st.get("status", "?")
        print(f"  [{status}] {st.get('progress', '')}   ", end="\r", flush=True)
        if status in ("completed", "failed", "cancelled"):
            final = status
            break
    print()
    if final == "completed":
        from backend.services.ragsearch.service import get_status
        dense = get_status().get("dense", {})
        print(f"✅ 完成，摘要已晋升 dense/video（dense 库 {dense.get('doc_count', '?')} 篇）")
    elif final == "failed":
        print("❌ 提取/摘要失败（检查 bilibili cookie 与 LLM 配置）")
    else:
        print("⏳ 超时（任务仍在后台，可查 /videos 页）")
    return 0


def cmd_paper_search(args: argparse.Namespace) -> int:
    """多平台论文检索：搜索(arXiv/Crossref/OpenAlex/GS/万方/知网/S2) → 去重 → 入库(元数据) → 可选下载。"""
    if getattr(args, "merge", False):
        return _cmd_merge_searches(args)
    import json
    import re as _re
    import subprocess
    import time as _time
    from pathlib import Path

    from backend.services.papers import netctx, run_task
    from backend.services.papers.adapter import to_meta_clean
    from backend.services.papers.download import download_paper_pdf
    from backend.services.archive.service import add_paper_from_cleaned, archive_pdf

    searches_dir = Path(__file__).resolve().parent.parent / "data" / "paper_searches"
    searches_dir.mkdir(parents=True, exist_ok=True)
    ts = _time.strftime("%Y%m%d_%H%M%S")
    rec_id = getattr(args, "search_id", None) or (
        f"{ts}_{_re.sub(r'[^\w]+', '_', args.query)[:30].strip('_')}")
    record = {
        "id": rec_id, "query": args.query,
        "created_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "running", "stage": "searching",
        "per_source": {}, "total": 0, "stats": {},
        "results": [], "downloaded": [], "download_attempts": [],
        "error": "",
    }

    def persist(status: str | None = None, stage: str | None = None, **updates) -> None:
        if status:
            record["status"] = status
        if stage:
            record["stage"] = stage
        record.update(updates)
        (searches_dir / f"{rec_id}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        idx_path = searches_dir / "_index.json"
        idx = []
        if idx_path.exists():
            try:
                idx = json.loads(idx_path.read_text(encoding="utf-8"))
            except Exception:
                idx = []
        idx = [e for e in idx if e.get("id") != rec_id]
        idx.append({"id": rec_id, "query": args.query, "created_at": record["created_at"],
                    "per_source": record.get("per_source", {}),
                    "total": record.get("total", 0),
                    "downloaded": len(record.get("downloaded", [])),
                    "status": record.get("status", ""), "stage": record.get("stage", "")})
        idx.sort(key=lambda e: e.get("created_at", ""), reverse=True)
        idx_path.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")

    persist(stage="searching")
    try:
        print(f"网络: {netctx.describe()}")
        if not netctx.clash_online() and not args.no_gs:
            print("  [提示] Clash 离线，Google Scholar 将失败；可加 --no-gs 跳过")

        # 1. 搜索 + 去重 + 过滤 + 排序 + 下载源分类
        db_types = None
        if getattr(args, "db_types", ""):
            db_types = [t.strip() for t in args.db_types.split(",") if t.strip()]
        only_sources = None
        if getattr(args, "only_sources", ""):
            only_sources = [t.strip() for t in args.only_sources.split(",") if t.strip()]
        exclude_sources = None
        if getattr(args, "exclude_sources", ""):
            exclude_sources = [t.strip() for t in args.exclude_sources.split(",") if t.strip()]
        recs, counts = run_task.search_all(
            args.query, rows=args.rows, use_gs=not args.no_gs,
            title=args.title or "", author=args.author or "",
            year_from=args.year_from or 0, year_to=args.year_to or 0,
            category=args.category or "", db_types=db_types,
            s2_check=bool(args.max_download or getattr(args, "convert", 0)),
            only_sources=only_sources, exclude_sources=exclude_sources,
        )
        print(f"\n各源命中: {', '.join(f'{k}={v}' for k, v in counts.items())}")
        persist(stage="filtering", per_source=counts)
        run_task.filter_irrelevant(recs, args.query)
        # 按引用量排序（不用 run_task.rank：其 venue_score 逐 venue 查 OpenAlex 易 429 限流拖慢）
        recs.sort(key=lambda r: (r.citations or 0), reverse=True)
        print(f"合并去重后: {len(recs)} 条")
        stats = run_task.gate_download(recs)
        print(f"下载源分类: 开放 {stats['open']} | 机构 {stats['institution']} | 无 {stats['none']}")
        persist(stage="archiving", total=len(recs), stats=stats)

        # 2. 入库（转 meta_clean + 写 KB 元数据条目）
        added = []
        for r in recs:
            clean = to_meta_clean(r)
            res = add_paper_from_cleaned(clean)
            if res.get("ok"):
                added.append((r, res))
        print(f"\n入库 {len(added)} 篇：")
        for r, res in added[:10]:
            print(f"  ✅ {res['id']}: {r.title[:44]}")
        if len(added) > 10:
            print(f"  ... 共 {len(added)} 篇")

        # 3. 开放版下载 → data/pdfs（按论文 id 命名；下载成功即归档，/archive 立即可见）
        open_recs = [r for r in recs if r.links.get("download_source") == "open"]
        out_dir = Path(args.download_dir) if args.download_dir else Path("data/pdfs")
        entry_of = {id(r): res["id"] for r, res in added}
        downloaded: list[dict] = []       # 成功下载跟踪
        attempts: list[dict] = []         # 全部下载尝试（含失败原因）
        if args.max_download and open_recs:
            out_dir.mkdir(parents=True, exist_ok=True)
            dl_ok = 0
            print(f"\n=== 开放版下载 → {out_dir}（最多成功 {args.max_download} 篇）===")
            for r in open_recs:
                if dl_ok >= args.max_download:
                    break
                status, info = download_paper_pdf(r, str(out_dir))
                attempt = {"title": r.title, "entry_id": entry_of.get(id(r)),
                           "status": status}
                if status == "downloaded":
                    pid = entry_of.get(id(r))
                    p = Path(info)
                    if pid and p.suffix.lower() == ".pdf" and "--" not in p.stem:
                        new_p = p.with_name(f"{pid}--{p.stem}.pdf")
                        if not new_p.exists():
                            p.rename(new_p)
                        info = str(new_p)
                    attempt["path"] = info
                    downloaded.append({"title": r.title, "entry_id": entry_of.get(id(r)),
                                       "path": info, "status": status})
                    dl_ok += 1
                    print(f"  [下载] {r.title[:40]} -> {info}")
                    # 下载成功 → 立即归档到 data/papers/{id}/paper.pdf 并刷新 metadata
                    try:
                        arch = archive_pdf(info)
                        if arch.get("ok"):
                            print(f"  [归档] {arch['arxiv_id']} -> {arch['dest']}")
                    except Exception as _e:
                        print(f"  [warn] 归档失败: {_e}")
                else:
                    attempt["reason"] = info or ("无可用下载链接" if status == "paywall" else "下载失败")
                    print(f"  [{status}] {r.title[:40]} -> {attempt['reason'][:80]}")
                attempts.append(attempt)
            print(f"开放版下载成功 {dl_ok}/{args.max_download}（尝试 {len(attempts)} 篇）")
            if dl_ok < args.max_download:
                print("  [提示] 已无更多可成功下载的开放版论文（其余可能付费墙/链接失效）")
        persist(stage="downloading", downloaded=downloaded, download_attempts=attempts)

        # 4. 机构版下载（知网/万方/IEEE/ACM，node browser/download_institution.js）
        inst_recs = [r for r in recs if r.links.get("download_source") == "institution"]
        if args.inst_download and inst_recs:
            browser_dir = Path(__file__).resolve().parent / "services" / "papers" / "browser"
            payload = []
            for r in inst_recs[: args.inst_download]:
                host = r.links.get("inst_host", "")
                if not host:
                    host = "wanfang" if "wanfang" in (r.sources or []) else (
                        "cnki" if "cnki" in (r.sources or []) else "")
                if host in ("wanfang", "cnki"):
                    link = r.links.get("official", "")
                    if link:
                        payload.append({"source": host, "title": r.title, "link": link})
                elif host == "ieee" and r.doi:
                    # arnumber 由 download_institution.js 从 DOI 直接推出
                    payload.append({"source": "ieee", "title": r.title, "doi": r.doi})
                elif host == "acm" and r.doi:
                    payload.append({"source": "acm", "title": r.title, "doi": r.doi})
            if payload:
                inst_json = out_dir / "_inst_download.json"
                inst_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"\n=== 机构版下载（知网/万方/IEEE/ACM，{len(payload)} 篇）===")
                try:
                    # 注意：node 子进程 cwd=browser/，所以传给它的路径必须绝对化，
                    # 否则相对路径（如 data/pdfs/...）会在 browser/ 下解析而找不到文件。
                    _cmd = ["node", "download_institution.js", str(inst_json.resolve()), str(out_dir.resolve()),
                            str(len(payload)), f"--proxy={getattr(args, 'inst_proxy', 'auto')}"]
                    result = subprocess.run(
                        _cmd,
                        cwd=str(browser_dir), capture_output=True, text=True, encoding="utf-8", errors="replace",
                    )
                    if result.stdout:
                        print(result.stdout)
                    if result.returncode != 0:
                        print(f"  [warn] 机构版下载退出码 {result.returncode}: {result.stderr[-300:]}")
                except (FileNotFoundError, subprocess.CalledProcessError) as e:
                    print(f"  [warn] 机构版下载未执行: {e}")
            else:
                print(f"\n[提示] 机构版 {len(inst_recs)} 篇缺少可下载字段（link/doi），未下载")
        elif inst_recs:
            print(f"\n[提示] 机构版 {len(inst_recs)} 篇未下载（加 --inst-download N）")

        # 5. 写最终记录
        persist(status="completed", stage="completed",
                results=[
                    {"title": r.title, "year": r.year, "venue": r.venue, "citations": r.citations,
                     "doi": r.doi, "arxiv_id": r.arxiv_id, "sources": list(r.sources or []),
                     "download_source": r.links.get("download_source", ""),
                     "score": int(r.citations or 0), "entry_id": entry_of.get(id(r))}
                    for r in recs
                ])
        print(f"\n搜索任务已记录: data/paper_searches/{rec_id}.json")

        # 5.1 若指定论文调研项目：追加 search_rounds 留痕
        project_slug = getattr(args, "project", None)
        if project_slug:
            from backend.services.research_project import project_store as rp_store
            try:
                rp_store.load_project(project_slug)
                entry_ids = [r.get("entry_id") for r in record.get("results", []) if r.get("entry_id")]
                rp_store.append_search_round(project_slug, {
                    "round": _next_search_round(rp_store.search_rounds_path(project_slug)),
                    "request": args.query,
                    "commands": [" ".join(["python", "-m", "backend.main", "paper-search", args.query])],
                    "per_platform": counts,
                    "paper_ids": entry_ids,
                    "search_id": rec_id,
                })
                proj = rp_store.load_project(project_slug)
                rp_store.set_stage(proj, "search", "completed", last_round=_next_search_round(rp_store.search_rounds_path(project_slug)))
                rp_store.save_project(proj)
                print(f"  已写入论文调研项目留痕: {project_slug}/search_rounds.jsonl")
            except FileNotFoundError as e:
                print(f"  [warn] {e}（跳过项目留痕）")

        # 6. 可选：下载后自动提交远程转换（--convert N，一条命令走完 搜索→下载→转换）
        if getattr(args, "convert", 0) and downloaded:
            from backend.services.pdf_remote.service import RemotePDFService
            pdfs = [d["path"] for d in downloaded[: args.convert]
                    if str(d.get("path", "")).lower().endswith(".pdf")]
            if pdfs:
                try:
                    res = RemotePDFService().submit_batch(file_list=pdfs)
                    if res.get("success"):
                        print(f"\n已提交远程转换 {len(pdfs)} 篇（marker，job {res.get('job_id')}）；稍后 warmup 拉结果")
                    else:
                        err = next((s.get("error") for s in res.get("steps", [])
                                    if s.get("status") == "failed"), res.get("error", "unknown"))
                        print(f"\n[warn] 转换提交失败: {err}")
                except Exception as e:
                    print(f"\n[warn] 转换提交异常: {e}")

        return 0
    except Exception as e:
        import traceback
        traceback.print_exc()
        persist(status="failed", stage="failed", error=str(e)[:500])
        return 1


def _cmd_merge_searches(args: argparse.Namespace) -> int:
    """合并已存多份 paper-search 结果：全局去重 → 关键词过滤 → 引用排序 → top-N（可批量入库）。"""
    from backend.services.papers.merge_searches import (
        export_json, filter_keywords, import_to_kb, list_searches, load_papers, rank_by_citations,
    )

    if getattr(args, "list_searches", False):
        searches = list_searches()
        if not searches:
            print("没有已存的检索记录")
            return 0
        for s in searches:
            print(f"  {s.get('id')}  [{s.get('total', 0)}条]  {s.get('query', '')}  {s.get('created_at', '')}")
        return 0

    searches = None
    if getattr(args, "searches", None):
        searches = [x.strip() for x in args.searches.split(",") if x.strip()]
    papers = load_papers(searches)
    print(f"合并去重后: {len(papers)} 篇")

    kws = [x.strip() for x in (getattr(args, "keywords", "") or "").split(",") if x.strip()]
    if kws:
        papers = filter_keywords(papers, kws)
        print(f"关键词 [{', '.join(kws)}] 过滤后: {len(papers)} 篇")

    papers = rank_by_citations(papers, min_citations=getattr(args, "min_citations", 0) or 0)
    top_n = papers[: args.top_n] if getattr(args, "top_n", 0) else papers
    print(f"\n按引用排序，前 {len(top_n)} 篇：")
    for i, p in enumerate(top_n, 1):
        print(f"  [{i}] ({p.get('citations') or 0}引) {p.get('title', '')[:52]} | "
              f"{p.get('venue', '')[:22]} | {p.get('year', '')} | {p.get('arxiv_id', '')} {p.get('doi', '')}")

    if getattr(args, "export_json", ""):
        n = export_json(top_n, args.export_json)
        print(f"导出 JSON: {args.export_json} ({n} 篇)")
    if getattr(args, "import", False):
        added = import_to_kb(top_n)
        print(f"批量入库 {len(added)} 篇")
    return 0


def cmd_download_papers(args: argparse.Namespace) -> int:
    """按 arxiv_id / DOI 清单精确批量下载 PDF → data/papers/{id}/paper.pdf（并入 KB 二进制区）。"""
    import re as _re
    from pathlib import Path

    from backend.services.archive.service import _BIN_PAPERS, _doi_slug, add_paper_by_meta
    from backend.services.arxiv.meta_resolver import resolve_by_arxiv, resolve_by_doi
    from backend.services.papers.download import download_by_id

    ids: list[tuple[str, str]] = []
    if getattr(args, "arxiv", ""):
        ids += [("arxiv", x.strip()) for x in args.arxiv.split(",") if x.strip()]
    if getattr(args, "doi", ""):
        ids += [("doi", x.strip()) for x in args.doi.split(",") if x.strip()]
    if getattr(args, "file", ""):
        fp = Path(args.file)
        if fp.exists():
            for line in fp.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if _re.match(r"\d{4}\.\d{4,5}", line):
                    ids.append(("arxiv", line))
                else:
                    ids.append(("doi", line))

    if not ids:
        print("用法: download-papers --arxiv 2302.08496,1712.01767 [--doi 10.1109/tr.2021.3054617] [--file 清单] [--import-meta]")
        return 1

    ok = fail = no_url = 0
    for kind, pid in ids:
        try:
            meta = resolve_by_arxiv(pid) if kind == "arxiv" else resolve_by_doi(pid)
        except Exception:
            meta = {}
        paper_id = meta.get("arxiv_id") or (_doi_slug(meta.get("doi")) if meta.get("doi") else "")
        if not paper_id:
            paper_id = pid if kind == "arxiv" else _doi_slug(pid)
        target = _BIN_PAPERS / paper_id / "paper.pdf"

        if getattr(args, "import_meta", False):
            try:
                add_paper_by_meta(arxiv_id=pid if kind == "arxiv" else None,
                                  doi=pid if kind == "doi" else None)
            except Exception:
                pass

        status, detail = download_by_id(
            arxiv_id=pid if kind == "arxiv" else "",
            doi=pid if kind == "doi" else "",
            target_path=str(target),
        )
        if status == "downloaded":
            print(f"  ✅ [{paper_id}] {detail}")
            ok += 1
        elif status == "no_url":
            print(f"  ⚠️ [{paper_id}] 无开放版链接")
            no_url += 1
        else:
            print(f"  ❌ [{paper_id}] 下载失败: {detail}")
            fail += 1
    print(f"\n下载成功 {ok} / 无开放链接 {no_url} / 失败 {fail}")
    return 0 if fail == 0 else 1


def cmd_import_papers(args: argparse.Namespace) -> int:
    """从外部论文文件夹导入（md+pdf+图，防丢；copy 不 move 源）。"""
    import re
    import shutil
    from pathlib import Path

    from backend.services.archive.service import (
        _BIN_PAPERS, _FULL_PAPERS, _KB_LEGACY, _merge_frontmatter,
    )

    _AID = re.compile(r"(\d{4}\.\d{4,5})")

    def _title_from_md(text: str) -> str:
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("# ") or (s.startswith("#") and not s.startswith("##")):
                return s.lstrip("# ").strip().strip("*`").strip()
        return ""

    src = Path(args.src)
    if not src.is_dir():
        print(f"来源不存在: {src}")
        return 1

    log: list[str] = []
    for f in sorted(src.glob("*.md")):
        m = _AID.search(f.stem)
        if not m:
            log.append(f"[跳过] 无 arxiv_id: {f.name}")
            continue
        aid = m.group(1)
        full_dir = _FULL_PAPERS / aid
        bin_dir = _BIN_PAPERS / aid
        full_dir.mkdir(parents=True, exist_ok=True)
        bin_dir.mkdir(parents=True, exist_ok=True)
        text = f.read_text(encoding="utf-8", errors="replace")

        # 1. md → paper.md（已有不覆盖；内容不同 → 备份 _kb_legacy）
        pmd = full_dir / "paper.md"
        if pmd.exists():
            if pmd.read_text(encoding="utf-8", errors="replace") != text:
                bk = _KB_LEGACY / aid
                bk.mkdir(parents=True, exist_ok=True)
                (bk / f.name).write_text(text, encoding="utf-8")
                log.append(f"[冲突] {aid}: paper.md 已存在且内容不同 → 备份 _kb_legacy/{aid}/{f.name}")
            else:
                log.append(f"[跳过] {aid}: paper.md 已存在且相同")
        else:
            shutil.copy2(str(f), str(pmd))
            log.append(f"[md] {aid} → paper.md")

        # 2. pdf → data/papers/{id}/paper.pdf
        pdf = f.with_suffix(".pdf")
        if pdf.exists():
            dpdf = bin_dir / "paper.pdf"
            if not dpdf.exists():
                shutil.copy2(str(pdf), str(dpdf))
                log.append(f"[pdf] {aid} → paper.pdf")
            else:
                log.append(f"[pdf] {aid}: paper.pdf 已存在，跳过")

        # 3. 图片子目录 → data/papers/{id}/images/
        sub = src / f.stem
        if sub.is_dir():
            img_dir = bin_dir / "images"
            img_dir.mkdir(parents=True, exist_ok=True)
            n = 0
            for img in sub.glob("*"):
                if img.is_file() and not (img_dir / img.name).exists():
                    shutil.copy2(str(img), str(img_dir / img.name))
                    n += 1
            if n:
                log.append(f"[img] {aid}: 复制 {n} 图")

        # 4. frontmatter（title 取 # 标题，source 由 arxiv_id 生成）
        _merge_frontmatter(pmd, {"title": _title_from_md(text), "source_type": "paper"}, aid)

    print("\n".join(log) if log else "无操作")
    print(f"\n处理 {len(list(src.glob('*.md')))} 篇 md")
    return 0


def cmd_paper_cookie(args: argparse.Namespace) -> int:
    """导出/刷新 知网/万方 登录 cookie（Playwright 手动登录，保存 storage_state.json）。"""
    import subprocess
    from pathlib import Path

    browser_dir = Path(__file__).resolve().parent / "services" / "papers" / "browser"
    target = "wanfang" if args.target == "wanfang" else "cnki"
    print(f"运行 node export_cookies.js {target}")
    print("步骤：① 浏览器打开登录页 → ② 你手动完成学校认证/机构登录 → ③ 点右上角绿色按钮「完成登录并导出 cookie」（或回终端按回车）")
    print("保存位置: backend/services/papers/{target}_session/storage_state.json")
    return subprocess.call(["node", "export_cookies.js", target], cwd=str(browser_dir))


def cmd_add_paper(args: argparse.Namespace) -> int:
    """从 DOI 或 arxiv_id 创建论文 KB 条目（元数据入库，无需文件）。"""
    from backend.services.archive.service import add_paper_by_meta

    if args.doi:
        r = add_paper_by_meta(doi=args.doi)
    elif args.arxiv:
        r = add_paper_by_meta(arxiv_id=args.arxiv)
    else:
        print("用法: add-paper --doi 10.xxx/yyy  或  --arxiv 2302.08496")
        return 1
    if r.get("ok"):
        print(f"✅ {r['id']}: {str(r['meta'].get('title', ''))[:50]}")
        print(f"   目录: {r['dir']}（尚无 md，可转换/导入后入库检索）")
    else:
        print(f"❌ {r.get('error')}")
    return 0


def _split_csv(value: str) -> list[str]:
    return [x.strip() for x in value.split(",") if x.strip()] if value else []


def _next_search_round(path) -> int:
    """返回 search_rounds.jsonl 的下一轮次。"""
    if not path.exists():
        return 1
    n = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            n += 1
    return n + 1


def _parse_criteria(value: str) -> dict:
    out = {"include": [], "exclude": []}
    if not value:
        return out
    for part in value.split(";"):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            k, _, v = part.partition(":")
            k = k.strip().lower()
            if k in out:
                out[k] = [x.strip() for x in v.split(",") if x.strip()]
    return out


def cmd_paper_project(args: argparse.Namespace) -> int:
    """论文调研项目管理：init/status/edit/add/remove/move/run/list/import-pdf。"""
    from backend.services.research_project import project_store as ps
    from backend.services.research_project import mirror as pm

    action = args.action

    if action == "list":
        projects = ps.list_projects()
        if not projects:
            print("（暂无论文调研项目）")
            return 0
        for p in projects:
            print(f"  {p['id']} | {p['name']} | papers={p['papers_count']} | {p['created_at']}")
        return 0

    if action == "init":
        if not args.name:
            print("用法: paper-project init --name '项目名' [--question ...] [--keywords ...]")
            return 1
        r = ps.init_project(
            name=args.name,
            question=args.question or "",
            keywords=_split_csv(args.keywords),
            criteria=_parse_criteria(args.criteria),
            target_venues=_split_csv(args.venues),
        )
        slug = r["id"]
        if r.get("exists"):
            print(f"项目已存在: {slug}（未覆盖）")
        else:
            print(f"✅ 初始化项目: {slug}")
            pm.refresh_all(slug)
        print(f"   目录: {ps.work_dir(slug)}")
        print(f"   镜像: {ps.mirror_dir(slug)}")
        return 0

    slug = args.project or ""
    if not slug:
        print("需要 --project <slug>")
        return 1
    try:
        ps.load_project(slug)
    except FileNotFoundError as e:
        print(f"❌ {e}")
        return 1

    if action == "status":
        st = ps.project_status(slug)
        proj = st["project"]
        print(f"项目: {proj.get('name')} ({slug})")
        print(f"研究问题: {proj.get('research_question') or '（空）'}")
        print("\n阶段状态:")
        for stage, info in proj.get("stages", {}).items():
            status = info.get("status", "pending") if isinstance(info, dict) else "pending"
            print(f"  {stage}: {status}")
        print("\n产物检查:")
        for k, v in st["checks"].items():
            print(f"  {'✅' if v else '❌'} {k}")
        print(f"\n已选论文: {len(proj.get('papers', []))}")
        return 0

    if action == "edit":
        changed = False
        if args.set:
            key, _, value = args.set.partition("=")
            ps.set_field(slug, key.strip(), value, note=args.note)
            print(f"✅ 已设置 {key.strip()}")
            changed = True
        if args.set_paper_role:
            pid, _, role = args.set_paper_role.partition("=")
            r = ps.set_paper_role(slug, pid.strip(), role, reason=args.reason, confirmed=True)
            print(f"✅ 角色确认: {pid.strip()} -> {r['roles']}")
            changed = True
        if changed:
            pm.refresh_all(slug)
        else:
            print("没有变更；可用 --set key=value 或 --set-paper-role id=role")
        return 0

    if action == "add":
        paper_ids = _split_csv(args.papers)
        if args.search_id:
            import json
            from pathlib import Path
            rec_path = Path("data/paper_searches") / f"{args.search_id}.json"
            if not rec_path.exists():
                print(f"❌ 搜索记录不存在: {rec_path}")
                return 1
            try:
                rec = json.loads(rec_path.read_text(encoding="utf-8"))
                paper_ids = [str(r.get("entry_id")) for r in rec.get("results", []) if r.get("entry_id")]
            except Exception as e:
                print(f"❌ 读取搜索记录失败: {e}")
                return 1
        if not paper_ids:
            print("需要 --papers id1,id2 或 --search-id <id>")
            return 1
        r = ps.add_papers(slug, paper_ids, reason=args.reason, search_id=args.search_id)
        print(f"✅ 新增 {len(r['added'])} 篇（已存在 {len(r['exists'])} 篇）")
        for pid in r["added"][:10]:
            print(f"  + {pid}")
        proj = ps.load_project(slug)
        ps.set_stage(proj, "select", "in_progress")
        ps.save_project(proj)
        pm.refresh_all(slug)
        return 0

    if action == "remove":
        paper_ids = _split_csv(args.papers)
        if not paper_ids:
            print("需要 --papers id1,id2")
            return 1
        r = ps.remove_papers(slug, paper_ids, reason=args.reason)
        print(f"✅ 移除 {len(r['removed'])} 篇")
        pm.refresh_all(slug)
        return 0

    if action == "move":
        paper_ids = _split_csv(args.papers)
        if not paper_ids or not args.to:
            print("需要 --papers id1,id2 和 --to <目标项目>")
            return 1
        r = ps.move_papers(slug, paper_ids, args.to, reason=args.reason)
        print(f"✅ 移出 {len(r['moved'])} 篇 → {args.to}")
        pm.refresh_all(slug)
        pm.refresh_all(args.to)
        return 0

    if action == "run":
        stage = args.stage
        if stage == "search":
            print("search 阶段由 paper-search --project <slug> 驱动；此处仅标记状态")
            proj = ps.load_project(slug)
            ps.set_stage(proj, "search", "completed")
            ps.save_project(proj)
            pm.refresh_all(slug)
            return 0
        if stage == "basic-matrix":
            from backend.services.research_project.matrix import write_basic_matrix
            path = write_basic_matrix(slug, keywords=_split_csv(args.keywords))
            print(f"✅ 基础矩阵: {path}")
            proj = ps.load_project(slug)
            ps.set_stage(proj, "basic_matrix", "completed")
            ps.save_project(proj)
            pm.refresh_all(slug)
            return 0
        if stage == "semantic-matrix":
            from backend.services.research_project.matrix import write_semantic_matrix
            path = write_semantic_matrix(slug, model_key=args.model)
            print(f"✅ 语义矩阵: {path}")
            proj = ps.load_project(slug)
            ps.set_stage(proj, "semantic_matrix", "completed")
            ps.save_project(proj)
            pm.refresh_all(slug)
            return 0
        if stage == "classify":
            from backend.services.research_project.classify import classify_candidates
            r = classify_candidates(slug, model_key=args.model)
            print(f"✅ 分类完成: 成功 {len(r.get('classified', []))}, 失败 {len(r.get('failed', []))}")
            for f in r.get("failed", []):
                print(f"  ❌ {f.get('paper_id')}: {f.get('error')}")
            proj = ps.load_project(slug)
            ps.set_stage(proj, "classify", "completed")
            ps.save_project(proj)
            pm.refresh_all(slug)
            return 0
        if stage == "stats":
            from backend.services.research_project.stats import write_stats
            path = write_stats(slug)
            print(f"✅ 统计报告: {path}")
            proj = ps.load_project(slug)
            ps.set_stage(proj, "stats", "completed")
            ps.save_project(proj)
            pm.refresh_all(slug)
            return 0
        if stage == "refs":
            from backend.services.research_project.references import mine_references
            r = mine_references(slug)
            if not r.get("ok"):
                print(f"❌ {r.get('error')}")
                return 1
            print(f"✅ 参考文献挖掘: 候选 {r['candidate_count']} 篇")
            proj = ps.load_project(slug)
            ps.set_stage(proj, "refs", "completed")
            ps.save_project(proj)
            pm.refresh_all(slug)
            return 0
        print(f"未知 stage: {stage}")
        return 1

    if action == "import-pdf":
        if not args.papers or not args.pdf:
            print("用法: paper-project import-pdf --project xxx --papers <id> --pdf <path>")
            return 1
        from backend.services.archive.service import archive_pdf
        pid = args.papers.split(",")[0].strip()
        r = archive_pdf(args.pdf)
        if not r.get("ok"):
            print(f"❌ {r.get('error')}")
            return 1
        print(f"✅ PDF 已归档: {r['arxiv_id']} -> {r['dest']}")
        return 0

    print(f"未知 action: {action}")
    return 1


def cmd_paper_matrix(args: argparse.Namespace) -> int:
    from backend.services.research_project.matrix import write_basic_matrix, write_semantic_matrix
    paper_ids = _split_csv(args.papers) if args.papers else None
    if args.basic:
        path = write_basic_matrix(args.project, paper_ids=paper_ids, keywords=_split_csv(args.keywords))
        print(f"✅ 基础矩阵: {path}")
    if args.semantic:
        path = write_semantic_matrix(args.project, paper_ids=paper_ids, model_key=args.model)
        print(f"✅ 语义矩阵: {path}")
    if not args.basic and not args.semantic:
        print("需要 --basic 或 --semantic")
        return 1
    return 0


def cmd_paper_classify(args: argparse.Namespace) -> int:
    from backend.services.research_project.classify import classify_candidates, classify_status
    if args.status:
        st = classify_status(args.project)
        print(f"确认 {len(st['confirmed'])} 篇，候选 {len(st['candidates'])} 篇")
        return 0
    if not args.candidate:
        print("用法: paper-classify --project xxx --candidate [--model deepseek-chat] | --status")
        return 1
    r = classify_candidates(args.project, paper_ids=_split_csv(args.papers) if args.papers else None,
                            model_key=args.model)
    if not r.get("ok"):
        print(f"❌ {r.get('error')}")
        return 1
    print(f"✅ 候选分类: 成功 {len(r.get('classified', []))}, 失败 {len(r.get('failed', []))}")
    for c in r.get("classified", []):
        print(f"  {c['paper_id']}: {c['roles']} (conf={c['confidence']})")
    for f in r.get("failed", []):
        print(f"  ❌ {f.get('paper_id')}: {f.get('error')}")
    return 0 if not r.get("failed") else 1


def cmd_paper_refs(args: argparse.Namespace) -> int:
    from backend.services.research_project.references import mine_references
    r = mine_references(args.project)
    if not r.get("ok"):
        print(f"❌ {r.get('error')}")
        return 1
    print(f"✅ 参考文献权威挖掘: 候选 {r['candidate_count']} 篇")
    print(f"  JSON: {r['json_path']}")
    print(f"  MD:   {r['md_path']}")
    return 0


def cmd_paper_stats(args: argparse.Namespace) -> int:
    from backend.services.research_project.stats import write_stats
    path = write_stats(args.project)
    print(f"✅ 统计报告: {path}")
    return 0


def cmd_project_import(args: argparse.Namespace) -> int:
    """代码项目入库：拉取 GitHub/GitLab/npm/Gitee/Bing 元数据 → {调研分区}/projects/{id}/。"""
    from backend.services.projects import ingest

    # ── 子动作：渲染富字段回 project.md（B1）──
    if getattr(args, "render", ""):
        kb = args.render
        if getattr(args, "pid", None):
            p = ingest.render_project(kb, args.pid)
            print(f"✅ 渲染 {args.pid} → {p}")
        else:
            files = ingest.render_all(kb)
            print(f"✅ {kb} 渲染 {len(files)} 个项目: {', '.join(files) or '（无）'}")
        return 0

    # ── 子动作：生态关系矩阵（F4）──
    if getattr(args, "matrix", ""):
        print(ingest.project_matrix(args.matrix))
        return 0

    # ── 子动作：列出候选清单（B3）──
    if getattr(args, "list_candidates", ""):
        cand = ingest._candidates_path(args.list_candidates)
        if cand.exists():
            print(cand.read_text(encoding="utf-8"))
        else:
            print(f"（{args.list_candidates} 无候选清单，可先 project-import 初始化）")
        return 0

    # ── 子动作：校验富字段 schema（D4）──
    if getattr(args, "validate", ""):
        return _project_validate(args.validate)

    # ── 子动作：列出待重验证（F5）──
    if getattr(args, "list_stale", 0):
        stale = ingest.list_stale(days=args.list_stale)
        if not stale:
            print("无超期/未验证项目")
        for p in stale:
            print(f"  [stale] {p['id']} | last_verified={p['last_verified'] or '未验证'}")
        return 0

    # ── 子动作：从论文 md 提取代码链接 → 候选（F1）──
    if getattr(args, "scan", ""):
        return _project_scan(args.scan)

    from ragsearch.config import KNOWLEDGE_BASES

    # 校验 kb
    kb = args.kb or ""
    if not kb or f"{kb}-projects" not in KNOWLEDGE_BASES:
        print(f"❌ 需指定调研分区 --kb（registry 里要有 {kb}-projects）")
        print(f"   可用: {[n[:-len('-projects')] for n in KNOWLEDGE_BASES if n.endswith('-projects')]}")
        return 1

    tags = None
    if getattr(args, "tags", ""):
        tags = [t.strip() for t in args.tags.split(",") if t.strip()]

    # ── 批量导入（F2）：--file 清单 ──
    if getattr(args, "file", None):
        from pathlib import Path
        lines = Path(args.file).read_text(encoding="utf-8", errors="replace").splitlines()
        targets = [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]
        ok, fail = 0, []
        for t in targets:
            r = ingest.ingest_project(
                t, kb,
                host=getattr(args, "host", None) or None,
                section=getattr(args, "section", "") or "",
                tags=tags,
                display_name=getattr(args, "display_name", "") or "",
                cn_note=getattr(args, "cn_note", "") or "",
                force=args.force,
            )
            if r.get("ok"):
                ok += 1
                print(f"  ✅ {r['id']}: {r['title']} | {r['url']}")
            else:
                fail.append((t, r.get("error")))
                print(f"  ❌ {t}: {r.get('error')}")
        print(f"\n批量完成: 成功 {ok} / {len(targets)}")
        if fail:
            print("失败:")
            for t, e in fail:
                print(f"  - {t}: {e}")
        return 0 if not fail else 1

    if not args.query:
        print("用法: project-import <URL 或项目名> --kb 1-3-VERA [--section --tags --display-name]")
        print("      或 --file 清单 --kb 分区 | --render 分区 | --matrix 分区 | --scan 分区 | --list-candidates 分区")
        return 1

    r = ingest.ingest_project(
        args.query, kb,
        host=getattr(args, "host", None) or None,
        section=getattr(args, "section", "") or "",
        tags=tags,
        display_name=getattr(args, "display_name", "") or "",
        cn_note=getattr(args, "cn_note", "") or "",
        force=args.force,
    )
    if not r.get("ok"):
        print(f"❌ {r.get('error')}")
        return 1
    print(f"✅ 入库 {r['id']}: {r['title']}")
    print(f"   仓库: {r['url']}（{r['host']}）| stars={r['stars']}")
    print(f"   目录: {r['dir']}")
    print(f"   状态: {'已验证' if r['verified'] else '未验证（候选，已写 _candidates.md）'}")
    if not r["verified"]:
        print("   提示: 未验证，建议给完整 GitHub/GitLab URL 或确认名字拼写")
    print("   富字段（core_algorithms/生态地位/关系等）在 metadata.json；填后可用 project-import --render 分区 渲染回 md")
    return 0


def _project_scan(kb: str) -> int:
    """F1：扫描论文 md 里的 GitHub/GitLab URL → 写入分区候选清单。"""
    from pathlib import Path
    from backend.services.projects.ingest import _append_candidate, _candidates_path, ensure_candidates
    import re as _re

    papers_dir = Path("knowledgebase") / kb / "papers"
    if not papers_dir.is_dir():
        print(f"❌ 论文目录不存在: {papers_dir}")
        return 1
    url_re = _re.compile(r"(https?://(?:github|gitlab)\.com/[\w.\-]+/[\w.\-]+)")
    found = {}
    for md in papers_dir.rglob("*.md"):
        text = md.read_text(encoding="utf-8", errors="replace")
        for m in url_re.findall(text):
            # 去掉 .git 后缀和 #/tree 片段
            url = m.rstrip("/").rstrip(".git")
            found.setdefault(url, md.name)
    ensure_candidates(kb)
    cand = _candidates_path(kb)
    existing = cand.read_text(encoding="utf-8") if cand.exists() else ""
    added = 0
    for url, src in sorted(found.items()):
        if url in existing:
            continue
        line = f"- {url.split('/')[-1]} | {url} | 来自论文 {src}"
        with open(cand, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        added += 1
    print(f"✅ {kb} 论文中提取到 {len(found)} 个代码链接，新增候选 {added} 条 → {cand}")
    return 0


def _project_validate(kb: str) -> int:
    """D4：校验分区下所有项目的富字段 schema。"""
    import json as _json
    from pathlib import Path
    from backend.services.projects.ingest import validate_metadata

    projects_dir = Path("knowledgebase") / kb / "projects"
    if not projects_dir.is_dir():
        print(f"❌ 项目目录不存在: {projects_dir}")
        return 1
    issues = 0
    for proj_dir in sorted(projects_dir.glob("*/")):
        if proj_dir.name.startswith("_") or not proj_dir.is_dir():
            continue
        mpath = proj_dir / "metadata.json"
        if not mpath.exists():
            continue
        try:
            meta = _json.loads(mpath.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  [json错误] {proj_dir.name}: {e}")
            issues += 1
            continue
        probs = validate_metadata(meta)
        if probs:
            issues += len(probs)
            print(f"  [问题] {proj_dir.name}:")
            for p in probs:
                print(f"    - {p}")
    if issues == 0:
        print(f"✅ {kb} 全部项目富字段 schema 通过")
    else:
        print(f"⚠️ {kb} 共 {issues} 处问题")
    return 0 if issues == 0 else 1


def cmd_rag_folder(args: argparse.Namespace) -> int:
    """在任意文件夹里立刻做 BM25 检索（不向量化、不进知识库、不登记 registry，纯 AI 用）。

    文件夹格式：内含 .md 文件（递归、UTF-8）；带 YAML frontmatter 可选
    （title/source/section/authors/year/tags/source_type → 支持过滤/分组），不带也能搜。
    """
    from backend.services.ragsearch.service import search_folder

    if not args.query:
        print('用法: rag-folder <文件夹> "<查询>" [--top-k N] [--section S] [--tags T] [--year Y] [--source-type ST] [--group]')
        print('文件夹格式：内含 .md（递归、UTF-8）；frontmatter 可选（title/source/section/authors/year/tags/source_type）。')
        return 1

    results = search_folder(args.folder, args.query, top_k=args.top_k,
                            section=args.section, tags=args.tags, year=args.year,
                            authors=args.authors, source_type=args.source_type,
                            group_by_doc=args.group)
    if not results:
        print("无命中")
        return 0
    for i, g in enumerate(results, 1):
        if "versions" in g:  # 分组
            print(f"[{i}] {g.get('title', '?')}  (doc_id={g.get('doc_id', '?')}, "
                  f"{g.get('version_count', 0)}版本, score={g.get('score')})")
            for v in g["versions"]:
                print(f"    ├─ {v.get('source_type', '?')} | {v.get('filename', '')}")
                if v.get('section'):
                    print(f"    │  section: {v['section']}")
                if v.get('source'):
                    print(f"    │  source: {v['source']}")
                print(f"    │  片段: {v.get('snippet', '')[:200]}")
        else:
            r = g
            print(f"[{i}] {r.get('title', '?')}  (doc_id={r.get('doc_id', '?')}, "
                  f"{r.get('source_type', '?')}, score={r.get('score')})")
            if r.get('section'):
                print(f"    section: {r['section']}")
            print(f"    片段: {r.get('snippet', '')[:200]}")
        print()
    return 0


def cmd_rag_migrate(args: argparse.Namespace) -> int:
    """库↔库批量迁移论文（copy/move）。筛选同时覆盖全文 frontmatter 与纯元数据 metadata.json，目录级整体搬。"""
    import json
    import os
    import shutil
    import tempfile
    import time
    from pathlib import Path

    from ragsearch.config import KNOWLEDGE_BASES
    from ragsearch.engine.indexer import DocIndexer

    if args.frm not in KNOWLEDGE_BASES:
        print(f"源库不存在: {args.frm}；可用库: {list(KNOWLEDGE_BASES)}")
        return 1
    if args.to not in KNOWLEDGE_BASES:
        print(f"目标库不存在: {args.to}")
        return 1
    src = Path(KNOWLEDGE_BASES[args.frm])
    dst = Path(KNOWLEDGE_BASES[args.to])
    if not src.is_dir():
        print(f"源目录不存在: {src}")
        return 1

    # 候选收集：① 全文 frontmatter（paper.md）② 纯元数据（metadata.json）
    cands: dict = {}  # dir_name -> {doc_id,title,tags,section,source_type,year}

    idx = DocIndexer(docs_dir=str(src),
                     index_dir=os.path.join(tempfile.gettempdir(), "dsh_migrate_idx"))
    for d in idx._load_md_files():
        dir_name = os.path.dirname(d.filename.replace("\\", "/"))
        if not dir_name:
            continue
        cands[dir_name] = {
            "doc_id": d.doc_id, "title": d.title or dir_name,
            "tags": [str(t) for t in (d.tags or [])],
            "section": d.section or "", "source_type": d.source_type or "",
            "year": str(d.year or ""),
        }

    for sub in src.iterdir():
        if not sub.is_dir() or sub.name in cands:
            continue
        mj = sub / "metadata.json"
        if not mj.exists():
            continue
        try:
            m = json.load(open(mj, encoding="utf-8"))
        except Exception:
            continue
        fm = m.get("frontmatter") or {}
        year = str(fm.get("year") or m.get("year") or "")
        cands[sub.name] = {
            "doc_id": m.get("id") or sub.name,
            "title": m.get("title") or sub.name,
            "tags": [str(t) for t in (m.get("tags") or [])],
            "section": m.get("section") or "",
            "source_type": m.get("type") or "",
            "year": year,
        }

    tag_list = [t.strip().lower() for t in (args.tags or "").split(",") if t.strip()]
    id_list = [i.strip() for i in (args.ids or "").split(",") if i.strip()]

    def match(c):
        if id_list and c["doc_id"] not in id_list:
            return False
        if tag_list:
            ct = {t.lower() for t in c["tags"]}
            if not any(t in ct for t in tag_list):
                return False
        if args.section and args.section.lower() not in (c["section"] or "").lower():
            return False
        if args.source_type and args.source_type.lower() != (c["source_type"] or "").lower():
            return False
        if args.year and (c["year"] or "") != str(args.year):
            return False
        return True

    selected = [(dir_name, c) for dir_name, c in cands.items() if match(c)]

    if args.dry_run:
        print(f"将迁移 {len(selected)} 篇（{args.frm} → {args.to}）:")
        for dir_name, c in selected:
            print(f"  {c['doc_id']:36} {(c['title'] or '')[:55]}")
        return 0
    if not selected:
        print("无匹配论文")
        return 0

    dst.mkdir(parents=True, exist_ok=True)
    log_path = Path(__file__).resolve().parent.parent / "data" / "migrate_log.jsonl"
    moved = copied = skipped = 0
    for dir_name, c in selected:
        sdir = src / dir_name
        ddir = dst / dir_name
        if not sdir.is_dir():
            print(f"[缺目录] {c['doc_id']}")
            skipped += 1
            continue
        if ddir.exists() and not args.force:
            print(f"[已存在] {c['doc_id']}")
            skipped += 1
            continue
        if ddir.exists() and args.force:
            shutil.rmtree(ddir)
        shutil.copytree(str(sdir), str(ddir))
        if args.move:
            shutil.rmtree(str(sdir))
            moved += 1
        else:
            copied += 1
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "t": time.strftime("%Y-%m-%d %H:%M:%S"),
                "from": args.frm, "to": args.to, "id": c["doc_id"],
                "action": "move" if args.move else "copy",
            }, ensure_ascii=False) + "\n")

    print(f"完成：move {moved} / copy {copied} / skip {skipped}")
    if moved or copied:
        print("提示：知识库索引需重建（受影响库）：")
        print("  D:/Anaconda/envs/infosystem/python.exe -m backend.main kb-scan")
        print("  D:/Anaconda/envs/infosystem/python.exe -m backend.main kb-embed")
    return 0


def cmd_survey(args: argparse.Namespace) -> int:
    """arXiv 文献调研：搜索 → 引用丰富 → CCF 排序 → 下载。"""
    from backend.services.arxiv.survey import run_survey

    result = run_survey(
        raw_query=args.raw_query,
        max_papers=args.max_papers,
        top_n=args.top_n,
        output_dir=args.output,
        download=args.download,
        workers=args.workers,
        cit_weight=args.sort_citation_weight,
        ccf_weight=args.sort_ccf_weight,
        time_weight=args.sort_time_weight,
    )
    if "error" in result:
        print(f"错误: {result['error']}")
        return 1

    print(f"\n完整结果: {result['json_path']}")
    print(f"CSV 表格: {result['csv_path']}")
    if result.get("top_downloads"):
        print(f"已下载 {len(result['top_downloads'])} 篇 PDF")
    return 0


def cmd_chat(args: argparse.Namespace) -> int:
    """文件对话：读取文件夹全部文件 + 问题 → LLM 回答。"""
    settings = Settings()
    llm = settings.get_llm_client(model_key=args.model)

    # 支持从文件读取问题（--question-file 优先级高于 --question）
    question = args.question
    if args.question_file:
        qp = Path(args.question_file)
        if qp.exists():
            question = qp.read_text(encoding="utf-8").strip()

    if args.search:
        print("[提示] --search 仅对部分第三方 DeepSeek 平台生效，官方 api.deepseek.com 不支持此参数")

    from backend.pipelines.file_chat import run_file_chat

    ctx = run_file_chat(
        input_dir=args.input,
        question=question,
        llm=llm,
        output_dir=args.output,
        enable_search=args.search,
        enable_think=args.think,
    )

    print("\n" + "=" * 50)
    print("AI 回答:")
    print("=" * 50)
    print(ctx["chat_result"])
    print(f"\n回答已保存: {ctx['report_path']}")
    return 0


def cmd_persona(args: argparse.Namespace) -> int:
    """从 AI 对话记录提取人物画像要点。

    按可自定义的标记正则切块 → 逐轮并行 LLM 提炼 → 汇总报告。
    支持断点续传（同输入文件自动续跑）。
    """
    from backend.pipelines.persona_extract import run_persona_pipeline
    from backend.utils.cli_helpers import resolve_text, apply_config_file

    # --config 文件合并（CLI 显式值优先于 config 文件）
    apply_config_file(args, "persona")

    # 校验必要参数
    if not args.input:
        print("错误：必须提供 --input（或 config 文件中的 input）")
        return 1

    # --story / --story-file / --task-prompt / --task-prompt-file 统一解析
    story = resolve_text(args.story, args.story_file)
    task_prompt = resolve_text(args.task_prompt, args.task_prompt_file)

    settings = Settings()
    llm = settings.get_llm_client(model_key=args.model)

    ctx = run_persona_pipeline(
        input_file=args.input,
        llm=llm,
        output_dir=args.output,
        user_pattern=args.user_pattern,
        ai_pattern=args.ai_pattern,
        story_context=story,
        task_requirements=task_prompt,
        workers=args.workers,
        force=args.force,
    )
    print(f"画像提取完成: {ctx['report_path']}")
    print(f"  轮次: {ctx['rounds']}, "
          f"成功: {ctx['summary'].get('success', 0)}, "
          f"失败: {ctx['summary'].get('failed', 0)}")
    return 0


def cmd_warmup(args: argparse.Namespace) -> int:
    """环境预热 + 自检 + 自动收尾。

    可在此扩展新的检查项，每项是 (name, callable) 对。
    """
    from backend.services.pdf_remote.service import RemotePDFService

    passed = 0
    failed = 0
    checks: list[tuple[str, callable]] = []

    # ── 1. 远程 PDF 结果拉取（含翻译）─────────────
    def _check_remote():
        svc = RemotePDFService()
        results = svc.check_and_collect(output_dir=args.output)
        if not results:
            return "OK", "无待处理的远程作业"
        lines = []
        for r in results:
            status = r["status"]
            job_type = r.get("type", "convert")
            if job_type == "translate":
                type_label = "🌐 翻译"
            elif job_type == "mineru":
                type_label = "🧪 mineru"
            else:
                type_label = "📄 转换"
            files = r.get("files", [])
            if status == "completed":
                for f in files:
                    ftype = f.get("type", "")
                    prefix = "  📄" if ftype == "original" else "  🌐" if ftype == "translation" else "  📦" if f.get("remote", "").endswith(".tar") else "  📄"
                    lines.append(f"{prefix} {f['local']}")
            elif status == "running":
                lines.append(f"  ⏳ {type_label} 作业 {r['job_id']}: 仍在运行")
            elif status == "failed":
                tail = r.get("log_tail", "")
                lines.append(f"  ❌ {type_label} 作业 {r['job_id']}: 失败, 日志尾: {tail[:200]}")
            else:
                lines.append(f"  {type_label} 作业 {r['job_id']}: {status}")
        return "OK" if any(r["status"] == "completed" for r in results) else "PENDING", "\n".join(lines)

    checks.append(("远程 PDF 收尾", _check_remote))

    # ── 2. Bilibili 环境 ─────────────────────────
    def _check_bilibili():
        from backend.config.settings import Settings
        s = Settings()
        cookie_parts = [
            ("SESSDATA", s.bili_cookie_sessdata),
            ("bili_jct", s.bili_cookie_bili_jct),
            ("DedeUserID", s.bili_cookie_dedeuserid),
        ]
        configured = [k for k, v in cookie_parts if v]
        if not configured:
            return "FAIL", "Bilibili cookie 未配置"
        # 轻量 API 探测
        import urllib.request
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Cookie": f"SESSDATA={s.bili_cookie_sessdata}; bili_jct={s.bili_cookie_bili_jct}; DedeUserID={s.bili_cookie_dedeuserid}",
        }
        try:
            req = urllib.request.Request("https://api.bilibili.com/x/web-interface/nav", headers=headers)
            resp = urllib.request.urlopen(req, timeout=10)
            import json
            data = json.loads(resp.read())
            if data.get("code") == 0:
                uname = data.get("data", {}).get("uname", "?")
                return "OK", f"Cookie 有效, 登录用户: {uname}"
            return "WARN", f"API 返回 code={data.get('code')}, cookie 可能已失效"
        except Exception as e:
            return "WARN", f"API 不可达: {e}"

    checks.append(("Bilibili 环境", _check_bilibili))

    # ── 3. LLM 端点连通性 ────────────────────────
    def _check_llm():
        from backend.config.settings import Settings
        import json as _json
        import urllib.request as _request
        s = Settings()
        if not s.llm_config_path.exists():
            return "FAIL", f"LLM 配置文件不存在: {s.llm_config_path}"
        configs = _json.loads(s.llm_config_path.read_text(encoding="utf-8"))
        lines = []
        for key, cfg in configs.items():
            endpoint = cfg.get("endpoint", "")
            api_key = cfg.get("api_key", "")
            if not endpoint:
                lines.append(f"  {key}: 无 endpoint")
                continue
            if not api_key:
                lines.append(f"  {key}: 无 API key")
                continue
            # 向 endpoint 发轻量 GET 请求 /v1/models（OpenAI 兼容标准，无副作用）
            from urllib.parse import urlparse
            parsed = urlparse(endpoint)
            # 构造 /v1/models URL
            models_url = f"{parsed.scheme}://{parsed.netloc}/v1/models"
            req = _request.Request(models_url, method="GET")
            req.add_header("Authorization", f"Bearer {api_key}")
            req.add_header("Content-Type", "application/json")
            try:
                resp = _request.urlopen(req, timeout=8)
                code = resp.status
                # 200=正常, 401/403=可连通但认证无效, 都说明网络可达
                if code == 200:
                    lines.append(f"  {key}: HTTP {code} (API 正常)")
                elif code in (401, 403):
                    lines.append(f"  {key}: HTTP {code} (API 可达，认证问题)")
                else:
                    lines.append(f"  {key}: HTTP {code} (状态异常)")
            except _request.HTTPError as e:
                # 401/403 也是 HTTPError，但说明网络是通的
                if e.code in (401, 403):
                    lines.append(f"  {key}: HTTP {e.code} (API 可达，认证问题)")
                else:
                    lines.append(f"  {key}: HTTP {e.code} ({e.reason})")
            except Exception as e:
                lines.append(f"  {key}: 不可达 ({e})")
        return "OK" if lines else "FAIL", "\n".join(lines)

    checks.append(("LLM 端点连通性", _check_llm))

    # ── 4. 运行环境 ──────────────────────────────
    def _check_env():
        import shutil
        lines = []
        # ffmpeg
        if shutil.which("ffmpeg"):
            lines.append("  ffmpeg: 可用")
        else:
            lines.append("  ffmpeg: 未安装")
        # 关键目录
        from backend.config.settings import Settings
        s = Settings()
        for name, p in [("data_dir", s.data_dir), ("pdfs_dir", s.local_pdfs_dir)]:
            pp = Path(p)
            if pp.exists():
                lines.append(f"  {name}: {pp} 存在")
            else:
                lines.append(f"  {name}: {pp} 不存在")
        return "OK", "\n".join(lines)

    checks.append(("运行环境", _check_env))

    # ── 执行 ─────────────────────────────────────
    print("=" * 50)
    print("系统预热检查")
    print("=" * 50)
    for name, fn in checks:
        try:
            status, detail = fn()
        except Exception as e:
            status, detail = "ERROR", str(e)
        icon = {"OK": "  OK", "WARN": " WARN", "PENDING": " INFO", "FAIL": " FAIL", "ERROR": "ERROR"}.get(status, "  ???")
        print(f"[{icon}] {name}")
        if detail:
            for line in detail.strip().split("\n"):
                print(f"       {line}")
        if status in ("FAIL", "ERROR"):
            failed += 1
        elif status in ("OK", "WARN", "PENDING"):
            passed += 1
        print()

    print(f"{'=' * 50}")
    print(f"结果: {passed} passed, {failed} failed")
    return 1 if failed else 0


# ═══════════════════════════════════════════════════
#  ocr — 本地 Markdown 图片 OCR
# ═══════════════════════════════════════════════════


def cmd_ocr(args: argparse.Namespace) -> int:
    """对 Markdown 文档中的图片进行 OCR 识别（本地，不涉及远端）。

    图片位于 `{md文件所在目录}/{md文件名}/` 下。
    支持单个文件或 `data/ocr_docs/` 目录下所有 .md 文件。
    """
    from backend.services.ocr.service import OCRService

    svc = OCRService(model_key=args.model)
    kw = dict(parallel=not args.no_parallel, retry=args.retry,
              inplace=args.inplace, min_size_kb=args.min_size_kb,
              strip_refs=not args.no_strip_refs)

    if args.file:
        print(f"📄 处理文件: {args.file}")
        result = svc.process_file(args.file, **kw)
        print(f"   总计 {result['total']} 图 → {result['success']} 成功 / {result['failed']} 失败 / {result['skipped']} 跳过")
        if not args.inplace:
            print(f"   输出: {result['output']}")
        if result.get("warn_same_dir"):
            print(f"   ⚠ 部分图片与 md 在同一目录（非 {Path(args.file).stem}/ 子目录）")
        return 0

    # 默认：处理 data/ocr_docs/
    ocr_dir = Path("data/ocr_docs")
    if not ocr_dir.is_dir():
        ocr_dir.mkdir(parents=True, exist_ok=True)
        print(f"已创建目录: {ocr_dir}，放入 .md 文件后重新运行")
        return 1

    print(f"📂 处理目录: {ocr_dir}")
    results = svc.process_directory(str(ocr_dir), **kw)
    total = sum(r["total"] for r in results)
    success = sum(r["success"] for r in results)
    failed = sum(r["failed"] for r in results)
    skipped = sum(r["skipped"] for r in results)
    print(f"\n{'='*40}")
    print(f"全部完成: {total} 图 → {success} 成功 / {failed} 失败 / {skipped} 跳过")
    if not args.inplace:
        for r in results:
            print(f"  {r['output']}")

def cmd_smart_ocr(args: argparse.Namespace) -> int:
    """智能 OCR：本地二分类 + GLM 4.6/5 双模型 + 独立 md 输出。"""
    from backend.services.ocr.smart_ocr import run_smart_ocr
    result = run_smart_ocr(args.dir, args.output, args.dry_run)
    return 0 if result.get("ok") else 1


# ═══════════════════════════════════════════════════
#  translate — 远程翻译
# ═══════════════════════════════════════════════════


def cmd_translate(args: argparse.Namespace) -> int:
    """提交远程翻译任务。

    对已转换完成的 .md 文件进行翻译（调用服务端的 Ollama）。
    三种模式：
    - --auto：自动翻译所有标记了需翻译的已完成 convert job
    - --files：指定翻译远程 output 目录中的 .md 文件
    - --upload-md：上传本地 .md 文件并翻译
    """
    from backend.services.pdf_remote.service import RemotePDFService
    from backend.services.pdf_remote.parser import _load_jobs, _update_job

    svc = RemotePDFService()

    # ── 模式 1: --auto ──────────────────────────
    if args.auto:
        from backend.config.settings import Settings as _S
        from backend.services.pdf_remote.ssh import SSHClient as _SSH
        import shlex

        jobs = _load_jobs()
        # 找所有标记了需翻译且尚未翻译/提交的 convert job（不限制 status）
        candidates = [
            j for j in jobs
            if j.get("type") == "convert"
            and j.get("translate_requested")
            and not j.get("translate_done")
            and not j.get("translate_submitted")
        ]
        if not candidates:
            print("没有找到需要翻译的转换任务")
            print("  (使用 `python -m backend.main pdf --translate` 提交转换时标记)")
            return 0

        # SSH 检查远端 .md 文件是否存在
        _settings = _S()
        ssh = _SSH()
        if not ssh.connect():
            print("错误: SSH 连接失败，无法检查远端文件")
            return 1

        ls_out, _, ls_rc = ssh.execute_command(
            f"ls -1 {shlex.quote(_settings.remote_output_dir)} 2>/dev/null || true"
        )
        remote_files = set(ls_out.splitlines()) if ls_rc == 0 else set()
        ssh.close()

        ready = []
        for job in candidates:
            uploaded = job.get("uploaded_files", [])
            md_names = [
                os.path.basename(u.get("local", "")).replace(".pdf", ".md")
                for u in uploaded
            ]
            missing = [n for n in md_names if n not in remote_files]
            if missing:
                print(f"  作业 {job['id']}: 远端 .md 尚未就绪（缺少: {missing}），跳过")
                continue
            ready.append(job)

        if not ready:
            print("所有候选任务的远端 .md 文件尚未就绪，请等待转换完成")
            return 0

        submitted = 0
        for job in candidates:
            job_id = job["id"]
            # 查找该 job 是否已有 translate job
            existing_translate = [
                j for j in jobs
                if j.get("type") == "translate"
                and j.get("source_job_id") == job_id
                and j.get("status") in ("running", "submitted")
            ]
            if existing_translate:
                print(f"  作业 {job_id}: 翻译任务已提交（{existing_translate[0]['status']}），跳过")
                continue

            print(f"  作业 {job_id}: 提交翻译...")
            result = svc.submit_translate(
                source_job_id=job_id,
                model=args.model,
                detect=not args.no_detect,
                content_type=args.content_type,
                prompt_override=args.translate_prompt,
            )
            if result.get("success"):
                submitted += 1
                _update_job(job_id, translate_submitted=True)
                print(f"    ✅ 已提交 (PID: {result.get('remote_pid', 'N/A')})")
                print(f"    日志: {result.get('remote_log_file', 'N/A')}")
            else:
                for step in result.get("steps", []):
                    if step.get("status") == "failed":
                        print(f"    ❌ [{step.get('step', '?')}] {step.get('error', 'unknown')}")

        print(f"\n共提交 {submitted} 个翻译任务")
        print("稍后运行 `python -m backend.main warmup` 拉取翻译结果")
        return 0 if submitted else 1

    # ── 模式 2: --files (远程 .md) ──────────────
    if args.files:
        md_files = [f.strip() for f in args.files if f.strip().endswith(".md")]
        if not md_files:
            print("错误: --files 需要指定远程 .md 文件名（如 paper.md）")
            return 1
        result = svc.submit_translate(
            md_file_names=md_files,
            model=args.model,
            detect=not args.no_detect,
            content_type=args.content_type,
            prompt_override=args.translate_prompt,
        )
        if result.get("success"):
            print("远程翻译任务已提交")
            print(f"  文件: {', '.join(md_files)}")
            print(f"  Job ID: {result.get('job_id', 'N/A')}")
            print(f"  远端 PID: {result.get('remote_pid', 'N/A')}")
            print(f"  日志文件: {result.get('remote_log_file', 'N/A')}")
        else:
            print("提交失败")
            for step in result.get("steps", []):
                if step.get("status") == "failed":
                    print(f"  [{step.get('step', '?')}] 失败: {step.get('error', 'unknown')}")
        return 0 if result.get("success") else 1

    # ── 模式 3: --upload-md ──────────────────────
    if args.upload_md:
        from pathlib import Path as _Path
        local_md = _Path(args.upload_md)
        if not local_md.exists() or not local_md.suffix == ".md":
            print(f"错误: 文件不存在或不是 .md 文件: {args.upload_md}")
            return 1

        # 上传到 remote_input_dir
        from backend.config.settings import Settings
        settings = Settings()

        parser_type = type("ParserProxy", (), {})()
        parser_type.ssh_client = type("SSHProxy", (), {"upload_file": lambda self, l, r: True})()

        # Use direct SSH upload for the md file
        from backend.services.pdf_remote.ssh import SSHClient
        ssh = SSHClient()
        if not ssh.connect():
            print("错误: SSH 连接失败")
            return 1

        remote_path = f"{settings.remote_input_dir.rstrip('/')}/{local_md.name}"
        if not ssh.upload_file(str(local_md), remote_path):
            print(f"错误: 上传 {local_md.name} 失败")
            ssh.close()
            return 1
        print(f"  已上传: {local_md.name} -> {remote_path}")
        ssh.close()

        # 但 translate 要从 remote_output_dir 读取，所以再 scp 从 input 拷到 output
        copy_cmd = (
            "bash -lc "
            + f"\"cp {remote_path} {settings.remote_output_dir.rstrip('/')}/{local_md.name}\""
        )
        ssh2 = SSHClient()
        if not ssh2.connect():
            print("错误: SSH 连接失败")
            return 1
        ssh2.execute_command(copy_cmd)
        ssh2.close()

        result = svc.submit_translate(
            md_file_names=[local_md.name],
            model=args.model,
            detect=not args.no_detect,
            content_type=args.content_type,
            prompt_override=args.translate_prompt,
        )
        if result.get("success"):
            print("远程翻译任务已提交（上传模式）")
            print(f"  文件: {local_md.name}")
            print(f"  远端 PID: {result.get('remote_pid', 'N/A')}")
        else:
            print("提交失败")
            for step in result.get("steps", []):
                if step.get("status") == "failed":
                    print(f"  [{step.get('step', '?')}] 失败: {step.get('error', 'unknown')}")
            return 1
        return 0

    # ── 无有效参数 ─────────────────────────────
    print("错误: 请使用 --auto / --files / --upload-md 之一")
    print("示例:")
    print("  python -m backend.main translate --auto")
    print("  python -m backend.main translate --files paper1.md,paper2.md")
    print("  python -m backend.main translate --upload-md /path/to/file.md")
    return 1


# ═══════════════════════════════════════════════════
#  remote_fs — 远程文件系统
# ═══════════════════════════════════════════════════


def _get_remote_fs() -> RemoteFSService:
    """创建 RemoteFSService 实例（带配置）。"""
    from backend.config.settings import Settings
    from backend.services.remote_fs._ftp import _FTPConfig
    settings = Settings()
    return RemoteFSService(
        http_base_url=settings.remote_fs_http_url,
        ftp_config=_FTPConfig(
            host=settings.remote_fs_ftp_host,
            port=settings.remote_fs_ftp_port,
            user=settings.remote_fs_ftp_user,
            passwd=settings.remote_fs_ftp_pass,
            proxy_host=settings.ssh_proxy_host,
            proxy_port=settings.ssh_proxy_port,
        ),
    )


def cmd_remote_fs_list(args: argparse.Namespace) -> int:
    """列举远程目录。"""
    svc = _get_remote_fs()
    entries = svc.list_dir(args.path)
    for e in entries:
        icon = "📁" if e.type == "dir" else "📄"
        size_str = f"{e.size / 1024:.0f}KB" if e.size > 0 else ""
        print(f"  {icon} {e.name:30s}  {'dir' if e.type=='dir' else size_str}")
    print(f"\n共 {len(entries)} 项")
    return 0


def cmd_remote_fs_tree(args: argparse.Namespace) -> int:
    """打印远程目录树。"""
    svc = _get_remote_fs()
    output = svc.tree(args.path, max_depth=args.depth)
    print(output)
    return 0


def cmd_remote_fs_search(args: argparse.Namespace) -> int:
    """精确搜索远程文件夹名。"""
    svc = _get_remote_fs()
    results = svc.search(args.name, root=args.root)
    if not results:
        print(f"未找到名为 '{args.name}' 的文件夹")
        return 1
    print(f"找到 {len(results)} 个匹配的文件夹:\n")
    for p in results:
        print(f"  📁 {p}")

    if len(results) == 1 and args.tree:
        print(f"\n目录树:")
        print(svc.tree(results[0]))
    return 0


def cmd_remote_fs_download(args: argparse.Namespace) -> int:
    """下载远程文件夹（tar.gz）。"""
    svc = _get_remote_fs()

    def _progress(p):
        if p.phase == "downloading" and p.total_bytes > 0:
            pct = p.bytes_downloaded * 100 // p.total_bytes
            mb_dl = p.bytes_downloaded / 1024 / 1024
            mb_total = p.total_bytes / 1024 / 1024
            print(f"\r  {mb_dl:.1f}MB / {mb_total:.1f}MB ({pct}%)", end="", flush=True)
        elif p.phase == "done":
            print(f"\r  ✅ {p.message}")
        elif p.phase == "fallback":
            print(f"  ⚠ {p.message}")
        elif p.phase == "packing":
            print(f"  🔄 {p.message}")

    result = svc.download_folder(args.path, args.output, progress_cb=_progress)
    if result.success:
        print(f"下载成功: {result.local_path}")
        print(f"  策略: {result.strategy}, 大小: {result.bytes_transferred / 1024 / 1024:.1f}MB")
        print(f"  耗时: {result.elapsed_s:.1f}s")
        return 0
    else:
        print(f"下载失败: {result.error}")
        return 1


def cmd_remote_fs_download_file(args: argparse.Namespace) -> int:
    """下载远程单个文件。"""
    svc = _get_remote_fs()

    def _progress(p):
        if p.phase == "done":
            print(f"  ✅ {p.message}")

    result = svc.download_file(args.path, args.output, progress_cb=_progress)
    if result.success:
        print(f"下载成功: {result.local_path}")
        print(f"  大小: {result.bytes_transferred / 1024:.0f}KB, 耗时: {result.elapsed_s:.1f}s")
        return 0
    else:
        print(f"下载失败: {result.error}")
        return 1


def cmd_remote_fs_download_name(args: argparse.Namespace) -> int:
    """按名字定位远程文件夹并下载。"""
    svc = _get_remote_fs()

    def _progress(p):
        if p.phase == "done":
            print(f"  ✅ {p.message}")
        elif p.phase == "fallback":
            print(f"  ⚠ {p.message}")

    result = svc.download_by_name(
        args.parent, args.name, args.output, progress_cb=_progress,
    )
    if result is None:
        print(f"未找到文件夹: {args.name}")
        return 1
    if result.success:
        print(f"下载成功: {result.local_path}")
        print(f"  策略: {result.strategy}, 大小: {result.bytes_transferred / 1024 / 1024:.1f}MB")
        return 0
    else:
        print(f"下载失败: {result.error}")
        return 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="信息处理系统")
    sp = p.add_subparsers(dest="command", required=True)

    _paths = Paths()

    # ── bilibili ──
    b = sp.add_parser("bilibili", help="【已下线】改用 bili-visual-scan")
    b.add_argument("--url", default="", help="（已下线，参数仅为兼容旧命令）")
    b.add_argument("--input", default="", help="（已下线）")
    b.add_argument("--output", default="", help="（已下线）")
    b.add_argument("--model", default="glm-flash", help="（已下线）")
    b.add_argument("--workers", type=int, default=3, help="（已下线）")
    b.add_argument("--user-profile", default="", help="（已下线）")
    b.add_argument("--task-prompt", default="", help="（已下线）")
    b.add_argument("--screenshots", action="store_true", help="（已下线）")
    b.set_defaults(func=cmd_bilibili)

    # ── bili-export (已下线) ──
    be = sp.add_parser("bili-export", help="【已下线】用 bili-survey-stage1 --fav-id")
    be.add_argument("--folder", type=int, default=0)
    be.add_argument("--max", type=int, default=200)
    be.set_defaults(func=cmd_bili_export)

    # ── bili-analyze (已下线) ──
    ba = sp.add_parser("bili-analyze", help="【已下线】用 bili-find 选片 + bili-visual-scan")
    ba.add_argument("--csv", default="")
    ba.add_argument("--rows", default="")
    ba.add_argument("--output", default="")
    ba.add_argument("--model", default="glm-flash")
    ba.add_argument("--user-profile", default="")
    ba.add_argument("--task-prompt", default="")
    ba.add_argument("--screenshots", action="store_true")
    ba.set_defaults(func=cmd_bili_analyze)

    # ── bili-find (元信息检索) ──
    bf = sp.add_parser("bili-find", help="在只有元信息的视频库上检索（挑片去深度解析）")
    bf.add_argument("query", help="关键词，空格分隔；每个词都要命中")
    bf.add_argument("--limit", type=int, default=20, help="返回条数")
    bf.add_argument("--min-view", type=int, default=0, help="播放量下限")
    bf.add_argument("--sort", default="relevance", choices=["relevance", "view"])
    bf.add_argument("--bvid-only", action="store_true", help="只输出 BV 号，方便喂给 bili-visual-scan")
    bf.set_defaults(func=cmd_bili_find)

    # ── bili-info (单视频速查) ──
    bi = sp.add_parser("bili-info", help="单视频速查：元信息 + 字幕概况")
    bi.add_argument("bvid", help="BV 号或视频链接")
    bi.add_argument("--json", action="store_true", help="输出原始 JSON")
    bi.set_defaults(func=cmd_bili_info)

    # ── bili-visual-scan / apply (画面要素提取，自 test/bili_scan_exp 合并) ──
    bvs = sp.add_parser("bili-visual-scan", help="画面要素提取一站跑：字幕→分段→扫帧→看图浓缩→落库")
    bvs.add_argument("bvids", nargs="*")
    bvs.add_argument("--thr", type=float, default=3, help="只扫分数>=thr的段落（0=全片）")
    bvs.add_argument("--min-sub-cover", type=float, default=0.15,
                     help="字幕覆盖率(末条字幕时间/片长)低于此值判不可用→跳过不入库")
    bvs.add_argument("--model", default="v4flash", help="分段打分模型")
    bvs.add_argument("--from", dest="start", default="fetch",
                     choices=["fetch", "sections", "scan", "digest", "all"])
    bvs.add_argument("--keep-temps", action="store_true", help="保留低清/高清中间帧")
    bvs.add_argument("--force-scan", action="store_true", help="画面诊断说不用也照样扫")
    bvs.add_argument("--scan", dest="scan_mode", default="auto", choices=["auto", "off"],
                     help="扫帧总开关：auto=按字幕诊断自己决定（默认）；off=一条都不扫，全走纯字幕浓缩")
    bvs.add_argument("--probe", action="store_true", help="分段说没页时抽8帧复核（慢且不可靠）")
    bvs.add_argument("--no-probe-veto", action="store_true",
                     help="分段说有页时，不先用8帧抽样否决（默认会否决：0 帧整页文档就不扫）")
    bvs.add_argument("--from-stdin", action="store_true", help="从标准输入读BV号（配合 bili-find --bvid-only）")
    bvs.add_argument("--list-only", action="store_true", help="只打印要处理哪些视频")
    bvs.add_argument("--no-index", action="store_true", help="跑完不重建检索索引")
    bvs.add_argument("--backend-python", default=r"D:/Anaconda/envs/infosystem/python.exe")
    bvs.add_argument("--asr-fallback", action="store_true",
                     help="字幕不可用（无轨/抓错/太短/覆盖<0.5）时用本地 ASR 兜底转写。"
                          "默认关：ASR 是小时级 GPU 开销")
    bvs.add_argument("--asr-model", default="medium",
                     help="兜底 whisper 模型（注意与 --model 的分段模型不是一个东西）")
    bvs.add_argument("--asr-compute", default="float16",
                     help="float16/int8_float16/int8（6GB 卡显存紧就降 int8_float16）")
    bvs.set_defaults(func=cmd_bili_visual_scan)

    bva = sp.add_parser("bili-visual-apply", help="把 visual/<bvid>/digest.md 落进知识库")
    bva.add_argument("bvids", nargs="*")
    bva.add_argument("--all", action="store_true", help="落全部有 digest 的")
    bva.add_argument("--dry", action="store_true", help="只查重不写")
    bva.add_argument("--no-force", action="store_true", help="已有条目不覆盖")
    bva.set_defaults(func=cmd_bili_visual_apply)

    # ── bili-asr (本地 ASR 单跑转写，施工单07) ──
    bar = sp.add_parser("bili-asr",
                        help="本地 ASR 转写：只产字幕缓存，不跑后续流程（补历史/调试用）")
    bar.add_argument("bvids", nargs="+", help="BV 号，支持 BVxxx?p=2 指定分P")
    bar.add_argument("--model", default="medium",
                     help="whisper 模型 tiny/base/small/medium/large-v3（默认 medium）")
    bar.add_argument("--compute", default="float16",
                     help="float16/int8_float16/int8/float32（默认 float16）")
    bar.add_argument("--force", action="store_true",
                     help="覆盖已有 ASR 缓存（旧 ASR 缓存放 .asr_old；官方轨备份不覆盖）")
    bar.add_argument("--json", action="store_true", help="只打印 transcribe 返回的 JSON 数组")
    bar.set_defaults(func=cmd_bili_asr)

    # ── bili-survey-stage1 (作品-作者级信息收集) ──
    s1 = sp.add_parser("bili-survey-stage1",
                       help="收集视频信息：收藏夹(--fav-id) / 视频→合集+作者(--video-url) / 作者视频(--mid)")
    s1.add_argument("--fav-id", type=int, default=None, help="收藏夹 ID")
    s1.add_argument("--video-url", default="", help="视频 URL，用于查找合集和作者")
    s1.add_argument("--mid", type=int, default=None, help="作者 MID，直接收集作者视频")
    s1.add_argument("--order", default="click", choices=["pubdate", "click", "stow"],
                    help="作者视频排序（默认 click=最多播放）")
    s1.add_argument("--max-pages", type=int, default=5, help="作者视频最大翻页数")
    s1.add_argument("--max-items", type=int, default=200, help="收藏夹最大导出数")
    s1.add_argument("--output", default="data/bili_servey/videos", help="输出目录")
    s1.set_defaults(func=cmd_bili_survey_stage1)

    # ── bili-survey-stage2 (简介收集 + 评分) ──
    s2 = sp.add_parser("bili-survey-stage2", help="Stage 2: 丰富信息 + 评分排序")
    s2.add_argument("--input", required=True, help="输入的 JSON 文件路径（Stage 1 产出）")
    s2.add_argument("--output", default="", help="输出路径（默认 servey/ 目录下自动命名）")
    s2.add_argument("--enrich-only", action="store_true", help="仅丰富信息，不执行评分")
    s2.add_argument("--profile", default="", help="用户画像文件路径（评分必填）")
    s2.add_argument("--system-prompt", default="", help="评分 system prompt 路径（可选，默认 video_scorer_system.txt）")
    s2.add_argument("--model", default="glm-flash", help="LLM 模型名称（仅评分时使用）")
    s2.add_argument("--relevance-weight", type=float, default=0.6, help="兴趣匹配权重")
    s2.add_argument("--popularity-weight", type=float, default=0.4, help="热度权重")
    s2.set_defaults(func=cmd_bili_survey_stage2)

    # ── bili-survey-stage3 (已下线) ──
    s3 = sp.add_parser("bili-survey-stage3", help="【已下线】提字幕并入 bili-visual-scan")
    s3.add_argument("--input", default="")
    s3.add_argument("--output", default="")
    s3.add_argument("--top-n", type=int, default=10)
    s3.add_argument("--min-score", type=float, default=50.0)
    s3.set_defaults(func=cmd_bili_survey_stage3)

    # ── bili-annotate (补标注：分类+链接) ──
    bat = sp.add_parser("bili-annotate", help="给已摘要但未分类的视频补 分类/仓库链接 标注")
    bat.add_argument("--limit", type=int, default=0, help="最多处理条数，0=全部")
    bat.set_defaults(func=cmd_bili_annotate)

    # ── bili-unfav (解析完成后取消收藏) ──
    bu = sp.add_parser("bili-unfav", help="把已解析入库的视频从收藏夹删掉（默认只演练）")
    bu.add_argument("bvids", nargs="*")
    bu.add_argument("--from-runs", action="store_true",
                    help="从 _runs.jsonl 取 status=ok 且 kb_ok 的")
    bu.add_argument("--fav-id", type=int, default=51087974, help="收藏夹 id（默认收藏夹）")
    bu.add_argument("--apply", action="store_true", help="真的删；不给就是 dry-run")
    bu.set_defaults(func=cmd_bili_unfav)

    # ── bili-status (视频库进度总表) ──
    bs = sp.add_parser("bili-status", help="视频处理进度总表：线索→入库/跳过/待处理")
    bs.add_argument("--limit", type=int, default=12, help="最近记录显示条数")
    bs.add_argument("--pending", type=int, default=15, help="待处理示例条数")
    bs.add_argument("--json", action="store_true", help="输出 JSON")
    bs.set_defaults(func=cmd_bili_status)

    # ── bili-backfill (批量回刷：没进库的用 ASR 兜底重跑) ──
    bb = sp.add_parser("bili-backfill",
                       help="批量回刷：试过但没进库的视频用 ASR 兜底重跑（串行、可断点续跑）")
    bb.add_argument("--dry", action="store_true", help="只列靶子，不跑")
    bb.add_argument("--only", nargs="*", default=[], help="只跑指定 BV 号")
    bb.add_argument("--retry-failed", action="store_true",
                    help="连内容判定类失败也重跑（默认只跑可重试故障）")
    bb.add_argument("--timeout", type=int, default=2400, help="单条超时秒数（默认 2400）")
    bb.add_argument("--no-index", action="store_true", help="跑完不重建索引")
    bb.add_argument("--backend-python", default=r"D:/Anaconda/envs/infosystem/python.exe")
    bb.set_defaults(func=cmd_bili_backfill)

    # ── bili-coverage (收藏视频终态对账) ──
    bc = sp.add_parser("bili-coverage",
                       help="收藏视频终态对账：已入库/隔离/未入库原因/长视频未跑")
    bc.add_argument("--csv", default="", help="收藏清单 CSV（默认 favorites_51087974_latest200.csv）")
    bc.add_argument("--out", default="", help="明细 md 输出路径")
    bc.add_argument("--all", action="store_true", help="连 ≥30 分钟的长视频一起列")
    bc.add_argument("--json", action="store_true", help="只输出 JSON 摘要")
    bc.set_defaults(func=cmd_bili_coverage)

    # ── bili-author-mine (宝藏作者聚合) ──
    bam = sp.add_parser("bili-author-mine", help="按分类聚合作者，产出宝藏作者挖掘报告")
    bam.add_argument("--output", default="", help="报告路径（默认 data/bili_servey/author_mining.md）")
    bam.set_defaults(func=cmd_bili_author_mine)

    # ── bili-author-scan (收藏夹锚点→作者发布视频维度扫描) ──
    bas = sp.add_parser("bili-author-scan",
                        help="从收藏锚点扫作者发布的视频：计算机类>阈值 → AI 确认领域 → 注册表")
    bas.add_argument("--fav-id", type=int, required=True, help="收藏夹 ID")
    bas.add_argument("--anchors", type=int, default=150,
                     help="取收藏最新 N 条作为作者锚点（默认150=近几年的引入）")
    bas.add_argument("--mids", default="", help="只扫指定作者 mid，逗号分隔（测试用）")
    bas.add_argument("--threshold", type=int, default=20,
                     help="作者发布的视频里计算机类 >N 才算宝藏（默认20）")
    bas.add_argument("--max-pages", type=int, default=5,
                     help="每作者最多拉多少页视频（30条/页，按播放排序抽样）")
    bas.add_argument("--top", type=int, default=30, help="确认领域时给 AI 看的播放 Top-N 标题数")
    bas.add_argument("--batch", type=int, default=20, help="标题粗分类的批量大小")
    bas.add_argument("--output", default="", help="注册表路径（默认 data/bili_servey/treasure_authors.json）")
    bas.set_defaults(func=cmd_bili_author_scan)

    # ── arxiv ──
    a = sp.add_parser("arxiv", help="arXiv 批量研读价值汇总")
    a.add_argument("--input", required=True, help="输入文件（JSON 或 Markdown）")
    a.add_argument("--source-format", choices=["json", "md"], default="json", help="输入格式")
    a.add_argument("--output", default=str(_paths.arxiv_analysis), help="输出目录")
    a.add_argument("--model", default="v4flash", help="模型名称")
    a.add_argument("--workers", type=int, default=4, help="并行数")
    a.add_argument("--user-profile", default="", help="用户画像文本")
    a.add_argument("--user-profile-file", default="", help="用户画像文件路径")
    a.add_argument("--item-template", default="arxiv_value", help="单篇模板名称")
    a.add_argument("--batch-template", default="arxiv_batch", help="汇总模板名称")
    a.set_defaults(func=cmd_arxiv)

    # ── search (快捷) ──
    s = sp.add_parser("search", help="快速搜索 arXiv")
    s.add_argument("--query", default="", help="搜索关键词")
    s.add_argument("--raw-query", default="", help="原始 arXiv API 查询表达式")
    s.add_argument("--title", default="", help="按标题搜索")
    s.add_argument("--author", default="", help="按作者搜索")
    s.add_argument("--category", default="", help="按分类搜索，例如 cs.CL")
    s.add_argument("--abstract", default="", help="按摘要搜索")
    s.add_argument("--date-from", default="", help="起始日期 (YYYYMMDD)")
    s.add_argument("--date-to", default="", help="截止日期 (YYYYMMDD)")
    s.add_argument("--start", type=int, default=0, help="结果偏移量")
    s.add_argument("--max-results", type=int, default=10, help="最大结果数")
    s.add_argument("--sort-by", default="relevance", choices=["relevance", "lastUpdatedDate", "submittedDate"])
    s.add_argument("--sort-order", default="descending", choices=["ascending", "descending"])
    s.add_argument("--output", default=str(_paths.search), help="输出目录（用于导出）")
    s.add_argument("--export-json", action="store_true", help="导出 JSON")
    s.add_argument("--export-md", action="store_true", help="导出 Markdown")
    s.add_argument("--auto-submit", action="store_true", help="自动将第一篇论文下载并提交到远程 PDF 转换")
    s.set_defaults(func=cmd_search)

    # ── survey (文献调研) ──
    sv = sp.add_parser("survey", help="arXiv 文献调研：搜索 → 排序 → 下载")
    sv.add_argument("--raw-query", required=True, help="arXiv API 查询表达式")
    sv.add_argument("--max-papers", type=int, default=200, help="搜索论文总数上限（默认 200）")
    sv.add_argument("--top-n", type=int, default=15, help="下载 top N 篇 PDF（默认 15，0 为不下载）")
    sv.add_argument("--output", default="", help="输出目录（默认 output/survey/）")
    sv.add_argument("--download", action="store_true", help="下载 top N 的 PDF")
    sv.add_argument("--workers", type=int, default=5, help="OpenAlex 并发线程数（默认 5）")
    sv.add_argument("--sort-citation-weight", type=float, default=0.5, help="引用量排序权重")
    sv.add_argument("--sort-ccf-weight", type=float, default=0.3, help="CCF 等级排序权重")
    sv.add_argument("--sort-time-weight", type=float, default=0.2, help="时效排序权重")
    sv.set_defaults(func=cmd_survey)

    # ── pdf (远程) ──
    pdf_p = sp.add_parser("pdf", help="远程 PDF 批量转换")
    pdf_p.add_argument("--files", nargs="*", default=None, help="指定 PDF 文件路径（不指定则自动扫描 pdfs/ 目录）")
    pdf_p.add_argument("--translate", action="store_true", help="标记此批 PDF 后续需要翻译（配合 `translate --auto` 使用）")
    pdf_p.set_defaults(func=cmd_pdf)

    # ── mineru (远程 mineru 转换) ──
    m = sp.add_parser("mineru", help="远程 mineru 转换 pdf/pptx/docx/xlsx/图片 → Markdown")
    m.add_argument("--files", nargs="*", default=None, help="指定本地文件路径（pdf/pptx/docx/xlsx/图片）")
    m.add_argument("--dir", default=None, help="扫描本地目录下所有支持格式文件（可与 --files 叠加）")
    m.add_argument("--method", choices=["auto", "txt", "ocr"], default=None,
                   help="解析方法（默认交给 mineru 自动决定）")
    m.add_argument("--lang", default=None, help="文档语言（pipeline 后端改善 OCR）")
    m.add_argument("--backend", default="pipeline", help="mineru 后端（默认 pipeline）")
    m.add_argument("--formula", choices=["True", "False"], default=None, help="公式解析开关")
    m.add_argument("--table", choices=["True", "False"], default=None, help="表格解析开关")
    m.set_defaults(func=cmd_mineru)

    # ── chat (文件对话) ──
    c = sp.add_parser("chat", help="文件对话：上传文件夹全部文件 + 提问，AI 一次性回答")
    c.add_argument("--input", required=True, help="待分析的文件夹路径")
    c.add_argument("--question", "-q", default="", help="向 AI 提出的问题")
    c.add_argument("--question-file", default="", help="从文件读取问题（优先级高于 --question）")
    c.add_argument("--model", default="deepseek-chat", help="模型名称（默认 deepseek-chat）")
    c.add_argument("--search", action="store_true", help="启用联网搜索 (仅部分第三方 DeepSeek 平台支持，官方 api.deepseek.com 无此能力)")
    c.add_argument("--think", action="store_true", help="启用深度思考 (thinking)")
    c.add_argument("--output", default=str(_paths.file_chat), help="输出目录")
    c.set_defaults(func=cmd_chat)

    # ── persona (人物画像提取) ──
    pp = sp.add_parser("persona", help="从 AI 对话记录提取人物画像要点")
    pp.add_argument("--config", default="", help="配置文件路径 (JSON)，可包含所有参数")
    pp.add_argument("--input", default="", help="对话记录文件路径（必须，可在 config 中提供）")
    pp.add_argument("--output", default="", help="输出目录（默认 output/persona/）")
    pp.add_argument("--model", default="v4flash", help="模型名称")
    pp.add_argument("--user-pattern", default=r"^##\s*【您】", help="用户段落标记正则")
    pp.add_argument("--ai-pattern", default=r"^##\s*【对方】", help="AI 段落标记正则")
    pp.add_argument("--story", default="", help="故事/对话背景设定（注入 system prompt）")
    pp.add_argument("--story-file", default="", help="从文件读取故事背景")
    pp.add_argument("--task-prompt", default="", help="本次提取的额外要求（注入 user prompt）")
    pp.add_argument("--task-prompt-file", default="", help="从文件读取额外要求")
    pp.add_argument("--workers", type=int, default=4, help="并行线程数")
    pp.add_argument("--force", action="store_true", help="强制重新处理（忽略已有进度）")
    pp.set_defaults(func=cmd_persona)

    # ── warmup (预热/自检/收尾) ──
    w = sp.add_parser("warmup", help="环境自检 + 自动拉取远端结果")
    w.add_argument("--output", default=str(_paths.remote_pdf), help="结果存放目录")
    w.set_defaults(func=cmd_warmup)

    # ── reconcile (远程任务旧账整理) ──
    rc = sp.add_parser("reconcile", help="盘点整理远程任务：归档已转源文件、删除孤儿记录")
    rc.set_defaults(func=cmd_reconcile)

    # ── condense (论文浓缩) ──
    cd = sp.add_parser("condense", help="浓缩已转换的论文 md → knowledgebase/papers/{arxiv_id}/condensation.md")
    cd.add_argument("--survey", default=None, help="survey 任务 id（浓缩其 convert 产物目录）")
    cd.add_argument("--dir", default=None, help="md 目录（直接浓缩其中所有 .md）")
    cd.add_argument("--model", default="deepseek-chat", help="LLM 模型（默认 deepseek-chat）")
    cd.set_defaults(func=cmd_condense)

    # ── archive (归档散乱产物) ──
    ar = sp.add_parser("archive", help="归档散乱产物到统一结构 knowledgebase/papers|videos|docs/（补 frontmatter + metadata.json）")
    ar.set_defaults(func=cmd_archive)

    # ── rag-ask（已下线，只留提示壳）──
    ra = sp.add_parser("rag-ask", help="【已下线】改用 kb-ask / kb-fetch；留着只为打印迁移提示")
    ra.add_argument("query", nargs="?", default="", help="查询文本")
    ra.add_argument("--papers", default=None, help="指定论文 arxiv_id（逗号分隔），直接取全文")
    from ragsearch.config import KNOWLEDGE_BASES
    _kb_choices = ["all"] + list(KNOWLEDGE_BASES.keys())
    ra.add_argument("--kb", default="all", choices=_kb_choices,
                    help="检索库（默认 all，动态读 registry）")
    ra.add_argument("--top-k", type=int, default=10)
    ra.add_argument("--dense", action="store_true",
                    help="dense 段落语义检索（bge-m3，需先 ragsearch-index；用 conda infosystem 环境跑）")
    ra.add_argument("--tags", default=None, help="tags 过滤（逗号分隔，任一命中；如 实验室内）")
    ra.add_argument("--section", default=None, help="section 子串过滤")
    ra.add_argument("--source-type", default=None, help="source_type 精确过滤")
    ra.set_defaults(func=cmd_rag_ask)

    # ── ragsearch-index（已下线，只留提示壳）──
    ri = sp.add_parser("ragsearch-index", help="【已下线】改用 kb-scan + kb-embed；留着只为打印迁移提示")
    ri.add_argument("--kb", default="papers", help="知识库名（默认 papers）")
    ri.add_argument("--no-rebuild", action="store_true", help="有现成索引则直接加载不重建")
    ri.add_argument("--list-kbs", action="store_true", help="列出已登记知识库")
    ri.set_defaults(func=cmd_ragsearch_index)

    # ── fill-meta (补齐元信息) ──
    fm = sp.add_parser("fill-meta", help="补齐论文缺失元信息：解析 arxiv_id/DOI → frontmatter + metadata.json")
    fm.add_argument("--papers", default=None, help="指定论文 arxiv_id/DOI-slug（逗号分隔）")
    fm.add_argument("--doi", default=None, help="按 DOI 解析（打印；若反推 arxiv_id 且在库则填充）")
    fm.add_argument("--all", action="store_true", help="扫描 full/papers 缺 authors/year 的论文批量补齐（需显式声明）")
    fm.add_argument("--dry-run", action="store_true", help="只列出将处理/解析的内容，不写入")
    fm.set_defaults(func=cmd_fill_meta)

    # ── rag-folder (任意文件夹临时检索) ──
    rf = sp.add_parser("rag-folder", help="在任意文件夹里立刻 BM25 检索（不向量化/不进知识库/纯 AI 用）")
    rf.add_argument("folder", help="目标文件夹（假定内含 .md）")
    rf.add_argument("query", nargs="?", default="", help="查询文本")
    rf.add_argument("--top-k", type=int, default=10)
    rf.add_argument("--section", default=None)
    rf.add_argument("--tags", default=None)
    rf.add_argument("--year", default=None)
    rf.add_argument("--authors", default=None)
    rf.add_argument("--source-type", default=None)
    rf.add_argument("--group", action="store_true", help="按 doc_id 分组折叠")
    rf.set_defaults(func=cmd_rag_folder)

    # ── add-paper (DOI/arxiv 元数据入库) ──
    ap = sp.add_parser("add-paper", help="从 DOI 或 arxiv_id 创建论文 KB 条目（元数据入库，无需文件）")
    ap.add_argument("--doi", default=None, help="DOI，如 10.1007/978-3-030-12345-6")
    ap.add_argument("--arxiv", default=None, help="arxiv_id，如 2302.08496")
    ap.set_defaults(func=cmd_add_paper)

    # ── video-import (批量导入 B站视频) ──
    vi = sp.add_parser("video-import", help="批量导入 B站视频：bvid/URL → 采集 → 提取字幕+摘要 → 晋升 dense/video")
    vi.add_argument("--bvids", default=None, help="逗号分隔的 BV 号或视频 URL")
    vi.add_argument("--file", default=None, help="含 bvid 的文本文件（每行一个）")
    vi.add_argument("--no-extract", action="store_true", help="仅采集入库，不提取字幕+摘要")
    vi.add_argument("--timeout", type=int, default=1800, help="等待提取完成的最大秒数（默认 1800）")
    vi.set_defaults(func=cmd_video_import)

    # ── paper-search (多平台论文检索) ──
    ps = sp.add_parser("paper-search", help="多平台论文检索：搜索(arXiv/Crossref/OpenAlex/GS/万方/知网/ePrint/S2) → 入库 → 可选下载；--merge 合并已存检索结果；--only-sources/--exclude-sources 指定源")
    ps.add_argument("query", nargs="?", default="", help="关键词（--merge 模式可省略）")
    ps.add_argument("--rows", type=int, default=15, help="每源最大条数（默认 15）")
    ps.add_argument("--no-gs", action="store_true", help="跳过 Google Scholar（Clash 离线时）")
    ps.add_argument("--only-sources", default=None, help="只用指定源（逗号分隔：crossref/openalex/arxiv/google_scholar/wanfang/cnki/eprint），其余全关")
    ps.add_argument("--exclude-sources", default=None, help="排除指定源（逗号分隔，同上取值），其余保留")
    ps.add_argument("--max-download", type=int, default=0, help="开放版下载前 N 篇 PDF（默认 0 不下）")
    ps.add_argument("--inst-download", type=int, default=0, help="机构版(知网/万方/IEEE/ACM)下载 N 篇（默认 0 不下；万方每篇约扣机构 3 元；IEEE/ACM 走 elib 代理）")
    ps.add_argument("--inst-proxy", default="auto", choices=["auto", "socks5", "none"],
                    help="机构版下载网络路由：**auto/none 都是直连**（elib 反向代理自带机构身份，不该走隧道）；"
                         "socks5=显式强制 wssocks 隧道（仅当某源确实按源 IP 判权且无代理通道时才用；用于 elib 会超时）")
    ps.add_argument("--download-dir", default=None, help="PDF 落点目录（默认 data/pdfs/）")
    ps.add_argument("--title", default=None, help="标题过滤（arXiv ti:/OpenAlex title.search/Crossref query.title）")
    ps.add_argument("--author", default=None, help="作者过滤")
    ps.add_argument("--year-from", type=int, default=0, help="起始年份")
    ps.add_argument("--year-to", type=int, default=0, help="结束年份")
    ps.add_argument("--category", default=None, help="arXiv 分类（如 cs.CR）")
    ps.add_argument("--db-types", default=None, help="万方文献类型（逗号分隔：journal/thesis/conference/patent/standard）")
    ps.add_argument("--convert", type=int, default=0, help="下载前 N 篇后自动提交远程 marker 转换（一条命令走完 搜索→下载→转换）")
    ps.add_argument("--search-id", default=None, help="内部：Web 提交时预生成的搜索任务 id（用于实时进度）")
    ps.add_argument("--project", default=None, help="论文调研项目 slug；搜索完成后写入 search_rounds.jsonl（须先 paper-project init）")
    # --merge 模式：合并已存检索结果（不发起新搜索）
    ps.add_argument("--merge", action="store_true", help="合并 data/paper_searches/ 已存结果（去重→过滤→排序），不搜索")
    ps.add_argument("--searches", default=None, help="指定合并哪些搜索 id（逗号分隔；默认全部）")
    ps.add_argument("--keywords", default=None, help="课题关键词过滤（逗号分隔，标题任一命中）")
    ps.add_argument("--top-n", type=int, default=0, help="取引用排序前 N 篇（默认全部）")
    ps.add_argument("--min-citations", type=int, default=0, help="最小引用数门槛")
    ps.add_argument("--export-json", default=None, help="导出合并清单到 JSON 文件")
    ps.add_argument("--import", action="store_true", help="把筛选后的 top-N 批量建 KB 元数据条目（幂等）")
    ps.add_argument("--list-searches", action="store_true", help="列出已存检索记录")
    ps.set_defaults(func=cmd_paper_search)

    # ── download-papers (定向批量下载) ──
    dp = sp.add_parser("download-papers", help="按 arxiv_id/DOI 清单精确批量下载 PDF → data/papers/{id}/paper.pdf")
    dp.add_argument("--arxiv", default=None, help="arxiv_id 逗号分隔")
    dp.add_argument("--doi", default=None, help="DOI 逗号分隔")
    dp.add_argument("--file", default=None, help="清单文件，每行一个 arxiv_id 或 DOI")
    dp.add_argument("--import-meta", action="store_true", help="下载同时建 KB 元数据条目（幂等）")
    dp.set_defaults(func=cmd_download_papers)

    # ── paper-project (论文调研项目) ──
    pp = sp.add_parser("paper-project", help="论文调研项目：init/status/edit/add/remove/move/run/list/import-pdf")
    pp.add_argument("action", choices=["init", "status", "edit", "add", "remove", "move", "run", "list", "import-pdf"],
                    help="子动作")
    pp.add_argument("--project", default="", help="项目 slug（status/edit/add/remove/move/run/import-pdf 必填）")
    pp.add_argument("--name", default="", help="项目名（init）")
    pp.add_argument("--question", default="", help="研究问题（init）")
    pp.add_argument("--keywords", default="", help="关键词逗号分隔（init/basic-matrix）")
    pp.add_argument("--venues", default="", help="目标期刊/会议逗号分隔（init）")
    pp.add_argument("--criteria", default="", help="筛选标准，如 include:近5年;exclude:纯工程")
    pp.add_argument("--papers", default="", help="论文 id 逗号分隔（add/remove/move/import-pdf）")
    pp.add_argument("--search-id", default=None, help="从某次 paper-search 记录导入候选（add）")
    pp.add_argument("--reason", default="", help="行为说明")
    pp.add_argument("--to", default="", help="目标项目 slug（move）")
    pp.add_argument("--stage", default="", help="run 阶段：search/basic-matrix/semantic-matrix/classify/stats/refs")
    pp.add_argument("--model", default="deepseek-chat", help="LLM 模型（classify/semantic-matrix）")
    pp.add_argument("--set", default="", help="人工覆盖字段，如 research_question=新的问题")
    pp.add_argument("--set-paper-role", default="", help="确认角色，如 2302.08496=key")
    pp.add_argument("--note", default="", help="人工说明")
    pp.add_argument("--pdf", default="", help="手动导入的 PDF 路径（import-pdf）")
    pp.set_defaults(func=cmd_paper_project)

    # ── paper-matrix (文献矩阵) ──
    pmx = sp.add_parser("paper-matrix", help="文献矩阵：--basic 无AI基础矩阵 / --semantic AI语义矩阵")
    pmx.add_argument("--project", required=True, help="项目 slug")
    pmx.add_argument("--basic", action="store_true", help="生成基础矩阵")
    pmx.add_argument("--semantic", action="store_true", help="生成语义矩阵")
    pmx.add_argument("--papers", default="", help="论文 id 逗号分隔（可选，默认项目全部）")
    pmx.add_argument("--keywords", default="", help="关键词逗号分隔（基础矩阵打分用）")
    pmx.add_argument("--model", default="deepseek-chat", help="LLM 模型（语义矩阵）")
    pmx.set_defaults(func=cmd_paper_matrix)

    # ── paper-classify (角色分类) ──
    pcl = sp.add_parser("paper-classify", help="文献角色分类：--candidate 生成候选 / --status 查看")
    pcl.add_argument("--project", required=True, help="项目 slug")
    pcl.add_argument("--candidate", action="store_true", help="LLM 生成候选角色")
    pcl.add_argument("--status", action="store_true", help="查看已确认/候选数量")
    pcl.add_argument("--papers", default="", help="论文 id 逗号分隔（可选）")
    pcl.add_argument("--model", default="deepseek-chat", help="LLM 模型")
    pcl.set_defaults(func=cmd_paper_classify)

    # ── paper-refs (参考文献权威挖掘) ──
    prf = sp.add_parser("paper-refs", help="参考文献重要权威挖掘（OpenAlex；失败可本地频次兜底）")
    prf.add_argument("--project", required=True, help="项目 slug")
    prf.set_defaults(func=cmd_paper_refs)

    # ── paper-stats (统计报告) ──
    pst = sp.add_parser("paper-stats", help="论文调研项目统计报告（Markdown + JSON，优先给 AI）")
    pst.add_argument("--project", required=True, help="项目 slug")
    pst.set_defaults(func=cmd_paper_stats)

    # ── project-import (代码项目入库) ──
    pj = sp.add_parser("project-import", help="代码项目入库：GitHub/GitLab/npm/Gitee/Bing 拉元数据 → {调研分区}/projects/{id}/（与论文同组）；含 render/matrix/scan/批量")
    pj.add_argument("query", nargs="?", default="", help="仓库 URL 或项目名（owner/repo 直接 gh api 精确查；纯名字先 gh search repos）")
    pj.add_argument("--kb", required=False, help="调研分区（1-3-VERA / 3-IMA ...），项目挂到 {kb}/projects/")
    pj.add_argument("--host", default=None, help="强制指定 host：github/gitlab/npm/gitee/official")
    pj.add_argument("--section", default="", help="检索用主题分类（默认未分类）")
    pj.add_argument("--tags", default="", help="关键词标签，逗号分隔（manual_tags 与 GitHub topics 去重合并）")
    pj.add_argument("--display-name", default="", help="人工规范名（如 HEIR，覆盖 GitHub 小写名）")
    pj.add_argument("--cn-note", default="", help="中文一句话（description 英文之外的补充）")
    pj.add_argument("--file", default=None, help="批量导入清单（每行一个 URL/名字；# 注释跳过）")
    pj.add_argument("--force", action="store_true", help="覆盖已存在的项目目录")
    pj.add_argument("--list-stale", type=int, default=0, help="列出 last_verified 超期 N 天的项目")
    pj.add_argument("--render", default="", help="把某分区所有项目的 metadata 富字段渲染回 project.md（B1）；可加 position 指定单项目")
    pj.add_argument("--pid", default=None, help="与 --render 搭配：只渲染指定项目")
    pj.add_argument("--matrix", default="", help="生成某分区的项目生态关系矩阵（F4）")
    pj.add_argument("--scan", default="", help="从某分区论文 md 提取 GitHub/GitLab 链接 → 候选清单（F1）")
    pj.add_argument("--list-candidates", default="", help="显示某分区的项目候选清单（B3）")
    pj.add_argument("--validate", default="", help="校验某分区项目的富字段 schema（D4）")
    pj.set_defaults(func=cmd_project_import)

    # ── rag-migrate (库↔库批量迁移论文) ──
    rm = sp.add_parser("rag-migrate", help="库↔库批量迁移论文（copy/move，按 tags/ids/section/source_type/year 筛选，目录级整体搬）")
    rm.add_argument("--from", dest="frm", required=True, help="源库名（registry.json 里的 key）")
    rm.add_argument("--to", required=True, help="目标库名")
    rm.add_argument("--ids", default=None, help="论文 id 逗号分隔")
    rm.add_argument("--tags", default=None, help="tag 逗号分隔（任一命中）")
    rm.add_argument("--section", default=None, help="section 子串")
    rm.add_argument("--source-type", default=None, help="source_type 精确")
    rm.add_argument("--year", default=None, help="年份精确")
    rm.add_argument("--move", action="store_true", help="移动（复制后删源；默认复制）")
    rm.add_argument("--force", action="store_true", help="目标已存在时覆盖（覆盖前不备份）")
    rm.add_argument("--dry-run", action="store_true", help="只列清单不执行")
    rm.set_defaults(func=cmd_rag_migrate)

    # ── paper-cookie (导出/刷新登录 cookie) ──
    pc = sp.add_parser("paper-cookie", help="导出/刷新知网/万方登录 cookie（Playwright 手动登录，保存 storage_state）")
    pc.add_argument("target", choices=["cnki", "wanfang"], help="哪个站：cnki 知网 / wanfang 万方")
    pc.set_defaults(func=cmd_paper_cookie)

    # ── import-papers (外部论文文件夹导入) ──
    ip = sp.add_parser("import-papers", help="从外部论文文件夹导入（md+pdf+图，防丢；copy 不 move 源）")
    ip.add_argument("--src", default=r"C:\Users\HP\Desktop\开题新\搜集相关论文\实验室外",
                    help="外部论文文件夹（含 {arxiv_id}_{title}.md / .pdf / 图子目录）")
    ip.set_defaults(func=cmd_import_papers)

    # ── ocr (本地 Markdown 图片识别) ──
    o = sp.add_parser("ocr", help="对本地 .md 文件中的图片进行 OCR 识别（默认生成 {stem}_ocr.md 新文件）")
    o.add_argument("--file", default=None, help="处理单个 .md 文件（不指定则处理 data/ocr_docs/ 下全部）")
    o.add_argument("--model", default="glm-5v-turbo_ocr",
                   choices=["glm-5v-turbo_ocr", "glm-4.6v_ocr", "qwen-vl-max_ocr", "paddleocr-vl1.6_ocr"],
                   help="视觉模型：glm-5v-turbo_ocr（默认），glm-4.6v_ocr，qwen-vl-max_ocr，paddleocr-vl1.6_ocr")
    o.add_argument("--no-parallel", action="store_true", help="禁用并行处理（逐张识别）")
    o.add_argument("--retry", action="store_true", help="重试模式：跳过已有 OCR 结果的图片，只处理未识别的")
    o.add_argument("--inplace", action="store_true", help="原地修改源 .md（默认生成 {stem}_ocr.md 新文件，保留原文件）")
    o.add_argument("--min-size-kb", type=int, default=7,
                   help="小于该尺寸的图片跳过（疑似装饰图/头像/二维码），默认 7KB")
    o.add_argument("--no-strip-refs", action="store_true", help="不去除末尾 References/参考文献 段（默认去除）")
    o.set_defaults(func=cmd_ocr)

    # ── ocr-smart (智能 OCR：本地分类 + 双模型独立输出) ──
    so = sp.add_parser("ocr-smart", help="智能 OCR：本地复杂度二分类后分别调 GLM-4.6V/5V，输出独立 md")
    so.add_argument("--dir", required=True, help="图片文件夹路径")
    so.add_argument("--output", default=None, help="输出 md 文件名；缺省为 <dir>/ocr_result.md")
    so.add_argument("--dry-run", action="store_true", help="只分类不调用 GLM API，不写输出文件")
    so.set_defaults(func=cmd_smart_ocr)

    # ── translate (远程翻译) ──
    t = sp.add_parser("translate", help="远程翻译已转换的 .md 文件（需服务端部署 translate.py）")
    t.add_argument("--auto", action="store_true", help="自动翻译所有标记了需翻译的已完成 convert job")
    t.add_argument("--files", nargs="*", default=None, help="指定远程 .md 文件名（已在远程 output 目录中）")
    t.add_argument("--upload-md", default=None, help="上传本地 .md 文件并翻译")
    t.add_argument("--model", default="qwen3:8b", choices=["qwen3:8b", "qwen3:32b"],
                   help="翻译模型（默认 qwen3:8b）")
    t.add_argument("--content-type", default="paper", choices=["paper", "other"],
                   help="内容类型：paper=论文（默认），other=其他内容")
    t.add_argument("--translate-prompt", default=None,
                   help="自定义翻译提示词（覆盖默认 prompt，仅 content-type=other 时推荐）")
    t.add_argument("--no-detect", action="store_true",
                   help="跳过中文占比检测，强制翻译")
    t.set_defaults(func=cmd_translate)

    # ══════════════════════════════════════════════
    #  remote-fs（远程文件系统）
    # ══════════════════════════════════════════════

    # ── remote-fs list ──
    rl = sp.add_parser("remote-fs-list", help="列举远程目录")
    rl.add_argument("path", help="远程路径")
    rl.set_defaults(func=cmd_remote_fs_list)

    # ── remote-fs tree ──
    rt = sp.add_parser("remote-fs-tree", help="打印远程目录树")
    rt.add_argument("path", help="远程路径")
    rt.add_argument("--depth", type=int, default=-1, help="最大递归深度（默认不限）")
    rt.set_defaults(func=cmd_remote_fs_tree)

    # ── remote-fs search ──
    rs = sp.add_parser("remote-fs-search", help="精确搜索远程文件夹名")
    rs.add_argument("name", help="要搜索的文件夹名")
    rs.add_argument("--root", default="/", help="搜索起点（默认 /）")
    rs.add_argument("--tree", action="store_true", help="唯一匹配时自动打印目录树")
    rs.set_defaults(func=cmd_remote_fs_search)

    # ── remote-fs download ──
    rd = sp.add_parser("remote-fs-download", help="下载远程文件夹为 tar.gz")
    rd.add_argument("path", help="远程文件夹路径")
    rd.add_argument("--output", "-o", default=".", help="本地保存路径（.zip→ZIP / .tar.gz→tar.gz，默认 tar.gz）")
    rd.set_defaults(func=cmd_remote_fs_download)

    # ── remote-fs download-file ──
    rdf = sp.add_parser("remote-fs-download-file", help="下载远程单个文件")
    rdf.add_argument("path", help="远程文件路径")
    rdf.add_argument("--output", "-o", required=True, help="本地保存路径")
    rdf.set_defaults(func=cmd_remote_fs_download_file)

    # ── remote-fs download-name ──
    rdn = sp.add_parser("remote-fs-download-name", help="按名字定位远程文件夹并下载")
    rdn.add_argument("parent", help="远程父目录路径")
    rdn.add_argument("name", help="要下载的文件夹名")
    rdn.add_argument("--output", "-o", default=".", help="本地保存目录")
    rdn.set_defaults(func=cmd_remote_fs_download_name)

    # ── kb-* (私域知识库框架) ──
    from backend.services.kb.cli import register_kb_parsers
    register_kb_parsers(sp)

    # ── backlog（多来源备选调研方向）──
    from backend.services.community.backlog import register_parser as register_backlog_parser
    register_backlog_parser(sp)

    # ── tieba（贴吧抓取与知识点提炼）──
    from backend.services.community.tieba.cli import register_parser as register_tieba_parser
    register_tieba_parser(sp)

    # ── qq（QCE 导出 + 归档管线封装）──
    from backend.services.community.qq.cli import register_parser as register_qq_parser
    register_qq_parser(sp)

    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())


