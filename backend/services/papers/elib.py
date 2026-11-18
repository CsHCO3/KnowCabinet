"""机构图书馆代理客户端。连接参数及授权会话由部署者提供。"""

from __future__ import annotations

import binascii
import json
import logging
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path

from backend.config.deployment import require_env
from typing import Any

logger = logging.getLogger(__name__)

IEEL_PDF_URL = "https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber={arn}&ref="

# 共享 token 缓存（论文检索域的公共资产）
_NODE_DIR = Path(__file__).resolve().parent / "browser"
_CACHE_CANDIDATES = [
    Path(__file__).resolve().parents[3] / "文档以及其它代码" / "(5)搜集信息源" / "论文检索" / "elib-proxy" / "host_tokens.json",
    Path(__file__).resolve().parents[3] / "文档以及其它代码" / "论文检索" / "elib-proxy" / "host_tokens.json",  # 旧布局兜底
]

_cache: dict[str, str] | None = None


# ---------------------------------------------------------------- token

def forge_token(host: str) -> str:
    """本地伪造 token：AES-128-CFB(key=iv=KEY) 加密主机名 + 魔数前缀（任意长度均正确）。"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms

    try:  # cryptography ≥ 43：CFB 归入 decrepit（旧位置将在 49.0 移除）
        from cryptography.hazmat.decrepit.ciphers.modes import CFB
    except ImportError:
        from cryptography.hazmat.primitives.ciphers.modes import CFB

    key_hex, = require_env("ELIB_KEY_HEX")
    key = bytes.fromhex(key_hex)
    if len(key) != 16:
        raise ValueError("ELIB_KEY_HEX 应为 16 字节的十六进制值")
    enc = Cipher(algorithms.AES(key), CFB(key)).encryptor()
    return binascii.hexlify(key + enc.update(host.encode()) + enc.finalize()).decode()


def load_cache() -> dict[str, str]:
    global _cache
    if _cache is None:
        _cache = {}
        for p in _CACHE_CANDIDATES:
            try:
                _cache = json.loads(p.read_text(encoding="utf-8"))
                break
            except Exception:
                continue
    return _cache


def token_for(host: str) -> str | None:
    """host → token（缓存优先，其次本地伪造）；未知返回 None。"""
    h = str(host or "").strip()
    h = re.sub(r"^https?://", "", h).split("/")[0]
    if not h:
        return None
    cached = load_cache().get(h)
    if cached:
        return cached
    return forge_token(h)


def proxy_url(target: str) -> str | None:
    """目标 URL → elib 代理 URL；token 未知返回 None。"""
    from urllib.parse import urlsplit, urlunsplit

    u = urlsplit(target)
    if not u.scheme or not u.netloc:
        return None
    tok = token_for(u.netloc)
    if not tok:
        return None
    origin, = require_env("ELIB_PROXY_ORIGIN")
    return f"{origin.rstrip('/')}/{u.scheme}/{tok}{urlunsplit(('', '', u.path or '/', u.query, u.fragment))}"


# ---------------------------------------------------------------- 下载

_IEEE_DOI_RE = re.compile(r"^10\.1109/", re.I)
_ACM_DOI_RE = re.compile(r"^10\.1145/", re.I)


def inst_source_for_doi(doi: str) -> str:
    """DOI → 机构源名（'ieee' / 'acm' / ''）。"""
    d = (doi or "").strip()
    if _IEEE_DOI_RE.match(d):
        return "ieee"
    if _ACM_DOI_RE.match(d):
        return "acm"
    return ""


def ieee_arnumber_from_crossref(doi: str, timeout: float = 12) -> str:
    """Crossref 元数据取 IEEE document number（直连 HTTP）。"""
    url = "https://api.crossref.org/works/" + urllib.parse.quote(doi.strip(), safe="")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "infosystem-papers/1.0 (mailto:noreply@example.com)"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as resp:
            msg = json.loads(resp.read().decode("utf-8", "ignore")).get("message") or {}
    except Exception as e:
        logger.debug("crossref 查 %s 失败: %s", doi, e)
        return ""
    # (a) resource.primary.URL
    m = re.search(r"ieeexplore\.ieee\.org/document/(\d+)",
                  str(((msg.get("resource") or {}).get("primary") or {}).get("URL") or ""))
    if m:
        return m.group(1)
    # (b) link[].URL 里的 arnumber=
    for l in msg.get("link") or []:
        m = re.search(r"[?&]arnumber=(\d+)", str(l.get("URL") or ""))
        if m:
            return m.group(1)
    # (c) alternate-id
    for alt in msg.get("alternate-id") or []:
        m = re.search(r"doc/(\d+)", str(alt))
        if m:
            return m.group(1)
    # (d) article-number
    m = re.fullmatch(r"(\d{6,8})", str(msg.get("article-number") or ""))
    return m.group(1) if m else ""


def download_via_institution(doi: str, target_path: str, *, proxy_mode: str = "none") -> tuple[str, str]:
    """用 Node 侧 `download_institution.js`（含浏览器过 WAF）下载订阅库全文。

    返回 (状态, 详情)：'downloaded' | 'no_url' | 'failed'

    默认 `proxy_mode="none"`（直连）：elib 是反向代理、自己就带机构身份，
    而把**整个浏览器**塞进 wssocks SOCKS5 隧道会让 WAF 挑战与三跳转发慢到超时
    （实测直连 21.7s 成功 / 隧道 300s 超时）。万方等按 IP 判权的源才需要隧道。
    """
    import subprocess
    import tempfile

    src = inst_source_for_doi(doi)
    if not src:
        return "no_url", ""

    out_dir = os.path.dirname(os.path.abspath(target_path)) or "."
    os.makedirs(out_dir, exist_ok=True)
    script = _NODE_DIR / "download_institution.js"
    if not script.exists():
        return "failed", f"缺少 {script}"

    with tempfile.TemporaryDirectory(prefix="elib_inst_") as tmp:
        payload_path = Path(tmp) / "papers.json"
        payload = [{"source": src, "title": Path(target_path).stem, "doi": doi}]
        payload_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            r = subprocess.run(
                ["node", "download_institution.js", str(payload_path), out_dir, "1", f"--proxy={proxy_mode}"],
                cwd=str(_NODE_DIR), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=180,
            )
        except Exception as e:
            return "failed", f"机构下载未执行: {e}"
        if r.returncode != 0:
            return "failed", (r.stderr or r.stdout or "")[-300:]

    # 脚本按 title 命名；这里把产物规整到目标路径
    safe = re.sub(r"[^\w一-鿿-]+", "_", Path(target_path).stem)[:60]
    produced = Path(out_dir) / f"{safe}.pdf"
    if produced.exists() and produced.resolve() != Path(target_path).resolve():
        try:
            os.replace(produced, target_path)
        except OSError:
            pass
    if os.path.exists(target_path) and Path(target_path).stat().st_size > 1024:
        return "downloaded", target_path
    return "failed", "机构下载未产出 PDF（可能无订阅权限或该文非订阅库）"


def describe() -> str:
    """人类可读状态。"""
    cache = load_cache()
    return f"机构代理配置: {bool(os.getenv('ELIB_PROXY_ORIGIN'))} | token 缓存 {len(cache)} 个主机"
