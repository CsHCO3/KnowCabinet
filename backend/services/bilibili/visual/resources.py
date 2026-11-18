# -*- coding: utf-8 -*-
"""从视频简介/页面文字里抽资源地址，写进「备选调研方向」(community/backlog)。

用户的规则：作者在简介等地方给出的资源地址（GitHub 为主），要收进那个所有信息源共用的
待调研清单里；视频站自身、社交短链不算资源。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse


URL_RE = re.compile(r"https?://[^\s<>\"'）)\]】，。；、]+")

VIDEO_HOSTS = ("bilibili.com", "b23.tv", "youtube.com", "youtu.be",
               "douyin.com", "x.com", "twitter.com", "weibo.com")
SOCIAL_HOSTS = ("weixin.qq.com", "t.me", "discord.gg", "discord.com", "zhihu.com/people")

CODE_HOSTS = ("github.com", "gitee.com", "gitlab.com", "codeberg.org", "sourceforge.net")
MODEL_HOSTS = ("huggingface.co", "modelscope.cn", "pypi.org", "npmjs.com", "crates.io")


def _host(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower()
    except Exception:
        return ""


def _clean_url(url: str) -> str:
    url = url.strip().rstrip(".,;:!?。，；：")
    # 中括号/圆括号收尾常见于 markdown 链接
    return url.rstrip(")]>\"'")


def _kind_tags(host: str) -> tuple[str, list[str]]:
    if any(host.endswith(h) or host == h for h in CODE_HOSTS):
        return "project", ["项目", "备选调研"]
    if any(host.endswith(h) for h in MODEL_HOSTS):
        return "tool", ["工具", "备选调研"]
    return "tutorial", ["资料", "备选调研"]


def _is_noise(url: str, host: str) -> bool:
    """用户主页、issue 列表这类不算"待调研的资源"，收了只会让清单变脏。"""
    path = urlparse(url).path.rstrip("/")
    if any(host.endswith(h) for h in ("github.com", "gitee.com", "gitlab.com")):
        seg = [x for x in path.split("/") if x]
        if len(seg) <= 1:                     # 用户/组织主页
            return True
        if seg[-1] in ("issues", "pulls", "releases", "commits", "tree"):
            return True
    return False


def extract(text: str, *, title: str = "") -> list[dict]:
    """逐行扫 URL；用所在行的说明当标题，没有就用 host/path。"""
    found: list[dict] = []
    seen: set[str] = set()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        for m in URL_RE.finditer(line):
            url = _clean_url(m.group(0))
            host = _host(url)
            if not url or not host:
                continue
            if any(host.endswith(h) for h in VIDEO_HOSTS + SOCIAL_HOSTS):
                continue
            if _is_noise(url, host):
                continue
            if url in seen:
                continue
            seen.add(url)
            label = URL_RE.sub("", line).strip(" -|：:·—>《》【】()（）｜")
            label = re.sub(r"\s{2,}", " ", label).strip(" ｜|：:")[:40]
            kind, tags = _kind_tags(host)
            found.append({
                "title": label or (host + urlparse(url).path)[:60],
                "url": url,
                "kind": kind,
                "tags": tags,
                "note": f"来自视频《{title}》简介" if title else "",
            })
    return found


from .paths import VISUAL_ROOT
STAGING = VISUAL_ROOT / "_backlog_staging.jsonl"


def _stage(items: list[dict], source: str, source_ref: str) -> None:
    """knowledgebase/ 在沙箱外写不进去：先落暂存，最后一条命令统一提权入库。"""
    STAGING.parent.mkdir(parents=True, exist_ok=True)
    known = set()
    if STAGING.exists():
        for line in STAGING.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    known.add(json.loads(line).get("url", ""))
                except json.JSONDecodeError:
                    pass
    with STAGING.open("a", encoding="utf-8", newline="\n") as f:
        for it in items:
            if it["url"] in known:
                continue
            known.add(it["url"])
            f.write(json.dumps({**it, "source": source, "source_ref": source_ref},
                               ensure_ascii=False) + "\n")


def push(items: list[dict], *, source: str = "bilibili", source_ref: str = "") -> int:
    """写进 backlog，返回真正新增的条数；写不了就暂存，不中断流程。"""
    if not items:
        return 0
    from backend.services.community.backlog import add_item

    created = 0
    for it in items:
        try:
            _, new = add_item(
                title=it["title"], url=it["url"], note=it.get("note", ""),
                source=source, source_ref=source_ref,
                tags=it.get("tags") or ["项目", "备选调研"],
                kind=it.get("kind", "project"), update=True,
            )
        except (PermissionError, OSError) as e:
            _stage(items, source, source_ref)
            print(f"    backlog 暂存（{type(e).__name__}），最后统一入库：{it['url']}")
            return 0
        created += int(new)
        print(f"    {'新增' if new else '已存在'} {it['url']}")
    return created


def apply_staged(path: str = "") -> int:
    """把暂存项真正写进 backlog（要提权跑：knowledgebase/ 在沙箱外）。"""
    import json as _json
    p = Path(path) if path else STAGING
    if not p.exists():
        print("没有暂存文件")
        return 0
    from backend.services.community.backlog import add_item

    n = 0
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        it = _json.loads(line)
        _, new = add_item(title=it["title"], url=it["url"], note=it.get("note", ""),
                          source=it.get("source", "bilibili"),
                          source_ref=it.get("source_ref", ""),
                          tags=it.get("tags") or ["项目", "备选调研"],
                          kind=it.get("kind", "project"), update=True)
        n += int(new)
        print(f"  {'新增' if new else '已存在'} {it['url']}")
    print(f"入库完成：新增 {n} 条，暂存 {p}")
    return n


if __name__ == "__main__":
    # 调试：python resources.py <desc 文本文件>
    if len(sys.argv) > 2 and sys.argv[1] == "apply":
        apply_staged(sys.argv[2])
        sys.exit(0)
    p = Path(sys.argv[1])
    for x in extract(p.read_text(encoding="utf-8"), title=p.stem):
        print(x["kind"], x["url"], "|", x["title"])
