#!/usr/bin/env python3
"""PreToolUse hook: 为 Bash 命令自动添加 timeout，防止卡死。

协议：
  输出到 stdout JSON：
    {"hookSpecificOutput": {"permissionDecision": "allow|deny", "updatedInput": {...}}}

逻辑：
1. 非 Bash → 放行
2. 已有 run_in_background → 放行，记录跟踪
3. 已有合理的 timeout → 放行
4. 安全命令 → 放行
5. 否则 → 在 updatedInput 中添加 timeout: 60000（60s），然后放行
   这样如果命令超时，Claude 会看到超时错误，自然知道该用 background
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
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        f"Background task running for over 10 minutes: "
                        f"{task.get('command', 'unknown')[:100]}"
                    ),
                }
            }
            print(json.dumps(result))
            sys.exit(0)


def track_background(command: str):
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

    # 非 Bash → 放行
    if data.get("tool_name") != "Bash":
        sys.exit(0)

    # 已 background → 放行（记录跟踪）
    if run_bg:
        track_background(command)
        sys.exit(0)

    # 已有 timeout → 放行
    if timeout is not None:
        sys.exit(0)

    # 安全命令 → 放行
    if SAFE_PATTERN.match(command.strip()):
        sys.exit(0)

    # 其他命令 → 自动加 timeout
    updated = dict(tool_input)
    updated["timeout"] = 60000
    if "description" not in updated:
        updated["description"] = f"Auto-added 60s timeout to prevent blocking"

    result = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": "Added 60s timeout to prevent blocking. If it times out, retry with run_in_background: true.",
            "updatedInput": updated,
        }
    }
    print(json.dumps(result))
    sys.exit(0)


if __name__ == "__main__":
    main()
