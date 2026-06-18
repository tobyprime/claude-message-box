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
            created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_messages_created_at
            ON messages(created_at);
        CREATE INDEX IF NOT EXISTS idx_messages_category
            ON messages(category);
        """
        ),
    )


def insert_message(db_path: str, type_: str, title: str, content: str, props: dict | None = None, category: str = "normal") -> int:
    init_central_db(db_path)
    row_id = _with_cursor(
        db_path,
        lambda c: c.execute(
            "INSERT INTO messages (type, title, content, props, category) VALUES (?, ?, ?, ?, ?)",
            [type_, title, content, json.dumps(props or {}), category],
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


def get_undelivered_messages(db_path: str, excluded_ids: set[int], categories: tuple[str, ...], limit: int = 50) -> list[dict]:
    if not excluded_ids:
        placeholders = ""
        params: list = []
    else:
        placeholders = " AND id NOT IN (" + ",".join("?" * len(excluded_ids)) + ")"
        params = list(excluded_ids)
    cat_placeholders = ",".join("?" * len(categories))
    sql = f"SELECT * FROM messages WHERE category IN ({cat_placeholders}){placeholders} ORDER BY id ASC LIMIT ?"
    rows = _with_cursor(
        db_path,
        lambda c: c.execute(sql, list(categories) + params + [limit]).fetchall(),
    )
    return [dict(r) for r in rows]


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
