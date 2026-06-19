"""测试 Todo 模块"""

import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from msgbox import config
from msgbox import db as central_db
from msgbox import session as session_db
from msgbox import todo as todo_logic


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
def db(temp_sessions_dir, temp_central_db):
    """返回一个已初始化的 session db_path。"""
    sid = "todo-test-session"
    db_path = str(config.SESSIONS_DIR / f"{sid}.session.db")
    session_db.init_session_db(db_path)
    yield db_path
    if Path(db_path).exists():
        Path(db_path).unlink()


def _activate_at(db_path: int, todo_id: int, ago_seconds: int):
    """辅助：将任务激活时间设为 ago_seconds 前。"""
    activated_at = (datetime.now(timezone.utc) - timedelta(seconds=ago_seconds)).isoformat()
    session_db.update_todo(db_path, todo_id, {"status": "active", "activated_at": activated_at})


class TestParseDuration:
    def test_seconds(self):
        assert todo_logic.parse_duration("30s") == 30

    def test_minutes(self):
        assert todo_logic.parse_duration("5m") == 300

    def test_hours(self):
        assert todo_logic.parse_duration("2h") == 7200

    def test_decimal(self):
        assert todo_logic.parse_duration("1.5h") == 5400

    def test_raw_int(self):
        assert todo_logic.parse_duration("120") == 120

    def test_invalid(self):
        with pytest.raises(ValueError):
            todo_logic.parse_duration("abc")


class TestTodoCRUD:
    def test_add_and_get(self, db):
        tid = todo_logic.add_todo(db, "A", "do A", "10m")
        todo = todo_logic.get_todo(db, tid)
        assert todo["title"] == "A"
        assert todo["duration_s"] == 600
        assert todo["status"] == "pending"

    def test_add_with_parent_and_dependency(self, db):
        a = todo_logic.add_todo(db, "A", "", "5m")
        b = todo_logic.add_todo(db, "B", "", "5m", parent_id=a)
        c = todo_logic.add_todo(db, "C", "", "5m", dependency_id=a)
        assert todo_logic.get_todo(db, b)["parent_id"] == a
        assert todo_logic.get_todo(db, c)["dependency_id"] == a

    def test_list_by_status(self, db):
        t1 = todo_logic.add_todo(db, "T1", "", "5m")
        t2 = todo_logic.add_todo(db, "T2", "", "5m")
        todo_logic.activate_todo(db, t1)
        pending = todo_logic.list_todos(db, status="pending")
        active = todo_logic.list_todos(db, status="active")
        assert len(pending) == 1
        assert len(active) == 1

    def test_delete_cascades_children(self, db):
        a = todo_logic.add_todo(db, "A", "", "5m")
        b = todo_logic.add_todo(db, "B", "", "5m", parent_id=a)
        todo_logic.delete_todo(db, a)
        assert todo_logic.get_todo(db, a) is None
        assert todo_logic.get_todo(db, b) is None


class TestSiblingOrder:
    def test_default_order_append_last(self, db):
        a = todo_logic.add_todo(db, "A", "", "5m")
        b = todo_logic.add_todo(db, "B", "", "5m", parent_id=a)
        c = todo_logic.add_todo(db, "C", "", "5m", parent_id=a)
        children = session_db.get_child_todos(db, a)
        assert [c["title"] for c in children] == ["B", "C"]

    def test_insert_after(self, db):
        a = todo_logic.add_todo(db, "A", "", "5m")
        b = todo_logic.add_todo(db, "B", "", "5m", parent_id=a)
        c = todo_logic.add_todo(db, "C", "", "5m", parent_id=a, insert_after=b)
        children = session_db.get_child_todos(db, a)
        assert [c["title"] for c in children] == ["B", "C"]

    def test_insert_before(self, db):
        a = todo_logic.add_todo(db, "A", "", "5m")
        b = todo_logic.add_todo(db, "B", "", "5m", parent_id=a)
        c = todo_logic.add_todo(db, "C", "", "5m", parent_id=a, insert_before=b)
        children = session_db.get_child_todos(db, a)
        assert [c["title"] for c in children] == ["C", "B"]

    def test_insert_between(self, db):
        a = todo_logic.add_todo(db, "A", "", "5m")
        b = todo_logic.add_todo(db, "B", "", "5m", parent_id=a)
        d = todo_logic.add_todo(db, "D", "", "5m", parent_id=a)
        c = todo_logic.add_todo(db, "C", "", "5m", parent_id=a, insert_after=b)
        children = session_db.get_child_todos(db, a)
        assert [c["title"] for c in children] == ["B", "C", "D"]

    def test_insert_requires_same_parent(self, db):
        a = todo_logic.add_todo(db, "A", "", "5m")
        b = todo_logic.add_todo(db, "B", "", "5m")
        with pytest.raises(ValueError):
            todo_logic.add_todo(db, "C", "", "5m", parent_id=a, insert_before=b)


