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
        tasks = json.loads(Path(temp_bg_file).read_text())
        assert len(tasks) == 1
        task = list(tasks.values())[0]
        assert "pip install flask" in task["command"]

    def test_multiple_tasks(self, temp_bg_file):
        check_bash.track_background("task1")
        check_bash.track_background("task2")
        tasks = json.loads(Path(temp_bg_file).read_text())
        assert len(tasks) == 2

    def test_empty(self, temp_bg_file):
        check_bash.track_background("new task")
        tasks = json.loads(Path(temp_bg_file).read_text())
        assert len(tasks) == 1

    def test_corrupted(self, temp_bg_file):
        Path(temp_bg_file).write_text("{invalid json}")
        check_bash.track_background("new task")
        tasks = json.loads(Path(temp_bg_file).read_text())
        assert len(tasks) == 1


class TestCheckBackgroundTasks:
    @pytest.fixture
    def old_tasks(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        with patch.object(check_bash, "BG_TRACKING_FILE", path):
            tasks = {
                "1": {"command": "pip install", "started_at": time.time() - 660, "notified": False}
            }
            Path(path).write_text(json.dumps(tasks))
            yield path
        if os.path.exists(path):
            os.unlink(path)

    def test_notify_stale(self, old_tasks):
        with pytest.raises(SystemExit):
            check_bash.check_background_tasks()

    def test_recent_no_notify(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        with patch.object(check_bash, "BG_TRACKING_FILE", path):
            tasks = {
                "1": {"command": "pip install", "started_at": time.time(), "notified": False}
            }
            Path(path).write_text(json.dumps(tasks))
            check_bash.check_background_tasks()
        if os.path.exists(path):
            os.unlink(path)


class TestHookMain:
    def _run(self, data: dict) -> tuple[str, str]:
        """运行 main()，返回 (stdout, stderr)。"""
        old_stdin = sys.stdin
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        sys.stdin = StringIO(json.dumps(data))
        stdout_out = StringIO()
        stderr_out = StringIO()
        sys.stdout = stdout_out
        sys.stderr = stderr_out
        with (
            patch.object(check_bash, "BG_TRACKING_FILE", "/tmp/nonexistent-bg-test.json"),
            patch.object(check_bash, "check_background_tasks"),
        ):
            try:
                check_bash.main()
            except SystemExit:
                pass
        sys.stdin = old_stdin
        sys.stdout = old_stdout
        sys.stderr = old_stderr
        return stdout_out.getvalue(), stderr_out.getvalue()

    def test_non_bash_passthrough(self):
        stdout, stderr = self._run({"tool_name": "Write", "tool_input": {}})
        assert stdout == ""
        assert stderr == ""

    def test_background_passthrough(self):
        stdout, stderr = self._run({
            "tool_name": "Bash",
            "tool_input": {"command": "sleep 30", "run_in_background": True}
        })
        assert stdout == ""
        assert stderr == ""

    def test_safe_command_passthrough(self):
        stdout, stderr = self._run({
            "tool_name": "Bash",
            "tool_input": {"command": "ls -la"}
        })
        assert stdout == ""
        assert stderr == ""

    def test_existing_timeout_passthrough(self):
        """已有 timeout 的命令应直接放行"""
        stdout, stderr = self._run({
            "tool_name": "Bash",
            "tool_input": {"command": "pip install flask", "timeout": 30000}
        })
        assert stdout == ""
        assert stderr == ""

    def test_no_timeout_adds_timeout(self):
        """没有 timeout 的命令应自动加 60s timeout"""
        stdout, stderr = self._run({
            "tool_name": "Bash",
            "tool_input": {"command": "pip install flask"}
        })
        result = json.loads(stdout)
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
        assert result["hookSpecificOutput"]["updatedInput"]["timeout"] == 60000
        assert result["hookSpecificOutput"]["updatedInput"]["command"] == "pip install flask"

    def test_invalid_json_passthrough(self):
        old_stdin = sys.stdin
        sys.stdin = StringIO("not json")
        with (
            patch.object(check_bash, "BG_TRACKING_FILE", "/tmp/nonexistent-bg-test.json"),
            patch.object(check_bash, "check_background_tasks"),
        ):
            try:
                check_bash.main()
            except SystemExit:
                pass
        sys.stdin = old_stdin
