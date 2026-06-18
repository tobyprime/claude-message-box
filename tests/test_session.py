"""测试会话跟踪数据库"""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from msgbox import session as session_db


@pytest.fixture
def db_path():
    with tempfile.NamedTemporaryFile(suffix=".session.db", delete=False) as f:
        path = f.name
    session_db.init_session_db(path)
    yield path
    session_db._local.conn = None
    session_db._local.conn_path = None
    os.unlink(path)


class TestReadCursor:
    def test_empty(self, db_path):
        assert session_db.get_read_cursor(db_path) == 0

    def test_set_and_get(self, db_path):
        session_db.set_read_cursor(db_path, 42)
        assert session_db.get_read_cursor(db_path) == 42

    def test_idempotent(self, db_path):
        session_db.set_read_cursor(db_path, 10)
        session_db.set_read_cursor(db_path, 10)
        assert session_db.get_read_cursor(db_path) == 10


class TestOpenPopups:
    def test_empty(self, db_path):
        assert session_db.get_open_popups(db_path) == set()
        assert session_db.get_open_popups(db_path, delivered_only=True) == set()

    def test_add_and_close(self, db_path):
        session_db.add_open_popups(db_path, [1, 2, 3])
        assert session_db.get_open_popups(db_path) == {1, 2, 3}
        assert session_db.get_open_popups(db_path, delivered_only=True) == set()

        session_db.close_popups(db_path, [1, 3])
        assert session_db.get_open_popups(db_path) == {2}

    def test_mark_delivered(self, db_path):
        session_db.add_open_popups(db_path, [1, 2])
        session_db.mark_popups_delivered(db_path, [2, 3])
        assert session_db.get_open_popups(db_path) == {1, 2, 3}
        assert session_db.get_open_popups(db_path, delivered_only=True) == {2, 3}


class TestLegacyCompat:
    def test_mark_delivered_advances_cursor(self, db_path):
        session_db.mark_delivered(db_path, [3, 4])
        assert session_db.get_read_cursor(db_path) == 4
        assert session_db.get_open_popups(db_path, delivered_only=True) == {3, 4}

    def test_mark_done_closes_popups(self, db_path):
        session_db.mark_popups_delivered(db_path, [1, 2])
        session_db.mark_done(db_path, [1])
        assert session_db.get_open_popups(db_path) == {2}

    def test_excluded_ids_returns_read_range(self, db_path):
        session_db.set_read_cursor(db_path, 5)
        assert session_db.get_excluded_ids(db_path) == {1, 2, 3, 4, 5}

    def test_delivered_ids(self, db_path):
        session_db.set_read_cursor(db_path, 3)
        session_db.mark_popups_delivered(db_path, [5])
        assert session_db.get_delivered_ids(db_path) == {1, 2, 3, 5}


class TestActiveSessions:
    def test_no_sessions_dir(self):
        with patch("msgbox.config.SESSIONS_DIR", Path("/nonexistent/path")):
            assert session_db.get_active_sessions() == []
