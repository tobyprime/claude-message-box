"""数据库模块 - 中央消息数据库"""

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_local = threading.local()


def _get_conn(db_path: str) -> sqlite3.Connection:
    cached_path = getattr(_local, "conn_path", None)
    if cached_path == db_path:
        conn = getattr(_local, "conn", None)
        if conn is not None:
            return conn
    _local.conn = sqlite3.connect(db_path)
    _local.conn.row_factory = sqlite3.Row
    _local.conn.execute("PRAGMA journal_mode=WAL")
    _local.conn.execute("PRAGMA busy_timeout=5000")
    _local.conn_path = db_path
    return _local.conn


def _with_cursor(db_path: str, cb):
    conn = _get_conn(db_path)
    try:
        return cb(conn.cursor())
    except sqlite3.OperationalError:
        conn.rollback()
        raise
    finally:
        conn.commit()


# ── 中央数据库 ──────────────────────────────────────────────


def init_central_db(db_path: str):
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    _with_cursor(
        db_path,
        lambda c: c.executescript(
            """
        CREATE TABLE IF NOT EXISTS messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            type        TEXT    NOT NULL,
            props       TEXT    NOT NULL DEFAULT '{}',
            title       TEXT    NOT NULL DEFAULT '',
            content     TEXT    NOT NULL DEFAULT '',
            category    TEXT    NOT NULL DEFAULT 'normal',
            source      TEXT    NOT NULL DEFAULT '',
            created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_messages_created_at
            ON messages(created_at);
        CREATE INDEX IF NOT EXISTS idx_messages_category
            ON messages(category);
        CREATE INDEX IF NOT EXISTS idx_messages_category
            ON messages(category);
        """
        ),
    )
    # Migration: add source column + index if missing (for existing DBs created before)
    try:
        _with_cursor(
            db_path,
            lambda c: c.execute("ALTER TABLE messages ADD COLUMN source TEXT NOT NULL DEFAULT '';"),
        )
    except Exception:
        pass  # Column already exists
    try:
        _with_cursor(
            db_path,
            lambda c: c.execute("CREATE INDEX IF NOT EXISTS idx_messages_source ON messages(source);"),
        )
    except Exception:
        pass  # Index already exists


def insert_message(db_path: str, type_: str, title: str, content: str, props: dict | None = None, category: str = "normal", source: str = "") -> int:
    init_central_db(db_path)
    row_id = _with_cursor(
        db_path,
        lambda c: c.execute(
            "INSERT INTO messages (type, title, content, props, category, source) VALUES (?, ?, ?, ?, ?, ?)",
            [type_, title, content, json.dumps(props or {}), category, source],
        ).lastrowid,
    )
    return row_id


def get_messages_since(db_path: str, since_id: int, limit: int = 50) -> list[dict]:
    rows = _with_cursor(
        db_path,
        lambda c: c.execute(
            "SELECT * FROM messages WHERE id > ? ORDER BY id ASC LIMIT ?",
            [since_id, limit],
        ).fetchall(),
    )
    return [dict(r) for r in rows]


def get_messages_by_ids(db_path: str, ids: list[int]) -> list[dict]:
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    rows = _with_cursor(
        db_path,
        lambda c: c.execute(
            f"SELECT * FROM messages WHERE id IN ({placeholders}) ORDER BY id ASC",
            ids,
        ).fetchall(),
    )
    return [dict(r) for r in rows]


def get_unread_popup_count(db_path: str, excluded_ids: set[int]) -> int:
    if excluded_ids:
        placeholders = ",".join("?" * len(excluded_ids))
        row = _with_cursor(
            db_path,
            lambda c: c.execute(
                f"SELECT COUNT(*) as cnt FROM messages WHERE category='popup' AND id NOT IN ({placeholders})",
                list(excluded_ids),
            ).fetchone(),
        )
    else:
        row = _with_cursor(
            db_path,
            lambda c: c.execute("SELECT COUNT(*) as cnt FROM messages WHERE category='popup'").fetchone(),
        )
    return row["cnt"] if row else 0


def get_all_popup_ids(db_path: str) -> set[int]:
    rows = _with_cursor(
        db_path,
        lambda c: c.execute("SELECT id FROM messages WHERE category='popup'").fetchall(),
    )
    return {r["id"] for r in rows}


def get_max_message_id(db_path: str) -> int:
    row = _with_cursor(
        db_path,
        lambda c: c.execute("SELECT COALESCE(MAX(id), 0) AS max_id FROM messages").fetchone(),
    )
    return row["max_id"] if row else 0


def get_messages_after(
    db_path: str,
    after_id: int,
    categories: tuple[str, ...],
    excluded_ids: set[int] | None = None,
    limit: int = 50,
) -> list[dict]:
    """查询 id > after_id、指定类别、可选排除集合的消息。"""
    conditions = ["id > ?"]
    params: list = [after_id]

    if categories:
        cat_placeholders = ",".join("?" * len(categories))
        conditions.append(f"category IN ({cat_placeholders})")
        params.extend(categories)

    if excluded_ids:
        placeholders = ",".join("?" * len(excluded_ids))
        conditions.append(f"id NOT IN ({placeholders})")
        params.extend(excluded_ids)

    where_clause = " WHERE " + " AND ".join(conditions)
    sql = f"SELECT * FROM messages{where_clause} ORDER BY id ASC LIMIT ?"
    params.append(limit)

    rows = _with_cursor(
        db_path,
        lambda c: c.execute(sql, params).fetchall(),
    )
    return [dict(r) for r in rows]


def get_undelivered_messages(db_path: str, excluded_ids: set[int], categories: tuple[str, ...], limit: int = 50) -> list[dict]:
    """兼容旧 API：基于排除集合查询未读消息。"""
    return get_messages_after(db_path, 0, categories, excluded_ids=excluded_ids, limit=limit)


def message_exists_by_url(db_path: str, source: str, url: str) -> bool:
    """检查是否存在相同 source + url 的消息（用于去重）"""
    if not url:
        return False
    row = _with_cursor(
        db_path,
        lambda c: c.execute(
            "SELECT 1 FROM messages WHERE source=? AND props LIKE ? LIMIT 1",
            [source, f"%{url}%"],
        ).fetchone(),
    )
    return row is not None


def get_messages(
    db_path: str,
    *,
    limit: int = 50,
    offset: int = 0,
    categories: tuple[str, ...] | None = None,
    type_pattern: str | None = None,
) -> list[dict]:
    """查询历史消息，支持分页和类别/类型过滤。

    SELECT * FROM messages
    [WHERE category IN (...)]
    [AND type LIKE ...]
    ORDER BY id DESC
    LIMIT ? OFFSET ?
    """
    conditions: list[str] = []
    params: list = []

    if categories:
        cat_placeholders = ",".join("?" * len(categories))
        conditions.append(f"category IN ({cat_placeholders})")
        params.extend(categories)

    if type_pattern:
        conditions.append("type LIKE ?")
        params.append(type_pattern.replace("*", "%"))

    where_clause = ""
    if conditions:
        where_clause = " WHERE " + " AND ".join(conditions)

    sql = f"SELECT * FROM messages{where_clause} ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    rows = _with_cursor(
        db_path,
        lambda c: c.execute(sql, params).fetchall(),
    )
    return [dict(r) for r in rows]
