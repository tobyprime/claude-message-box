"""测试 check-bash PreToolUse hook"""

import importlib.util
import json
import os
import sys
import tempfile
import time
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

# 加载 hooks/check-bash.py（带连字符，不能直接 import）
_check_bash_path = os.path.join(os.path.dirname(__file__), "..", "hooks", "check-bash.py")
spec = importlib.util.spec_from_file_location("check_bash", _check_bash_path)
check_bash = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_bash)


class TestSafePattern:
    def test_safe_commands(self):
        safe = ["ls", "ls -la", "pwd", "echo hello", "grep foo bar.txt",
                "git status", "git diff", "git log --oneline", "msgbox history",
                "python3 -c 'print(1)'", "python3 -m pytest tests/", "pip list",
                "cat file.txt", "head -5 file", "tail -f file", "which python3",
                "date", "whoami", "awk '{print $1}' file", "jq '.' file.json"]
        for cmd in safe:
            assert check_bash.SAFE_PATTERN.match(cmd.strip()), f"'{cmd}' should be safe"

    def test_unsafe_commands(self):
        unsafe = ["apt-get install", "pip install flask", "docker run",
                  "git push", "git clone https://...", "sleep 10",
                  "curl https://example.com", "make build", "cargo build"]
        for cmd in unsafe:
            assert not check_bash.SAFE_PATTERN.match(cmd.strip()), f"'{cmd}' should not be safe"


class TestLongPattern:
    def test_long_commands(self):
        long_cmds = ["apt-get install python3", "pip install flask",
                     "npm install express", "docker run nginx",
                     "git push origin main", "git clone https://...",
                     "sleep 30", "curl https://example.com", "make build",
                     "cargo build --release", "npx playwright test"]
        for cmd in long_cmds:
            assert check_bash.LONG_PATTERN.search(cmd), f"'{cmd}' should be long"

    def test_non_long_commands(self):
        normal = ["ls -la", "pwd", "echo hello", "git status", "grep foo"]
        for cmd in normal:
            assert not check_bash.LONG_PATTERN.search(cmd), f"'{cmd}' should not be long"


class TestBackgroundTracking:
    @pytest.fixture
    def temp_bg_file(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        with patch.object(check_bash, "BG_TRACKING_FILE", path):
            yield path
        if os.path.exists(path):
            os.unlink(path)

    def test_track_and_load(self, temp_bg_file):
        check_bash.track_background("pip install flask")
        tasks = check_bash._load_tasks()
        assert len(tasks) == 1
        task = list(tasks.values())[0]
        assert "pip install flask" in task["command"]
        assert task["notified"] is False

    def test_multiple_tasks(self, temp_bg_file):
        check_bash.track_background("task1")
        check_bash.track_background("task2")
        tasks = check_bash._load_tasks()
        assert len(tasks) == 2

    def test_empty(self, temp_bg_file):
        assert check_bash._load_tasks() == {}

    def test_corrupted(self, temp_bg_file):
        Path(temp_bg_file).write_text("{invalid json}")
        assert check_bash._load_tasks() == {}


class TestCheckBackgroundTasks:
    @pytest.fixture
    def old_tasks(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        with patch.object(check_bash, "BG_TRACKING_FILE", path):
            tasks = {
                "1": {"command": "pip install", "started_at": time.time() - 660, "notified": False}
            }
            check_bash._save_tasks(tasks)
            yield path
        if os.path.exists(path):
            os.unlink(path)

    def test_notify_stale(self, old_tasks):
        with patch.object(check_bash, "sys", exit=lambda x: (_ for _ in ()).throw(SystemExit(x))):
            try:
                check_bash.check_background_tasks()
            except SystemExit:
                pass

    def test_recent_no_notify(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        with patch.object(check_bash, "BG_TRACKING_FILE", path):
            tasks = {
                "1": {"command": "pip install", "started_at": time.time(), "notified": False}
            }
            check_bash._save_tasks(tasks)
            check_bash.check_background_tasks()  # should not raise
        if os.path.exists(path):
            os.unlink(path)


class TestHookMain:
    def _run(self, data: dict):
        """运行 main()，返回 stderr 输出。"""
        old_stdin = sys.stdin
        old_stderr = sys.stderr
        sys.stdin = StringIO(json.dumps(data))
        stderr_out = StringIO()
        sys.stderr = stderr_out
        try:
            check_bash.main()
        except SystemExit:
            pass
        sys.stdin = old_stdin
        sys.stderr = old_stderr
        return stderr_out.getvalue()

    def test_non_bash_passthrough(self):
        stderr = self._run({"tool_name": "Write", "tool_input": {}})
        assert stderr == ""

    def test_background_passthrough(self):
        stderr = self._run({
            "tool_name": "Bash",
            "tool_input": {"command": "pip install flask", "run_in_background": True}
        })
        assert stderr == ""

    def test_safe_command_passthrough(self):
        stderr = self._run({
            "tool_name": "Bash",
            "tool_input": {"command": "ls -la"}
        })
        assert stderr == ""

    def test_timeout_passthrough(self):
        stderr = self._run({
            "tool_name": "Bash",
            "tool_input": {"command": "apt-get install", "timeout": 30000}
        })
        assert stderr == ""

    def test_long_command_deny(self):
        stderr = self._run({
            "tool_name": "Bash",
            "tool_input": {"command": "pip install flask"}
        })
        result = json.loads(stderr)
        assert result["decision"] == "deny"
        assert "reason" in result

    def test_unknown_command_no_timeout_deny(self):
        stderr = self._run({
            "tool_name": "Bash",
            "tool_input": {"command": "some_unknown_tool --do-something"}
        })
        result = json.loads(stderr)
        assert result["decision"] == "deny"

    def test_invalid_json_passthrough(self):
        old_stdin = sys.stdin
        sys.stdin = StringIO("not json")
        try:
            check_bash.main()
        except SystemExit:
            pass
        sys.stdin = old_stdin
