"""B站 SQLite 数据层 — 统一管理视频记录和问答任务。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

DB_DIR = Path(__file__).resolve().parent.parent.parent / "data"
DB_PATH = DB_DIR / "bili.db"


def get_connection() -> sqlite3.Connection:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """创建或升级数据库表结构。"""
    conn = get_connection()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bvid TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                author TEXT NOT NULL DEFAULT '',
                author_mid INTEGER DEFAULT 0,
                duration INTEGER DEFAULT 0,
                pubdate INTEGER DEFAULT 0,
                pic TEXT DEFAULT '',
                description TEXT DEFAULT '',
                view INTEGER DEFAULT 0,
                like_count INTEGER DEFAULT 0,
                fav_count INTEGER DEFAULT 0,
                coin_count INTEGER DEFAULT 0,
                share INTEGER DEFAULT 0,
                danmaku INTEGER DEFAULT 0,
                reply INTEGER DEFAULT 0,
                source_type TEXT DEFAULT '',
                source_detail TEXT DEFAULT '',
                subtitle_status TEXT DEFAULT 'none',
                subtitle_path TEXT DEFAULT '',
                summary_status TEXT DEFAULT 'none',
                summary_text TEXT DEFAULT '',
                category TEXT DEFAULT '',
                tags TEXT DEFAULT '',
                repo_urls TEXT DEFAULT '',
                bili_tags TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS query_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT DEFAULT '未命名任务',
                user_prompt TEXT NOT NULL,
                prompt_file_path TEXT DEFAULT '',
                filter_mode TEXT DEFAULT 'keyword',
                keyword TEXT DEFAULT '',
                video_ids TEXT DEFAULT '',
                result_text TEXT DEFAULT '',
                status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT (datetime('now')),
                completed_at TEXT DEFAULT ''
            );
        """)
        # 旧库迁移：标注三列（P-08 修复配套，2026-10-28 加）+ 官方标签列（问题4，2026-10-30 加）
        for col in ("category", "tags", "repo_urls", "bili_tags"):
            try:
                conn.execute(f"ALTER TABLE videos ADD COLUMN {col} TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass  # 列已存在
        conn.commit()
    finally:
        conn.close()


# ── 视频 CRUD ──────────────────────────────────────────


def insert_video(data: dict) -> int:
    """插入一条视频记录（bvid 冲突时忽略）。

    Returns:
        插入行的 id，若已存在则返回 0。
    """
    fields = [
        "bvid", "title", "author", "author_mid", "duration", "pubdate",
        "pic", "description", "view", "like_count", "fav_count",
        "coin_count", "share", "danmaku", "reply",
        "source_type", "source_detail",
        "subtitle_status", "subtitle_path", "summary_status", "summary_text",
        "category", "tags", "repo_urls", "bili_tags",
    ]
    placeholders = ", ".join(f":{f}" for f in fields)
    cols = ", ".join(fields)

    conn = get_connection()
    try:
        conn.execute(
            f"INSERT OR IGNORE INTO videos ({cols}) VALUES ({placeholders})",
            {f: data.get(f, "") for f in fields},
        )
        conn.commit()
        return conn.execute(
            "SELECT id FROM videos WHERE bvid = ?", (data.get("bvid", ""),)
        ).fetchone()["id"]
    finally:
        conn.close()


def insert_videos_batch(videos: list[dict]) -> dict:
    """批量插入，自动去重。

    Returns:
        {"inserted": N, "skipped": N, "ids": [id, ...]}
    """
    inserted = 0
    skipped = 0
    ids = []
    conn = get_connection()
    try:
        for v in videos:
            row = conn.execute(
                "SELECT id FROM videos WHERE bvid = ?", (v.get("bvid", ""),)
            ).fetchone()
            if row:
                skipped += 1
                ids.append(row["id"])
                continue
            cur = conn.execute(
                "INSERT INTO videos (bvid, title, author, author_mid, duration, pubdate, "
                "pic, description, view, like_count, fav_count, coin_count, share, "
                "danmaku, reply, source_type, source_detail) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    v.get("bvid", ""), v.get("title", ""), v.get("author", ""),
                    v.get("author_mid", 0), v.get("duration", 0), v.get("pubdate", 0),
                    v.get("pic", ""), v.get("description", ""),
                    v.get("view", 0), v.get("like_count", 0), v.get("fav_count", 0),
                    v.get("coin_count", 0), v.get("share", 0),
                    v.get("danmaku", 0), v.get("reply", 0),
                    v.get("source_type", ""), v.get("source_detail", ""),
                ),
            )
            inserted += 1
            ids.append(cur.lastrowid)
        conn.commit()
    finally:
        conn.close()
    return {"inserted": inserted, "skipped": skipped, "ids": ids}


