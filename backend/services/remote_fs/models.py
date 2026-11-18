"""remote_fs 数据契约 — 所有模块的 I/O 都使用这里的模型。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Literal


# ── 文件条目 ─────────────────────────────────────


@dataclass
class FileEntry:
    """远程文件/目录条目（单层列表时使用）。"""
    name: str
    path: str
    type: Literal["dir", "file"]
    size: int = 0
    mtime: float = 0.0              # Unix 时间戳


# ── 目录树 ───────────────────────────────────────


@dataclass
class TreeNode:
    """目录树节点（递归结构）。

    - children=None  → 文件
    - children=[]    → 空目录
    - children=[...] → 非空目录
    """

    name: str
    path: str
    type: Literal["dir", "file"]
    size: int = 0
    mtime: float = 0.0
    children: list[TreeNode] | None = None
    note: str = ""                   # 附加备注，如 "[代码项目]"


# ── 扫描配置 ─────────────────────────────────────


# 默认排除目录（系统级，不可遍历也不会出现在搜索结果中）
_DEFAULT_EXCLUDE: set[str] = {
    "$Recycle.Bin", "System Volume Information", "RECYCLER",
    "lost+found", ".Trash", ".Trash-1000",
    "USB1_e4", "USB2_e4", "sda1", "sdb1",
}


@dataclass
class ScanConfig:
    """扫描参数 — 聚焦目录结构探索，不涉及文件内容理解。

    Args:
        max_depth:      最大递归深度，-1 为不限制
        smart_prune:    是否跳过系统级目录
        detect_code_projects:
                        是否检测代码项目标记目录（.git / .svn / .idea 等），
                        检测到后不深入内部，只捞取重要文件（PDF、doc 等）
        rescue_important:
                        代码项目熔断时是否捞取其中的重要文档文件
        extra_exclude:  调用方追加的排除目录名
    """
    max_depth: int = -1
    smart_prune: bool = True
    detect_code_projects: bool = True
    rescue_important: bool = True
    extra_exclude: set[str] = field(default_factory=set)

    @property
    def all_exclude(self) -> set[str]:
        if self.smart_prune:
            return _DEFAULT_EXCLUDE | self.extra_exclude
        return self.extra_exclude


# ── 扫描结果 ─────────────────────────────────────


@dataclass
class ScanResult:
    """单次扫描的完整结果。"""
    root: TreeNode
    total_dirs: int = 0
    total_files: int = 0
    errors: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0


# ── 传输进度 / 结果 ──────────────────────────────


@dataclass
class TransferProgress:
    """下载进度回调参数。"""
    bytes_downloaded: int = 0
    total_bytes: int = 0
    phase: str = ""                  # "connecting" / "downloading" / "packing" / "done"
    message: str = ""


@dataclass
class TransferResult:
    """传输操作结果。"""
    success: bool
    local_path: str = ""
    remote_path: str = ""
    bytes_transferred: int = 0
    elapsed_s: float = 0.0
    error: str | None = None
    strategy: str = ""               # 实际使用的策略标识


# 进度回调签名
ProgressCallback = Callable[[TransferProgress], None] | None
