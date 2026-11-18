"""文件传输 — 单文件下载 + 可插拔的文件夹打包策略。"""

from __future__ import annotations

import json
import os
import tarfile
import tempfile
import time
import zipfile
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from ftplib import FTP, error_perm, error_temp
from pathlib import Path
from typing import Callable

from ._ftp import FTPConnector, _FTPConfig
from .models import TransferProgress, TransferResult

# 进度回调签名
ProgressCallback = Callable[[TransferProgress], None] | None


# ═══════════════════════════════════════════════════
#  文件夹打包策略（可插拔）
# ═══════════════════════════════════════════════════


class FolderPackStrategy(ABC):
    """文件夹打包下载策略。

    实现此接口以提供不同的打包方式。
    内置实现：
      - HTTPPackStrategy      — 服务端打包（PHP HTTP API /download）
      - FTPFileByFileStrategy — 客户端逐文件拉取，本地打包
      - AutoPackStrategy      — 自动选择（HTTP → FTP 回退）
    """

    @abstractmethod
    def download_folder(
        self,
        remote_path: str,
        local_path: str | Path,
        progress_cb: ProgressCallback = None,
    ) -> TransferResult:
        ...


class HTTPPackStrategy(FolderPackStrategy):
    """服务端打包方案：通过 PHP HTTP API 的 /download 端点获取 tar.gz。"""

    def __init__(self, base_url: str):
        self._base_url = base_url.rstrip("/")

    def download_folder(
        self,
        remote_path: str,
        local_path: str | Path,
        progress_cb: ProgressCallback = None,
    ) -> TransferResult:
        if not self._base_url:
            return TransferResult(success=False, remote_path=remote_path,
                                  error="请在 .envs 配置 REMOTE_FS_HTTP_URL", strategy="http")
        local = _ensure_archive_path(local_path, remote_path)
        start = time.time()

        params = urllib.parse.urlencode({"path": remote_path})
        url = f"{self._base_url}/download?{params}"

        if progress_cb:
            progress_cb(TransferProgress(phase="connecting", message="连接 HTTP 服务…"))

        try:
            resp = urllib.request.urlopen(url, timeout=600)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            return TransferResult(
                success=False,
                remote_path=remote_path,
                error=f"HTTP 请求失败: {exc}",
                elapsed_s=time.time() - start,
                strategy="http",
            )

        total = int(resp.headers.get("Content-Length", 0))
        downloaded = 0
        last_report = 0.0
        CHUNK = 64 * 1024

        local.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(local, "wb") as f:
                while True:
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb and total > 0:
                        now = time.time()
                        if now - last_report > 0.25:  # throttle: ~4 Hz
                            progress_cb(TransferProgress(
                                bytes_downloaded=downloaded,
                                total_bytes=total,
                                phase="downloading",
                                message=f"{downloaded // 1024}KB / {total // 1024}KB",
                            ))
                            last_report = now
        except (OSError, IOError) as exc:
            # 清理不完整的下载
            if local.exists():
                local.unlink()
            return TransferResult(
                success=False,
                remote_path=remote_path,
                error=f"写入失败: {exc}",
                elapsed_s=time.time() - start,
                strategy="http",
            )

        elapsed = time.time() - start
        if progress_cb:
            progress_cb(TransferProgress(
                bytes_downloaded=downloaded,
                total_bytes=total,
                phase="done",
                message=f"{downloaded / 1024 / 1024:.1f}MB → {local}",
            ))

        return TransferResult(
            success=True,
            local_path=str(local),
            remote_path=remote_path,
            bytes_transferred=downloaded,
            elapsed_s=elapsed,
            strategy="http",
        )


