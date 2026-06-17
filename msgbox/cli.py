"""CLI 入口 - msgbox 命令行工具"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import config
from . import db as central_db
from . import session as session_db
from .filter import classify_message
from .template import render_brief
from .yaml_config import add_rule, get_config_value, list_rules, load_config, remove_rule, set_config_value


def _session_id() -> str | None:
    """从环境变量获取 Claude Code session_id"""
    return os.environ.get("CLAUDE_CODE_SESSION_ID")


def _session_db_path(session_id: str) -> str:
    config.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    return str(config.SESSIONS_DIR / f"{session_id}.session.db")


# ── msgbox start ────────────────────────────────────────────


def cmd_start(args):
    sid = _session_id()
    if not sid:
        print("CLAUDE_CODE_SESSION_ID not set", file=sys.stderr)
        sys.exit(1)
    db_path = _session_db_path(sid)
    session_db.init_session_db(db_path)
    print(f"msg_box activated: session={sid}")


# ── msgbox stop ─────────────────────────────────────────────


def cmd_stop(args):
    sid = _session_id()
    if not sid:
        print("CLAUDE_CODE_SESSION_ID not set", file=sys.stderr)
        sys.exit(1)
    db_path = _session_db_path(sid)
    if Path(db_path).exists():
        Path(db_path).unlink()
        print("msg_box deactivated")
    else:
        print("msg_box not active", file=sys.stderr)
        sys.exit(1)


# ── msgbox send ─────────────────────────────────────────────


def cmd_send(args):
    props = {}
    if args.props:
        try:
            props = json.loads(args.props)
        except json.JSONDecodeError:
            print("Invalid JSON for --props", file=sys.stderr)
            sys.exit(1)

    category = args.category
    if not category:
        category = classify_message(args.type, props)

    msg_id = central_db.insert_message(
        config.CENTRAL_DB,
        type_=args.type,
        title=args.title,
        content=args.content,
        props=props,
        category=category,
    )
    print(f"Message #{msg_id} stored (category: {category})")


# ── msgbox wait ─────────────────────────────────────────────


def cmd_wait(args):
    sid = _session_id()
    if not sid:
        sys.exit(0)

    db_path = _session_db_path(sid)
    if not Path(db_path).exists():
        sys.exit(0)

    session_db.init_session_db(db_path)
    central_db.init_central_db(config.CENTRAL_DB)

    cfg = load_config()
    templates = cfg.get("templates", {})
    brief_template = templates.get("brief", "")
    item_template = templates.get("item", "")

    excluded_ids = session_db.get_excluded_ids(db_path)
    idle_duration = config.IDLE_DURATION
    sleep_duration = config.SLEEP_DURATION

    # Phase 1: 检查 popup
    popup_count = central_db.get_unread_popup_count(config.CENTRAL_DB, excluded_ids)
    if popup_count > 0:
        popups = central_db.get_undelivered_messages(config.CENTRAL_DB, excluded_ids, ("popup",))
        session_db.mark_delivered(db_path, [m["id"] for m in popups])
        output = render_brief(brief_template, item_template, popups, [], [])
        print(output)
        return

    # Phase 2: IdleDuration 轮询
    elapsed = 0
    poll_interval = 5
    while elapsed < idle_duration:
        time.sleep(poll_interval)
        elapsed += poll_interval
        excluded_ids = session_db.get_excluded_ids(db_path)
        normals = central_db.get_undelivered_messages(config.CENTRAL_DB, excluded_ids, ("popup", "normal"))
        if normals:
            popups = [m for m in normals if m["category"] == "popup"]
            msgs = [m for m in normals if m["category"] == "normal"]
            session_db.mark_delivered(db_path, [m["id"] for m in normals])
            output = render_brief(brief_template, item_template, popups, msgs, [])
            print(output)
            return

    # Phase 3: SleepDuration 轮询
    while elapsed < sleep_duration:
        time.sleep(poll_interval)
        elapsed += poll_interval
        excluded_ids = session_db.get_excluded_ids(db_path)
        new_msgs = central_db.get_undelivered_messages(config.CENTRAL_DB, excluded_ids, ("popup", "normal", "silent"))
        if new_msgs:
            popups = [m for m in new_msgs if m["category"] == "popup"]
            msgs = [m for m in new_msgs if m["category"] == "normal"]
            silents = [m for m in new_msgs if m["category"] == "silent"]
            session_db.mark_delivered(db_path, [m["id"] for m in new_msgs])
            output = render_brief(brief_template, item_template, popups, msgs, silents)
            print(output)
            return

    # 无消息 — exit 2 让 hooks 继续
    sys.exit(2)


# ── msgbox peek ─────────────────────────────────────────────


def _peek_cooldown_file(session_id: str) -> str:
    config.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    return str(config.SESSIONS_DIR / f"{session_id}.peek_ts")


def _check_peek_cooldown(session_id: str) -> bool:
    f = _peek_cooldown_file(session_id)
    if not Path(f).exists():
        return False
    try:
        elapsed = time.time() - float(Path(f).read_text().strip())
        return elapsed < config.PEEK_COOLDOWN
    except (ValueError, OSError):
        return False


def _touch_peek_cooldown(session_id: str):
    Path(_peek_cooldown_file(session_id)).write_text(str(time.time()))


def cmd_peek(args):
    sid = _session_id()
    if not sid:
        return

    if _check_peek_cooldown(sid):
        return
    _touch_peek_cooldown(sid)

    db_path = _session_db_path(sid)
    if not Path(db_path).exists():
        return

    session_db.init_session_db(db_path)
    central_db.init_central_db(config.CENTRAL_DB)

    cfg = load_config()
    templates = cfg.get("templates", {})
    brief_template = templates.get("brief", "")
    item_template = templates.get("item", "")

    excluded_ids = session_db.get_excluded_ids(db_path)
    new_msgs = central_db.get_undelivered_messages(config.CENTRAL_DB, excluded_ids, ("popup", "normal", "silent"))

    if not new_msgs:
        return

    popups = [m for m in new_msgs if m["category"] == "popup"]
    msgs = [m for m in new_msgs if m["category"] == "normal"]
    silents = [m for m in new_msgs if m["category"] == "silent"]

    session_db.mark_delivered(db_path, [m["id"] for m in new_msgs])

    output = render_brief(brief_template, item_template, popups, msgs, silents)
    print(output)


# ── msgbox mark-done ────────────────────────────────────────


def cmd_mark_done(args):
    sid = _session_id()
    if not sid:
        print("CLAUDE_CODE_SESSION_ID not set", file=sys.stderr)
        sys.exit(1)

    db_path = _session_db_path(sid)
    if not Path(db_path).exists():
        print("msg_box not active", file=sys.stderr)
        sys.exit(1)

    if args.all:
        session_db.init_session_db(db_path)
        ids = session_db.get_excluded_ids(db_path)
        if ids:
            session_db.mark_done(db_path, list(ids))
            print(f"Marked {len(ids)} messages as done")
        else:
            print("No messages to mark")
    elif args.ids:
        msg_ids = [int(x) for x in args.ids.split(",")]
        session_db.mark_done(db_path, msg_ids)
        print(f"Marked {len(msg_ids)} messages as done")
    else:
        print("Specify --ids or --all", file=sys.stderr)
        sys.exit(1)


# ── msgbox config ───────────────────────────────────────────


def cmd_config_get(args):
    val = get_config_value(args.key)
    if val is None:
        print(f"Key '{args.key}' not found", file=sys.stderr)
        sys.exit(1)
    if isinstance(val, (dict, list)):
        print(json.dumps(val, ensure_ascii=False, indent=2))
    else:
        print(val)


def cmd_config_set(args):
    val = args.value
    try:
        val = json.loads(val)
    except (json.JSONDecodeError, TypeError):
        pass
    set_config_value(args.key, val)
    print(f"Set {args.key} = {val}")


def cmd_config_rules(args):
    rules = list_rules()
    if not rules:
        print("No rules configured")
        return
    for r in rules:
        print(f"[{r['index']}] {r['type']:20s} type={r['pattern']:30s} props={json.dumps(r['props'], ensure_ascii=False)}")


def cmd_config_rules_add(args):
    props = {}
    if args.props:
        try:
            props = json.loads(args.props)
        except json.JSONDecodeError:
            print("Invalid JSON for --props", file=sys.stderr)
            sys.exit(1)
    add_rule(args.rule_type, args.pattern, props)
    print(f"Added {args.rule_type} rule: type={args.pattern} props={props}")


def cmd_config_rules_remove(args):
    remove_rule(args.rule_type, args.index)
    print(f"Removed rule [{args.index}] from {args.rule_type}")


# ── msgbox list-sessions ────────────────────────────────────


def cmd_list_sessions(args):
    sessions = session_db.get_active_sessions()
    if not sessions:
        print("No active sessions")
        return
    for s in sessions:
        print(f"  {s['project']:30s} {s['session_id']}")


# ── argparse ────────────────────────────────────────────────


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

    sp = sub.add_parser("mark-done", help="Mark popup messages as processed")
    sp.add_argument("--ids", help="Comma-separated message IDs")
    sp.add_argument("--all", action="store_true", help="Mark all delivered messages as done")
    sp.set_defaults(func=cmd_mark_done)

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
