"""SQLite 连接、schema 初始化与 v1→v2 迁移。"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

_PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = _PACKAGE_DIR.parents[2]
# 派生层：kb.db 与知识库同源生成，放在 data/（删了可由 knowledgebase/ 重扫重建）
DATA_DIR = PROJECT_ROOT / "data" / "kb"
DB_PATH = DATA_DIR / "kb.db"
# 知识库自身的规则与记录（分区表/分级表/评测集/验收报告/词表）——
# 它们不是"处理中间物"，删了 knowledgebase 就没法扫，故与知识正文同处 knowledgebase/_meta。
META_DIR = PROJECT_ROOT / "knowledgebase" / "_meta"
SCHEMA_PATH = _PACKAGE_DIR / "schema.sql"

SCHEMA_VERSION = 2
VEC_DIM = 1024
VEC_TABLE = "kb_vec"


def now() -> str:
    """UTC ISO-8601 时间，便于排序与排障。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_sqlite_vec(conn: sqlite3.Connection) -> bool:
    """尝试把 sqlite-vec 扩展到当前连接；失败不抛错，返回 False 走容器 B 兜底。"""
    try:
        import sqlite_vec  # type: ignore
    except Exception:
        return False
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except Exception:
        try:
            conn.enable_load_extension(False)
        except Exception:
            pass
        return False


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    if column not in _table_columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def ensure_vec_table(conn: sqlite3.Connection) -> bool:
    """创建 kb_vec（vec0）虚表；扩展不可用/创建失败时返回 False。"""
    try:
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {VEC_TABLE} USING vec0("
            f"chunk_id INTEGER PRIMARY KEY, embedding float[{VEC_DIM}])"
        )
        return True
    except sqlite3.Error:
        return False


def has_vec_table(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (VEC_TABLE,)
    ).fetchone()
    return row is not None


def _migrate(conn: sqlite3.Connection) -> None:
    """v1 schema → v2：documents.arxiv_cats / chunks / kb_vec；fts_hash 为增量扫描指纹列。"""
    _ensure_column(conn, "documents", "arxiv_cats", "arxiv_cats TEXT NOT NULL DEFAULT '[]'")
    _ensure_column(conn, "documents", "fts_hash", "fts_hash TEXT")
    if load_sqlite_vec(conn):
        ensure_vec_table(conn)
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row is None:
        conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        return
    try:
        current = int(row[0])
    except (TypeError, ValueError):
        current = 0
    if current < SCHEMA_VERSION:
        conn.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """打开 kb.db，自动建立父目录、schema 与基础 PRAGMA，并完成 v1→v2 迁移。"""
    target = Path(path) if path else DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    _migrate(conn)
    conn.commit()
    return conn


# ── M6：写操作纪律（02 §16「短事务 + busy_timeout 重试 3 次」）────────────────

WRITE_RETRIES = 3          # 总尝试次数（02 §16 定稿"重试 3 次"）
WRITE_RETRY_DELAY = 0.2    # 秒；线性退避 0.2 / 0.4

_LOCK_MARKERS = ("database is locked", "database table is locked")


def is_locked_error(exc: BaseException) -> bool:
    """SQLite 竞争类错误（locked/busy）判定；非竞争错误不重试。"""
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    text = str(exc).lower()
    return any(marker in text for marker in _LOCK_MARKERS)


def write_retry(action, *, retries: int = WRITE_RETRIES, delay: float = WRITE_RETRY_DELAY):
    """执行一个「短事务写」动作，遇 locked/busy 整体重试，最多 `retries` 次尝试。

    `action()` 必须**自包含且可重入**：自己开事务（`BEGIN IMMEDIATE`）、自己
    commit、失败时 `action` 内部或此处负责 rollback。重试会重新调用整个动作，
    所以块内不能夹带不可回滚的外部副作用（写文件等）。

    单写者纪律：MCP 工具与 CLI 都经此入口，配合 WAL + busy_timeout=5000
    把"并发写"收敛成"排队 + 有限次重试"，耗尽后如实抛给调用方。
    """
    attempts = max(1, int(retries))
    last: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return action()
        except sqlite3.Error as exc:  # noqa: PERF203 — 重试是刻意的小循环
            last = exc
            if not is_locked_error(exc) or attempt >= attempts:
                raise
            time.sleep(delay * attempt)
    if last is not None:  # pragma: no cover — 循环必然 return 或 raise
        raise last
