"""DirectoryScanner — FTP 远程目录扫描器，带智能剪枝。"""

from __future__ import annotations

import re
import time
from ftplib import error_perm, error_temp

from ._ftp import FTPConnector, _FTPConfig
from .models import FileEntry, TreeNode, ScanConfig, ScanResult


# ── 内置常量（移植自 frp.py）────────────────────

_CODE_MARKER_DIRS: set[str] = {
    ".git", ".svn", ".hg", ".idea", ".myeclipse", ".vscode", ".settings",
    "META-INF", "__pycache__", "node_modules", "vendor",
    "dist", "build", "target", "out", "bin", "obj",
    "lib", "libs", "packages", "venv", "env", "conda",
}

_CODE_EXTS: set[str] = {
    ".java", ".class", ".jar", ".py", ".js", ".ts", ".jsx", ".tsx",
    ".c", ".cpp", ".h", ".hpp", ".go", ".rs", ".swift", ".kt",
    ".html", ".htm", ".css", ".scss", ".less", ".xml", ".json",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd",
}

_IMPORTANT_EXTS: set[str] = {
    ".md", ".pdf", ".doc", ".docx", ".vsdx",
    ".ppt", ".pptx", ".xls", ".xlsx", ".jpg", ".png",
    ".gif", ".bmp", ".svg", ".zip", ".rar", ".7z",
}

_TXT_CHINESE_RE = re.compile(r"[一-鿿]")


# ── 扫描器 ───────────────────────────────────────


