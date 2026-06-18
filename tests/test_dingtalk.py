"""测试 DingTalk 消息源

重点验证：
1. 未读会话按 unreadPoint 数量拉取消息。
2. 本地已读水位线能过滤旧消息、保留新消息。
3. 插入成功后更新水位线，避免下次重复拉取。
"""

import json
import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from msgbox.sources import dingtalk


@pytest.fixture
def tmp_state_db(monkeypatch):
    """为每个测试提供独立的 DingTalk 水位线 DB。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "dingtalk_state.db"
        monkeypatch.setattr(dingtalk, "_STATE_DB", str(db_path))
        # 清除线程本地缓存的 connection，避免跨测试复用
        dingtalk._state_local.conn = None
        yield str(db_path)
        dingtalk._state_local.conn = None


@pytest.fixture
def tmp_central_db(monkeypatch):
    """为每个测试提供独立的中央消息 DB。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "msgbox.db"
        monkeypatch.setattr("msgbox.config.CENTRAL_DB", db_path)
        from msgbox import db as central_db

        central_db.init_central_db(str(db_path))
        yield str(db_path)


class TestConversationWatermark:
    """验证本地已读水位线逻辑"""

    def test_get_watermark_missing_returns_none(self, tmp_state_db):
        """没有水位线时返回 None"""
        assert dingtalk._get_watermark("cid123") is None

    def test_set_and_get_watermark(self, tmp_state_db):
        """设置后能读回"""
        dingtalk._set_watermark("cid123", "2026-06-18 10:00:00")
        assert dingtalk._get_watermark("cid123") == "2026-06-18 10:00:00"

    def test_update_watermark_keeps_latest(self, tmp_state_db):
        """同一会话多次更新保留最新时间"""
        dingtalk._set_watermark("cid123", "2026-06-18 10:00:00")
        dingtalk._set_watermark("cid123", "2026-06-18 11:00:00")
        assert dingtalk._get_watermark("cid123") == "2026-06-18 11:00:00"

    def test_is_newer_than_watermark(self, tmp_state_db):
        """时间字符串比较新消息"""
        assert dingtalk._is_newer_than_watermark("cid123", "2026-06-18 10:00:00") is True
        dingtalk._set_watermark("cid123", "2026-06-18 10:00:00")
        assert dingtalk._is_newer_than_watermark("cid123", "2026-06-18 09:59:59") is False
        assert dingtalk._is_newer_than_watermark("cid123", "2026-06-18 10:00:01") is True

    def test_filter_new_messages_sorts_by_time(self, tmp_state_db):
        """过滤后按时间正序排列"""
        dingtalk._set_watermark("cid123", "2026-06-18 10:00:00")
        msgs = [
            {"openConversationId": "cid123", "createTime": "2026-06-18 10:00:02"},
            {"openConversationId": "cid123", "createTime": "2026-06-18 09:59:59"},
            {"openConversationId": "cid123", "createTime": "2026-06-18 10:00:01"},
        ]
        result = dingtalk._filter_new_messages(msgs)
        assert [m["createTime"] for m in result] == [
            "2026-06-18 10:00:01",
            "2026-06-18 10:00:02",
        ]

    def test_update_watermark_from_messages(self, tmp_state_db):
        """根据处理过的消息批量更新水位线"""
        msgs = [
            {"openConversationId": "cidA", "createTime": "2026-06-18 10:00:01"},
            {"openConversationId": "cidA", "createTime": "2026-06-18 10:00:03"},
            {"openConversationId": "cidB", "createTime": "2026-06-18 09:00:05"},
        ]
        dingtalk._update_watermark_from_messages(msgs)
        assert dingtalk._get_watermark("cidA") == "2026-06-18 10:00:03"
        assert dingtalk._get_watermark("cidB") == "2026-06-18 09:00:05"


