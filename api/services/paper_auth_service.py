"""论文平台登录态 / 连通性检测服务。

平台分类（决定"登入状态"的含义）：
- cookie 类（需登录后导出 storage_state）：知网 CNKI、万方 —— 检测 = 文件+关键 cookie + 真实搜索探测
- IP/验证码类（无登录，看 Clash + 封禁状态）：Google Scholar —— 检测 = 代理在线 + 轻量 GET 看是否 sorry
- API 类（免登录，看连通性）：arXiv / OpenAlex / Crossref —— 检测 = 可达性

配套机构网络（netctx）：wssocks 隧道 / Clash / 校园 IP → 决定订阅库（IEEE/Elsevier/知网/万方）下载能力。
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from backend.services.papers import netctx

_PAPERS_DIR = Path(__file__).resolve().parent.parent.parent / "backend" / "services" / "papers"
CNKI_SS = _PAPERS_DIR / "cnki_session" / "storage_state.json"
WANFANG_SS = _PAPERS_DIR / "wanfang_session" / "storage_state.json"

# 探测结果缓存：key -> (time.time(), result)；防刷新自动读回，避免反复打 GS/arXiv/知网/万方
_PROBE_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 300  # 5 分钟
_LAST_FULL_TEST = 0.0  # 全量检测时间戳（防连点）

PLATFORM_DEFS = [
    {"key": "cnki", "name": "知网 CNKI", "kind": "cookie",
     "needs": "登录后导出 storage_state（cnki_agent.js）"},
    {"key": "wanfang", "name": "万方", "kind": "cookie",
     "needs": "CAS 登录后导出 storage_state"},
    {"key": "google_scholar", "name": "Google Scholar", "kind": "ip",
     "needs": "必须开 Clash；被封后换节点"},
    {"key": "arxiv", "name": "arXiv", "kind": "api", "needs": "免登录，直连"},
    {"key": "openalex", "name": "OpenAlex", "kind": "api", "needs": "免登录，直连"},
    {"key": "crossref", "name": "Crossref", "kind": "api", "needs": "免登录，直连"},
]

_API_URLS = {
    "arxiv": "http://export.arxiv.org/api/query?search_query=all:test&start=0&max_results=1",
    "openalex": "https://api.openalex.org/works?per-page=1&search=test",
    "crossref": "https://api.crossref.org/works?rows=1&query=test",
}


# ── 缓存 ──────────────────────────────────────────


def _cache_get(key: str) -> dict | None:
    item = _PROBE_CACHE.get(key)
    if not item:
        return None
    ts, result = item
    if time.time() - ts > _CACHE_TTL:
        _PROBE_CACHE.pop(key, None)
        return None
    return result


def _cache_put(key: str, result: dict) -> dict:
    _PROBE_CACHE[key] = (time.time(), result)
    return result


# ── 网络状态 ──────────────────────────────────────


def network_status(quick: bool = True) -> dict:
    """网络状态。quick=True 只做端口探测（快，1~3s）；quick=False 追加直连 IP 查询（慢）。"""
    d = {
        "wssocks_tunnel": netctx.wssocks_online(),
        "clash": netctx.clash_online(),
        "institution_route": netctx.institution_route(),
    }
    if not quick:
        ip = netctx.direct_ip(timeout=4)
        d["direct_ip"] = ip
        d["campus"] = netctx.is_campus_ip(ip) if ip else None
    return d


# ── 即时状态（不发起真实搜索，仅读文件/端口）──────


def _cnki_instant() -> dict:
    info = {"configured": False, "file": str(CNKI_SS), "cookie_keys": []}
    if not CNKI_SS.exists():
        return {**info, "error": "未找到 cookie 文件"}
    try:
        state = json.loads(CNKI_SS.read_text(encoding="utf-8"))
    except Exception:
        return {**info, "error": "cookie 文件损坏"}
    keys = sorted({c.get("name", "") for c in state.get("cookies", []) if "cnki" in c.get("domain", "")})
    info["cookie_keys"] = keys
    info["configured"] = bool(keys)
    return info


def _wanfang_instant() -> dict:
    info = {"configured": False, "file": str(WANFANG_SS), "has_castgc": False}
    if not WANFANG_SS.exists():
        return {**info, "error": "未找到 cookie 文件"}
    try:
        state = json.loads(WANFANG_SS.read_text(encoding="utf-8"))
    except Exception:
        return {**info, "error": "cookie 文件损坏"}
    info["has_castgc"] = any(c.get("name") == "CASTGC" for c in state.get("cookies", []))
    info["configured"] = info["has_castgc"]
    return info


def _instant_platform(key: str) -> dict:
    if key == "cnki":
        return _cnki_instant()
    if key == "wanfang":
        return _wanfang_instant()
    if key == "google_scholar":
        # 无 cookie：只看 Clash（进程内可判断）是否在线
        return {"clash": netctx.clash_online()}
    return {}


def _status_label(key: str, inst: dict, probe: dict | None, net: dict) -> tuple[str, str]:
    """返回 (状态, 说明)。状态: ok / fail / warn / none。"""
    if key == "cnki" or key == "wanfang":
        if not inst.get("configured"):
            return "none", inst.get("error") or "未配置（需登录导出）"
        if probe is not None:
            if probe.get("ok"):
                return "ok", f"✓ 搜索正常（{probe.get('results', 0)} 条）"
            return "fail", probe.get("error") or "登录态异常"
        return "warn", "已配置，未检测"
    if key == "google_scholar":
        if not net.get("clash"):
            return "none", "Clash 未开（GS 必须走代理）"
        if probe is not None:
            if probe.get("ok"):
                return "ok", "✓ 可访问，未被封禁"
            return "fail", probe.get("error") or "被拦截"
        return "warn", "Clash 在线，未检测"
    # API 类
    if probe is not None:
        if probe.get("ok"):
            return "ok", f"✓ HTTP {probe.get('status')}"
        note = probe.get("error") or "不可达"
        if "429" in note:
            note += "（限流，稍后自动恢复）"
        return "fail", note
    return "warn", "未检测"


# ── 真实探测（实际发请求）────────────────────────


def _api_probe(url: str) -> dict:
    from backend.services.papers.http_client import ROUTE_DIRECT, http_get

    try:
        status, _ = http_get(url, route=ROUTE_DIRECT, timeout=8, retries=1, backoff=2)
        return {"ok": status < 400, "status": status}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:100]}"}


def _run_probe(key: str) -> dict:
    if key == "cnki":
        from backend.services.papers.sources import cnki
        return cnki.probe()
    if key == "wanfang":
        from backend.services.papers.sources import wanfang
        return wanfang.probe()
    if key == "google_scholar":
        from backend.services.papers.sources import google_scholar
        return google_scholar.probe()
    return _api_probe(_API_URLS[key])


# ── 对外入口 ──────────────────────────────────────


def get_status() -> dict:
    """聚合即时状态 + 缓存中的最近探测结果（供页面加载/刷新用，不发真实请求）。"""
    net = network_status(quick=True)
    platforms = []
    for d in PLATFORM_DEFS:
        key = d["key"]
        inst = _instant_platform(key)
        probe = _cache_get("probe:" + key)
        status, note = _status_label(key, inst, probe, net)
        platforms.append({**d, "instant": inst, "probe": probe, "status": status, "note": note})
    return {"network": net, "platforms": platforms}


def test_all(force: bool = False) -> dict:
    """对全部平台做真实探测（知网/万方各发一次搜索、GS 走代理 GET、API 类可达性），并行执行。

    force=False 时命中 5 分钟缓存；force=True 强制重测，但 8 秒内连点会回退到缓存（防打爆）。
    """
    global _LAST_FULL_TEST
    now = time.monotonic()
    if force and _LAST_FULL_TEST and now - _LAST_FULL_TEST < 8:
        force = False  # 刚测过，回退缓存防连点
    _LAST_FULL_TEST = now

    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=len(PLATFORM_DEFS)) as ex:
        futures = {}
        for d in PLATFORM_DEFS:
            key = d["key"]
            cached = None if force else _cache_get("probe:" + key)
            if cached is not None:
                results[key] = cached
            else:
                futures[ex.submit(_run_probe, key)] = key
        for fut, key in futures.items():
            try:
                r = fut.result()
            except Exception as e:
                r = {"ok": False, "error": f"探测异常: {e}"}
            results[key] = _cache_put("probe:" + key, r)

    return {"network": network_status(quick=False), "probes": results}
