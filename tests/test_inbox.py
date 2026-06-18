"""测试 GitHub Inbox 消息源"""

from unittest.mock import patch, MagicMock

import pytest

from msgbox.sources import inbox


class TestMapNotification:
    """验证通知映射逻辑"""

    def test_issue_mention_popup(self):
        """Issue @提及 → popup"""
        n = {
            "id": "1",
            "reason": "mention",
            "repository": {"full_name": "owner/repo"},
            "subject": {
                "title": "Bug found",
                "type": "Issue",
                "url": "https://api.github.com/repos/owner/repo/issues/42",
            },
            "updated_at": "2026-01-01T00:00:00Z",
        }
        msg = inbox._map_notification(n)
        assert msg["type"] == "github.issue"
        assert msg["title"] == "Bug found"
        assert "mention" in msg["content"]
        assert "[#42]" in msg["content"]
        assert msg["props"]["reason"] == "mention"
        assert msg["props"]["source"] == "inbox"

    def test_pr_author_normal(self):
        """PR 自己操作 → normal"""
        n = {
            "id": "2",
            "reason": "author",
            "repository": {"full_name": "owner/repo"},
            "subject": {
                "title": "Fix the thing",
                "type": "PullRequest",
                "url": "https://api.github.com/repos/owner/repo/pulls/100",
            },
            "updated_at": "2026-01-01T00:00:00Z",
        }
        msg = inbox._map_notification(n)
        assert msg["type"] == "github.pr"
        assert "[#100]" in msg["content"]

    def test_discussion_comment_normal(self):
        """讨论新评论 → normal"""
        n = {
            "id": "3",
            "reason": "comment",
            "repository": {"full_name": "org/project"},
            "subject": {
                "title": "Ideas for v2",
                "type": "Discussion",
                "url": "https://api.github.com/repos/org/project/discussions/5",
            },
            "updated_at": "2026-01-01T00:00:00Z",
        }
        msg = inbox._map_notification(n)
        assert msg["type"] == "github.discussion"
        assert "[#5]" in msg["content"]

    def test_subscribed_silent(self):
        """订阅通知 → silent"""
        n = {
            "id": "4",
            "reason": "subscribed",
            "repository": {"full_name": "owner/repo"},
            "subject": {
                "title": "Random issue",
                "type": "Issue",
                "url": "https://api.github.com/repos/owner/repo/issues/1",
            },
            "updated_at": "2026-01-01T00:00:00Z",
        }
        msg = inbox._map_notification(n)
        assert msg["props"]["reason"] == "subscribed"

    def test_security_alert_popup(self):
        """安全告警 → popup"""
        n = {
            "id": "5",
            "reason": "security_alert",
            "repository": {"full_name": "owner/repo"},
            "subject": {
                "title": "Critical vulnerability",
                "type": "Issue",
                "url": "https://api.github.com/repos/owner/repo/issues/99",
            },
            "updated_at": "2026-01-01T00:00:00Z",
        }
        msg = inbox._map_notification(n)
        assert msg["props"]["reason"] == "security_alert"

    def test_no_url_no_crash(self):
        """没有 url 的也能正常映射"""
        n = {
            "id": "6",
            "reason": "author",
            "repository": {"full_name": "owner/repo"},
            "subject": {
                "title": "Plain notification",
                "type": "Issue",
            },
            "updated_at": "2026-01-01T00:00:00Z",
        }
        msg = inbox._map_notification(n)
        assert msg["title"] == "Plain notification"

    def test_unknown_type(self):
        """未知类型也能映射"""
        n = {
            "id": "7",
            "reason": "manual",
            "repository": {"full_name": "owner/repo"},
            "subject": {
                "title": "Something else",
                "type": "UnknownType",
                "url": "https://api.github.com/repos/owner/repo/something/1",
            },
            "updated_at": "2026-01-01T00:00:00Z",
        }
        msg = inbox._map_notification(n)
        assert msg["type"] == "github.unknowntype"