class TestActivate:
    def test_activate_pending(self, db):
        tid = todo_logic.add_todo(db, "A", "", "5m")
        result = todo_logic.activate_todo(db, tid)
        assert result["ok"]
        assert result["todo"]["status"] == "active"
        assert result["todo"]["activated_at"]

    def test_activate_dependency_not_done(self, db):
        a = todo_logic.add_todo(db, "A", "", "5m")
        b = todo_logic.add_todo(db, "B", "", "5m", dependency_id=a)
        result = todo_logic.activate_todo(db, b)
        assert not result["ok"]
        assert "not done" in result["error"]

    def test_activate_dependency_done(self, db):
        a = todo_logic.add_todo(db, "A", "", "5m")
        b = todo_logic.add_todo(db, "B", "", "5m", dependency_id=a)
        todo_logic.activate_todo(db, a)
        todo_logic.mark_todo_done(db, a)
        result = todo_logic.activate_todo(db, b)
        assert result["ok"]

    def test_parent_auto_done_when_active(self, db):
        a = todo_logic.add_todo(db, "A", "", "5m")
        b = todo_logic.add_todo(db, "B", "", "5m", parent_id=a)
        todo_logic.activate_todo(db, a)
        todo_logic.activate_todo(db, b)
        todo_logic.mark_todo_done(db, b)
        assert todo_logic.get_todo(db, a)["status"] == "done"

    def test_parent_not_auto_done_when_pending(self, db):
        a = todo_logic.add_todo(db, "A", "", "5m")
        b = todo_logic.add_todo(db, "B", "", "5m", parent_id=a)
        todo_logic.activate_todo(db, b)
        todo_logic.mark_todo_done(db, b)
        assert todo_logic.get_todo(db, a)["status"] == "pending"

    def test_done_requires_active(self, db):
        tid = todo_logic.add_todo(db, "A", "", "5m")
        result = todo_logic.mark_todo_done(db, tid)
        assert not result["ok"]
        assert "not active" in result["error"]
        assert todo_logic.get_todo(db, tid)["status"] == "pending"


class TestReminders:
    def test_half_reminder(self, db):
        tid = todo_logic.add_todo(db, "A", "", "10m")
        _activate_at(db, tid, 5 * 60 + 1)  # 刚过 50%
        emitted = todo_logic.check_and_emit_reminders(db)
        assert len(emitted) == 1
        assert emitted[0]["props"]["reminder_stage"] == "half"

    def test_three_quarter_reminder(self, db):
        tid = todo_logic.add_todo(db, "A", "", "10m")
        _activate_at(db, tid, 8 * 60)  # 80%
        emitted = todo_logic.check_and_emit_reminders(db)
        stages = [e["props"]["reminder_stage"] for e in emitted]
        assert "three_quarter" in stages

    def test_due_reminder(self, db):
        tid = todo_logic.add_todo(db, "A", "", "10m")
        _activate_at(db, tid, 10 * 60 + 1)
        emitted = todo_logic.check_and_emit_reminders(db)
        stages = {e["props"]["reminder_stage"] for e in emitted}
        assert stages == {"due"}

    def test_reminder_deduplication(self, db):
        tid = todo_logic.add_todo(db, "A", "", "10m")
        _activate_at(db, tid, 10 * 60 + 1)
        todo_logic.check_and_emit_reminders(db)
        emitted = todo_logic.check_and_emit_reminders(db)
        assert len(emitted) == 0

    def test_done_no_reminder(self, db):
        tid = todo_logic.add_todo(db, "A", "", "10m")
        _activate_at(db, tid, 10 * 60 + 1)
        todo_logic.mark_todo_done(db, tid)
        emitted = todo_logic.check_and_emit_reminders(db)
        assert len(emitted) == 0


