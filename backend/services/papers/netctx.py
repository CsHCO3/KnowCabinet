"""网络环境检测与机构下载路由。部署参数从 .envs 加载。"""

from __future__ import annotations

import os
import re
import socket
import urllib.request
from typing import Any

# 观察到的校园出口段（可扩展）
from backend.config import deployment
from urllib.parse import urlsplit

IP_SERVICES = ["https://myip.ipip.net", "https://ipinfo.io/ip"]


def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _configured_proxy_open(host: str, port: str, timeout: float) -> bool:
    return bool(host and port.isdecimal() and 0 < int(port) <= 65535
                and _port_open(int(port), host, timeout))


def wssocks_online(timeout: float = 1.5) -> bool:
    """校园隧道(SOCKS5)是否在线。"""
    return _configured_proxy_open(os.getenv("SSH_PROXY_HOST", ""), os.getenv("SSH_PROXY_PORT", ""), timeout)


def clash_online(timeout: float = 1.5) -> bool:
    """Clash 出墙代理是否在线。"""
    url = urlsplit(os.getenv("PAPER_HTTP_PROXY_URL", ""))
    try:
        port = url.port
    except ValueError:
        return False
    if port is None and url.scheme in ("http", "https"):
        port = 443 if url.scheme == "https" else 80
    return _configured_proxy_open(url.hostname or "", str(port or ""), timeout)


def direct_ip(timeout: float = 6) -> str:
    """获取直连出口 IP（best-effort）。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for svc in IP_SERVICES:
        try:
            req = urllib.request.Request(svc, headers={"User-Agent": "netctx/1.0"})
            with opener.open(req, timeout=timeout) as resp:
                text = resp.read().decode("utf-8", errors="ignore")
                m = re.search(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b", text)
                if m:
                    return m.group(1)
        except Exception:
            continue
    return ""


def is_campus_ip(ip: str) -> bool:
    """判断 IP 是否在校园出口段。"""
    prefixes = (part.strip() for part in os.getenv("CAMPUS_IP_PREFIXES", "").split(","))
    return any(ip.startswith(prefix) for prefix in prefixes if prefix)


def detect() -> dict[str, Any]:
    """检测网络状态。"""
    ip = direct_ip()
    return {
        "direct_ip": ip,
        "campus": is_campus_ip(ip) if ip else None,  # None=无法判断
        "wssocks_tunnel": wssocks_online(),
        "clash": clash_online(),
    }


def institution_route() -> str:
    """机构下载路由（**默认直连**）。

    2026-09-17 修正：原先写成"wssocks 在线 → tunnel"是**反的**——
    - elib 反向代理自带机构身份，走直连最快最稳（隧道反而超时）；
    - 校园网内更不需要隧道。
    只有明确知道某源按【源 IP】判权且无代理通道时，才显式要求 tunnel
    （调用方可传 `--proxy=socks5` / `--inst-proxy socks5` 强制）。
    """
    return "direct"


def elib_route() -> str:
    """elib 反向代理专属路由：**永远直连**（走隧道会超时）。"""
    return "direct"


def describe() -> str:
    """人类可读的网络状态描述。"""
    d = detect()
    parts = [
        f"直连IP={d['direct_ip'] or '?'}",
        f"校园={'是' if d['campus'] else '否' if d['campus'] is False else '?'}",
        f"wssocks隧道={'在线' if d['wssocks_tunnel'] else '离线'}",
        f"Clash={'在线' if d['clash'] else '离线'}",
        "机构路由=direct(elib 自带机构身份，勿走隧道)",
    ]
    return " | ".join(parts)
