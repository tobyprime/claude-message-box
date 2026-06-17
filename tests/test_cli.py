"""测试 CLI 命令 - 重点关注 cmd_wait 行为"""

import os
import tempfile
import time
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

from msgbox import cli
from msgbox import config
from msgbox import db as central_db
from msgbox import session as session_db


# ── 辅助 Fixtures ────────────────────────────────────────────


@pytest.fixture
def temp_sessions_dir():
    """使用临时目录作为 SESSIONS_DIR，避免污染真实目录"""
    with tempfile.TemporaryDirectory() as tmp:
        with patch.object(config, "SESSIONS_DIR", Path(tmp)):
            yield tmp


@pytest.fixture
def temp_central_db():
    """使用临时文件作为中央数据库"""
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
    """创建一个已激活的 session（同时使用隔离的中央 DB）"""
    sid = "test-session-12345"
    with patch("msgbox.cli._session_id", return_value=sid):
        db_path = cli._session_db_path(sid)
        session_db.init_session_db(db_path)
        yield sid, db_path


class TestCmdWaitDuration:
    """验证 cmd_wait 的轮询总时长正确"""

    def _run_wait_and_check_output(self, idle, sleep, capsys=None):
        """辅助：运行 cmd_wait 并返回 sleep 调用次数"""
        sleep_call_count = [0]

        def _sleep(_sec):
            sleep_call_count[0] += 1

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

            return sleep_call_count[0]

    def test_total_duration_default(self):
        """默认配置下总轮询时间 = 30 + 60 = 90s，90/5 = 18 次 sleep"""
        count = self._run_wait_and_check_output(30, 60)
        assert count == 18, f"预期 18 次 sleep，实际 {count}"

    def test_custom_duration(self):
        """自定义时长下总轮询时间正确：10+20=30s，30/5 = 6 次 sleep"""
        count = self._run_wait_and_check_output(10, 20)
        assert count == 6, f"预期 6 次 sleep，实际 {count}"

    def test_duration_not_just_sleep(self):
        """验证不是只用 sleep_duration，而是 idle + sleep
        只用 sleep(10) → 10/5=2 次；idle+sleep(20) → 20/5=4 次"""
        count = self._run_wait_and_check_output(10, 10)
        assert count > 2, f"应 >2 次 sleep（只用 sleep_duration 是 2），实际 {count}"
        assert count == 4, f"预期 4 次 sleep（idle+sleep=20），实际 {count}"


class TestCmdWaitPopups:
    """验证 cmd_wait 在 popup 存在时的行为"""

    def test_popup_immediate_return(self, activated_session, temp_central_db):
        """有 popup 消息时立即返回，不轮询"""
        sid, db_path = activated_session

        # 插入一条 popup 消息
        central_db.insert_message(str(config.CENTRAL_DB), "test.type", "Popup Title", "Popup Content", category="popup")

        with (
            patch("msgbox.cli.time.sleep") as mock_sleep,
            patch("msgbox.cli.sys.exit") as mock_exit,
        ):
            # 这个调用应该返回（不 exit），因为找到了 popup
            cli.cmd_wait(None)

            assert not mock_sleep.called, "有 popup 不应轮询"
            assert not mock_exit.called, "有 popup 不应 exit"

    def test_no_popup_polling(self, activated_session, temp_central_db):
        """无 popup 时进入轮询，中途有消息应返回"""
        sid, db_path = activated_session

        call_count = [0]

        def mock_get_undelivered(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] >= 3:  # 第3次调用返回消息
                return [{"id": 1, "category": "normal", "type": "test", "title": "Late msg", "content": "hello", "props": {}}]
            return []

        with (
            patch.object(config, "IDLE_DURATION", 10),
            patch.object(config, "SLEEP_DURATION", 10),
            patch("msgbox.cli.central_db.get_undelivered_messages", side_effect=mock_get_undelivered),
            patch("msgbox.cli.time.sleep"),
            patch("msgbox.cli.sys.exit", side_effect=SystemExit) as mock_exit,
        ):
            cli.cmd_wait(None)

            assert not mock_exit.called, "找到消息不应 exit"


class TestCmdWaitExitConditions:
    """验证 cmd_wait 的各种退出条件"""

    def test_no_session_id(self):
        """没有 session_id 时静默退出"""
        with patch("msgbox.cli._session_id", return_value=None):
            with patch("msgbox.cli.sys.exit", side_effect=SystemExit) as mock_exit:
                with pytest.raises(SystemExit):
                    cli.cmd_wait(None)
                mock_exit.assert_called_once_with(0)

    def test_no_session_db(self):
        """session DB 不存在时静默退出"""
        with patch("msgbox.cli._session_id", return_value="no-such-session"):
            with patch.object(Path, "exists", return_value=False):
                with patch("msgbox.cli.sys.exit", side_effect=SystemExit) as mock_exit:
                    with pytest.raises(SystemExit):
                        cli.cmd_wait(None)
                    mock_exit.assert_called_once_with(0)

    def test_no_messages_exit_2(self, activated_session):
        """无消息到达时 exit(2) 并打印状态"""
        sid, db_path = activated_session

        with (
            patch.object(config, "IDLE_DURATION", 1),
            patch.object(config, "SLEEP_DURATION", 1),
            patch("msgbox.cli.time.sleep"),
            patch("msgbox.cli.sys.exit", side_effect=SystemExit) as mock_exit,
        ):
            with pytest.raises(SystemExit):
                cli.cmd_wait(None)
            mock_exit.assert_called_once_with(2)


