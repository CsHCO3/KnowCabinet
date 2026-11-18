"""代码项目元数据拉取器（GitHub/GitLab/npm/Gitee/Bing）。

探路结论（2026-08-18 实测）：
- GitHub：gh CLI 已登录（token 在 keyring），`gh api repos/{o}/{r}` 直连可用，字段最全。
  也可用裸 REST（api.github.com 国内直连 200）。
- GitLab：`/api/v4/projects?search=` 免认证直连，公开项目可拉 name/desc/star/forks。
- npm：`registry.npmjs.org/{name}` 免认证，拿 description/license/repository。
- Gitee：`/api/v5/repos/{o}/{r}` 免认证直连（60次/分），查公开元数据；只有
  `/search/repositories` 需 access_token（存环境变量 GITEE_TOKEN）。
- Bing：`cn.bing.com/search?q=`（www 302→cn）免认证无验证码，解析 `b_algo` 结果，
  用于有官网但无代码托管页的项目。

统一返回 dict（与 project 入库 schema 对齐）。
"""
from __future__ import annotations

import html
import json
import logging
import re
import subprocess
import urllib.parse
from typing import Any

from backend.services.papers.http_client import ROUTE_DIRECT, http_get

logger = logging.getLogger(__name__)

# ── host 识别 ────────────────────────────────────

_HOST_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("github", re.compile(r"github\.com[/:]([\w.\-]+)/([\w.\-]+)")),
    ("gitlab", re.compile(r"gitlab\.com[/:]([\w.\-]+/[\w.\-]+)")),
    ("npm", re.compile(r"npmjs\.com/package/([\w@.\-/]+)")),
    ("gitee", re.compile(r"gitee\.com[/:]([\w.\-]+)/([\w.\-]+)")),
]


def resolve_host(url_or_name: str) -> tuple[str, str | None]:
    """识别 host 与关键名（owner/repo 或包名）。返回 (host, key|None)。

    host ∈ github/gitlab/npm/gitee/official。
    - 裸 `owner/repo`（含 / 无 URL 前缀）→ 视为 GitHub（精确查 gh api repos，GitHub 会
      对重命名/别名 owner 返回重定向到真实仓库；不存在则报错）。
    - 识别不出 → official（走 gh search repos / Bing 兜底）。
    """
    s = (url_or_name or "").strip()
    for host, pat in _HOST_PATTERNS:
        m = pat.search(s.lower())
        if m:
            if host == "gitlab":
                return host, m.group(1)
            if host == "npm":
                return host, m.group(1)
            return host, f"{m.group(1)}/{m.group(2)}"
    # 裸 owner/repo 格式（无 URL 前缀，含 / 且两段都是仓库名成分）
    if "/" in s and not s.startswith(("http", "www", "git")):
        parts = s.split("/")
        if len(parts) == 2 and all(re.match(r"^[\w.\-]+$", p) for p in parts):
            return "github", s
    return "official", url_or_name or None


def _gh_api(endpoint: str) -> dict:
    """调 gh api（已登录）。返回 dict；失败抛异常。"""
    r = subprocess.run(["gh", "api", endpoint, "--jq", "."],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"gh api {endpoint} 失败: {r.stderr.strip()[:200]}")
    return json.loads(r.stdout)


# ── 各 host 拉取器 ───────────────────────────────

def fetch_github(owner_repo: str) -> dict[str, Any]:
    """GitHub：gh api repos/{owner}/{repo} + /languages（D1: 语言统计补全）。"""
    d = _gh_api(f"repos/{owner_repo}")
    lic = d.get("license") or {}
    license_name = lic.get("spdx_id") or lic.get("name") or ""
    # D1: 语言统计（主语言 + 其它语言）
    languages = _github_languages(owner_repo)
    return {
        "name": d.get("full_name", owner_repo),
        "title": d.get("name", owner_repo.split("/")[-1]),
        "url": d.get("html_url", f"https://github.com/{owner_repo}"),
        "host": "github",
        "owner": d.get("owner", {}).get("login", owner_repo.split("/")[0]),
        "description": d.get("description") or "",
        "license": license_name or "unknown",   # D5: license 空标 unknown
        "language": languages or ([d.get("language")] if d.get("language") else []),
        "stars": int(d.get("stargazers_count") or 0),
        "forks": int(d.get("forks_count") or 0),
        "archived": bool(d.get("archived")),
        "last_commit": (d.get("pushed_at") or "")[:10],
        "latest_release": _github_latest_release(owner_repo),
        "topics": d.get("topics") or [],
    }


