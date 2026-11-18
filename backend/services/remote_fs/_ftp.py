"""FTP 连接管理（remote_fs 内部实现细节，不对外暴露）。"""

from __future__ import annotations

import socket
from dataclasses import dataclass, field
from ftplib import FTP, error_perm, error_temp

# PySocks 为可选依赖，无则禁用代理回退
try:
    import socks as _socks
    HAS_SOCKS = True
except ImportError:
    HAS_SOCKS = False


# ── 配置 ─────────────────────────────────────────


@dataclass
class _FTPConfig:
    """FTP 连接参数。"""
    host: str = ""
    user: str = ""
    passwd: str = ""
    port: int = 21
    connect_timeout: int = 10
    use_proxy_fallback: bool = True
    proxy_host: str = ""
    proxy_port: int = 0
    # PASV / ACTIVE 模式
    use_pasv: bool = True


# ── 连接器 ───────────────────────────────────────


class FTPConnector:
    """FTP 连接器 — 直连优先，SOCKS5 代理回退。"""

    def __init__(self, config: _FTPConfig | None = None):
        self._cfg = config or _FTPConfig()
        self.ftp: FTP | None = None
        self._connect()

    # ── 公开方法 ─────────────────────────────────

    def reconnect(self) -> None:
        """断开并重建连接。"""
        self.close()
        self._connect()

    def close(self) -> None:
        if self.ftp is not None:
            try:
                self.ftp.quit()
            except Exception:
                pass
            self.ftp = None

    # ── 连接流程 ─────────────────────────────────

    def _connect(self) -> None:
        missing = [name for name, value in (
            ("REMOTE_FS_FTP_HOST", self._cfg.host),
            ("REMOTE_FS_FTP_USER", self._cfg.user),
            ("REMOTE_FS_FTP_PASS", self._cfg.passwd),
        ) if not value.strip()]
        if missing:
            raise RuntimeError("请在 .envs 配置 " + ", ".join(missing))
        ftp = self._try_direct()
        if ftp is not None:
            self.ftp = ftp
            return

        if self._cfg.use_proxy_fallback and self._cfg.proxy_host and self._cfg.proxy_port > 0:
            ftp = self._try_proxy()
            if ftp is not None:
                self.ftp = ftp
                return
            raise ConnectionError(
                f"无法连接到 FTP {self._cfg.host}:{self._cfg.port} "
                f"（直连和代理均失败）"
            )
        raise ConnectionError(
            f"无法连接到 FTP {self._cfg.host}:{self._cfg.port}（直连失败，代理已禁用）"
        )

    @staticmethod
    def _negotiate_encoding(ftp: FTP) -> None:
        """告知服务器使用 UTF-8 编码路径，并将 ftplib 编码设为 utf-8。

        OPTS UTF8 ON 后服务器期待控制通道命令（如 CWD）中的路径为 UTF-8，
        因此 ftplib 需要用 UTF-8 编码命令。数据通道的 LIST 输出也会用 UTF-8 解码，
        个别非 UTF-8 字节由 _parse_dir 通过二进制回退处理。
        """
        try:
            resp = ftp.voidcmd("OPTS UTF8 ON")
            ftp.encoding = "utf-8"
        except (error_perm, error_temp):
            # 不支持 UTF-8 的服务器：保留 latin-1，后续靠 _decode_name 修复
            ftp.encoding = "latin-1"

    def _try_direct(self) -> FTP | None:
        try:
            ftp = FTP()
            ftp.connect(self._cfg.host, self._cfg.port, timeout=self._cfg.connect_timeout)
            ftp.login(self._cfg.user, self._cfg.passwd)
            ftp.set_pasv(self._cfg.use_pasv)
            self._negotiate_encoding(ftp)
            return ftp
        except (socket.timeout, ConnectionRefusedError, OSError, error_temp, error_perm):
            return None

    def _try_proxy(self) -> FTP | None:
        if not HAS_SOCKS:
            return None
        try:
            sock = _socks.socksocket()
            sock.set_proxy(_socks.SOCKS5, self._cfg.proxy_host, self._cfg.proxy_port)
            sock.settimeout(self._cfg.connect_timeout)
            sock.connect((self._cfg.host, self._cfg.port))

            ftp = FTP()
            ftp.sock = sock
            ftp.af = sock.family
            ftp.file = sock.makefile("r", encoding="latin-1")
            ftp.welcome = ftp.getresp()
            ftp.login(self._cfg.user, self._cfg.passwd)
            ftp.set_pasv(self._cfg.use_pasv)
            self._negotiate_encoding(ftp)
            return ftp
        except Exception:
            return None

    # ── 上下文管理器 ─────────────────────────────

    def __enter__(self) -> FTPConnector:
        if self.ftp is None:
            self._connect()
        return self

    def __exit__(self, *args) -> None:
        self.close()