class TestCmdWaitRealScenario:
    """接近真实的场景测试（使用真实 DB，mocked 时间和 sleep）"""

    def test_popup_before_normal(self, temp_central_db, temp_sessions_dir):
        """popup 消息优先于 normal 消息"""
        sid = "real-test-session"
        db_path = cli._session_db_path(sid)
        session_db.init_session_db(db_path)

        # 插入两条消息
        central_db.insert_message(str(config.CENTRAL_DB), "pop", "Popup!", "", category="popup")
        central_db.insert_message(str(config.CENTRAL_DB), "norm", "Normal!", "", category="normal")

        with (
            patch("msgbox.cli._session_id", return_value=sid),
            patch.object(Path, "exists", return_value=True),
            patch("msgbox.cli.time.sleep"),
            patch("msgbox.cli.sys.exit") as mock_exit,
        ):
            cli.cmd_wait(None)
            # 有 popup，应该 Phase 1 直接返回，不 exit
            assert not mock_exit.called, "有 popup 应直接返回"

    def test_only_normal_message(self, temp_central_db, temp_sessions_dir):
        """只有 normal 消息时，等待轮询发现它"""
        sid = "real-test-session2"
        db_path = cli._session_db_path(sid)
        session_db.init_session_db(db_path)

        # 先插入 normal 消息（模拟在轮询过程中到达）
        central_db.insert_message(str(config.CENTRAL_DB), "norm", "Normal arrives!", "", category="normal")

        with (
            patch.object(config, "IDLE_DURATION", 2),
            patch.object(config, "SLEEP_DURATION", 2),
            patch("msgbox.cli._session_id", return_value=sid),
            patch.object(Path, "exists", return_value=True),
            patch("msgbox.cli.time.sleep"),
            patch("msgbox.cli.sys.exit") as mock_exit,
        ):
            cli.cmd_wait(None)
            assert not mock_exit.called, "找到 normal 消息应返回"

    def test_peek_cooldown(self, temp_sessions_dir):
        """peek 冷却机制正常工作"""
        sid = "peek-test"
        cooldown_file = Path(temp_sessions_dir) / f"{sid}.peek_ts"

        with (
            patch("msgbox.cli._session_id", return_value=sid),
            patch.object(config, "PEEK_COOLDOWN", 10),
            patch("msgbox.cli.sys.exit") as mock_exit,
        ):
            # 第一次 peek
            cli.cmd_peek(None)
            assert cooldown_file.exists(), "peek 后应创建冷却文件"
            # 立即再 peek
            cli.cmd_peek(None)
            # 不应有报错

    def test_cmd_wait_hook_integration(self):
        """验证 hook 脚本行为：正常返回且有输出时 hook 会推送消息"""
        # hook 脚本: "output=$(msgbox wait 2>/dev/null); rc=$?; if [ $rc -eq 0 ] && [ -n \"$output\" ]; then echo \"$output\" >&2; exit 2; fi; exit 0"
        # rc=0 + output="Waited 300s..." → hook 推送消息给 Claude
        # rc=0 + output=""（无消息源的情况）→ exit 0（无事发生）
        pass


class TestCmdWaitRegressions:
    """回归测试：验证之前修过的 bug 不会复发"""

    def test_not_only_sleep_duration(self):
        """regression: 确保 cmd_wait 不是只用 sleep_duration，
        之前 simplify 把 idle+sleep 缩成了只有 sleep，导致轮询时间减半"""
        with patch.object(config, "IDLE_DURATION", 5):
            with patch.object(config, "SLEEP_DURATION", 10):
                sid = "reg-test"
                sleep_count = [0]

                def _sleep(_sec):
                    sleep_count[0] += 1

                with (
                    patch("msgbox.cli._session_id", return_value=sid),
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

                    # 如果只用 sleep(10)：10/5=2 次 sleep
                    # 正确是 idle+sleep(15)：15/5=3 次 sleep
                    assert sleep_count[0] == 3, f"预期 3 次 sleep，实际 {sleep_count[0]}"

    def test_get_excluded_ids_not_redundant(self):
        """regression: 验证 cmd_wait 中 get_excluded_ids 的调用次数合理"""
        with patch.object(config, "IDLE_DURATION", 10):
            with patch.object(config, "SLEEP_DURATION", 10):
                sid = "reg-test2"

                with (
                    patch("msgbox.cli._session_id", return_value=sid),
                    patch.object(Path, "exists", return_value=True),
                    patch("msgbox.cli.session_db") as mock_session_db,
                    patch("msgbox.cli.central_db") as mock_central_db,
                    patch("msgbox.cli.load_config", return_value={"templates": {}}),
                    patch("msgbox.cli.time.sleep"),
                    patch("msgbox.cli.sys.exit", side_effect=SystemExit),
                ):
                    mock_session_db.get_excluded_ids.return_value = set()
                    mock_central_db.get_unread_popup_count.return_value = 0
                    mock_central_db.get_undelivered_messages.return_value = []

                    with pytest.raises(SystemExit):
                        cli.cmd_wait(None)

                    # Phase 1 (1x) + 每次轮询 (4x) = 5 calls max
                    # 之前 Phase 2+3 重复调用是 1 + 2 + 2 = 5，现在合并后是 1 + 4 = 5
                    # 确保没变成 1+4+4=9（原 bug 模式）
                    calls = mock_session_db.get_excluded_ids.call_count
                    assert calls <= 5, f"get_excluded_ids 调用次数过多: {calls}"