class FTPFileByFileStrategy(FolderPackStrategy):
    """客户端逐文件拉取方案：遍历 FTP 目录结构，逐文件下载后本地打包为 tar.gz。

    此策略不依赖服务端任何额外能力，只要有 FTP 只读权限即可工作。
    """

    def __init__(self, connector: FTPConnector):
        self._connector = connector

    def download_folder(
        self,
        remote_path: str,
        local_path: str | Path,
        progress_cb: ProgressCallback = None,
    ) -> TransferResult:
        local = _ensure_archive_path(local_path, remote_path)
        start = time.time()

        ftp = self._connector.ftp
        if ftp is None:
            return TransferResult(
                success=False,
                remote_path=remote_path,
                error="FTP 未连接",
                elapsed_s=time.time() - start,
                strategy="ftp_file_by_file",
            )

        # 1. 创建临时目录
        tmp_dir = Path(tempfile.mkdtemp(prefix="remote_fs_ftp_"))
        try:
            # 2. 递归下载到临时目录
            file_count = [0]
            if progress_cb:
                progress_cb(TransferProgress(phase="packing", message="遍历 FTP 目录…"))

            dl_errors = self._download_tree(ftp, remote_path, tmp_dir, file_count)

            if file_count[0] == 0 and dl_errors:
                return TransferResult(
                    success=False,
                    remote_path=remote_path,
                    error=f"没有文件被下载: {dl_errors[0]}" if dl_errors else "空目录",
                    elapsed_s=time.time() - start,
                    strategy="ftp_file_by_file",
                )

            # 3. 本地打包为 tar.gz
            if progress_cb:
                progress_cb(TransferProgress(phase="packing", message="本地打包…"))

            local.parent.mkdir(parents=True, exist_ok=True)
            total_bytes = _make_archive(tmp_dir, local)

            elapsed = time.time() - start
            if progress_cb:
                progress_cb(TransferProgress(
                    bytes_downloaded=total_bytes,
                    phase="done",
                    message=f"{total_bytes / 1024 / 1024:.1f}MB → {local}",
                ))

            result = TransferResult(
                success=True,
                local_path=str(local),
                remote_path=remote_path,
                bytes_transferred=total_bytes,
                elapsed_s=elapsed,
                strategy="ftp_file_by_file",
            )
            if dl_errors:
                result.error = f"下载完成，但有 {len(dl_errors)} 个文件失败"
            return result

        finally:
            # 4. 清理临时目录
            _rmtree(tmp_dir)

    def _download_tree(
        self,
        ftp: FTP,
        remote_path: str,
        local_dir: Path,
        file_count: list[int],
        max_depth: int = 10,
        _depth: int = 0,
    ) -> list[str]:
        """递归下载 FTP 目录到本地，保持目录结构。"""
        errors: list[str] = []
        if _depth > max_depth:
            return errors

        # 列出当前目录
        try:
            raw = _list_ftp_entries(ftp, remote_path)
        except (error_perm, error_temp, OSError) as exc:
            return [f"[{remote_path}] {exc}"]

        dirs: list[str] = []
        files: list[tuple[str, int]] = []  # (name, size)

        for name, typ, size in raw:
            if name in (".", "..", "$Recycle.Bin", "System Volume Information",
                        "RECYCLER", "lost+found"):
                continue
            if name.startswith((".", "~")):
                continue
            if typ == "dir":
                dirs.append(name)
            else:
                files.append((name, size))

        # 创建本地对应目录
        local_dir.mkdir(parents=True, exist_ok=True)

        # 下载文件
        for fname, fsize in files:
            local_path = local_dir / fname
            try:
                with open(local_path, "wb") as f:
                    ftp.retrbinary(f"RETR {self._join(remote_path, fname)}", f.write)
                file_count[0] += 1
            except (error_perm, error_temp, OSError) as exc:
                errors.append(f"[{self._join(remote_path, fname)}] {exc}")

        # 递归子目录
        for d in dirs:
            sub_remote = self._join(remote_path, d)
            sub_local = local_dir / d
            sub_errors = self._download_tree(
                ftp, sub_remote, sub_local, file_count,
                max_depth, _depth + 1,
            )
            errors.extend(sub_errors)

        return errors

    @staticmethod
    def _join(base: str, name: str) -> str:
        if base == "/":
            return f"/{name}"
        return f"{base}/{name}"


class AutoPackStrategy(FolderPackStrategy):
    """自动选择策略：优先 HTTP 服务端打包，不可用时回退到 FTP 逐文件。

    回退触发条件：连接错误、HTTP 500+、超时等任何网络级异常。
    """

    def __init__(
        self,
        http_strategy: HTTPPackStrategy,
        ftp_strategy: FTPFileByFileStrategy,
    ):
        self._http = http_strategy
        self._ftp = ftp_strategy

    def download_folder(
        self,
        remote_path: str,
        local_path: str | Path,
        progress_cb: ProgressCallback = None,
    ) -> TransferResult:
        # 尝试 HTTP
        http_result = self._http.download_folder(remote_path, local_path, progress_cb)
        if http_result.success:
            return http_result

        # HTTP 失败，回退到 FTP
        if progress_cb:
            progress_cb(TransferProgress(
                phase="fallback",
                message=f"HTTP 不可用 ({http_result.error})，回退到 FTP 逐文件下载",
            ))

        ftp_result = self._ftp.download_folder(remote_path, local_path, progress_cb)
        return ftp_result


# ═══════════════════════════════════════════════════
#  单文件传输（内部辅助）
# ═══════════════════════════════════════════════════


