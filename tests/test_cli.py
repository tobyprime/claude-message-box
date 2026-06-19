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
    with patch("msgbox.cli._session_id", return_value=sid), patch(
        "msgbox.cli._is_main_agent", return_value=True
    ):
        db_path = cli._session_db_path(sid)
        session_db.init_session_db(db_path)
        yield sid, db_path


@pytest.fixture
def as_main_agent():
    """强制当前进程视为主 agent，避免测试环境变量影响。"""
    with patch("msgbox.cli._is_main_agent", return_value=True):
        yield


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
            patch("msgbox.cli._is_main_agent", return_value=True),
            patch.object(Path, "exists", return_value=True),
            patch("msgbox.cli.session_db") as mock_session_db,
            patch("msgbox.cli.central_db") as mock_central_db,
            patch("msgbox.cli.load_config", return_value={"templates": {}}),
            patch("msgbox.cli.time.sleep", side_effect=_sleep),
            patch("msgbox.cli.sys.exit", side_effect=SystemExit),
        ):
            mock_session_db.get_read_cursor.return_value = 0
            mock_session_db.get_open_popups.return_value = set()
            mock_central_db.get_messages_after.return_value = []
            mock_central_db.get_messages_by_ids.return_value = []

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

        def mock_get_messages_after(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] >= 3:
                return [{"id": 1, "category": "normal", "type": "test", "title": "Late msg", "content": "hello", "props": {}}]
            return []

        with (
            patch.object(config, "IDLE_DURATION", 10),
            patch.object(config, "SLEEP_DURATION", 10),
            patch("msgbox.cli.central_db.get_messages_after", side_effect=mock_get_messages_after),
            patch("msgbox.cli.central_db.get_messages_by_ids", return_value=[]),
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

    def test_child_agent_silent(self):
        """子 agent 不应触发 wait 提醒"""
        with (
            patch("msgbox.cli._session_id", return_value="test"),
            patch("msgbox.cli._is_main_agent", return_value=False),
            patch("msgbox.cli.sys.exit", side_effect=SystemExit) as mock_exit,
        ):
            with pytest.raises(SystemExit):
                cli.cmd_wait(None)
            mock_exit.assert_called_once_with(0)


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
            patch("msgbox.cli._is_main_agent", return_value=True),
            patch.object(config, "PEEK_COOLDOWN", 10),
        ):
            cli.cmd_peek(None)
            assert cooldown_file.exists(), "peek 后应创建冷却文件"
            # 立即再 peek 不应报错
            cli.cmd_peek(None)

    def test_peek_child_agent_silent(self, temp_sessions_dir):
        """子 agent 不应触发 peek 提醒"""
        sid = "peek-child-test"
        cooldown_file = Path(temp_sessions_dir) / f"{sid}.peek_ts"

        with (
            patch("msgbox.cli._session_id", return_value=sid),
            patch("msgbox.cli._is_main_agent", return_value=False),
            patch.object(config, "PEEK_COOLDOWN", 10),
        ):
            cli.cmd_peek(None)
            assert not cooldown_file.exists(), "子 agent peek 不应创建冷却文件"


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
                    patch("msgbox.cli._is_main_agent", return_value=True),
                    patch.object(Path, "exists", return_value=True),
                    patch("msgbox.cli.session_db") as mock_session_db,
                    patch("msgbox.cli.central_db") as mock_central_db,
                    patch("msgbox.cli.load_config", return_value={"templates": {}}),
                    patch("msgbox.cli.time.sleep", side_effect=_sleep),
                    patch("msgbox.cli.sys.exit", side_effect=SystemExit),
                ):
                    mock_session_db.get_read_cursor.return_value = 0
                    mock_session_db.get_open_popups.return_value = set()
                    mock_central_db.get_messages_after.return_value = []
                    mock_central_db.get_messages_by_ids.return_value = []

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


