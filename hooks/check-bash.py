#!/usr/bin/env python3
"""PreToolUse hook: 检查 Bash 命令是否可能长时间阻塞。

协议：
  exit 0 → 放行（Claude 执行工具）
  exit 2 + stderr JSON → 拒绝，Claude 看到 reason 后调整

逻辑：
1. 非 Bash / 已 background → 放行
2. 有合理 timeout (≤60s) → 放行
3. 安全命令 → 放行
4. 可能长时间的命令 → 拒绝，提示用 background
5. 无 timeout → 拒绝，提示加 timeout 或 background
6. 后台任务 > 10 分钟 → 通知
"""

import json
import os
import re
import sys
import time

BG_TRACKING_FILE = os.path.expanduser(
    "~/.claude/plugins/message-box/background_tasks.json"
)

SAFE_PATTERN = re.compile(
    r'^(ls\b|pwd\b|echo\b|date\b|whoami\b|which\b|cat\b|head\b|tail\b|grep\b|find\b|'
    r'cd\b|mkdir\b|cp\b|mv\b|rm\b|diff\b|cmp\b|stat\b|du\b|df\b|wc\b|sort\b|uniq\b|'
    r'cut\b|tr\b|sed\b|awk\b|jq\b|git status|git diff|git log|git show|git stash|'
    r'pip show|pip list|'
    r'python3 -c\b|python3 -m pytest|msgbox\b)'
)

LONG_PATTERN = re.compile(
    r'(apt-get|apt |pip install|npm install|pnpm install|yarn add|'
    r'docker |docker-compose|kubectl |terraform |ansible |sleep |'
    r'curl\b|wget\b|rsync |scp |git clone|git push|git pull|'
    r'make |cmake |bazel |mvn |gradle |cargo build|cargo test|'
    r'npx |playwright)'
)


def _load_tasks() -> dict:
    if not os.path.exists(BG_TRACKING_FILE):
        return {}
    try:
        with open(BG_TRACKING_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_tasks(tasks: dict):
    os.makedirs(os.path.dirname(BG_TRACKING_FILE), exist_ok=True)
    with open(BG_TRACKING_FILE, "w") as f:
        json.dump(tasks, f)


def check_background_tasks():
    """检查后台任务，超过 10 分钟未完成的发通知。"""
    tasks = _load_tasks()
    now = time.time()
    remaining = {}
    notified = []

    for key, task in tasks.items():
        elapsed = now - task.get("started_at", 0)
        if elapsed > 600 and not task.get("notified", False):
            task["notified"] = True
            notified.append(task)
        if elapsed <= 3600:
            remaining[key] = task

    _save_tasks(remaining)

    if notified:
        for task in notified:
            result = {
                "decision": "deny",
                "reason": (
                    f"Background task running for over 10 minutes: "
                    f"{task.get('command', 'unknown')[:100]}"
                ),
            }
            print(json.dumps(result), file=sys.stderr)
            sys.exit(2)


def track_background(command: str):
    """记录 background 任务。"""
    tasks = _load_tasks()
    key = str(int(time.time() * 1000))
    while key in tasks:
        key += "_"
    tasks[key] = {
        "command": command[:200],
        "started_at": time.time(),
        "notified": False,
    }
    _save_tasks(tasks)


def main():
    check_background_tasks()

    input_data = sys.stdin.read()
    try:
        data = json.loads(input_data)
    except json.JSONDecodeError:
        sys.exit(0)

    tool_input = data.get("tool_input", {})
    command = tool_input.get("command", "")
    timeout = tool_input.get("timeout")
    run_bg = tool_input.get("run_in_background", False)

    if data.get("tool_name") != "Bash":
        sys.exit(0)

    if run_bg:
        track_background(command)
        sys.exit(0)

    if timeout is not None and isinstance(timeout, (int, float)) and 0 < timeout <= 60000:
        sys.exit(0)

    if SAFE_PATTERN.match(command.strip()):
        sys.exit(0)

    if LONG_PATTERN.search(command):
        result = {
            "decision": "deny",
            "reason": "This command may take a long time. Re-run with `run_in_background: true`.",
        }
        print(json.dumps(result), file=sys.stderr)
        sys.exit(2)

    result = {
        "decision": "deny",
        "reason": "No timeout set. Add `timeout: 30000` or use `run_in_background: true`.",
    }
    print(json.dumps(result), file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
