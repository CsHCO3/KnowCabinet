"""集中化路径环境管理。

自动将项目根目录（以 CLAUDE.md 为标记）加入 sys.path。
向上一层层查找，无论文件在哪个子目录都能正确识别。

用法:
    # 入口脚本或模块在最前面导入即可:
    from backend.path_setup import PROJECT_ROOT, ensure

    # 任何需要独立运行的文件在顶部写入以下引导代码：
    #
    #   import sys
    #   from pathlib import Path
    #   _root = Path(__file__).resolve()
    #   for _p in _root.parents:
    #       if (_p / "CLAUDE.md").exists():
    #           _root = _p; break
    #   if str(_root) not in sys.path:
    #       sys.path.insert(0, str(_root))
    #
    # 之后即可正常 import backend.xxx
"""

import sys
from pathlib import Path

_MARKERS = ["CLAUDE.md", "README.md"]

def find_project_root() -> Path:
    """从当前文件向上查找，返回包含标记文件的项目根目录。"""
    start = Path(__file__).resolve()
    for parent in start.parents:
        for marker in _MARKERS:
            if (parent / marker).exists():
                return parent
    return start.parent  # fallback

def ensure() -> Path:
    """将项目根目录加入 sys.path（若尚未加入），返回根目录 Path。"""
    root = find_project_root()
    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    return root

PROJECT_ROOT: Path = ensure()