class TestFetchConversationMessages:
    """验证按未读数拉取消息"""

    @patch("msgbox.sources.dingtalk._dws")
    def test_uses_unread_point_as_limit(self, mock_dws):
        """limit=None 时应使用 unreadPoint 作为拉取条数"""
        mock_dws.return_value = {"result": {"messages": []}}
        conv = {
            "openConversationId": "cidG",
            "singleChat": False,
            "title": "Test Group",
            "lastMsgCreateAt": 1781783104995,
            "unreadPoint": 7,
        }
        dingtalk._fetch_conversation_messages(conv, limit=None)
        args = mock_dws.call_args[0][0]
        assert "--limit" in args
        assert args[args.index("--limit") + 1] == "7"

    @patch("msgbox.sources.dingtalk._dws")
    def test_explicit_limit_overrides_unread(self, mock_dws):
        """传入 limit 时优先使用显式值"""
        mock_dws.return_value = {"result": {"messages": []}}
        conv = {
            "openConversationId": "cidG",
            "singleChat": False,
            "title": "Test Group",
            "lastMsgCreateAt": 1781783104995,
            "unreadPoint": 7,
        }
        dingtalk._fetch_conversation_messages(conv, limit=3)
        args = mock_dws.call_args[0][0]
        assert args[args.index("--limit") + 1] == "3"

    @patch("msgbox.sources.dingtalk._dws")
    def test_zero_unread_falls_back_to_one(self, mock_dws):
        """未读为 0 时至少拉 1 条，用于更新水位线"""
        mock_dws.return_value = {"result": {"messages": []}}
        conv = {
            "openConversationId": "cidG",
            "singleChat": False,
            "title": "Test Group",
            "lastMsgCreateAt": 1781783104995,
            "unreadPoint": 0,
        }
        dingtalk._fetch_conversation_messages(conv, limit=None)
        args = mock_dws.call_args[0][0]
        assert args[args.index("--limit") + 1] == "1"

    @patch("msgbox.sources.dingtalk._dws")
    def test_injects_context_into_messages(self, mock_dws):
        """拉取到的消息会注入会话上下文"""
        mock_dws.return_value = {
            "result": {
                "messages": [
                    {"content": "hi", "createTime": "2026-06-18 10:00:00"},
                ]
            }
        }
        conv = {
            "openConversationId": "cidG",
            "singleChat": False,
            "title": "Test Group",
            "lastMsgCreateAt": 1781783104995,
            "unreadPoint": 5,
        }
        msgs = dingtalk._fetch_conversation_messages(conv, limit=None)
        assert len(msgs) == 1
        assert msgs[0]["_conversation_title"] == "Test Group"
        assert msgs[0]["_single_chat"] is False
        assert msgs[0]["_unread_count"] == 5
        assert msgs[0]["_conversation_id"] == "cidG"


class TestPollUnreadConversations:
    """验证未读会话轮询 + 水位线过滤"""

    @patch("msgbox.sources.dingtalk._dws")
    def test_poll_filters_old_messages_by_watermark(self, mock_dws, tmp_state_db):
        """已处理过的旧消息被过滤，只保留新消息"""
        dingtalk._set_watermark("cidOld", "2026-06-18 10:00:00")

        def side_effect(args):
            if "list-unread-conversations" in args:
                return {
                    "result": {
                        "conversations": [
                            {
                                "openConversationId": "cidOld",
                                "singleChat": False,
                                "title": "Old Group",
                                "lastMsgCreateAt": 1781783104995,
                                "unreadPoint": 3,
                            }
                        ]
                    }
                }
            # list messages
            return {
                "result": {
                    "messages": [
                        {"content": "old", "createTime": "2026-06-18 09:59:59"},
                        {"content": "new", "createTime": "2026-06-18 10:00:01"},
                    ]
                }
            }

        mock_dws.side_effect = side_effect
        result = dingtalk.poll_unread_conversations()
        assert len(result) == 1
        assert result[0]["content"] == "new"

    @patch("msgbox.sources.dingtalk._dws")
    def test_poll_returns_all_new_messages(self, mock_dws, tmp_state_db):
        """15s 内 n 条新消息全部返回"""

        def side_effect(args):
            if "list-unread-conversations" in args:
                return {
                    "result": {
                        "conversations": [
                            {
                                "openConversationId": "cidBurst",
                                "singleChat": False,
                                "title": "Burst Group",
                                "lastMsgCreateAt": 1781783104995,
                                "unreadPoint": 5,
                            }
                        ]
                    }
                }
            return {
                "result": {
                    "messages": [
                        {"content": f"m{i}", "createTime": f"2026-06-18 10:00:0{i}"}
                        for i in range(1, 6)
                    ]
                }
            }

        mock_dws.side_effect = side_effect
        result = dingtalk.poll_unread_conversations()
        assert len(result) == 5
        assert [m["content"] for m in result] == ["m1", "m2", "m3", "m4", "m5"]