def _github_languages(owner_repo: str) -> list[str]:
    """GitHub /languages → 按字节占比排序的语言列表（取占比≥5%的）。"""
    try:
        langs = _gh_api(f"repos/{owner_repo}/languages")
        if not isinstance(langs, dict) or not langs:
            return []
        total = sum(langs.values()) or 1
        # 按字节降序，取累计占比前 90% 的语言（或最多 4 种）
        ordered = sorted(langs.items(), key=lambda kv: -kv[1])
        out, acc = [], 0
        for name, n in ordered:
            out.append(name)
            acc += n
            if acc / total >= 0.9 or len(out) >= 4:
                break
        return out
    except Exception:  # noqa: BLE001
        return []


def _github_latest_release(owner_repo: str) -> str:
    try:
        rels = _gh_api(f"repos/{owner_repo}/releases?per_page=1")
        if rels:
            return (rels[0].get("tag_name") or "")[:30]
    except Exception:  # noqa: BLE001
        pass
    return ""


def fetch_gitlab(path: str) -> dict[str, Any]:
    """GitLab：/api/v4/projects?search= 取首个匹配；更精确用 /projects/{urlencoded path}。"""
    url = ("https://gitlab.com/api/v4/projects/"
           f"{urllib.parse.quote(path, safe='')}?simple=true")
    try:
        _, body = http_get(url, route=ROUTE_DIRECT, timeout=20, retries=2)
        d = json.loads(body.decode("utf-8"))
    except Exception:
        # fallback: search
        url = ("https://gitlab.com/api/v4/projects?search="
               f"{urllib.parse.quote(path.split('/')[-1])}&simple=true")
        _, body = http_get(url, route=ROUTE_DIRECT, timeout=20, retries=2)
        arr = json.loads(body.decode("utf-8"))
        d = arr[0] if arr else {}
    return {
        "name": d.get("path_with_namespace", path),
        "title": d.get("name", path.split("/")[-1]),
        "url": d.get("web_url", f"https://gitlab.com/{path}"),
        "host": "gitlab",
        "owner": path.split("/")[0],
        "description": d.get("description") or "",
        "license": (d.get("license") or {}).get("name", "") if d.get("license") else "",
        "language": [],
        "stars": int(d.get("star_count") or 0),
        "forks": int(d.get("forks_count") or 0),
        "archived": bool(d.get("archived")),
        "last_commit": (d.get("last_activity_at") or "")[:10],
        "latest_release": "",
        "topics": [],
    }


def fetch_npm(name: str) -> dict[str, Any]:
    """npm：registry.npmjs.org/{name}，取 latest 版本元数据。"""
    url = f"https://registry.npmjs.org/{urllib.parse.quote(name, safe='@/')}"
    _, body = http_get(url, route=ROUTE_DIRECT, timeout=20, retries=2)
    d = json.loads(body.decode("utf-8"))
    latest = d.get("dist-tags", {}).get("latest", "")
    v = d.get("versions", {}).get(latest, {})
    repo = v.get("repository") or {}
    repo_url = repo.get("url", "") if isinstance(repo, dict) else ""
    if isinstance(repo_url, str) and repo_url.startswith("git+"):
        repo_url = repo_url[4:]
    lic = v.get("license")
    if isinstance(lic, dict):
        lic = lic.get("type", "")
    return {
        "name": d.get("name", name),
        "title": d.get("name", name),
        "url": repo_url or f"https://www.npmjs.com/package/{name}",
        "host": "npm",
        "owner": name.split("/")[0] if "/" in name else name,
        "description": v.get("description") or d.get("description") or "",
        "license": str(lic or ""),
        "language": ["JavaScript"] if "javascript" in (str(v.get("description", "")).lower()) else [],
        "stars": 0, "forks": 0, "archived": False,
        "last_commit": (v.get("time") or {}).get("modified", "")[:10] if v.get("time") else "",
        "latest_release": latest,
        "topics": (d.get("keywords") or [])[:10],
    }


