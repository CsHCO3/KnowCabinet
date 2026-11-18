"""remote_fs — 受限环境下的远程文件系统探索与传输。

能力:
  - 目录浏览（list_dir, explore, tree）
  - 文件夹精确搜索（search）
  - 单文件下载（download_file）
  - 文件夹打包下载（download_folder），策略可插拔

领域模型见 models.py，统一入口见 service.py。
"""

from .service import RemoteFSService
from .models import (
    FileEntry,
    TreeNode,
    ScanConfig,
    ScanResult,
    TransferProgress,
    TransferResult,
)

__all__ = [
    "RemoteFSService",
    "FileEntry",
    "TreeNode",
    "ScanConfig",
    "ScanResult",
    "TransferProgress",
    "TransferResult",
]