def get_video(video_id: int) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_video_by_bvid(bvid: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM videos WHERE bvid = ?", (bvid,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_videos(
    subtitle_status: str | None = None,
    summary_status: str | None = None,
    keyword: str = "",
    author: str = "",
    view_min: int = 0,
    view_max: int = 0,
    source_type: str = "",
    order_by: str = "created_at",
    order_dir: str = "DESC",
    limit: int = 0,
    offset: int = 0,
) -> list[dict]:
    """查询视频列表，支持组合筛选。

    Args:
        subtitle_status: 筛选字幕状态，None 表示不限
        summary_status: 筛选摘要状态，None 表示不限
        keyword: 在 title/author/description 中搜索
        author: 按作者名精确匹配
        view_min/view_max: 播放量范围（0 表示不限）
        source_type: 来源类型
        order_by/order_dir: 排序
        limit/offset: 分页

    Returns:
        视频记录列表。
    """
    conditions = []
    params: list[Any] = []

    if subtitle_status is not None:
        conditions.append("subtitle_status = ?")
        params.append(subtitle_status)
    if summary_status is not None:
        conditions.append("summary_status = ?")
        params.append(summary_status)
    if keyword:
        conditions.append("(title LIKE ? OR author LIKE ? OR description LIKE ?)")
        kw = f"%{keyword}%"
        params.extend([kw, kw, kw])
    if author:
        conditions.append("author LIKE ?")
        params.append(f"%{author}%")
    if view_min > 0:
        conditions.append("view >= ?")
        params.append(view_min)
    if view_max > 0:
        conditions.append("view <= ?")
        params.append(view_max)
    if source_type:
        conditions.append("source_type = ?")
        params.append(source_type)

    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    # 白名单排序字段
    allowed_order = {"created_at", "updated_at", "view", "pubdate", "title", "duration"}
    if order_by not in allowed_order:
        order_by = "created_at"
    order_dir = "ASC" if order_dir.upper() == "ASC" else "DESC"

    sql = f"SELECT * FROM videos {where} ORDER BY {order_by} {order_dir}"
    if limit > 0:
        sql += f" LIMIT {limit}"
        if offset > 0:
            sql += f" OFFSET {offset}"

    conn = get_connection()
    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def count_videos(**filters) -> int:
    """统计视频数量，筛选条件同 get_videos。"""
    conditions = []
    params: list[Any] = []

    if filters.get("subtitle_status") is not None:
        conditions.append("subtitle_status = ?")
        params.append(filters["subtitle_status"])
    if filters.get("summary_status") is not None:
        conditions.append("summary_status = ?")
        params.append(filters["summary_status"])
    if filters.get("keyword"):
        conditions.append("(title LIKE ? OR author LIKE ? OR description LIKE ?)")
        kw = f"%{filters['keyword']}%"
        params.extend([kw, kw, kw])
    if filters.get("author"):
        conditions.append("author LIKE ?")
        params.append(f"%{filters['author']}%")
    if filters.get("source_type"):
        conditions.append("source_type = ?")
        params.append(filters["source_type"])

    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    conn = get_connection()
    try:
        row = conn.execute(f"SELECT COUNT(*) as cnt FROM videos {where}", params).fetchone()
        return row["cnt"] if row else 0
    finally:
        conn.close()


