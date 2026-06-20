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


class TestGetMessages:
    def test_all(self, db_path):
        for i in range(5):
            central_db.insert_message(db_path, f"type.{i}", f"Title {i}", f"Content {i}")
        msgs = central_db.get_messages(db_path)
        assert len(msgs) == 5
        # 默认降序
        assert msgs[0]["id"] == 5
        assert msgs[-1]["id"] == 1

    def test_limit(self, db_path):
        for i in range(10):
            central_db.insert_message(db_path, f"t.{i}", f"Title {i}", f"C{i}")
        msgs = central_db.get_messages(db_path, limit=3)
        assert len(msgs) == 3
        assert msgs[0]["id"] == 10

    def test_offset(self, db_path):
        for i in range(10):
            central_db.insert_message(db_path, f"t.{i}", f"Title {i}", f"C{i}")
        msgs = central_db.get_messages(db_path, limit=3, offset=5)
        assert len(msgs) == 3
        # offset 5, 降序 → id 5,4,3
        assert msgs[0]["id"] == 5
        assert msgs[-1]["id"] == 3

    def test_category_filter(self, db_path):
        central_db.insert_message(db_path, "a", "t", "c", category="popup")
        central_db.insert_message(db_path, "b", "t", "c", category="normal")
        central_db.insert_message(db_path, "c", "t", "c", category="silent")

        msgs = central_db.get_messages(db_path, categories=("popup",))
        assert len(msgs) == 1
        assert msgs[0]["category"] == "popup"

        msgs = central_db.get_messages(db_path, categories=("popup", "normal"))
        assert len(msgs) == 2

    def test_type_pattern(self, db_path):
        central_db.insert_message(db_path, "github.issue", "t", "c")
        central_db.insert_message(db_path, "github.pr", "t", "c")
        central_db.insert_message(db_path, "custom.event", "t", "c")

        msgs = central_db.get_messages(db_path, type_pattern="github.*")
        assert len(msgs) == 2
        assert all(m["type"].startswith("github.") for m in msgs)

        msgs = central_db.get_messages(db_path, type_pattern="custom.*")
        assert len(msgs) == 1

    def test_empty_db(self, db_path):
        msgs = central_db.get_messages(db_path)
        assert msgs == []


class TestForSession:
    def test_insert_with_session(self, db_path):
        """发送给指定 session 的消息应只有目标 session 能看到。"""
        sid_a = "session-a"
        sid_b = "session-b"

        # 广播消息
        central_db.insert_message(db_path, "broadcast", "B", "C", category="normal")
        # 定向给 A
        central_db.insert_message(db_path, "direct", "To A", "C", category="normal", for_session=sid_a)
        # 定向给 B
        central_db.insert_message(db_path, "direct", "To B", "C", category="normal", for_session=sid_b)

        # A 应看到广播 + 自己的消息
        msgs_a = central_db.get_messages_after(db_path, 0, ("normal",), for_session=sid_a)
        assert len(msgs_a) == 2
        assert msgs_a[0]["id"] == 1
        assert msgs_a[1]["id"] == 2

        # B 应看到广播 + 自己的消息
        msgs_b = central_db.get_messages_after(db_path, 0, ("normal",), for_session=sid_b)
        assert len(msgs_b) == 2
        assert msgs_b[0]["id"] == 1
        assert msgs_b[1]["id"] == 3

        # 无 session 过滤应只看到广播 (向后兼容)
        msgs = central_db.get_messages_after(db_path, 0, ("normal",))
        assert len(msgs) == 1
        assert msgs[0]["id"] == 1

    def test_get_messages_by_ids_with_session(self, db_path):
        central_db.insert_message(db_path, "b", "B", "C", category="normal")
        central_db.insert_message(db_path, "d", "D", "C", category="normal", for_session="s1")

        # for_session=s1 应看到定向消息 + 广播
        msgs = central_db.get_messages_by_ids(db_path, [1, 2], for_session="s1")
        assert len(msgs) == 2

        # 不过滤时返回所有（精确 ID 查询）
        msgs = central_db.get_messages_by_ids(db_path, [1, 2])
        assert len(msgs) == 2

    def test_get_messages_with_session(self, db_path):
        central_db.insert_message(db_path, "b", "B", "C", category="normal")
        central_db.insert_message(db_path, "d", "D", "C", category="normal", for_session="s1")

        msgs = central_db.get_messages(db_path, for_session="s1")
        assert len(msgs) == 2

        msgs = central_db.get_messages(db_path)
        assert len(msgs) == 1
