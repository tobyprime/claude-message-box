"""测试 CLI 命令 - 重点关注 cmd_wait/peek 行为"""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from msgbox import cli
from msgbox import config
from msgbox import db as central_db
from msgbox import session as session_db


# ── 辅助 Fixtures ────────────────────────────────────────────


@pytest.fixture
def temp_sessions_dir():
    with tempfile.TemporaryDirectory() as tmp:
        with patch.object(config, "SESSIONS_DIR", Path(tmp)):
            yield tmp


@pytest.fixture
def temp_central_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = Path(f.name)
    with patch.object(config, "CENTRAL_DB", path):
        central_db.init_central_db(str(path))
        yield str(path)
    central_db._local.conn = None
    central_db._local.conn_path = None
    if path.exists():
        path.unlink()


@pytest.fixture
def activated_session(temp_sessions_dir, temp_central_db):
    """创建一个已激活的 session（使用隔离的中央 DB）"""
    sid = "test-session-12345"
    with patch("msgbox.cli._session_id", return_value=sid):
        db_path = cli._session_db_path(sid)
        session_db.init_session_db(db_path)
        yield sid, db_path


class TestCmdWaitDuration:
    """验证 cmd_wait 的轮询总时长正确"""

    def _run_wait_and_count_sleeps(self, idle, sleep):
        sleep_count = [0]

        def _sleep(_sec):
            sleep_count[0] += 1

        with (
            patch.object(config, "IDLE_DURATION", idle),
            patch.object(config, "SLEEP_DURATION", sleep),
            patch("msgbox.cli._session_id", return_value="test"),
            patch.object(Path, "exists", return_value=True),
            patch("msgbox.cli.session_db") as mock_session_db,
            patch("msgbox.cli.central_db") as mock_central_db,
            patch("msgbox.cli.load_config", return_value={"templates": {}}),
            patch("msgbox.cli.time.sleep", side_effect=_sleep),
            patch("msgbox.cli.sys.exit", side_effect=SystemExit),
        ):
            mock_session_db.get_excluded_ids.return_value = set()
            mock_central_db.get_unread_popup_count.return_value = 0
            mock_central_db.get_undelivered_messages.return_value = []

            with pytest.raises(SystemExit):
                cli.cmd_wait(None)

            return sleep_count[0]

    def test_total_duration_default(self):
        """默认：30+60=90s，90/5=18 次 sleep"""
        assert self._run_wait_and_count_sleeps(30, 60) == 18

    def test_custom_duration(self):
        """自定义：10+20=30s，30/5=6 次 sleep"""
        assert self._run_wait_and_count_sleeps(10, 20) == 6

    def test_duration_not_just_sleep(self):
        """验证是 idle+sleep，不是只用 sleep_duration"""
        count = self._run_wait_and_count_sleeps(10, 10)
        assert count == 4, f"idle+sleep=20s → 4 次，实际 {count}"


class TestCmdWaitPopups:
    """验证 cmd_wait 在 popup 存在时的行为"""

    def test_popup_exit_2_with_stderr(self, activated_session, temp_central_db):
        """有 popup 消息时打印到 stderr 并 exit(2)"""
        central_db.insert_message(str(config.CENTRAL_DB), "test.type", "Popup!", "urgent", category="popup")

        with (
            patch("msgbox.cli.time.sleep") as mock_sleep,
            patch("msgbox.cli.sys.exit", side_effect=SystemExit) as mock_exit,
        ):
            with pytest.raises(SystemExit):
                cli.cmd_wait(None)

            assert not mock_sleep.called, "有 popup 不应轮询"
            mock_exit.assert_called_once_with(2)

    def test_no_popup_polling_finds_message(self, activated_session, temp_central_db):
        """无 popup 时进入轮询，中途有消息应 exit(2)"""
        call_count = [0]

        def mock_get_undelivered(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] >= 3:
                return [{"id": 1, "category": "normal", "type": "test", "title": "Late msg", "content": "hello", "props": {}}]
            return []

        with (
            patch.object(config, "IDLE_DURATION", 10),
            patch.object(config, "SLEEP_DURATION", 10),
            patch("msgbox.cli.central_db.get_undelivered_messages", side_effect=mock_get_undelivered),
            patch("msgbox.cli.time.sleep"),
            patch("msgbox.cli.sys.exit", side_effect=SystemExit) as mock_exit,
        ):
            with pytest.raises(SystemExit):
                cli.cmd_wait(None)

            mock_exit.assert_called_once_with(2)