class TestInsertConversationMessages:
    """验证消息入库后更新水位线"""

    @patch("msgbox.sources.dingtalk._register_group")
    @patch("msgbox.sources.dingtalk.central_db.insert_message")
    def test_updates_watermark_after_insert(self, mock_insert, mock_register, tmp_state_db, tmp_central_db):
        """消息成功插入后应更新已读水位线"""
        mock_insert.return_value = 42
        msgs = [
            {
                "openConversationId": "cidW",
                "_conversation_id": "cidW",
                "_conversation_title": "W Group",
                "_single_chat": False,
                "sender": "Alice",
                "content": "hello",
                "createTime": "2026-06-18 10:00:05",
                "openMessageId": "msg1",
            },
            {
                "openConversationId": "cidW",
                "_conversation_id": "cidW",
                "_conversation_title": "W Group",
                "_single_chat": False,
                "sender": "Bob",
                "content": "world",
                "createTime": "2026-06-18 10:00:06",
                "openMessageId": "msg2",
            },
        ]
        seen = set()
        dingtalk._insert_conversation_messages(msgs, seen)
        assert dingtalk._get_watermark("cidW") == "2026-06-18 10:00:06"

    @patch("msgbox.sources.dingtalk._register_group")
    @patch("msgbox.sources.dingtalk.central_db.insert_message")
    def test_no_update_when_insert_fails(self, mock_insert, mock_register, tmp_state_db):
        """插入失败时不应更新水位线"""
        mock_insert.return_value = None  # insert failed / dedup
        msgs = [
            {
                "openConversationId": "cidF",
                "_conversation_id": "cidF",
                "_conversation_title": "F Group",
                "_single_chat": False,
                "sender": "Alice",
                "content": "hello",
                "createTime": "2026-06-18 10:00:05",
                "openMessageId": "msg1",
            }
        ]
        seen = set()
        dingtalk._insert_conversation_messages(msgs, seen)
        assert dingtalk._get_watermark("cidF") is None


class TestMappers:
    """验证消息映射"""

    def test_map_group_message_normalizes_newlines(self):
        """群聊消息应还原字面量换行，标题展示内容摘要"""
        msg = {
            "_conversation_title": "G",
            "sender": "Alice",
            "content": "line1\\nline2\\tTab",
            "openConversationId": "cid1",
            "openMessageId": "msg1",
            "createTime": "2026-06-18 10:00:00",
        }
        mapped = dingtalk.map_group_message(msg)
        assert "line1\nline2\tTab" in mapped["content"]
        assert mapped["type"] == "dingtalk.group"
        # 标题应展示消息内容摘要
        assert "line1" in mapped["title"]
        # 标识信息应放在正文
        assert "openConversationId=cid1" in mapped["content"]
        assert "openMessageId=msg1" in mapped["content"]

    def test_map_direct_message_content_in_title_identifiers_in_body(self):
        """单聊标题展示内容摘要，标识信息放到正文"""
        msg = {
            "_conversation_title": "DM",
            "sender": "Bob",
            "content": "hi",
            "openConversationId": "cid2",
            "openMessageId": "msg2",
            "_user_id": "uid123",
            "createTime": "2026-06-18 10:00:00",
        }
        mapped = dingtalk.map_direct_message(msg)
        assert mapped["type"] == "dingtalk.direct"
        # 标题应包含内容和会话名
        assert "hi" in mapped["title"]
        assert "DM" in mapped["title"]
        # 标识信息应放在正文
        assert "openConversationId=cid2" in mapped["content"]
        assert "openMessageId=msg2" in mapped["content"]
        assert "userId=uid123" in mapped["content"]
        # 标题不应再塞满标识
        assert "openConversationId" not in mapped["title"]
        assert "userId" not in mapped["title"]


class TestDedupKey:
    """验证去重键"""

    def test_dedup_key_group_message(self):
        msg = {
            "type": "dingtalk.group",
            "title": "t",
            "content": "c",
            "props": {"dingtalk_type": "group", "openMessageId": "msgX"},
        }
        assert dingtalk._dedup_key(msg) == "group:msgX"

    def test_dedup_key_mention(self):
        msg = {
            "type": "dingtalk.mention",
            "title": "t",
            "content": "c",
            "props": {"dingtalk_type": "mention", "msgId": "mid"},
        }
        assert dingtalk._dedup_key(msg) == "mention:mid"
