"""RemoteFSService — 远程文件系统门面。

薄组合层，无业务逻辑。所有方法直接委托给 scanner / transfer。
"""

from __future__ import annotations

from pathlib import Path

from ._ftp import _FTPConfig, FTPConnector
from .models import (
    FileEntry,
    ProgressCallback,
    ScanConfig,
    ScanResult,
    TransferResult,
    TreeNode,
)
from .scanner import DirectoryScanner
from .transfer import (
    FolderPackStrategy,
    TransferClient,
)


class RemoteFSService:
    """远程文件系统服务 — 统一入口。

    组合 scanner（目录探索）和 transfer（文件传输）两个模块，
    对外暴露统一的方法签名。调用方无需关心底层走 FTP 还是 HTTP。

    Args:
        ftp_config:    部署者提供的 FTP 连接参数
        http_base_url: PHP HTTP API 地址
    """

    def __init__(
        self,
        ftp_config: _FTPConfig | None = None,
        http_base_url: str = "",
    ):
        self._ftp_config = ftp_config or _FTPConfig()
        self._http_base_url = http_base_url

        # 懒初始化：各自持有独立的 FTP 连接，避免相互干扰
        self._scanner: DirectoryScanner | None = None
        self._transfer_client: TransferClient | None = None
        self._ftp_connector: FTPConnector | None = None

    # ── 内部属性（懒初始化）──────────────────────

    @property
    def _scanner_inst(self) -> DirectoryScanner:
        if self._scanner is None:
            self._scanner = DirectoryScanner(self._ftp_config)
        return self._scanner

    @property
    def _transfer_inst(self) -> TransferClient:
        if self._transfer_client is None:
            if self._ftp_connector is None:
                self._ftp_connector = FTPConnector(self._ftp_config)
            self._transfer_client = TransferClient(
                self._ftp_connector, self._http_base_url,
            )
        return self._transfer_client

    # ══════════════════════════════════════════════
    #  目录探索（委托 scanner）
    # ══════════════════════════════════════════════

    def list_dir(self, path: str) -> list[FileEntry]:
        """列举远程路径下的文件和目录（单层，非递归）。"""
        return self._scanner_inst.list_dir(path)

    def explore(self, path: str, **kwargs) -> ScanResult:
        """递归扫描目录，返回结构化树。

        Keyword Args:
            传递给 ScanConfig 的参数：max_depth, smart_prune,
            detect_code_projects, rescue_important, extra_exclude
        """
        valid_keys = {
            "max_depth", "smart_prune", "detect_code_projects",
            "rescue_important", "extra_exclude",
        }
        config = ScanConfig(**{k: v for k, v in kwargs.items() if k in valid_keys})
        return self._scanner_inst.explore(path, config)

    def search(self, name: str, root: str = "/") -> list[str]:
        """精确搜索远程文件夹名称。

        Args:
            name: 要搜索的文件夹名（精确匹配）
            root: 搜索起点

        Returns:
            匹配路径列表
        """
        return self._scanner_inst.search(name, root)

    def tree(self, path: str, **kwargs) -> str:
        """递归扫描并返回人类可读的文本目录树。

        Keyword Args:
            同 explore()，传递给 ScanConfig
        """
        valid_keys = {
            "max_depth", "smart_prune", "detect_code_projects",
            "rescue_important", "extra_exclude",
        }
        config = ScanConfig(**{k: v for k, v in kwargs.items() if k in valid_keys})
        return self._scanner_inst.tree(path, config)

    # ══════════════════════════════════════════════
    #  文件传输（委托 transfer）
    # ══════════════════════════════════════════════

    def download_file(
        self,
        remote_path: str,
        local_path: str | Path,
        progress_cb: ProgressCallback = None,
    ) -> TransferResult:
        """下载远程单个文件。

        Args:
            remote_path: 远程文件路径
            local_path:  本地保存路径
            progress_cb: 进度回调
        """
        return self._transfer_inst.download_file(remote_path, local_path, progress_cb)

    def download_folder(
        self,
        remote_path: str,
        local_path: str | Path,
        progress_cb: ProgressCallback = None,
    ) -> TransferResult:
        """下载远程文件夹（使用当前打包策略）。

        Args:
            remote_path: 远程文件夹路径
            local_path:  本地保存路径（自动补 .tar.gz）
            progress_cb: 进度回调
        """
        return self._transfer_inst.download_folder(remote_path, local_path, progress_cb)

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
        return self._transfer_inst.download_by_name(
            remote_parent, folder_name, local_dir, progress_cb,
        )

    # ══════════════════════════════════════════════
    #  策略控制（高级用法）
    # ══════════════════════════════════════════════

    @property
    def pack_strategy(self) -> FolderPackStrategy:
        """当前文件夹打包策略。"""
        return self._transfer_inst.pack_strategy

    @pack_strategy.setter
    def pack_strategy(self, strategy: FolderPackStrategy) -> None:
        """替换文件夹打包策略。"""
        self._transfer_inst.pack_strategy = strategy

    @property
    def strategies(self) -> dict:
        """内置策略工厂（便捷访问）。"""
        from .transfer import AutoPackStrategy, FTPFileByFileStrategy, HTTPPackStrategy
        return {
            "http": HTTPPackStrategy(self._http_base_url),
            "ftp_file_by_file": FTPFileByFileStrategy(
                FTPConnector(self._ftp_config),
            ),
            "auto": AutoPackStrategy(
                http_strategy=HTTPPackStrategy(self._http_base_url),
                ftp_strategy=FTPFileByFileStrategy(
                    FTPConnector(self._ftp_config),
                ),
            ),
        }
