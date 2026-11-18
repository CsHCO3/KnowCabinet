"""统一 HTTP 客户端：per-source 代理路由 + 指数退避重试 + 节流。

探路结论驱动的设计：
- arXiv / OpenAlex / Crossref / S2 / DBLP / 万方 等国内直连可达 → 用 `direct` 路由
  （系统代理开着时 urllib 默认会继承代理，共享出口 IP 反而易被限流，
  例如 arXiv 对 Clash 共享香港 IP 经常 429 "Rate exceeded"）。
- Google Scholar / Sci-Hub 必须翻墙 → 用 `proxy` 路由（显式走 Clash）。
- IEEE/ACM 等订阅库：**不要**走 Clash（订阅按源 IP 判定，Clash 的共享出口 IP 无机构权限，
  且 IEEE Xplore 前面有 AWS WAF JS 挑战）。这类走 **elib 反向代理**（浏览器过 WAF），
  见 `browser/download_institution.js` 与 `论文检索/elib校外访问代理自动化.md`。
- 429/5xx/超时 → 指数退避重试。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

from backend.config.deployment import require_env
ROUTE_DIRECT = "direct"
ROUTE_PROXY = "proxy"

# 直连白名单源默认 UA；Crossref 建议带 mailto 以提高配额。
DEFAULT_UA = "multisource-pipeline/0.1"
# 换成你自己的邮箱可让 Crossref 配额更高（可选）。
CROSSREF_MAILTO = ""

_openers: dict[str, urllib.request.OpenerDirector] = {
    ROUTE_DIRECT: urllib.request.build_opener(urllib.request.ProxyHandler({})),
}

# 可重试状态码
_RETRYABLE_CODES = {429, 500, 502, 503, 504}

# 每 key 最近一次请求的单调时间，用于节流（线程安全）
_throttle_last: dict[str, float] = {}
_throttle_lock = threading.Lock()


def throttle(key: str, min_interval: float = 0.0) -> None:
    """保证同一 key 的两次请求间隔至少 min_interval 秒（并行安全）。"""
    if min_interval <= 0:
        return
    now = time.monotonic()
    with _throttle_lock:
        last = _throttle_last.get(key, 0.0)
        if last > 0 and now - last < min_interval:
            sleep_for = min_interval - (now - last)
            _throttle_last[key] = now + sleep_for
        else:
            sleep_for = 0.0
            _throttle_last[key] = now
    if sleep_for > 0:
        time.sleep(sleep_for)


def _default_headers(extra: dict[str, str] | None, mailto: str = "") -> dict[str, str]:
    ua = DEFAULT_UA
    if mailto:
        ua += f" (mailto:{mailto})"
    headers = {"User-Agent": ua}
    if extra:
        headers.update(extra)
    return headers


def http_get(
    url: str,
    *,
    route: str = ROUTE_DIRECT,
    timeout: float = 20,
    headers: dict[str, str] | None = None,
    retries: int = 3,
    backoff: float = 4.0,
    mailto: str = "",
) -> tuple[int, bytes]:
    """GET 请求。返回 (status, body)；重试耗尽后抛最后一次异常。"""
    if route == ROUTE_PROXY:
        proxy_url, = require_env("PAPER_HTTP_PROXY_URL")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
    else:
        opener = _openers[route]
    last_err: Exception | None = None
    hdrs = _default_headers(headers, mailto)
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, headers=hdrs)
        try:
            with opener.open(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            if e.code in _RETRYABLE_CODES and attempt < retries:
                logger.warning("GET %s -> %s (attempt %d/%d)", url, e.code, attempt, retries)
                time.sleep(backoff * (2 ** (attempt - 1)))
                continue
            last_err = e
            break
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            if attempt < retries:
                logger.warning("GET %s -> %s (attempt %d/%d)", url, type(e).__name__, attempt, retries)
                time.sleep(backoff * (2 ** (attempt - 1)))
                continue
            last_err = e
            break
    assert last_err is not None
    raise last_err


def http_get_json(
    url: str,
    *,
    route: str = ROUTE_DIRECT,
    timeout: float = 20,
    headers: dict[str, str] | None = None,
    retries: int = 3,
    backoff: float = 4.0,
    mailto: str = "",
) -> Any:
    """GET 并解析 JSON。"""
    _, body = http_get(
        url, route=route, timeout=timeout, headers=headers, retries=retries, backoff=backoff, mailto=mailto
    )
    return json.loads(body.decode("utf-8"))
