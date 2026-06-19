"""Todo commands — CRUD operations for session todos."""

import sys
from datetime import datetime, timezone

from .. import session as session_db
from .. import todo as todo_logic
from .session import _require_session_db


def _todo_session():
    return _require_session_db()


def cmd_todo_add(args):
    db_path = _todo_session()
    parent_id = args.parent
    dependency_id = args.after
    insert_before = args.insert_before
    insert_after = args.insert_after

    if dependency_id is not None and todo_logic.get_todo(db_path, dependency_id) is None:
        print(f"Dependency todo #{dependency_id} not found", file=sys.stderr)
        sys.exit(1)
    if parent_id is not None and todo_logic.get_todo(db_path, parent_id) is None:
        print(f"Parent todo #{parent_id} not found", file=sys.stderr)
        sys.exit(1)
    if insert_before is not None and todo_logic.get_todo(db_path, insert_before) is None:
        print(f"Insert-before todo #{insert_before} not found", file=sys.stderr)
        sys.exit(1)
    if insert_after is not None and todo_logic.get_todo(db_path, insert_after) is None:
        print(f"Insert-after todo #{insert_after} not found", file=sys.stderr)
        sys.exit(1)
    if (insert_before is not None or insert_after is not None) and parent_id is None:
        print("--insert-before/--insert-after require --parent", file=sys.stderr)
        sys.exit(1)

    try:
        todo_id = todo_logic.add_todo(
            db_path,
            title=args.title,
            approach=args.approach or "",
            duration=args.duration,
            wait_time=args.wait_time or 0,
            parent_id=parent_id,
            dependency_id=dependency_id,
            insert_before=insert_before,
            insert_after=insert_after,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    print(f"Todo #{todo_id} added: {args.title}")


def cmd_todo_list(args):
    db_path = _todo_session()
    todos = todo_logic.list_todos(db_path, status=args.status)
    if not todos:
        print("No todos")
        return
    for t in todos:
        status = t["status"]
        duration = todo_logic.format_duration(t["duration_s"])
        wait = ""
        if t.get("wait_time_s"):
            wait = f", wait={todo_logic.format_duration(t['wait_time_s'])}"
        print(f"  #{t['id']} [{status}] {t['title']} ({duration}{wait})")


def cmd_todo_tree(args):
    db_path = _todo_session()
    todos = todo_logic.list_todos(db_path)
    if not todos:
        print("No todos")
        return
    tree = todo_logic.build_todo_tree(todos)
    for line in todo_logic.format_todo_tree(tree):
        print(line)


def cmd_todo_start(args):
    db_path = _todo_session()
    result = todo_logic.activate_todo(db_path, args.id)
    if not result["ok"]:
        print(result["error"], file=sys.stderr)
        sys.exit(1)
    print(f"Todo #{args.id} activated")


def cmd_todo_done(args):
    db_path = _todo_session()
    result = todo_logic.mark_todo_done(db_path, args.id)
    if not result["ok"]:
        print(result["error"], file=sys.stderr)
        sys.exit(1)
    print(f"Todo #{args.id} marked as done")


def cmd_todo_cancel(args):
    db_path = _todo_session()
    result = todo_logic.cancel_todo(db_path, args.id)
    if not result["ok"]:
        print(result["error"], file=sys.stderr)
        sys.exit(1)
    print(f"Todo #{args.id} cancelled")


def cmd_todo_delete(args):
    db_path = _todo_session()
    if todo_logic.get_todo(db_path, args.id) is None:
        print(f"Todo #{args.id} not found", file=sys.stderr)
        sys.exit(1)
    todo_logic.delete_todo(db_path, args.id)
    print(f"Todo #{args.id} deleted")


def cmd_todo_wait_time(args):
    db_path = _todo_session()
    if todo_logic.get_todo(db_path, args.id) is None:
        print(f"Todo #{args.id} not found", file=sys.stderr)
        sys.exit(1)
    try:
        todo_logic.set_wait_time(db_path, args.id, args.duration)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    print(f"Todo #{args.id} wait-time set to {args.duration}")


def cmd_todo_active(args):
    db_path = _todo_session()
    active = todo_logic.get_current_tasks(db_path)
    if not active:
        print("No active todos")
        return
    for t in active:
        activated_at = t.get("activated_at")
        elapsed = "?"
        remaining = "?"
        if activated_at:
            try:
                ts = datetime.fromisoformat(activated_at)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                elapsed_s = int((datetime.now(timezone.utc) - ts).total_seconds())
                elapsed = todo_logic.format_duration(elapsed_s)
                remaining = todo_logic.format_duration(max(0, t["duration_s"] - elapsed_s))
            except (ValueError, TypeError):
                pass
        print(f"  #{t['id']} {t['title']} — 已进行 {elapsed}，剩余 {remaining}")
