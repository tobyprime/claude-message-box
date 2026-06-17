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


class TestDelivered:
    def test_empty(self, db_path):
        assert session_db.get_delivered_ids(db_path) == set()

    def test_mark_and_get(self, db_path):
        session_db.mark_delivered(db_path, [1, 2, 3])
        assert session_db.get_delivered_ids(db_path) == {1, 2, 3}

    def test_idempotent(self, db_path):
        session_db.mark_delivered(db_path, [1])
        session_db.mark_delivered(db_path, [1])
        assert session_db.get_delivered_ids(db_path) == {1}


class TestDone:
    def test_empty(self, db_path):
        assert session_db.get_done_ids(db_path) == set()

    def test_mark_and_get(self, db_path):
        session_db.mark_done(db_path, [1, 2])
        assert session_db.get_done_ids(db_path) == {1, 2}

    def test_idempotent(self, db_path):
        session_db.mark_done(db_path, [1])
        session_db.mark_done(db_path, [1])
        assert session_db.get_done_ids(db_path) == {1}


class TestExcluded:
    def test_union(self, db_path):
        session_db.mark_delivered(db_path, [1, 2])
        session_db.mark_done(db_path, [2, 3])
        assert session_db.get_excluded_ids(db_path) == {1, 2, 3}

    def test_empty(self, db_path):
        assert session_db.get_excluded_ids(db_path) == set()


class TestActiveSessions:
    def test_no_sessions_dir(self):
        with patch("msgbox.config.SESSIONS_DIR", Path("/nonexistent/path")):
            assert session_db.get_active_sessions() == []