class TestCmdWaitExitConditions:
    """验证 cmd_wait 的各种退出条件"""

    def test_no_session_id(self):
        """没有 session_id 时静默 exit(0)"""
        with patch("msgbox.cli._session_id", return_value=None):
            with patch("msgbox.cli.sys.exit", side_effect=SystemExit) as mock_exit:
                with pytest.raises(SystemExit):
                    cli.cmd_wait(None)
                mock_exit.assert_called_once_with(0)

    def test_no_session_db(self):
        """session DB 不存在时静默 exit(0)"""
        with patch("msgbox.cli._session_id", return_value="no-such-session"):
            with patch.object(Path, "exists", return_value=False):
                with patch("msgbox.cli.sys.exit", side_effect=SystemExit) as mock_exit:
                    with pytest.raises(SystemExit):
                        cli.cmd_wait(None)
                    mock_exit.assert_called_once_with(0)

    def test_no_messages_exit_2(self, activated_session):
        """无消息到达时 exit(2)"""
        with (
            patch.object(config, "IDLE_DURATION", 1),
            patch.object(config, "SLEEP_DURATION", 1),
            patch("msgbox.cli.time.sleep"),
            patch("msgbox.cli.sys.exit", side_effect=SystemExit) as mock_exit,
        ):
            with pytest.raises(SystemExit):
                cli.cmd_wait(None)
            mock_exit.assert_called_once_with(2)


class TestCmdPeek:
    """验证 cmd_peek 行为"""

    def test_peek_exit_2_with_stderr(self, activated_session, temp_central_db):
        """有消息时 peek 打印到 stderr 并 exit(2)"""
        central_db.insert_message(str(config.CENTRAL_DB), "test", "Peek msg", "content", category="normal")

        with patch("msgbox.cli.sys.exit", side_effect=SystemExit) as mock_exit:
            with pytest.raises(SystemExit):
                cli.cmd_peek(None)
            mock_exit.assert_called_once_with(2)

    def test_peek_no_messages_silent(self, activated_session, temp_central_db):
        """无消息时 peek 静默 exit(0)"""
        with patch("msgbox.cli.sys.exit", side_effect=SystemExit) as mock_exit:
            cli.cmd_peek(None)
            assert not mock_exit.called, "无消息不应 exit"

    def test_peek_cooldown(self, temp_sessions_dir):
        """peek 冷却机制正常工作"""
        sid = "peek-test"
        cooldown_file = Path(temp_sessions_dir) / f"{sid}.peek_ts"

        with (
            patch("msgbox.cli._session_id", return_value=sid),
            patch.object(config, "PEEK_COOLDOWN", 10),
        ):
            cli.cmd_peek(None)
            assert cooldown_file.exists(), "peek 后应创建冷却文件"
            # 立即再 peek 不应报错
            cli.cmd_peek(None)


class TestCmdWaitRegressions:
    """回归测试"""

    def test_not_only_sleep_duration(self):
        """确保不是只用 sleep_duration（之前 simplify 的 bug）"""
        with patch.object(config, "IDLE_DURATION", 5):
            with patch.object(config, "SLEEP_DURATION", 10):
                sleep_count = [0]

                def _sleep(_sec):
                    sleep_count[0] += 1

                with (
                    patch("msgbox.cli._session_id", return_value="reg-test"),
                    patch.object(Path, "exists", return_value=True),
                    patch("msgbox.cli.session_db") as mock_session_db,
                    patch("msgbox.cli.central_db") as mock_central_db,
                    patch("msgbox.cli.load_config", return_value={"templates": {}}),
                    patch("msgbox.cli.time.sleep", side_effect=_sleep),
                    patch("msgbox.cli.sys.exit", side_effect=SystemExit),
                ):
                    mock_session_db.get_excluded_ids.return_value = set()
                    mock_central_db.get_unread_popup_count.return_value = 0
                    mock_central_db.get_undelivered_messages.return_value = []

                    with pytest.raises(SystemExit):
                        cli.cmd_wait(None)

                    # idle+sleep=15s → 15/5=3 次 sleep
                    assert sleep_count[0] == 3, f"预期 3 次 sleep，实际 {sleep_count[0]}"

    def test_output_goes_to_stderr(self, activated_session, temp_central_db):
        """验证输出写到 stderr"""
        central_db.insert_message(str(config.CENTRAL_DB), "test", "Popup!", "", category="popup")

        with (
            patch("msgbox.cli.sys.exit", side_effect=SystemExit),
            patch("builtins.print") as mock_print,
        ):
            with pytest.raises(SystemExit):
                cli.cmd_wait(None)

            # 确保 print 调用了 file=sys.stderr
            assert any(
                kw.get("file") is not None for _, kw in mock_print.call_args_list
            ), "输出应到 stderr"
