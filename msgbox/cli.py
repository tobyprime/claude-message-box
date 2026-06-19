"""CLI 入口 - msgbox 命令行工具"""

import argparse
import io
import sys

# 强制 stdout/stderr 使用 UTF-8 编码，避免中文乱码
if sys.stdout.encoding and sys.stdout.encoding.upper() not in ("UTF-8", "UTF8"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
if sys.stderr.encoding and sys.stderr.encoding.upper() not in ("UTF-8", "UTF8"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

from . import config
from .commands.session import (
    cmd_start, cmd_stop, cmd_list_sessions,
    _session_id, _is_main_agent, _session_db_path, _require_session_db, _read_hook_input,
)
from .commands.message import cmd_wait, cmd_peek, cmd_send, cmd_close, cmd_mark_done, cmd_history, cmd_debug_hook
from .commands.todo import (
    cmd_todo_add, cmd_todo_list, cmd_todo_tree, cmd_todo_start, cmd_todo_done,
    cmd_todo_cancel, cmd_todo_delete, cmd_todo_wait_time, cmd_todo_active,
)
from .commands.config import cmd_config_get, cmd_config_set, cmd_config_rules, cmd_config_rules_add, cmd_config_rules_remove
from .commands.source import (
    cmd_source_github, cmd_source_inbox, cmd_source_dingtalk,
    cmd_subscribe, cmd_unsubscribe, cmd_subscriptions,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="msgbox", description="Claude Message Box")
    sub = p.add_subparsers(dest="command")

    sp = sub.add_parser("start", help="Activate message box for current session")
    sp.set_defaults(func=cmd_start)

    sp = sub.add_parser("stop", help="Deactivate message box")
    sp.set_defaults(func=cmd_stop)

    sp = sub.add_parser("send", help="Send a message")
    sp.add_argument("--type", "-t", required=True)
    sp.add_argument("--title", default="")
    sp.add_argument("--content", default="")
    sp.add_argument("--props", default="{}")
    sp.add_argument("--category", choices=["popup", "normal", "silent", ""], default="")
    sp.set_defaults(func=cmd_send)

    sp = sub.add_parser("wait", help="Wait for messages (hook use)")
    sp.set_defaults(func=cmd_wait)

    sp = sub.add_parser("peek", help="Quick peek for new messages (hook use)")
    sp.set_defaults(func=cmd_peek)

    sp = sub.add_parser("debug-hook", help="Debug hook stdin/env (test only)")
    sp.set_defaults(func=cmd_debug_hook)

    sp = sub.add_parser("close", help="Close popup messages so they stop appearing")
    sp.add_argument("--ids", help="Comma-separated message IDs to close")
    sp.set_defaults(func=cmd_close)

    sp = sub.add_parser("mark-done", help="Mark popup messages as processed")
    sp.add_argument("--ids", help="Comma-separated message IDs")
    sp.add_argument("--all", action="store_true", help="Mark all delivered messages as done")
    sp.set_defaults(func=cmd_mark_done)

    sp = sub.add_parser("source-github", help="Start GitHub webhook listener")
    sp.add_argument("--port", "-p", type=int, help="HTTP listen port (default: 3001)")
    sp.add_argument("--smee-url", help="Smee.io proxy URL")
    sp.add_argument("--repos", nargs="*", help="Repo allowlist (e.g. owner/repo)")
    sp.add_argument("--events", nargs="*", help="Event types to accept (e.g. push issues)")
    sp.add_argument("--foreground", "-f", action="store_true", help="Run in foreground (default: daemon)")
    sp.set_defaults(func=cmd_source_github)

    sp = sub.add_parser("source-inbox", help="Start GitHub inbox notification poller")
    sp.add_argument("--interval", "-i", type=int, default=30, help="Poll interval in seconds (default: 30)")
    sp.add_argument("--foreground", "-f", action="store_true", help="Run in foreground (default: daemon)")
    sp.set_defaults(func=cmd_source_inbox)

    sp = sub.add_parser("source-dingtalk", help="Start DingTalk notification poller (dws CLI)")
    sp.add_argument("--interval", "-i", type=int, default=0, help="Poll interval in seconds (default: DINGTALK_POLL_INTERVAL env or 15)")
    sp.add_argument("--foreground", "-f", action="store_true", help="Run in foreground")
    sp.set_defaults(func=cmd_source_dingtalk)

    sp = sub.add_parser("subscribe", help="Subscribe to thread notifications")
    sp.add_argument("thread_type", choices=["discussion", "issue", "pr"])
    sp.add_argument("number", type=int, help="Thread number")
    sp.add_argument("--popup", action="store_true", help="Show as popup (default: normal)")
    sp.set_defaults(func=cmd_subscribe)

    sp = sub.add_parser("unsubscribe", help="Unsubscribe from thread notifications")
    sp.add_argument("thread_type", choices=["discussion", "issue", "pr"])
    sp.add_argument("number", type=int, help="Thread number")
    sp.set_defaults(func=cmd_unsubscribe)

    sp = sub.add_parser("subscriptions", help="List active subscriptions")
    sp.set_defaults(func=cmd_subscriptions)

    sp = sub.add_parser("history", help="Browse historical messages")
    sp.add_argument("--limit", "-n", type=int, default=20, help="Number of messages (default: 20)")
    sp.add_argument("--offset", "-o", type=int, default=0, help="Start offset")
    sp.add_argument("--category", "-c", nargs="*", choices=["popup", "normal", "silent"], help="Filter by category")
    sp.add_argument("--type", "-t", help="Filter by type pattern (e.g. github.issue)")
    sp.set_defaults(func=cmd_history)

    cp = sub.add_parser("config", help="Manage configuration")
    csub = cp.add_subparsers(dest="config_cmd")

    sp = csub.add_parser("get", help="Get config value")
    sp.add_argument("key")
    sp.set_defaults(func=cmd_config_get)

    sp = csub.add_parser("set", help="Set config value")
    sp.add_argument("key")
    sp.add_argument("value")
    sp.set_defaults(func=cmd_config_set)

    sp = csub.add_parser("rules", help="List rules")
    sp.set_defaults(func=cmd_config_rules)

    sp = csub.add_parser("add-rule", help="Add filter rule")
    sp.add_argument("rule_type", choices=["popup", "popup_excluded", "silent", "silent_excluded"])
    sp.add_argument("pattern", help="Regex pattern for message type")
    sp.add_argument("--props", help='JSON props filters, e.g. \'{"repo":"my-project"}\'')
    sp.set_defaults(func=cmd_config_rules_add)

    sp = csub.add_parser("remove-rule", help="Remove filter rule by index")
    sp.add_argument("rule_type", choices=["popup", "popup_excluded", "silent", "silent_excluded"])
    sp.add_argument("index", type=int)
    sp.set_defaults(func=cmd_config_rules_remove)

    sp = sub.add_parser("list-sessions", help="List active sessions")
    sp.set_defaults(func=cmd_list_sessions)

    # todo 子命令
    tp = sub.add_parser("todo", help="Manage session todos")
    tsub = tp.add_subparsers(dest="todo_cmd")

    sp = tsub.add_parser("add", help="Add a todo")
    sp.add_argument("--title", required=True)
    sp.add_argument("--approach", default="")
    sp.add_argument("--duration", required=True, help="Duration like 30s, 5m, 2h")
    sp.add_argument("--wait-time", default="", help="Wait time like 30s, 5m; 0 disables resume")
    sp.add_argument("--parent", type=int, help="Parent todo id")
    sp.add_argument("--after", type=int, help="Dependency todo id (must finish before this)")
    sp.add_argument("--insert-before", type=int, help="Sibling todo id to insert before (requires --parent)")
    sp.add_argument("--insert-after", type=int, help="Sibling todo id to insert after (requires --parent)")
    sp.set_defaults(func=cmd_todo_add)

    sp = tsub.add_parser("list", help="List todos")
    sp.add_argument("--status", choices=["pending", "active", "paused", "done", "cancelled"])
    sp.set_defaults(func=cmd_todo_list)

    sp = tsub.add_parser("tree", help="Show todo tree")
    sp.set_defaults(func=cmd_todo_tree)

    sp = tsub.add_parser("start", help="Activate a todo")
    sp.add_argument("id", type=int)
    sp.set_defaults(func=cmd_todo_start)

    sp = tsub.add_parser("done", help="Mark a todo as done")
    sp.add_argument("id", type=int)
    sp.set_defaults(func=cmd_todo_done)

    sp = tsub.add_parser("cancel", help="Cancel a todo")
    sp.add_argument("id", type=int)
    sp.set_defaults(func=cmd_todo_cancel)

    sp = tsub.add_parser("delete", help="Delete a todo")
    sp.add_argument("id", type=int)
    sp.set_defaults(func=cmd_todo_delete)

    sp = tsub.add_parser("wait-time", help="Set wait-time for a todo")
    sp.add_argument("id", type=int)
    sp.add_argument("duration", help="Wait time like 30s, 5m")
    sp.set_defaults(func=cmd_todo_wait_time)

    sp = tsub.add_parser("active", help="Show current active todos")
    sp.set_defaults(func=cmd_todo_active)

    return p


def main():
    p = build_parser()
    args = p.parse_args()
    if not args.command:
        p.print_help()
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