class TestCurrentTasks:
    def test_current_task_is_self(self, db):
        tid = todo_logic.add_todo(db, "A", "", "5m")
        todo_logic.activate_todo(db, tid)
        tasks = todo_logic.get_current_tasks(db)
        assert len(tasks) == 1
        assert tasks[0]["id"] == tid

    def test_current_task_is_unfinished_child(self, db):
        a = todo_logic.add_todo(db, "A", "", "5m")
        b = todo_logic.add_todo(db, "B", "", "5m", parent_id=a)
        todo_logic.activate_todo(db, a)
        tasks = todo_logic.get_current_tasks(db)
        assert len(tasks) == 1
        assert tasks[0]["id"] == b

    def test_current_task_when_children_done(self, db):
        a = todo_logic.add_todo(db, "A", "", "5m")
        b = todo_logic.add_todo(db, "B", "", "5m", parent_id=a)
        todo_logic.activate_todo(db, a)
        todo_logic.activate_todo(db, b)
        todo_logic.mark_todo_done(db, b)
        # 子任务完成后父任务会被自动标记为 done，因此当前无 active 任务
        tasks = todo_logic.get_current_tasks(db)
        assert len(tasks) == 0


class TestResume:
    def test_resume_when_wait_exceeds_wait_time(self, db):
        tid = todo_logic.add_todo(db, "A", "", "5m", wait_time="10s")
        todo_logic.activate_todo(db, tid)
        tasks = todo_logic.check_resume_needed(db, 15)
        assert len(tasks) == 1

    def test_no_resume_when_wait_below_wait_time(self, db):
        tid = todo_logic.add_todo(db, "A", "", "5m", wait_time="10s")
        todo_logic.activate_todo(db, tid)
        tasks = todo_logic.check_resume_needed(db, 5)
        assert len(tasks) == 0

    def test_no_resume_when_wait_time_zero(self, db):
        tid = todo_logic.add_todo(db, "A", "", "5m")
        todo_logic.activate_todo(db, tid)
        tasks = todo_logic.check_resume_needed(db, 9999)
        assert len(tasks) == 0

    def test_emit_resume_message(self, db):
        tid = todo_logic.add_todo(db, "A", "", "5m", wait_time="10s")
        todo_logic.activate_todo(db, tid)
        tasks = todo_logic.check_resume_needed(db, 15)
        msg_id = todo_logic.emit_resume_message(db, tasks)
        assert msg_id is not None
        msg = central_db.get_messages_by_ids(config.CENTRAL_DB, [msg_id])[0]
        assert msg["type"] == "todo.session_resume"
        assert "继续任务" in msg["title"]


class TestTree:
    def test_build_tree(self, db):
        a = todo_logic.add_todo(db, "A", "", "5m")
        b = todo_logic.add_todo(db, "B", "", "5m", parent_id=a)
        c = todo_logic.add_todo(db, "C", "", "5m", parent_id=a)
        todos = todo_logic.list_todos(db)
        tree = todo_logic.build_todo_tree(todos)
        assert len(tree) == 1
        assert len(tree[0]["children"]) == 2

    def test_format_tree(self, db):
        a = todo_logic.add_todo(db, "A", "", "5m")
        todo_logic.add_todo(db, "B", "", "5m", parent_id=a)
        todos = todo_logic.list_todos(db)
        tree = todo_logic.build_todo_tree(todos)
        lines = todo_logic.format_todo_tree(tree)
        assert any("A" in line for line in lines)
        assert any("B" in line for line in lines)
