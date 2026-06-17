#!/usr/bin/env python3
"""PreToolUse hook: 接管 Bash 命令执行，带超时检测。

逻辑：
1. 非 Bash / 已 background → exit 0 放行
2. 安全命令（快速、只读）→ exit 0 放行
3. 否则 → hook 自己执行命令，设超时（工具 timeout 或默认 60s）
   - 命令在超时内完成 → exit 0 放行（Claude 也会执行一次，但无害）
   - 超时 → kill 进程，exit 2 提示转 background
4. 后台任务跟踪，超过 10 分钟发通知
"""

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time

BG_TRACKING_FILE = os.path.expanduser(
    "~/.claude/plugins/message-box/background_tasks.json"
)

# 快速命令前缀（直接放行，不经过 hook 执行）
SAFE_PATTERN = re.compile(
    r'^(ls\b|pwd\b|echo\b|date\b|whoami\b|which\b|cat\b|head\b|tail\b|grep\b|find\b|'
    r'cd\b|mkdir\b|cp\b|mv\b|rm\b|diff\b|cmp\b|stat\b|du\b|df\b|wc\b|sort\b|uniq\b|'
    r'cut\b|tr\b|sed\b|awk\b|jq\b|git status|git diff|git log|git show|git stash|'
    r'pip show|pip list|'
    r'python3 -c\b|python3 -m pytest|msgbox\b)'
)


def check_background_tasks():
    """检查后台任务，超过 10 分钟未完成的发通知。"""
    if not os.path.exists(BG_TRACKING_FILE):
        return
    try:
        with open(BG_TRACKING_FILE) as f:
            tasks = json.load(f)
    except (json.JSONDecodeError, OSError):
        return

    now = time.time()
    stale_notified = []
    remaining = {}

    for key, task in tasks.items():
        elapsed = now - task.get("started_at", 0)
        if elapsed > 600 and not task.get("notified", False):
            task["notified"] = True
            stale_notified.append(task)
        if elapsed <= 3600:  # 清理超过 1 小时的记录
            remaining[key] = task

    with open(BG_TRACKING_FILE, "w") as f:
        json.dump(remaining, f)

    if stale_notified:
        for task in stale_notified:
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
    """记录 background 任务用于超时监控。"""
    tasks = {}
    if os.path.exists(BG_TRACKING_FILE):
        try:
            with open(BG_TRACKING_FILE) as f:
                tasks = json.load(f)
        except (json.JSONDecodeError, OSError):
            tasks = {}
    key = str(int(time.time() * 1000))
    while key in tasks:
        key += "_"
    tasks[key] = {
        "command": command[:200],
        "started_at": time.time(),
        "notified": False,
    }
    os.makedirs(os.path.dirname(BG_TRACKING_FILE), exist_ok=True)
    with open(BG_TRACKING_FILE, "w") as f:
        json.dump(tasks, f)


def execute_with_timeout(command: str, timeout_ms: int) -> int:
    """执行命令，超时则 kill。返回 -1 表示超时，否则返回 exit code。"""
    proc = subprocess.Popen(
        ["bash", "-c", command],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=lambda: signal.signal(signal.SIGTERM, lambda *_: os._exit(1)),
    )

    timed_out = [False]

    def kill_proc():
        timed_out[0] = True
        try:
            proc.kill()
        except OSError:
            pass

    timer = threading.Timer(timeout_ms / 1000.0, kill_proc)
    timer.start()
    proc.wait()
    timer.cancel()

    return -1 if timed_out[0] else proc.returncode


def main():
    # 先检查后台任务
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

    # 1. 非 Bash / 已 background → 放行
    if run_bg:
        track_background(command)
        sys.exit(0)

    # 2. 安全命令 → 直接放行
    if SAFE_PATTERN.match(command.strip()):
        sys.exit(0)

    # 3. 执行命令，检测超时
    timeout_ms = timeout if timeout and isinstance(timeout, (int, float)) and timeout > 0 else 60000
    rc = execute_with_timeout(command, timeout_ms)

    if rc == -1:
        # 超时了 → 拒绝，提示用 background
        result = {
            "decision": "deny",
            "reason": (
                f"Command timed out after {timeout_ms // 1000}s. "
                f"It may take longer than expected. "
                f"Re-run with `run_in_background: true` so it doesn't block."
            ),
        }
        print(json.dumps(result), file=sys.stderr)
        sys.exit(2)

    # 完成了 → 放行（Claude 会再执行一次，但快命令第二次也很快）
    sys.exit(0)


if __name__ == "__main__":
    main()
