"""Message commands — send, wait, peek, close, mark-done, history."""

import json
import os
import sys
import time
from pathlib import Path

from .. import config
from .. import db as central_db
from .. import session as session_db
from .. import todo as todo_logic
from ..filter import classify_message
from ..template import render_brief
from ..yaml_config import load_config
from .session import _is_main_agent, _require_session_db, _session_db_path, _read_hook_input


def _session_id():
    return os.environ.get("CLAUDE_CODE_SESSION_ID")


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


def cmd_wait(args):
    sid = _session_id()
    if not sid:
        sys.exit(0)
    if not _is_main_agent():
        sys.exit(0)

    db_path = _session_db_path(sid)
    if not Path(db_path).exists():
        sys.exit(0)

    session_db.init_session_db(db_path)
    central_db.init_central_db(config.CENTRAL_DB)

    cfg = load_config()
    templates = cfg.get("templates", {})
    brief_template = templates.get("brief_wait") or templates.get("brief", "")
    item_template = templates.get("item", "")
    group_templates = templates.get("groups", {})

    idle_duration = config.IDLE_DURATION
    sleep_duration = config.SLEEP_DURATION

    def _collect_pending():
        cursor = session_db.get_read_cursor(db_path)
        open_popups = session_db.get_open_popups(db_path)
        new_popups = central_db.get_messages_after(
            config.CENTRAL_DB, cursor, ("popup",), excluded_ids=open_popups
        )
        open_popup_msgs = central_db.get_messages_by_ids(
            config.CENTRAL_DB, list(open_popups)
        )
        popups = new_popups + open_popup_msgs
        msgs = central_db.get_messages_after(config.CENTRAL_DB, cursor, ("normal",))
        return popups, msgs

    def _deliver(popups, msgs):
        all_ids = [m["id"] for m in popups] + [m["id"] for m in msgs]
        if all_ids:
            cursor = session_db.get_read_cursor(db_path)
            session_db.set_read_cursor(db_path, max(cursor, max(all_ids)))
        if popups:
            session_db.mark_popups_delivered(db_path, [m["id"] for m in popups])

    wait_start = time.monotonic()

    # Phase 0: check todo reminder
    todo_logic.check_and_emit_reminders(db_path)

    active_todos_var = todo_logic.format_active_todos(db_path)

    # Phase 1: check popup (immediate return)
    popups, msgs = _collect_pending()
    if popups:
        _deliver(popups, msgs)
        output = render_brief(
            brief_template, item_template, popups, msgs,
            group_templates=group_templates,
            extra_vars={"ACTIVE_TODOS": active_todos_var},
        )
        print(output, file=sys.stderr)
        sys.exit(2)

    # Phase 2+3: polling loop (idle then sleep)
    elapsed = 0
    poll_interval = 5
    batch_window = config.WAIT_BATCH_WINDOW
    total_duration = idle_duration + sleep_duration
    while elapsed < total_duration:
        time.sleep(poll_interval)
        elapsed += poll_interval

        todo_logic.check_and_emit_reminders(db_path)

        current_wait_elapsed = int(time.monotonic() - wait_start)
        resume_tasks = todo_logic.check_resume_needed(db_path, current_wait_elapsed)
        if resume_tasks:
            todo_logic.emit_resume_message(db_path, resume_tasks)
            popups, msgs = _collect_pending()
            if msgs:
                _deliver(popups, msgs)
                output = render_brief(
                    brief_template, item_template, popups, msgs,
                    group_templates=group_templates,
                    extra_vars={"ACTIVE_TODOS": todo_logic.format_active_todos(db_path)},
                )
                print(output, file=sys.stderr)
                sys.exit(2)

        popups, msgs = _collect_pending()
        if popups or msgs:
            buffer_deadline = time.monotonic() + batch_window
            while time.monotonic() < buffer_deadline:
                remaining = buffer_deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(remaining, 0.1))
                more_popups, more_msgs = _collect_pending()
                if more_popups:
                    popups = more_popups
                    msgs = more_msgs
                    break
                if more_msgs:
                    msgs = more_msgs
            _deliver(popups, msgs)
            output = render_brief(
                brief_template, item_template, popups, msgs,
                group_templates=group_templates,
                extra_vars={"ACTIVE_TODOS": todo_logic.format_active_todos(db_path)},
            )
            print(output, file=sys.stderr)
            sys.exit(2)

    # Fallback: final resume check after loop
    wait_elapsed = int(time.monotonic() - wait_start)
    resume_tasks = todo_logic.check_resume_needed(db_path, wait_elapsed)
    if resume_tasks:
        todo_logic.emit_resume_message(db_path, resume_tasks)
        popups, msgs = _collect_pending()
        if msgs:
            _deliver(popups, msgs)
            output = render_brief(
                brief_template, item_template, popups, msgs,
                group_templates=group_templates,
                extra_vars={"ACTIVE_TODOS": todo_logic.format_active_todos(db_path)},
            )
            print(output, file=sys.stderr)
            sys.exit(2)

    print(f"Waited {total_duration}s, no new messages", file=sys.stderr)
    sys.exit(2)


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
    if not _is_main_agent():
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
    brief_template = templates.get("brief_peek") or templates.get("brief", "")
    item_template = templates.get("item", "")
    group_templates = templates.get("groups", {})

    todo_logic.check_and_emit_reminders(db_path)

    cursor = session_db.get_read_cursor(db_path)
    open_popups = session_db.get_open_popups(db_path)

    new_popups = central_db.get_messages_after(
        config.CENTRAL_DB, cursor, ("popup",), excluded_ids=open_popups
    )
    open_popup_msgs = central_db.get_messages_by_ids(config.CENTRAL_DB, list(open_popups))
    popups = new_popups + open_popup_msgs
    msgs = central_db.get_messages_after(config.CENTRAL_DB, cursor, ("normal",))

    if not popups and not msgs:
        return

    all_ids = [m["id"] for m in popups] + [m["id"] for m in msgs]
    session_db.set_read_cursor(db_path, max(cursor, max(all_ids)))
    if popups:
        session_db.mark_popups_delivered(db_path, [m["id"] for m in popups])

    active_todos_var = todo_logic.format_active_todos(db_path)
    output = render_brief(
        brief_template, item_template, popups, msgs,
        group_templates=group_templates,
        extra_vars={"ACTIVE_TODOS": active_todos_var},
    )
    print(output, file=sys.stderr)
    sys.exit(2)


