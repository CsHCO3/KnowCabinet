"""SSH 客户端 — 支持 SOCKS5 代理。

从 remote/ssh_client.py 迁移而来，配置统一由 Settings 管理。
"""

import logging
import os
import socket
import shlex
from typing import Optional, Tuple

import paramiko
import socks
from scp import SCPClient

from backend.config.settings import Settings

logger = logging.getLogger("infosystem.pdf_remote")
_settings = Settings()


class SSHClient:
    """SSH 客户端，支持直连和 SOCKS5 代理。"""

    def __init__(self):
        self._client: Optional[paramiko.SSHClient] = None
        self._transport: Optional[paramiko.Transport] = None

    def _create_proxy_socket(self, host: str, port: int) -> socks.socksocket:
        sock = socks.socksocket()
        proxy_type = _settings.ssh_proxy_type
        if proxy_type == "socks4":
            sock.set_proxy(socks.SOCKS4, _settings.ssh_proxy_host, _settings.ssh_proxy_port)
        else:
            sock.set_proxy(socks.SOCKS5, _settings.ssh_proxy_host, _settings.ssh_proxy_port)
        sock.settimeout(_settings.ssh_timeout)
        sock.connect((host, port))
        logger.debug("Proxy socket: %s:%s -> %s:%s", _settings.ssh_proxy_host, _settings.ssh_proxy_port, host, port)
        return sock

    def _get_private_key(self) -> Optional[paramiko.PKey]:
        key_path = _settings.ssh_key_path
        if not key_path or not str(key_path).strip():
            return None
        expanded = str(key_path)
        if "~" in expanded:
            from pathlib import Path
            expanded = str(Path(expanded).expanduser())

        for cls, name in [
            (paramiko.RSAKey, "RSA"),
            (paramiko.Ed25519Key, "Ed25519"),
            (paramiko.ECDSAKey, "ECDSA"),
        ]:
            try:
                return cls.from_private_key_file(expanded)
            except paramiko.SSHException:
                continue
            except FileNotFoundError:
                continue

        logger.error("Unable to load private key from %s", expanded)
        return None

    def connect(self) -> bool:
        """尝试连接 SSH 服务器，自动检测直连/代理。

        先尝试直连（无代理开销），如果失败则自动走 SOCKS5 代理。
        两种方式都失败后才返回 False。
        """
        missing = []
        if not _settings.ssh_host.strip():
            missing.append("SSH_HOST")
        if not _settings.ssh_username.strip():
            missing.append("SSH_USERNAME")
        if not (_settings.ssh_key_path or _settings.ssh_password):
            missing.append("SSH_KEY_PATH 或 SSH_PASSWORD")
        if missing:
            raise RuntimeError("请在 .envs 配置 " + ", ".join(missing))

        pkey = self._get_private_key()
        if not (pkey or _settings.ssh_password):
            raise RuntimeError("SSH_KEY_PATH 中没有可用私钥，请检查 .envs")
        password = _settings.ssh_password if not pkey else None

        # ── 尝试方式 A：直连 ──
        direct_error = self._try_connect_direct(pkey, password)
        if direct_error is None:
            return True

        # ── 尝试方式 B：SOCKS5 代理 ──
        if not (_settings.ssh_proxy_host and _settings.ssh_proxy_port > 0):
            logger.error("SSH 直连失败。如需代理，请在 .envs 配置 SSH_PROXY_HOST 和 SSH_PROXY_PORT")
            return False
        proxy_error = self._try_connect_proxy(pkey, password)
        if proxy_error is None:
            return True

        logger.error(
            "SSH connection failed — both direct and proxy attempts exhausted.\n"
            "  direct: %s\n  proxy:  %s",
            direct_error, proxy_error,
        )
        return False

    def _try_connect_direct(
        self, pkey: paramiko.PKey | None, password: str | None
    ) -> str | None:
        """尝试直连，成功返回 None，失败返回错误信息。"""
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            kwargs: dict = {
                "hostname": _settings.ssh_host,
                "port": _settings.ssh_port,
                "username": _settings.ssh_username,
                "timeout": _settings.ssh_timeout,
                "allow_agent": False,
                "look_for_keys": False,
            }
            if pkey:
                kwargs["pkey"] = pkey
            else:
                kwargs["password"] = password

            client.connect(**kwargs)
            self._client = client
            logger.info("SSH direct connection: %s@%s:%s", _settings.ssh_username, _settings.ssh_host, _settings.ssh_port)
            return None
        except Exception as e:
            return str(e)

    def _try_connect_proxy(
        self, pkey: paramiko.PKey | None, password: str | None
    ) -> str | None:
        """尝试通过 SOCKS 代理连接，成功返回 None，失败返回错误信息。"""
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            sock = self._create_proxy_socket(_settings.ssh_host, _settings.ssh_port)
            transport = paramiko.Transport(sock)
            if pkey:
                transport.connect(username=_settings.ssh_username, pkey=pkey)
            else:
                transport.connect(username=_settings.ssh_username, password=password)
            client._transport = transport

            self._client = client
            self._transport = transport
            logger.info("SSH proxy connection: %s:%s → %s@%s:%s",
                         _settings.ssh_proxy_host, _settings.ssh_proxy_port,
                         _settings.ssh_username, _settings.ssh_host, _settings.ssh_port)
            return None
        except Exception as e:
            return str(e)

    def execute_command(self, command: str) -> Tuple[str, str, int]:
        if not self._client:
            raise RuntimeError("Not connected to SSH server")
        logger.debug("Executing: %s", command[:120])
        stdin, stdout, stderr = self._client.exec_command(command)
        out = stdout.read().decode("utf-8", errors="ignore")
        err = stderr.read().decode("utf-8", errors="ignore")
        return out, err, stdout.channel.recv_exit_status()

    def upload_file(self, local_path: str, remote_path: str, force: bool = True) -> bool:
        if not self._client:
            raise RuntimeError("Not connected to SSH server")
        try:
            remote_dir = "/".join(remote_path.split("/")[:-1])
            if remote_dir:
                self.execute_command(f"mkdir -p {shlex.quote(remote_dir)}")
            # 先清理远端已有文件，避免权限冲突
            if force:
                self.execute_command(f"rm -f {shlex.quote(remote_path)}")
            with SCPClient(self._client.get_transport()) as scp:
                scp.put(local_path, remote_path)
            logger.info("Uploaded: %s -> %s", local_path, remote_path)
            return True
        except Exception as e:
            logger.error("Upload failed: %s", e)
            return False

    def download_file(self, remote_path: str, local_path: str) -> bool:
        if not self._client:
            raise RuntimeError("Not connected to SSH server")
        try:
            local_dir = os.path.dirname(local_path)
            if local_dir:
                os.makedirs(local_dir, exist_ok=True)
            with SCPClient(self._client.get_transport()) as scp:
                scp.get(remote_path, local_path)
            logger.info("Downloaded: %s -> %s", remote_path, local_path)
            return True
        except Exception as e:
            logger.error("Download failed: %s", e)
            return False

    def list_files(self, remote_dir: str) -> list:
        if not self._client:
            raise RuntimeError("Not connected to SSH server")
        try:
            stdout, stderr, _ = self.execute_command(f"ls -la {shlex.quote(remote_dir)}")
            if stderr:
                logger.warning("list_files stderr: %s", stderr)
            return stdout.split("\n")
        except Exception as e:
            logger.error("list_files failed: %s", e)
            return []

    def close(self) -> None:
        if self._transport:
            self._transport.close()
        if self._client:
            self._client.close()
            logger.info("SSH connection closed")

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
