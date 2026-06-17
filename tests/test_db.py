"""测试中央消息数据库"""

import json
import os
import tempfile

import pytest

from msgbox import db as central_db


@pytest.fixture
def db_path():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    central_db.init_central_db(path)
    yield path
    # 清除连接缓存
    central_db._local.conn = None
    central_db._local.conn_path = None
    os.unlink(path)


class TestInsertAndQuery:
    def test_insert_and_count(self, db_path):
        msg_id = central_db.insert_message(db_path, "test.type", "Title", "Content", {"key": "val"})
        assert msg_id == 1

        msg_id2 = central_db.insert_message(db_path, "other", "T2", "C2")
        assert msg_id2 == 2

    def test_get_messages_since(self, db_path):
        central_db.insert_message(db_path, "t1", "a", "b")
        central_db.insert_message(db_path, "t2", "c", "d")
        msgs = central_db.get_messages_since(db_path, 0)
        assert len(msgs) == 2
        msgs = central_db.get_messages_since(db_path, 1)
        assert len(msgs) == 1

    def test_get_messages_by_ids(self, db_path):
        id1 = central_db.insert_message(db_path, "t1", "a", "b")
        id2 = central_db.insert_message(db_path, "t2", "c", "d")
        msgs = central_db.get_messages_by_ids(db_path, [id1, id2])
        assert len(msgs) == 2

    def test_category_stored(self, db_path):
        central_db.insert_message(db_path, "alert", "Popup", "urgent", category="popup")
        msgs = central_db.get_messages_since(db_path, 0)
        assert msgs[0]["category"] == "popup"

    def test_props_as_json(self, db_path):
        props = {"repo": "foo", "priority": "high"}
        central_db.insert_message(db_path, "t", "t", "c", props=props)
        msgs = central_db.get_messages_since(db_path, 0)
        assert json.loads(msgs[0]["props"]) == props


class TestUndelivered:
    def test_get_undelivered(self, db_path):
        id1 = central_db.insert_message(db_path, "t1", "a", "b", category="normal")
        id2 = central_db.insert_message(db_path, "t2", "c", "d", category="popup")

        msgs = central_db.get_undelivered_messages(db_path, {id1}, ("normal", "popup"))
        assert len(msgs) == 1
        assert msgs[0]["id"] == id2

    def test_empty_excluded(self, db_path):
        central_db.insert_message(db_path, "t", "a", "b")
        msgs = central_db.get_undelivered_messages(db_path, set(), ("normal",))
        assert len(msgs) == 1

    def test_all_excluded(self, db_path):
        id1 = central_db.insert_message(db_path, "t", "a", "b")
        msgs = central_db.get_undelivered_messages(db_path, {id1}, ("normal",))
        assert msgs == []


class TestUnreadPopupCount:
    def test_count(self, db_path):
        central_db.insert_message(db_path, "a", "t", "c", category="popup")
        central_db.insert_message(db_path, "b", "t", "c", category="popup")
        central_db.insert_message(db_path, "c", "t", "c", category="normal")

        assert central_db.get_unread_popup_count(db_path, set()) == 2
        assert central_db.get_unread_popup_count(db_path, {1, 2}) == 0
        assert central_db.get_unread_popup_count(db_path, {1}) == 1
