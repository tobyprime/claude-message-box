"""会话跟踪数据库 - 每个激活的 session 独立"""

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
        CREATE TABLE IF NOT EXISTS delivered (
            msg_id INTEGER PRIMARY KEY
        );
        CREATE TABLE IF NOT EXISTS done (
            msg_id INTEGER PRIMARY KEY
        );
        """
        ),
    )


def get_delivered_ids(db_path: str) -> set[int]:
    rows = _with_cursor(
        db_path,
        lambda c: c.execute("SELECT msg_id FROM delivered").fetchall(),
    )
    return {r["msg_id"] for r in rows}


def get_done_ids(db_path: str) -> set[int]:
    rows = _with_cursor(
        db_path,
        lambda c: c.execute("SELECT msg_id FROM done").fetchall(),
    )
    return {r["msg_id"] for r in rows}


def get_excluded_ids(db_path: str) -> set[int]:
    """返回 delivered + done 的并集"""
    return get_delivered_ids(db_path) | get_done_ids(db_path)


def mark_delivered(db_path: str, msg_ids: list[int]):
    if not msg_ids:
        return
    _with_cursor(
        db_path,
        lambda c: c.executemany(
            "INSERT OR IGNORE INTO delivered (msg_id) VALUES (?)",
            [(i,) for i in msg_ids],
        ),
    )


def mark_done(db_path: str, msg_ids: list[int]):
    if not msg_ids:
        return
    _with_cursor(
        db_path,
        lambda c: c.executemany(
            "INSERT OR IGNORE INTO done (msg_id) VALUES (?)",
            [(i,) for i in msg_ids],
        ),
    )


def get_active_sessions() -> list[dict]:
    if not config.SESSIONS_DIR.exists():
        return []
    sessions = []
    for f in sorted(config.SESSIONS_DIR.iterdir()):
        if f.name.endswith(".session.db"):
            sessions.append({"session_id": f.name.replace(".session.db", ""), "path": str(f)})
    return sessions