class _SingleFileTransfer:
    """单文件下载（走 HTTP /file 端点）。"""

    def __init__(self, base_url: str):
        self._base_url = base_url.rstrip("/")

    def download_file(
        self,
        remote_path: str,
        local_path: str | Path,
        progress_cb: ProgressCallback = None,
    ) -> TransferResult:
        if not self._base_url:
            return TransferResult(success=False, remote_path=remote_path,
                                  error="请在 .envs 配置 REMOTE_FS_HTTP_URL", strategy="http_file")
        local = Path(local_path)
        start = time.time()

        params = urllib.parse.urlencode({"path": remote_path})
        url = f"{self._base_url}/file?{params}"

        if progress_cb:
            progress_cb(TransferProgress(phase="connecting", message="连接 HTTP 服务…"))

        try:
            resp = urllib.request.urlopen(url, timeout=600)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            return TransferResult(
                success=False,
                remote_path=remote_path,
                error=f"HTTP 请求失败: {exc}",
                elapsed_s=time.time() - start,
                strategy="http_file",
            )

        total = int(resp.headers.get("Content-Length", 0))
        downloaded = 0

        local.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(local, "wb") as f:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
        except (OSError, IOError) as exc:
            if local.exists():
                local.unlink()
            return TransferResult(
                success=False,
                remote_path=remote_path,
                error=f"写入失败: {exc}",
                elapsed_s=time.time() - start,
                strategy="http_file",
            )

        elapsed = time.time() - start
        if progress_cb:
            progress_cb(TransferProgress(
                bytes_downloaded=downloaded,
                total_bytes=total,
                phase="done",
                message=f"{downloaded / 1024:.0f}KB → {local}",
            ))

        return TransferResult(
            success=True,
            local_path=str(local),
            remote_path=remote_path,
            bytes_transferred=downloaded,
            elapsed_s=elapsed,
            strategy="http_file",
        )


# ═══════════════════════════════════════════════════
#  传输客户端（统一入口）
# ═══════════════════════════════════════════════════


class TransferClient:
    """文件传输客户端。

    单文件走 HTTP，文件夹通过可插拔策略（默认自动选择）完成。
    """

    def __init__(
        self,
        connector: FTPConnector,
        http_base_url: str,
    ):
        self._ftp_connector = connector
        self._http_base = http_base_url.rstrip("/")

        # 单文件传输
        self._file_transfer = _SingleFileTransfer(http_base_url)

        # 文件夹打包策略（默认自动选择）
        self._pack_strategy: FolderPackStrategy = AutoPackStrategy(
            http_strategy=HTTPPackStrategy(http_base_url),
            ftp_strategy=FTPFileByFileStrategy(connector),
        )

    # ── 策略切换 ─────────────────────────────────

    @property
    def pack_strategy(self) -> FolderPackStrategy:
        return self._pack_strategy

    @pack_strategy.setter
    def pack_strategy(self, strategy: FolderPackStrategy) -> None:
        self._pack_strategy = strategy

    # ── 下载方法 ─────────────────────────────────

    def download_file(
        self,
        remote_path: str,
        local_path: str | Path,
        progress_cb: ProgressCallback = None,
    ) -> TransferResult:
        return self._file_transfer.download_file(remote_path, local_path, progress_cb)

    def download_folder(
        self,
        remote_path: str,
        local_path: str | Path,
        progress_cb: ProgressCallback = None,
    ) -> TransferResult:
        return self._pack_strategy.download_folder(remote_path, local_path, progress_cb)

    def download_by_name(
        self,
        remote_parent: str,
        folder_name: str,
        local_dir: str | Path = ".",
        progress_cb: ProgressCallback = None,
    ) -> TransferResult | None:
        """在远程父目录下按名字找到子文件夹并下载。

        Returns:
            TransferResult（找到并下载），或 None（未找到）
        """
        if not self._http_base:
            raise RuntimeError("请在 .envs 配置 REMOTE_FS_HTTP_URL")
        # 通过 HTTP 列举目录（轻量）
        parent = remote_parent.rstrip("/") + "/"
        try:
            entries = _http_list_dir(self._http_base, parent)
        except Exception as exc:
            # 如果 HTTP 不可用，返回 None（调用方决定是否重试）
            return None

        for e in entries:
            if e.get("type") == "dir" and e.get("name") == folder_name:
                full_path = parent + folder_name
                safe_name = folder_name
                local_path = Path(local_dir) / f"{safe_name}.tar.gz"
                return self.download_folder(full_path, local_path, progress_cb)

        return None


# ═══════════════════════════════════════════════════
#  工具函数
# ═══════════════════════════════════════════════════