class TestFetchNotifications:
    """验证通知拉取"""

    @patch("msgbox.sources.inbox.subprocess.run")
    def test_successful_fetch(self, mock_run):
        """成功拉取通知列表"""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='[{"id":"1","reason":"mention","repository":{"full_name":"a/b"},"subject":{"title":"T","type":"Issue","url":"u"},"updated_at":"2026-01-01T00:00:00Z"}]',
        )
        result = inbox._fetch_notifications()
        assert len(result) == 1
        assert result[0]["id"] == "1"

    @patch("msgbox.sources.inbox.subprocess.run")
    def test_failed_fetch(self, mock_run):
        """拉取失败返回空列表"""
        mock_run.return_value = MagicMock(returncode=1, stderr="error")
        result = inbox._fetch_notifications()
        assert result == []

    @patch("msgbox.sources.inbox.subprocess.run")
    def test_fetch_with_since(self, mock_run):
        """带 since 参数拉取"""
        mock_run.return_value = MagicMock(returncode=0, stdout="[]")
        inbox._fetch_notifications("2026-01-01T00:00:00Z")
        # Verify --raw-field since=... was passed
        args = mock_run.call_args[0][0]
        assert "--raw-field" in args
        assert "since=2026-01-01T00:00:00Z" in str(args)

    @patch("msgbox.sources.inbox.subprocess.run")
    def test_invalid_json(self, mock_run):
        """JSON 解析失败返回空列表"""
        mock_run.return_value = MagicMock(returncode=0, stdout="not json")
        result = inbox._fetch_notifications()
        assert result == []


class TestGetSelfUser:
    """验证用户名检测"""

    @patch("msgbox.sources.inbox.subprocess.run")
    def test_successful(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="tobylinas2\n")
        assert inbox._get_self_user() == "tobylinas2"

    @patch("msgbox.sources.inbox.subprocess.run")
    def test_failure(self, mock_run):
        mock_run.side_effect = Exception("timeout")
        assert inbox._get_self_user() == ""


class TestPollInbox:
    """验证轮询循环"""

    def test_poll_empty_notifications(self):
        """无通知时静默"""
        stop = MagicMock()
        stop.is_set.side_effect = [False, True]  # run once, then stop

        with (
            patch("msgbox.sources.inbox._fetch_notifications", return_value=[]),
            patch("msgbox.sources.inbox._get_self_user", return_value="tobylinas2"),
            patch("msgbox.sources.inbox.central_db"),
        ):
            inbox.poll_inbox(30, stop)
            # Should not crash

    def test_poll_with_notifications(self):
        """有通知时正常入库"""
        stop = MagicMock()
        stop.is_set.side_effect = [False, True]

        notif = {
            "id": "1",
            "reason": "mention",
            "repository": {"full_name": "owner/repo"},
            "subject": {
                "title": "Test",
                "type": "Issue",
                "url": "https://api.github.com/repos/owner/repo/issues/1",
            },
            "updated_at": "2026-01-01T00:00:00Z",
        }

        with (
            patch("msgbox.sources.inbox._fetch_notifications", return_value=[notif]),
            patch("msgbox.sources.inbox._get_self_user", return_value="tobylinas2"),
            patch("msgbox.sources.inbox.central_db.insert_message", return_value=1) as mock_insert,
        ):
            inbox.poll_inbox(30, stop)
            assert mock_insert.called, "消息应被插入 DB"

    def test_poll_dedup_by_url(self):
        """同 URL 不重复插入"""
        stop = MagicMock()
        stop.is_set.side_effect = [False, True]

        notif = {
            "id": "1",
            "reason": "mention",
            "repository": {"full_name": "owner/repo"},
            "subject": {
                "title": "Test",
                "type": "Issue",
                "url": "https://api.github.com/repos/owner/repo/issues/1",
            },
            "updated_at": "2026-01-01T00:00:00Z",
        }

        with (
            patch("msgbox.sources.inbox._fetch_notifications", return_value=[notif, notif]),
            patch("msgbox.sources.inbox._get_self_user", return_value="tobylinas2"),
            patch("msgbox.sources.inbox.central_db.insert_message", return_value=1) as mock_insert,
        ):
            inbox.poll_inbox(30, stop)
            assert mock_insert.call_count == 1, "相同 URL 不应重复插入"

    def test_poll_error_handling(self):
        """异常不崩溃"""
        stop = MagicMock()
        stop.is_set.side_effect = [False, True]

        with (
            patch("msgbox.sources.inbox._fetch_notifications", side_effect=Exception("boom")),
            patch("msgbox.sources.inbox._get_self_user", return_value="tobylinas2"),
        ):
            inbox.poll_inbox(30, stop)
            # Should not crash