def update_video(video_id: int, data: dict) -> bool:
    """更新视频记录。

    data 中只包含要更新的字段。自动设置 updated_at。
    """
    allowed = {
        "title", "author", "author_mid", "duration", "pubdate", "pic",
        "description", "view", "like_count", "fav_count", "coin_count",
        "share", "danmaku", "reply",
        "subtitle_status", "subtitle_path", "summary_status", "summary_text",
        "category", "tags", "repo_urls", "bili_tags",
    }
    updates = {k: v for k, v in data.items() if k in allowed}
    if not updates:
        return False

    updates["updated_at"] = "datetime('now')"
    set_clause = ", ".join(f"{k} = ?" if k != "updated_at" else f"{k} = datetime('now')" for k in updates)
    values = [v for k, v in updates.items() if k != "updated_at"]

    conn = get_connection()
    try:
        cur = conn.execute(
            f"UPDATE videos SET {set_clause} WHERE id = ?",
            values + [video_id],
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_video(video_id: int) -> dict | None:
    """删除视频记录。

    Returns:
        被删除的记录 dict，或 None（不存在）。
    """
    video = get_video(video_id)
    if not video:
        return None
    conn = get_connection()
    try:
        conn.execute("DELETE FROM videos WHERE id = ?", (video_id,))
        conn.commit()
    finally:
        conn.close()
    return video


def cleanup_video_files(bvid: str) -> dict:
    """清理 bvid 关联的所有文件。

    Returns:
        {"deleted": [path, ...], "failed": [path, ...]}
    """
    import glob as glob_mod

    project_root = Path(__file__).resolve().parent.parent.parent
    patterns = [
        project_root / "data" / "bili_subtitles" / f"{bvid}.json",
        project_root / "data" / "bili_subtitles" / f"{bvid}_subtitle.txt",
        project_root / "output" / "bilibili" / f"*{bvid}*",
        project_root / ".data" / f"{bvid}_subtitle.txt",
    ]

    deleted = []
    failed = []
    for pattern in patterns:
        if "*" in str(pattern):
            for path in glob_mod.glob(str(pattern)):
                try:
                    path = Path(path)
                    path.unlink()
                    deleted.append(str(path))
                except Exception as e:
                    failed.append({"path": str(path), "error": str(e)})
        else:
            try:
                if pattern.exists():
                    pattern.unlink()
                    deleted.append(str(pattern))
            except Exception as e:
                failed.append({"path": str(pattern), "error": str(e)})
    return {"deleted": deleted, "failed": failed}


# ── 问答任务管理 ────────────────────────────────────────


def create_query_task(data: dict) -> int:
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO query_tasks (title, user_prompt, prompt_file_path, "
            "filter_mode, keyword, video_ids) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                data.get("title", "未命名任务"),
                data.get("user_prompt", ""),
                data.get("prompt_file_path", ""),
                data.get("filter_mode", "keyword"),
                data.get("keyword", ""),
                json.dumps(data.get("video_ids", []), ensure_ascii=False),
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_query_task(task_id: int) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM query_tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        try:
            result["video_ids"] = json.loads(result.get("video_ids", "[]"))
        except (json.JSONDecodeError, TypeError):
            result["video_ids"] = []
        return result
    finally:
        conn.close()


def get_query_tasks(limit: int = 50, offset: int = 0) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM query_tasks ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        tasks = []
        for r in rows:
            t = dict(r)
            try:
                t["video_ids"] = json.loads(t.get("video_ids", "[]"))
            except (json.JSONDecodeError, TypeError):
                t["video_ids"] = []
            tasks.append(t)
        return tasks
    finally:
        conn.close()


def update_query_task(task_id: int, data: dict) -> bool:
    allowed = {"title", "result_text", "status", "completed_at", "user_prompt",
               "prompt_file_path", "filter_mode", "keyword"}
    updates = {}
    for k, v in data.items():
        if k in allowed:
            updates[k] = v
    if not updates:
        return False

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values())

    conn = get_connection()
    try:
        cur = conn.execute(
            f"UPDATE query_tasks SET {set_clause} WHERE id = ?",
            values + [task_id],
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
