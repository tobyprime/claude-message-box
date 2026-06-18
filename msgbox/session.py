"""会话跟踪数据库 - 每个激活的 session 独立

状态模型：
- read_cursor: 已阅普通消息的最大 id（所有 id <= cursor 的 normal 视为已阅）
- open_popups: 仍未关闭的 popup 消息 id 集合（delivered 后仍保留，close 时删除）
"""

import sqlite3
import threading
from pathlib import Path

from . import config

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


def init_session_db(db_path: str):
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    _with_cursor(
        db_path,
        lambda c: c.executescript(
            """
        CREATE TABLE IF NOT EXISTS read_cursor (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            cursor INTEGER NOT NULL DEFAULT 0
        );
        INSERT OR IGNORE INTO read_cursor (id, cursor) VALUES (1, 0);

        CREATE TABLE IF NOT EXISTS open_popups (
            msg_id INTEGER PRIMARY KEY,
            delivered BOOLEAN NOT NULL DEFAULT 0
        );
        """
        ),
    )


def get_read_cursor(db_path: str) -> int:
    row = _with_cursor(
        db_path,
        lambda c: c.execute("SELECT cursor FROM read_cursor WHERE id = 1").fetchone(),
    )
    return row["cursor"] if row else 0


def set_read_cursor(db_path: str, cursor: int):
    _with_cursor(
        db_path,
        lambda c: c.execute(
            "INSERT OR REPLACE INTO read_cursor (id, cursor) VALUES (1, ?)",
            [cursor],
        ),
    )


def get_open_popups(db_path: str, delivered_only: bool = False) -> set[int]:
    sql = "SELECT msg_id FROM open_popups"
    if delivered_only:
        sql += " WHERE delivered = 1"
    rows = _with_cursor(
        db_path,
        lambda c: c.execute(sql).fetchall(),
    )
    return {r["msg_id"] for r in rows}


def add_open_popups(db_path: str, msg_ids: list[int]):
    if not msg_ids:
        return
    _with_cursor(
        db_path,
        lambda c: c.executemany(
            "INSERT OR IGNORE INTO open_popups (msg_id, delivered) VALUES (?, 0)",
            [(i,) for i in msg_ids],
        ),
    )


def mark_popups_delivered(db_path: str, msg_ids: list[int]):
    if not msg_ids:
        return
    _with_cursor(
        db_path,
        lambda c: c.executemany(
            "INSERT OR REPLACE INTO open_popups (msg_id, delivered) VALUES (?, 1)",
            [(i,) for i in msg_ids],
        ),
    )


def close_popups(db_path: str, msg_ids: list[int]):
    if not msg_ids:
        return
    _with_cursor(
        db_path,
        lambda c: c.executemany(
            "DELETE FROM open_popups WHERE msg_id = ?",
            [(i,) for i in msg_ids],
        ),
    )


def get_excluded_ids(db_path: str) -> set[int]:
    """返回 normal 已阅范围 + 已关闭 popup 的 id（用于历史兼容命令）。"""
    cursor = get_read_cursor(db_path)
    return set(range(1, cursor + 1))


def get_done_ids(db_path: str) -> set[int]:
    """历史兼容：返回已关闭 popup 的集合。

    由于 popup 是否已关闭完全由中央库 messages 表决定，
    这里不再单独维护关闭集合。调用方应通过 get_open_popups
    与中央库 get_all_popup_ids 对比得到已关闭集合。
    """
    return set()


def get_delivered_ids(db_path: str) -> set[int]:
    """返回已阅 normal 的 id 集合（<= cursor）+ 已交付 popup 的 id。"""
    cursor = get_read_cursor(db_path)
    return set(range(1, cursor + 1)) | get_open_popups(db_path, delivered_only=True)


def mark_delivered(db_path: str, msg_ids: list[int]):
    """兼容旧 API：按消息 id 标记为已阅/已交付。"""
    if not msg_ids:
        return
    cursor = get_read_cursor(db_path)
    max_normal = cursor
    popup_ids = []
    for mid in msg_ids:
        if mid <= cursor:
            continue
        popup_ids.append(mid)
        if mid > max_normal:
            max_normal = mid
    if max_normal > cursor:
        set_read_cursor(db_path, max_normal)
    if popup_ids:
        mark_popups_delivered(db_path, popup_ids)


def mark_done(db_path: str, msg_ids: list[int]):
    """兼容旧 API：标记为已完成/关闭 popup。"""
    close_popups(db_path, msg_ids)


def get_active_sessions() -> list[dict]:
    if not config.SESSIONS_DIR.exists():
        return []
    sessions = []
    for f in sorted(config.SESSIONS_DIR.iterdir()):
        if f.name.endswith(".session.db"):
            sessions.append({"session_id": f.name.replace(".session.db", ""), "path": str(f)})
    return sessions