def cmd_close(args):
    sid = _session_id()
    if not sid:
        print("CLAUDE_CODE_SESSION_ID not set", file=sys.stderr)
        sys.exit(1)

    db_path = _session_db_path(sid)
    if not Path(db_path).exists():
        print("msg_box not active", file=sys.stderr)
        sys.exit(1)

    session_db.init_session_db(db_path)

    if args.ids:
        msg_ids = [int(x) for x in args.ids.split(",")]
        session_db.close_popups(db_path, msg_ids)
        print(f"Closed {len(msg_ids)} popup messages")
        return

    delivered_open = session_db.get_open_popups(db_path, delivered_only=True)
    if delivered_open:
        session_db.close_popups(db_path, list(delivered_open))
        print(f"Closed {len(delivered_open)} popup messages")
        return

    print("No popup messages to close")


def cmd_mark_done(args):
    sid = _session_id()
    if not sid:
        print("CLAUDE_CODE_SESSION_ID not set", file=sys.stderr)
        sys.exit(1)

    db_path = _session_db_path(sid)
    if not Path(db_path).exists():
        print("msg_box not active", file=sys.stderr)
        sys.exit(1)

    session_db.init_session_db(db_path)

    if args.all:
        open_popups = session_db.get_open_popups(db_path)
        if open_popups:
            session_db.close_popups(db_path, list(open_popups))
            print(f"Marked {len(open_popups)} messages as done")
        else:
            print("No messages to mark")
    elif args.ids:
        msg_ids = [int(x) for x in args.ids.split(",")]
        session_db.close_popups(db_path, msg_ids)
        print(f"Marked {len(msg_ids)} messages as done")
    else:
        print("Specify --ids or --all", file=sys.stderr)
        sys.exit(1)


def cmd_history(args):
    central_db.init_central_db(config.CENTRAL_DB)

    msgs = central_db.get_messages(
        config.CENTRAL_DB,
        limit=args.limit,
        offset=args.offset,
        categories=tuple(args.category) if args.category else None,
        type_pattern=args.type,
    )

    if not msgs:
        print("No messages found")
        return

    popups = [m for m in msgs if m["category"] == "popup"]
    normals = [m for m in msgs if m["category"] == "normal"]
    silents = [m for m in msgs if m["category"] == "silent"]

    cfg = load_config()
    templates = cfg.get("templates", {})
    group_templates = templates.get("groups", {})
    output = render_brief(
        templates.get("brief", ""),
        templates.get("item", ""),
        popups, normals, silents,
        group_templates=group_templates,
    )
    print(output)
    print(f"--- {len(msgs)} messages (offset={args.offset}) ---")


def cmd_debug_hook(args):
    """Debug hook: dump stdin JSON and env to stderr."""
    data = _read_hook_input()
    status = "✅ MAIN AGENT" if _is_main_agent() else "⚠️  SUB AGENT"
    print("=" * 40, file=sys.stderr)
    print(f"  _is_main_agent() = {status}", file=sys.stderr)
    print(f"  CLAUDE_CODE_SESSION_ID = {os.environ.get('CLAUDE_CODE_SESSION_ID', 'N/A')}", file=sys.stderr)
    print(f"  CLAUDE_CODE_CHILD_SESSION = {os.environ.get('CLAUDE_CODE_CHILD_SESSION', 'N/A')}", file=sys.stderr)
    print(f"  HAS agent_id in stdin = {'YES' if data.get('agent_id') is not None else 'NO'}", file=sys.stderr)
    print(f"  agent_id = {data.get('agent_id', 'N/A')}", file=sys.stderr)
    print(f"  agent_type = {data.get('agent_type', 'N/A')}", file=sys.stderr)
    print(f"  hook_event_name = {data.get('hook_event_name', 'N/A')}", file=sys.stderr)
    print(f"  session_id in stdin = {data.get('session_id', 'N/A')}", file=sys.stderr)
    print(f"  Full stdin JSON:", file=sys.stderr)
    print(f"  {json.dumps(data, indent=2, ensure_ascii=False)}", file=sys.stderr)
    print("=" * 40, file=sys.stderr)
    sys.exit(2)