def fetch_gitee(owner_repo: str) -> dict[str, Any]:
    """Gitee：/api/v5/repos/{o}/{r} 免 token。search 需 token（暂不做搜索）。"""
    from backend.services.papers.http_client import throttle
    throttle("gitee", 1.0)
    url = f"https://gitee.com/api/v5/repos/{urllib.parse.quote(owner_repo, safe='/')}"
    _, body = http_get(url, route=ROUTE_DIRECT, timeout=20, retries=2)
    d = json.loads(body.decode("utf-8"))
    if "message" in d and isinstance(d["message"], str):
        raise RuntimeError(f"gitee 查询失败: {d['message']}")
    return {
        "name": d.get("full_name", owner_repo),
        "title": d.get("name", owner_repo.split("/")[-1]),
        "url": d.get("html_url", f"https://gitee.com/{owner_repo}"),
        "host": "gitee",
        "owner": d.get("owner", {}).get("login", owner_repo.split("/")[0]),
        "description": d.get("description") or "",
        "license": (d.get("license") or {}).get("name", "") if d.get("license") else "",
        "language": [d.get("language")] if d.get("language") else [],
        "stars": int(d.get("stargazers_count") or 0),
        "forks": int(d.get("forks_count") or 0),
        "archived": False,
        "last_commit": (d.get("updated_at") or "")[:10],
        "latest_release": "",
        "topics": [],
    }


# ── Bing 兜底（有官网但无代码托管页）──────────────

_BING_URL = "https://cn.bing.com/search?q="
_BING_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_BING_RESULT_RE = re.compile(r'<h2><a[^>]*href="([^"]+)"[^>]*>(.*?)</a></h2>', re.S)


def fetch_bing(query: str) -> dict[str, Any]:
    """Bing 搜索首个结果作候选（只拿标题+URL，标注未验证）。"""
    q = urllib.parse.quote(query)
    url = f"{_BING_URL}{q}"
    _, body = http_get(url, route=ROUTE_DIRECT, timeout=20, retries=2,
                       headers={"User-Agent": _BING_UA})
    text = body.decode("utf-8", errors="replace")
    m = _BING_RESULT_RE.search(text)
    if not m:
        return {
            "name": query, "title": query, "url": "", "host": "official",
            "description": "", "license": "", "language": [],
            "stars": 0, "forks": 0, "archived": False,
            "last_commit": "", "latest_release": "", "topics": [], "verified": False,
        }
    return {
        "name": query,
        "title": html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip(),
        "url": html.unescape(m.group(1)),
        "host": "official",
        "description": "", "license": "", "language": [],
        "stars": 0, "forks": 0, "archived": False,
        "last_commit": "", "latest_release": "", "topics": [],
        "verified": False,   # Bing 结果未验证，仅候选
    }


# ── 统一入口 ─────────────────────────────────────

_FETCHERS = {
    "github": fetch_github,
    "gitlab": fetch_gitlab,
    "npm": fetch_npm,
    "gitee": fetch_gitee,
    "official": fetch_bing,
}


def _gh_search_repos(query: str) -> str | None:
    """gh search repos 找首个仓库的 full_name。找不到返回 None。"""
    try:
        r = subprocess.run(["gh", "search", "repos", query, "--limit", "1",
                            "--json", "fullName"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        if r.returncode != 0:
            return None
        arr = json.loads(r.stdout)
        return arr[0]["fullName"] if arr else None
    except Exception:  # noqa: BLE001
        return None


def fetch_project(url_or_name: str, host: str | None = None) -> dict[str, Any]:
    """按 URL/名字拉取项目元数据（统一 dict）。host 自动识别或指定。

    纯名字（无 URL，如 "OpenFHE"）→ 先 gh search repos 找 GitHub 仓库；命中拉
    GitHub，否则走 Bing 兜底（候选，verified=False）。
    """
    if host:
        h, key = url_or_name, None
    else:
        h, key = resolve_host(url_or_name)

    if h == "official":
        # 纯名字：优先 GitHub search（拉到真实元数据 = 已验证）
        gh_name = _gh_search_repos(url_or_name)
        if gh_name:
            d = fetch_github(gh_name)
            d["name"] = gh_name
            d["verified"] = True
            return d
        d = fetch_bing(url_or_name)
        return d

    fetcher = _FETCHERS[h]
    d = fetcher(key if key else url_or_name)
    d.setdefault("verified", True)
    d["observed_at"] = ""
    return d