class DirectoryScanner:
    """基于 FTP 的远程目录扫描器。

    职责：
      - list_dir(path)          单层目录列表
      - explore(path, config)   递归扫描 → TreeNode 树
      - search(name, root)      精确搜索文件夹名
      - tree(path, config)      人类可读文本树
    """

    def __init__(self, config: _FTPConfig | None = None):
        self._connector = FTPConnector(config)

    # ── 公共 API ─────────────────────────────────

    def list_dir(self, path: str) -> list[FileEntry]:
        """列举指定路径下的文件和目录（单层，非递归）。

        Raises:
            ConnectionError: FTP 不可用
            FileNotFoundError: 远程路径不存在
            PermissionError: 权限不足
        """
        ftp = self._connector.ftp
        if ftp is None:
            raise ConnectionError("FTP 未连接")

        raw = self._list_entries(ftp, path)
        return [
            FileEntry(
                name=name,
                path=self._join(path, name),
                type=typ,
                size=size,
                mtime=mtime,
            )
            for name, typ, size, mtime in raw
        ]

    def explore(self, path: str, config: ScanConfig | None = None) -> ScanResult:
        """递归扫描目录，返回结构化树。

        Args:
            path:   远程起始路径
            config: 扫描参数，默认全开

        Returns:
            ScanResult，含 root TreeNode + 统计 + 非致命错误
        """
        cfg = config or ScanConfig()
        start = time.time()
        errors: list[str] = []
        root = self._scan_node(path, cfg, depth=0, errors=errors)
        if root is None:
            root = TreeNode(
                name=path.rstrip("/").split("/")[-1] or "/",
                path=path,
                type="dir",
                children=[],
            )

        stats = self._count(root)
        return ScanResult(
            root=root,
            total_dirs=stats[0],
            total_files=stats[1],
            errors=errors,
            elapsed_s=time.time() - start,
        )

    def search(self, name: str, root: str = "/") -> list[str]:
        """精确搜索文件夹名（大小写敏感）。

        不进入被排除的系统目录。不会触发模糊匹配。
        广度优先遍历。返回全部匹配路径。

        Args:
            name: 要搜索的文件夹名（精确匹配）
            root: 搜索起点

        Returns:
            匹配路径列表，如 ["/a/b/TargetDir", "/c/TargetDir"]
        """
        ftp = self._connector.ftp
        if ftp is None:
            raise ConnectionError("FTP 未连接")

        cfg = ScanConfig()
        found: list[str] = []
        queue: list[str] = [root]

        while queue:
            cur = queue.pop(0)
            try:
                entries = self._list_entries(ftp, cur)
            except (error_perm, error_temp, OSError) as exc:
                # 无法访问的目录跳过，不影响其他分支
                continue

            for entry_name, typ, _size, _mtime in entries:
                if typ != "dir":
                    continue
                if entry_name in cfg.all_exclude or entry_name.startswith((".", "~")):
                    continue

                full = self._join(cur, entry_name)
                if entry_name == name:
                    found.append(full)
                queue.append(full)

        return found

    def tree(self, path: str, config: ScanConfig | None = None) -> str:
        """递归扫描并以文本树形式返回。"""
        result = self.explore(path, config)
        lines: list[str] = []

        lines.append("=" * 60)
        if result.errors:
            lines.append(f"扫描完成（{len(result.errors)} 个告警）")
        else:
            lines.append("扫描完成")
        lines.append(f"目录: {path}")
        lines.append(f"耗时: {result.elapsed_s:.1f}s")
        lines.append(f"统计: {result.total_dirs} 目录, {result.total_files} 文件")
        lines.append("=" * 60)
        lines.append("")
        lines.append("[文件夹] = 目录  [文件] = 文件  [代码项目] = 已熔断（内部结构省略）")
        lines.append("")

        self._format_tree(result.root, lines)
        lines.append("")

        if result.errors:
            lines.append("-" * 40)
            lines.append(f"告警 ({len(result.errors)}):")
            for e in result.errors:
                lines.append(f"  {e}")

        return "\n".join(lines)

    # ── 内部：目录列表 ───────────────────────────

    @staticmethod
    def _list_entries(
        ftp: FTP, path: str
    ) -> list[tuple[str, str, int, float]]:
        """使用 LIST 解析目录。直接走 dir 解析，不尝试 MLSD。

        BusyBox ftpd 的 MLSD 在 UTF-8 模式下可能返回无效字节序列，
        LIST 更稳定且已通过 ftp.encoding 正确解码路径名。
        """
        return _parse_dir(ftp, path)

    # ── 内部：递归扫描 ───────────────────────────

    def _scan_node(
        self, path: str, cfg: ScanConfig, depth: int, errors: list[str]
    ) -> TreeNode | None:
        """递归扫描一个节点。返回 TreeNode 或 None（不可访问 / 应跳过）。"""
        ftp = self._connector.ftp
        if ftp is None:
            return None

        if cfg.max_depth != -1 and depth > cfg.max_depth:
            return None

        # 拿到当前目录的条目
        try:
            raw = self._list_entries(ftp, path)
        except (error_perm, error_temp, OSError) as exc:
            errors.append(f"[{path}] {exc}")
            return None

        # 过滤忽略目录
        dirs: list[str] = []
        files: list[tuple[str, int, float]] = []  # (name, size, mtime)
        for name, typ, size, mtime in raw:
            if typ == "dir":
                if name in cfg.all_exclude or name.startswith((".", "~")):
                    continue
                dirs.append(name)
            else:
                files.append((name, size, mtime))

        if not dirs and not files:
            return TreeNode(
                name=path.rstrip("/").split("/")[-1] or "/",
                path=path,
                type="dir",
                children=[],
            )

        # 检测是否为代码项目
        is_code_proj = False
        note = ""
        if cfg.detect_code_projects:
            has_marker = any(d in _CODE_MARKER_DIRS for d in dirs)
            has_code = any(
                "." in fname and fname.rsplit(".", 1)[1].lower() in
                {e.lstrip(".") for e in _CODE_EXTS}
                for fname, _, _ in files
            )
            if has_marker or has_code:
                is_code_proj = True
                note = " [代码项目]"

        # 组装当前节点的文件子节点
        children: list[TreeNode] = []
        for fname, fsize, fmtime in files:
            children.append(TreeNode(
                name=fname,
                path=self._join(path, fname),
                type="file",
                size=fsize,
                mtime=fmtime,
            ))

        if is_code_proj:
            # 熔断：不深入子目录，但捞取重要文件
            if cfg.rescue_important:
                rescued = self._rescue_important(path, dirs, cfg, depth, errors)
                children.extend(rescued)
        else:
            # 正常递归子目录
            for d in dirs:
                sub_path = self._join(path, d)
                child = self._scan_node(sub_path, cfg, depth + 1, errors)
                if child is not None:
                    children.append(child)

        # 按 dir → file 排序，各自按名字排序
        children.sort(key=lambda n: (0 if n.type == "dir" else 1, n.name))

        name_part = path.rstrip("/").split("/")[-1] or "/"
        return TreeNode(
            name=name_part,
            path=path,
            type="dir",
            children=children,
            note=note,
        )

    def _rescue_important(
        self,
        path: str,
        subdirs: list[str],
        cfg: ScanConfig,
        depth: int,
        errors: list[str],
    ) -> list[TreeNode]:
        """在代码项目熔断时，从非标记子目录中捞取重要文档文件。"""
        rescued: list[TreeNode] = []
        ftp = self._connector.ftp
        if ftp is None:
            return rescued

        for d in subdirs:
            if d in _CODE_MARKER_DIRS:
                continue
            sub_path = self._join(path, d)
            self._rescue_recurse(sub_path, cfg, depth + 1, rescued, errors)

        return rescued

    def _rescue_recurse(
        self, path: str, cfg: ScanConfig, depth: int,
        rescued: list[TreeNode], errors: list[str],
    ) -> None:
        """递归捞取重要文件（跳过标记目录和忽略目录）。"""
        if cfg.max_depth != -1 and depth > cfg.max_depth:
            return

        ftp = self._connector.ftp
        if ftp is None:
            return

        try:
            raw = self._list_entries(ftp, path)
        except (error_perm, error_temp, OSError) as exc:
            errors.append(f"[{path}] 救援失败: {exc}")
            return

        dirs: list[str] = []
        for name, typ, _, _ in raw:
            if typ == "dir":
                if name in cfg.all_exclude or name.startswith((".", "~")):
                    continue
                if name in _CODE_MARKER_DIRS:
                    continue
                dirs.append(name)
            else:
                if self._is_important_file(name):
                    rescued.append(TreeNode(
                        name=name,
                        path=self._join(path, name),
                        type="file",
                    ))

        for d in dirs:
            self._rescue_recurse(
                self._join(path, d), cfg, depth + 1, rescued, errors,
            )

    # ── 内部：帮助方法 ───────────────────────────

    @staticmethod
    def _join(base: str, name: str) -> str:
        """拼接路径，避免重复斜杠。"""
        if base == "/":
            return f"/{name}"
        return f"{base}/{name}"

    @staticmethod
    def _is_important_file(name: str) -> bool:
        ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext in _IMPORTANT_EXTS:
            return True
        if ext == ".txt":
            return bool(_TXT_CHINESE_RE.search(name))
        return False

    @staticmethod
    def _count(node: TreeNode) -> tuple[int, int]:
        """递归统计目录数和文件数。返回 (dirs, files)。"""
        dirs = 0
        files = 0
        if node.type == "file":
            return 0, 1
        dirs = 1  # self
        if node.children:
            for c in node.children:
                d, f = DirectoryScanner._count(c)
                dirs += d
                files += f
        return dirs, files

    # ── 内部：文本树格式化 ───────────────────────

    @staticmethod
    def _format_tree(node: TreeNode, lines: list[str], prefix: str = "",
                     is_last: bool = True) -> None:
        """递归输出文本树。"""
        connector = "└── " if is_last else "├── "
        note_str = node.note if node.note else ""
        if node.type == "dir":
            lines.append(f"{prefix}{connector}[文件夹] {node.name}{note_str}")
        else:
            size_str = _format_size(node.size)
            lines.append(f"{prefix}{connector}[文件] {node.name} ({size_str}){note_str}")

        if node.children is None:
            return

        new_prefix = prefix + ("    " if is_last else "│   ")
        for i, child in enumerate(node.children):
            DirectoryScanner._format_tree(
                child, lines, new_prefix,
                is_last=(i == len(node.children) - 1),
            )