def _ensure_archive_path(local_path: str | Path, remote_path: str) -> Path:
    """确认本地路径有合法的压缩包扩展名，自动补全。

    优先保留用户指定的 .zip 或 .tar.gz；否则根据路径名自动补默认 (.tar.gz)。
    """
    p = Path(local_path)
    name = p.name
    if name.endswith(".tar.gz") or name.endswith(".tgz"):
        return p
    if name.endswith(".zip"):
        return p
    # 已有其他后缀 → 替换为 .tar.gz
    if p.suffix:
        return p.with_name(p.stem + ".tar.gz")
    # 无后缀 → 用远程目录名
    dirname = remote_path.rstrip("/").split("/")[-1] or "download"
    return p / f"{dirname}.tar.gz"


def _detect_archive_format(path: Path) -> str:
    """根据路径后缀判断打包格式。返回 'tar.gz' 或 'zip'。"""
    name = path.name
    if name.endswith(".zip"):
        return "zip"
    return "tar.gz"


def _http_list_dir(base_url: str, path: str) -> list[dict]:
    """通过 HTTP API 列举目录（用于 download_by_name 的轻量查询）。"""
    url = f"{base_url}/list?{urllib.parse.urlencode({'path': path})}"
    resp = urllib.request.urlopen(url, timeout=15)
    data = json.loads(resp.read().decode("utf-8"))
    return data.get("entries", [])


def _decode_name(name: str) -> str:
    """将 FTP LIST 中的文件名解码为正确编码。"""
    try:
        name.encode("latin-1")
    except UnicodeEncodeError:
        return name
    raw = name.encode("latin-1")
    for enc in ("utf-8", "gbk", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return name


def _list_raw(ftp: FTP, path: str) -> list[str]:
    """通过二进制数据连接读取 LIST 输出，每行安全解码。"""
    from ftplib import error_reply

    try:
        ftp.voidcmd("TYPE I")
    except error_reply:
        pass

    cmd = f"LIST {path}"
    try:
        sock = ftp.transfercmd(cmd)
    except (error_perm, error_temp, error_reply):
        pwd = ftp.pwd()
        ftp.cwd(path)
        sock = ftp.transfercmd("LIST")
        ftp.cwd(pwd)

    raw = b""
    while True:
        chunk = sock.recv(8192)
        if not chunk:
            break
        raw += chunk
    sock.close()
    ftp.voidresp()

    lines = raw.replace(b"\r\n", b"\n").split(b"\n")
    decoded = []
    for line in lines:
        if not line:
            continue
        try:
            decoded.append(line.decode("utf-8"))
        except UnicodeDecodeError:
            decoded.append(line.decode("latin-1", errors="replace"))
    return decoded


def _list_ftp_entries(
    ftp: FTP,
    path: str,
) -> list[tuple[str, str, int]]:
    """从 FTP 列出条目，返回 [(name, type, size)]。"""
    lines = _list_raw(ftp, path)

    results: list[tuple[str, str, int]] = []
    for line in lines:
        parts = line.split()
        if len(parts) < 6:
            continue
        perms = parts[0]
        typ = "dir" if perms.startswith("d") else "file"
        size = 0
        try:
            size = int(parts[4])
        except (ValueError, IndexError):
            pass
        name = _decode_name(line.split(None, 8)[-1] if len(parts) >= 9 else parts[-1])
        if name in (".", ".."):
            continue
        results.append((name, typ, size))
    return results


def _make_archive(source_dir: Path, output_path: Path) -> int:
    """根据 output_path 后缀自动选择打包格式。返回压缩前总字节数。"""
    fmt = _detect_archive_format(output_path)
    if fmt == "zip":
        return _make_zip(source_dir, output_path)
    return _make_targz(source_dir, output_path)


def _make_targz(source_dir: Path, output_path: Path) -> int:
    """将 source_dir 的全部内容打包为 tar.gz。返回总字节数。"""
    total = 0
    with tarfile.open(output_path, "w:gz") as tar:
        for entry in source_dir.rglob("*"):
            if entry.is_file():
                rel = entry.relative_to(source_dir)
                tar.add(entry, arcname=str(rel))
                total += entry.stat().st_size
    return total


def _make_zip(source_dir: Path, output_path: Path) -> int:
    """将 source_dir 的全部内容打包为 zip。返回总字节数。"""
    total = 0
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for entry in source_dir.rglob("*"):
            if entry.is_file():
                rel = entry.relative_to(source_dir)
                zf.write(entry, arcname=str(rel))
                total += entry.stat().st_size
    return total


def _rmtree(path: Path) -> None:
    """递归删除目录。"""
    import shutil
    shutil.rmtree(path, ignore_errors=True)
