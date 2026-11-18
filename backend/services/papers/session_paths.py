"""论文站点的登录态与下载残留放在哪 —— 唯一定义。

这些文件的共同点：都是本机的**运行时数据**，里面是 cookie / storage_state / 下载下来的
pdf，绝不能进仓库。所以统一落在 ``data/paper_sessions/<站点>/``（``data/`` 已被
.gitignore 整目录排除），而不是像 2026-09 那样写进包目录 ``papers/<站点>_session/``：
那种写法只要 .gitignore 漏一条，cookie 就会跟着代码一起被提交（2026-10-27 就是这么漏的）。

新增站点只用改这个文件；JS 侧对应 ``browser/paths.js``，两边保持同一套目录。

迁移期兼容：新位置没有、但老位置（包目录下的 ``*_session/``）有时，读老位置。
"""
from __future__ import annotations

from pathlib import Path

from backend.path_setup import PROJECT_ROOT

#: 所有站点登录态的根目录（data/ 已被 .gitignore 排除）
SESSIONS_DIR = Path(PROJECT_ROOT) / "data" / "paper_sessions"

#: 老位置，只读兼容，2026-10-27 迁移到 SESSIONS_DIR 之后不再写入
LEGACY_DIRS = {
    "acm": Path(__file__).resolve().parent / "browser" / "acm_session",
    "cnki": Path(__file__).resolve().parent / "cnki_session",
    "wanfang": Path(__file__).resolve().parent / "wanfang_session",
}


def site_dir(site: str) -> Path:
    return SESSIONS_DIR / site


def storage_state(site: str) -> Path:
    """``<站点>/storage_state.json``；新位置没有、老位置有时返回老位置。"""
    p = site_dir(site) / "storage_state.json"
    if p.exists():
        return p
    legacy = LEGACY_DIRS.get(site)
    if legacy is not None:
        lp = legacy / "storage_state.json"
        if lp.exists():
            return lp
    return p


def ensure_dir(path: Path) -> Path:
    """建目录（写 login 态之前调用），返回原路径。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def downloads_dir(site: str) -> Path:
    d = site_dir(site) / "downloads"
    d.mkdir(parents=True, exist_ok=True)
    return d