class TestCmdStartAutoClose:
    """验证 cmd_start 启动行为"""

    def test_start_sets_cursor_to_max(self, temp_sessions_dir, temp_central_db):
        """启动新 session 时把 read_cursor 推到最大 id，避免历史消息刷屏"""
        sid = "new-session"
        central_db.insert_message(str(config.CENTRAL_DB), "test", "Old popup", "", category="popup")
        central_db.insert_message(str(config.CENTRAL_DB), "test", "Normal msg", "", category="normal")

        with patch("msgbox.cli._session_id", return_value=sid):
            cli.cmd_start(None)

        db_path = cli._session_db_path(sid)
        assert session_db.get_read_cursor(db_path) == 2
        assert session_db.get_open_popups(db_path) == set()

    def test_start_ignores_historical_messages(self, temp_sessions_dir, temp_central_db):
        """启动后 historical normal 不再被 peek 出来"""
        sid = "new-session"
        central_db.insert_message(str(config.CENTRAL_DB), "test", "Old normal", "", category="normal")

        with patch("msgbox.cli._session_id", return_value=sid):
            cli.cmd_start(None)

        with (
            patch("msgbox.cli.sys.exit", side_effect=SystemExit) as mock_exit,
            patch("msgbox.cli._touch_peek_cooldown"),
            patch("msgbox.cli._check_peek_cooldown", return_value=False),
            patch("msgbox.cli._is_main_agent", return_value=True),
        ):
            cli.cmd_peek(None)
            assert not mock_exit.called, "历史消息不应触发 exit"


class TestCmdClose:
    """验证 cmd_close 行为"""

    def test_close_by_ids(self, activated_session, temp_central_db):
        """按 ID close 消息"""
        sid, db_path = activated_session
        msg_id = central_db.insert_message(str(config.CENTRAL_DB), "test", "Popup!", "", category="popup")

        args = MagicMock(ids=f"{msg_id}")

        with patch("msgbox.cli._session_id", return_value=sid):
            cli.cmd_close(args)

        assert session_db.get_open_popups(db_path) == set()

    def test_close_only_delivered_open_popups(self, activated_session, temp_central_db):
        """默认只 close 已经 delivery 过的 popup"""
        sid, db_path = activated_session
        p1 = central_db.insert_message(str(config.CENTRAL_DB), "test", "P1", "", category="popup")
        p2 = central_db.insert_message(str(config.CENTRAL_DB), "test", "P2", "", category="popup")

        # Simulate peek: p1 delivered, p2 not yet
        session_db.mark_popups_delivered(db_path, [p1])

        args = MagicMock(ids=None)

        with patch("msgbox.cli._session_id", return_value=sid):
            cli.cmd_close(args)

        assert p1 not in session_db.get_open_popups(db_path), "已 delivered 的 popup 应被 close"
        assert p2 not in session_db.get_open_popups(db_path), "未 delivery 的 popup 不应进入 open_popups"

    def test_close_no_popups_silent(self, activated_session):
        """无 popup 时静默"""
        sid, _ = activated_session
        args = MagicMock(ids=None)

        with patch("msgbox.cli._session_id", return_value=sid):
            cli.cmd_close(args)  # 不应抛异常


class TestCmdWaitPopupCloseFilter:
    """验证 wait 中 popup 用 open_popups 过滤"""

    def test_popup_auto_delivered_by_wait(self, activated_session, temp_central_db):
        """wait 标记 popup 为 delivered（供 close 识别已看过）"""
        central_db.insert_message(str(config.CENTRAL_DB), "test", "Popup!", "", category="popup")

        with (
            patch("msgbox.cli.sys.exit", side_effect=SystemExit),
            patch("msgbox.cli.time.sleep"),
        ):
            with pytest.raises(SystemExit):
                cli.cmd_wait(None)

        sid, db_path = activated_session
        assert session_db.get_open_popups(db_path, delivered_only=True) == {1}, "wait 应自动 delivered popup"

    def test_popup_not_shown_after_close(self, activated_session, temp_central_db):
        """close 后的 popup 不再弹出"""
        sid, db_path = activated_session
        msg_id = central_db.insert_message(str(config.CENTRAL_DB), "test", "Popup!", "", category="popup")

        # close it
        session_db.close_popups(db_path, [msg_id])

        with (
            patch("msgbox.cli.sys.exit", side_effect=SystemExit) as mock_exit,
            patch("msgbox.cli.time.sleep"),
        ):
            with pytest.raises(SystemExit):
                cli.cmd_wait(None)

        # exit code 2 是从超时路径来的，不是 popup 路径
        mock_exit.assert_called_once_with(2)