# ── 底层 FTP LIST 解析 ──────────────────────────


def _decode_name(name: str) -> str:
    """将 FTP LIST 中的文件名解码为正确编码。

    - 如果 name 已包含非 latin-1 字符（已被 UTF-8 正确解码），直接返回
    - 否则尝试将 latin-1 解码的字节重试 UTF-8/GBK
    """
    # 已含非 latin-1 字符 → 已是正确编码
    try:
        name.encode("latin-1")
    except UnicodeEncodeError:
        return name

    # 是 latin-1 解码的字节 → 重解码
    raw = name.encode("latin-1")
    for enc in ("utf-8", "gbk", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return name


def _parse_mlsd(ftp: FTP, path: str) -> list[tuple[str, str, int, float]]:
    """使用 MLSD（RFC 3659）解析目录。"""
    import calendar
    from email.utils import parsedate_to_datetime

    results: list[tuple[str, str, int, float]] = []
    for name, facts in ftp.mlsd(path):
        if name in (".", ".."):
            continue
        typ = "dir" if facts.get("type") == "dir" else "file"
        size = int(facts.get("size", 0))
        mtime_str = facts.get("modify", "")
        mtime = 0.0
        if mtime_str:
            try:
                dt = parsedate_to_datetime(mtime_str)
                mtime = dt.timestamp()
            except Exception:
                pass
        results.append((name, typ, size, mtime))
    return results


def _list_raw(ftp: FTP, path: str) -> list[str]:
    """通过二进制数据连接读取 LIST 输出，每行用 UTF-8 或 latin-1 安全解码。"""
    from ftplib import error_reply

    # 切换为二进制模式（避免数据通道编码干扰）
    try:
        ftp.voidcmd("TYPE I")
    except error_reply:
        pass

    # 发送 LIST 命令，获取原始数据连接
    cmd = f"LIST {path}"
    try:
        sock = ftp.transfercmd(cmd)
    except (error_perm, error_temp, error_reply):
        # 如果 LIST path 失败，尝试 LIST 当前目录
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
    ftp.voidresp()  # 消费 "226 Transfer complete"

    # 分割行并安全解码
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


def _parse_dir(ftp: FTP, path: str) -> list[tuple[str, str, int, float]]:
    """解析 LIST 输出（Unix 格式）。"""
    from calendar import month_name

    lines = _list_raw(ftp, path)

    results: list[tuple[str, str, int, float]] = []
    month_map = {m.lower(): i for i, m in enumerate(month_name) if m}

    for line in lines:
        parts = line.split()
        if len(parts) < 6:
            continue

        perms = parts[0]
        typ = "dir" if perms.startswith("d") else "file"

        size = 0
        mtime = 0.0
        try:
            time_fields = parts[-4:-1] if len(parts) >= 9 else parts[-3:]
            month_str = time_fields[0].lower() if len(time_fields) >= 1 else ""
            day_str = time_fields[1] if len(time_fields) >= 2 else ""
            time_str = time_fields[2] if len(time_fields) >= 3 else ""

            if month_str in month_map:
                mon = month_map[month_str]
                day = int(day_str) if day_str.isdigit() else 1
                if ":" in time_str:
                    h, m = time_str.split(":")
                    now = time.localtime()
                    tm = (now.tm_year, mon, day, int(h), int(m), 0, 0, 0, 0)
                else:
                    tm = (int(time_str), mon, day, 0, 0, 0, 0, 0, 0)
                mtime = time.mktime(tm)

            if len(parts) >= 5:
                try:
                    size = int(parts[4])
                except (ValueError, IndexError):
                    size = 0
        except (ValueError, IndexError):
            pass

        name = _decode_name(line.split(None, 8)[-1] if len(parts) >= 9 else parts[-1])
        if name in (".", ".."):
            continue

        results.append((name, typ, size, mtime))

    return results


def _format_size(size_bytes: int) -> str:
    """人类可读文件大小。"""
    if size_bytes < 1024:
        return f"{size_bytes}B"
    elif size_bytes < 1024 ** 2:
        return f"{size_bytes / 1024:.0f}KB"
    elif size_bytes < 1024 ** 3:
        return f"{size_bytes / 1024 ** 2:.1f}MB"
    else:
        return f"{size_bytes / 1024 ** 3:.1f}GB"
