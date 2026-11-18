"""tieba CLI：抓取、搜索、收藏和知识点提炼。"""

from __future__ import annotations

import argparse
import json
import sys


def _utf8():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and (stream.encoding or "").lower() != "utf-8":
            stream.reconfigure(encoding="utf-8", errors="replace")


def _print_json(value):
    _utf8()
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _service():
    from backend.services.community.tieba.service import TiebaService
    return TiebaService()


def cmd_thread(args: argparse.Namespace) -> int:
    _print_json(_service().thread(args.tid, page=args.page))
    return 0


def cmd_forum(args: argparse.Namespace) -> int:
    _print_json(_service().forum(args.forum, page=args.page))
    return 0


def cmd_featured(args: argparse.Namespace) -> int:
    _print_json(_service().featured(args.forum, page=args.page))
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    _print_json(_service().search(args.forum, args.keyword, page=args.page, max_pages=args.max_pages))
    return 0


def cmd_favorites(args: argparse.Namespace) -> int:
    result = _service().favorites(max_pages=args.max_pages)
    if args.urls:
        for item in result.get("favorites", []):
            print(item.get("url", ""))
    else:
        _print_json(result)
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    from backend.config.settings import Settings
    service = _service()
    llm = Settings().get_llm_client(args.model)
    result = service.extract(args.tid, llm=llm, update=not args.rebuild, model_key=args.model)
    if args.json:
        _print_json(result)
    else:
        print(
            f"[OK] {result['mode']}：候选 {result['candidate_records']}/"
            f"{result['fetched_records']}，新增 {result['new_entries']}，"
            f"更新 {result['updated_entries']}，合计 {result['total_entries']} 条"
        )
        print(f"知识文件：{result['path']}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    result = _service().load_knowledge(args.tid)
    if not result:
        print(f"未找到 {args.tid} 的知识点文件")
        return 2
    _print_json(result)
    return 0


def cmd_cookie_login(args: argparse.Namespace) -> int:
    from backend.services.community.tieba.legacy_client import TiebaClient
    from backend.services.community.tieba.paths import COOKIE_FILE
    client = TiebaClient()
    client.save_cookies(args.bduss, args.stoken, args.baiduid)
    print(f"Cookie 已保存：{COOKIE_FILE}")
    return 0


def cmd_cookie_check(args: argparse.Namespace) -> int:
    from backend.services.community.tieba.legacy_client import check_stored_cookie
    status = check_stored_cookie()
    _print_json(status)
    return 0 if status.get("logged_in") else 2


def register_parser(sp: argparse._SubParsersAction) -> None:
    parser = sp.add_parser("tieba", help="贴吧抓取与知识点提炼")
    sub = parser.add_subparsers(dest="action", required=True)

    thread = sub.add_parser("thread", help="读取帖子；只输出，不作为长期档案保存")
    thread.add_argument("tid")
    thread.add_argument("--page", type=int, default=None, help="只读指定页；缺省全帖")
    thread.set_defaults(func=cmd_thread)

    forum = sub.add_parser("forum", help="贴吧帖子列表")
    forum.add_argument("forum")
    forum.add_argument("--page", type=int, default=1)
    forum.set_defaults(func=cmd_forum)

    featured = sub.add_parser("featured", help="精华帖列表")
    featured.add_argument("forum")
    featured.add_argument("--page", type=int, default=1)
    featured.set_defaults(func=cmd_featured)

    search = sub.add_parser("search", help="吧内搜索")
    search.add_argument("forum")
    search.add_argument("keyword")
    search.add_argument("--page", type=int, default=1)
    search.add_argument("--max-pages", type=int, default=1)
    search.set_defaults(func=cmd_search)

    fav = sub.add_parser("favorites", help="账号收藏")
    fav.add_argument("--max-pages", type=int, default=0, help="0 表示自动翻完")
    fav.add_argument("--urls", action="store_true")
    fav.set_defaults(func=cmd_favorites)

    extract = sub.add_parser("extract", help="抓全帖并只保存知识点/证据")
    extract.add_argument("tid")
    extract.add_argument("--model", default="deepseek-chat")
    extract.add_argument("--rebuild", action="store_true", help="忽略增量状态，从空状态重建")
    extract.add_argument("--json", action="store_true")
    extract.set_defaults(func=cmd_extract)

    show = sub.add_parser("show", help="查看已保存知识点")
    show.add_argument("tid")
    show.set_defaults(func=cmd_show)

    login = sub.add_parser("login", help="录入 BDUSS/STOKEN/BAIDUID")
    login.add_argument("--bduss", required=True)
    login.add_argument("--stoken", default="")
    login.add_argument("--baiduid", default="")
    login.set_defaults(func=cmd_cookie_login)

    check = sub.add_parser("cookie-check", help="检查 Cookie")
    check.set_defaults(func=cmd_cookie_check)
