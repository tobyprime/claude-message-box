"""Todo 业务逻辑 - 基于 session.db 的逐会话任务管理

核心能力：
- 任务 CRUD、层级/依赖关系
- 激活后 50% / 75% / 100% 三个阶段提醒
- wait 阶段按 wait_time_s 决定是否重新激活会话
- 当前任务判定：有未完成子任务则取子任务，否则取自身
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from . import config
from . import db as central_db
from .session import _with_cursor, init_session_db


# ── 时间解析 ────────────────────────────────────────────────


_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([smh])$", re.IGNORECASE)


def parse_duration(text: str) -> int:
    """解析人类可读时长，返回秒数。

    支持格式：30s, 5m, 2h, 1.5h
    """
    text = text.strip().lower()
    m = _DURATION_RE.match(text)
    if not m:
        try:
            return int(text)
        except ValueError as exc:
            raise ValueError(f"Invalid duration: {text!r}") from exc
    value = float(m.group(1))
    unit = m.group(2)
    multipliers = {"s": 1, "m": 60, "h": 3600}
    return int(value * multipliers[unit])


def format_duration(seconds: int) -> str:
    """将秒数转为人类可读字符串。"""
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        minutes = seconds // 60
        secs = seconds % 60
        return f"{minutes}m{secs}s" if secs else f"{minutes}m"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    return f"{hours}h{minutes}m" if minutes else f"{hours}h"


# ── 核心 CRUD ──────────────────────────────────────────────


def _next_sibling_order(db_path: str, parent_id: int | None) -> float:
    """计算同一 parent 下新的 sibling_order（放在最后）。"""
    row = _with_cursor(
        db_path,
        lambda c: c.execute(
            "SELECT COALESCE(MAX(sibling_order), 0) FROM todos WHERE parent_id IS ?",
            [parent_id],
        ).fetchone(),
    )
    return (row[0] if row else 0) + 1.0


def add_todo_raw(
    db_path: str,
    title: str,
    approach: str,
    duration_s: int,
    wait_time_s: int = 0,
    parent_id: int | None = None,
    dependency_id: int | None = None,
    sibling_order: float | None = None,
) -> int:
    """低层插入一条 todo，返回自增 id。"""
    if sibling_order is None:
        sibling_order = _next_sibling_order(db_path, parent_id)

    def _insert(c):
        return c.execute(
            """
            INSERT INTO todos (title, approach, duration_s, wait_time_s, parent_id, dependency_id, sibling_order)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [title, approach, duration_s, wait_time_s, parent_id, dependency_id, sibling_order],
        ).lastrowid

    return _with_cursor(db_path, _insert)


def get_todo_raw(db_path: str, todo_id: int) -> dict | None:
    """低层按 id 获取 todo。"""
    row = _with_cursor(
        db_path,
        lambda c: c.execute("SELECT * FROM todos WHERE id = ?", [todo_id]).fetchone(),
    )
    return dict(row) if row else None


def list_todos_raw(db_path: str, status: str | None = None) -> list[dict]:
    """低层列出 todo，可选按 status 过滤。"""
    if status:
        rows = _with_cursor(
            db_path,
            lambda c: c.execute(
                "SELECT * FROM todos WHERE status = ? ORDER BY sibling_order ASC, created_at ASC", [status]
            ).fetchall(),
        )
    else:
        rows = _with_cursor(
            db_path,
            lambda c: c.execute("SELECT * FROM todos ORDER BY sibling_order ASC, created_at ASC").fetchall(),
        )
    return [dict(r) for r in rows]


def update_todo_raw(db_path: str, todo_id: int, updates: dict):
    """低层更新 todo 字段。"""
    allowed = {
        "title", "approach", "duration_s", "wait_time_s", "status",
        "parent_id", "dependency_id", "sibling_order", "activated_at", "completed_at",
    }
    cols = [k for k in updates if k in allowed]
    if not cols:
        return
    params = [updates[k] for k in cols]
    params.append(todo_id)
    sql = "UPDATE todos SET " + ", ".join(f"{k} = ?" for k in cols) + " WHERE id = ?"
    _with_cursor(db_path, lambda c: c.execute(sql, params))


def delete_todo_raw(db_path: str, todo_id: int):
    """低层删除 todo（级联删除子任务和提醒记录）。"""
    def _delete(conn):
        conn.execute("DELETE FROM todos WHERE parent_id = ?", [todo_id])
        conn.execute("DELETE FROM todo_reminders WHERE todo_id = ?", [todo_id])
        conn.execute("DELETE FROM todos WHERE id = ?", [todo_id])

    _with_cursor(db_path, lambda c: _delete(c.connection))


def record_reminder_raw(db_path: str, todo_id: int, stage: str):
    """记录某 stage 的提醒已发送。"""
    _with_cursor(
        db_path,
        lambda c: c.execute(
            "INSERT OR IGNORE INTO todo_reminders (todo_id, stage) VALUES (?, ?)",
            [todo_id, stage],
        ),
    )


def has_reminder_been_sent_raw(db_path: str, todo_id: int, stage: str) -> bool:
    """检查某 stage 的提醒是否已发送。"""
    row = _with_cursor(
        db_path,
        lambda c: c.execute(
            "SELECT 1 FROM todo_reminders WHERE todo_id = ? AND stage = ?",
            [todo_id, stage],
        ).fetchone(),
    )
    return row is not None


def get_active_todos_raw(db_path: str) -> list[dict]:
    """低层获取所有 active 状态的任务。"""
    return list_todos_raw(db_path, status="active")


def get_child_todos_raw(db_path: str, todo_id: int, status: str | None = None) -> list[dict]:
    """低层获取某 todo 的子任务。"""
    if status:
        rows = _with_cursor(
            db_path,
            lambda c: c.execute(
                "SELECT * FROM todos WHERE parent_id = ? AND status = ? ORDER BY sibling_order ASC, created_at ASC",
                [todo_id, status],
            ).fetchall(),
        )
    else:
        rows = _with_cursor(
            db_path,
            lambda c: c.execute(
                "SELECT * FROM todos WHERE parent_id = ? ORDER BY sibling_order ASC, created_at ASC", [todo_id]
            ).fetchall(),
        )
    return [dict(r) for r in rows]


def has_unfinished_children_raw(db_path: str, todo_id: int) -> bool:
    """是否存在未完成的子任务。"""
    row = _with_cursor(
        db_path,
        lambda c: c.execute(
            "SELECT 1 FROM todos WHERE parent_id = ? AND status NOT IN ('done', 'cancelled') LIMIT 1",
            [todo_id],
        ).fetchone(),
    )
    return row is not None


# ── 业务逻辑包装 ────────────────────────────────────────────


def add_todo(
    db_path: str,
    title: str,
    approach: str,
    duration: str | int,
    wait_time: str | int = 0,
    parent_id: int | None = None,
    dependency_id: int | None = None,
    insert_before: int | None = None,
    insert_after: int | None = None,
) -> int:
    """添加 todo，返回 id。

    insert_before / insert_after 用于在同一 parent 下指定兄弟插入位置。
    """
    duration_s = duration if isinstance(duration, int) else parse_duration(duration)
    wait_time_s = wait_time if isinstance(wait_time, int) else parse_duration(wait_time)

    sibling_order = None
    if insert_before is not None or insert_after is not None:
        sibling_order = _compute_insert_order(
            db_path, parent_id, insert_before, insert_after
        )

    return add_todo_raw(
        db_path,
        title=title,
        approach=approach,
        duration_s=duration_s,
        wait_time_s=wait_time_s,
        parent_id=parent_id,
        dependency_id=dependency_id,
        sibling_order=sibling_order,
    )


def _get_sibling_orders(db_path: str, parent_id: int | None) -> list[tuple[int, float]]:
    """获取同一 parent 下所有子任务的 (id, sibling_order)，按 order 升序。"""
    rows = list_todos_raw(db_path)
    siblings = [
        (r["id"], r.get("sibling_order") or 0)
        for r in rows
        if r.get("parent_id") == parent_id
    ]
    siblings.sort(key=lambda x: x[1])
    return siblings


def _compute_insert_order(
    db_path: str,
    parent_id: int | None,
    insert_before: int | None,
    insert_after: int | None,
) -> float:
    """根据 insert_before/after 计算 sibling_order。

    使用浮点数中间值；若相邻 order 差距过小，触发重排。
    """
    target_id = insert_before or insert_after
    todo = get_todo_raw(db_path, target_id)
    if todo is None:
        raise ValueError(f"Reference todo #{target_id} not found")
    if todo.get("parent_id") != parent_id:
        raise ValueError(f"Reference todo #{target_id} is not under the same parent")

    siblings = _get_sibling_orders(db_path, parent_id)
    positions = {tid: order for tid, order in siblings}
    target_order = positions.get(target_id)
    if target_order is None:
        target_order = 0

    # 找到目标在兄弟列表中的索引
    sorted_ids = [tid for tid, _ in siblings]
    idx = sorted_ids.index(target_id)

    if insert_before is not None:
        prev_order = siblings[idx - 1][1] if idx > 0 else None
        if prev_order is None:
            new_order = target_order - 1.0
        else:
            new_order = (prev_order + target_order) / 2.0
            if target_order - prev_order < 0.001:
                _normalize_sibling_orders(db_path, parent_id)
                return _compute_insert_order(db_path, parent_id, insert_before, None)
    else:  # insert_after
        next_order = siblings[idx + 1][1] if idx < len(siblings) - 1 else None
        if next_order is None:
            new_order = target_order + 1.0
        else:
            new_order = (target_order + next_order) / 2.0
            if next_order - target_order < 0.001:
                _normalize_sibling_orders(db_path, parent_id)
                return _compute_insert_order(db_path, parent_id, None, insert_after)

    return new_order


def _normalize_sibling_orders(db_path: str, parent_id: int | None):
    """将同一 parent 下所有子任务的 sibling_order 重新规范化为连续整数。"""
    siblings = _get_sibling_orders(db_path, parent_id)
    for i, (tid, _) in enumerate(siblings, start=1):
        update_todo_raw(db_path, tid, {"sibling_order": float(i)})


def get_todo(db_path: str, todo_id: int) -> dict | None:
    return get_todo_raw(db_path, todo_id)


def list_todos(db_path: str, status: str | None = None) -> list[dict]:
    return list_todos_raw(db_path, status=status)


def update_todo(db_path: str, todo_id: int, **kwargs):
    update_todo_raw(db_path, todo_id, kwargs)


def delete_todo(db_path: str, todo_id: int):
    delete_todo_raw(db_path, todo_id)


def set_wait_time(db_path: str, todo_id: int, wait_time: str | int):
    """修改任务的 wait_time_s。"""
    wait_time_s = wait_time if isinstance(wait_time, int) else parse_duration(wait_time)
    update_todo(db_path, todo_id, wait_time_s=wait_time_s)


# ── 状态转换 ────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        ts = datetime.fromisoformat(iso)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    except (ValueError, TypeError):
        return None


def activate_todo(db_path: str, todo_id: int) -> dict:
    """激活任务，检查依赖是否完成。

    返回 {"ok": bool, "todo": dict|None, "error": str|None}
    """
    todo = get_todo(db_path, todo_id)
    if todo is None:
        return {"ok": False, "todo": None, "error": f"Todo #{todo_id} not found"}

    if todo["status"] == "active":
        return {"ok": True, "todo": todo, "error": None}

    if todo["status"] in ("done", "cancelled"):
        return {"ok": False, "todo": todo, "error": f"Todo #{todo_id} is already {todo['status']}"}

    dep_id = todo.get("dependency_id")
    if dep_id:
        dep = get_todo(db_path, dep_id)
        if dep is None:
            return {"ok": False, "todo": todo, "error": f"Dependency todo #{dep_id} not found"}
        if dep["status"] != "done":
            return {
                "ok": False,
                "todo": todo,
                "error": f"Dependency todo #{dep_id} is not done yet",
            }

    updates = {"status": "active", "activated_at": _now_iso()}
    update_todo(db_path, todo_id, **updates)
    todo = get_todo(db_path, todo_id)
    return {"ok": True, "todo": todo, "error": None}


def mark_todo_done(db_path: str, todo_id: int) -> dict:
    """标记任务完成，同时尝试完成父任务。"""
    todo = get_todo(db_path, todo_id)
    if todo is None:
        return {"ok": False, "todo": None, "error": f"Todo #{todo_id} not found"}
    if todo["status"] in ("done", "cancelled"):
        return {"ok": True, "todo": todo, "error": None}
    if todo["status"] != "active":
        return {
            "ok": False,
            "todo": todo,
            "error": f"Todo #{todo_id} is not active (status: {todo['status']}); start it first",
        }

    update_todo(db_path, todo_id, status="done", completed_at=_now_iso())
    todo = get_todo(db_path, todo_id)

    # 如果所有子任务都完成，尝试将父任务也标记为完成
    parent_id = todo.get("parent_id")
    if parent_id:
        parent = get_todo(db_path, parent_id)
        if parent and parent["status"] == "active":
            children = get_child_todos_raw(db_path, parent_id)
            if children and all(c["status"] in ("done", "cancelled") for c in children):
                mark_todo_done(db_path, parent_id)

    return {"ok": True, "todo": todo, "error": None}


def cancel_todo(db_path: str, todo_id: int) -> dict:
    todo = get_todo(db_path, todo_id)
    if todo is None:
        return {"ok": False, "todo": None, "error": f"Todo #{todo_id} not found"}
    update_todo(db_path, todo_id, status="cancelled", completed_at=_now_iso())
    return {"ok": True, "todo": get_todo(db_path, todo_id), "error": None}


# ── 提醒计算 ────────────────────────────────────────────────


REMINDER_STAGES = [
    ("half", 0.5),
    ("three_quarter", 0.75),
    ("due", 1.0),
]


def _calc_reminder_stage(todo: dict, now: datetime) -> str | None:
    """计算当前应触发哪个 stage 提醒（尚未发送的最高阶段）。"""
    activated_at = _parse_iso(todo.get("activated_at"))
    if activated_at is None:
        return None
    duration_s = todo.get("duration_s", 0)
    if duration_s <= 0:
        return None

    elapsed_s = int((now - activated_at).total_seconds())
    triggered_stage: str | None = None
    for stage, ratio in REMINDER_STAGES:
        threshold = int(duration_s * ratio)
        if elapsed_s >= threshold:
            triggered_stage = stage
    return triggered_stage


def check_and_emit_reminders(db_path: str, now: datetime | None = None) -> list[dict]:
    """检查 active 任务，为应触发但未发送的 stage 插入 todo.reminder 消息。

    返回插入的 reminder 消息列表。
    """
    if now is None:
        now = datetime.now(timezone.utc)

    try:
        init_session_db(db_path)
    except Exception:
        pass

    try:
        active_todos = get_active_todos_raw(db_path)
        if not isinstance(active_todos, list):
            return []
    except Exception:
        return []

    emitted: list[dict] = []
    for todo in active_todos:
        stage = _calc_reminder_stage(todo, now)
        if not stage:
            continue
        if has_reminder_been_sent_raw(db_path, todo["id"], stage):
            continue

        activated_at = _parse_iso(todo.get("activated_at"))
        elapsed_s = int((now - activated_at).total_seconds()) if activated_at else 0
        remaining_s = max(0, todo["duration_s"] - elapsed_s)

        title = f"[待办提醒] {todo['title']}"
        content = (
            f"任务已进行 {format_duration(elapsed_s)}，"
            f"预计还需 {format_duration(remaining_s)}。"
        )
        if stage == "half":
            content = "已过半。" + content
        elif stage == "three_quarter":
            content = "已进行 75%。" + content
        elif stage == "due":
            content = "已到达预计完成时间。" + content

        msg_id = central_db.insert_message(
            config.CENTRAL_DB,
            type_="todo.reminder",
            title=title,
            content=content,
            props={
                "todo_id": todo["id"],
                "reminder_stage": stage,
                "remaining_s": remaining_s,
                "elapsed_s": elapsed_s,
            },
            category="normal",
            source="todo",
        )
        record_reminder_raw(db_path, todo["id"], stage)
        emitted.append({
            "id": msg_id,
            "type": "todo.reminder",
            "title": title,
            "content": content,
            "props": {"todo_id": todo["id"], "reminder_stage": stage},
            "category": "normal",
        })
    return emitted


# ── 当前任务判定与会话重激活 ─────────────────────────────────


def get_current_tasks(db_path: str) -> list[dict]:
    """获取当前真正需要推进的任务。

    对 active 任务：若有未完成子任务，则返回这些子任务；否则返回自身。
    """
    try:
        init_session_db(db_path)
        todos = get_active_todos_raw(db_path)
        if not isinstance(todos, list):
            return []
    except Exception:
        return []

    result: list[dict] = []
    for todo in todos:
        try:
            if has_unfinished_children_raw(db_path, todo["id"]):
                children = get_child_todos_raw(
                    db_path, todo["id"], status=None
                )
                for child in children:
                    if child["status"] not in ("done", "cancelled"):
                        result.append(child)
            else:
                result.append(todo)
        except Exception:
            continue
    return result


def check_resume_needed(db_path: str, wait_elapsed_s: int) -> list[dict]:
    """根据 wait 已阻塞时长，判断哪些 active 任务需要重新激活会话。

    仅当 wait_elapsed_s > task.wait_time_s 且 wait_time_s > 0 时触发。
    返回需要被提示的任务列表（已做当前任务判定）。
    """
    result: list[dict] = []
    for todo in get_current_tasks(db_path):
        wait_time_s = todo.get("wait_time_s") or 0
        if wait_time_s > 0 and wait_elapsed_s > wait_time_s:
            result.append(todo)
    return result


def emit_resume_message(db_path: str, tasks: list[dict]) -> int | None:
    """插入 todo.session_resume 消息，返回消息 id；无任务时返回 None。"""
    if not tasks:
        return None

    now = datetime.now(timezone.utc)
    lines: list[str] = []
    for t in tasks:
        activated_at = _parse_iso(t.get("activated_at"))
        elapsed_s = int((now - activated_at).total_seconds()) if activated_at else 0
        remaining_s = max(0, t["duration_s"] - elapsed_s)
        lines.append(
            f"• {t['title']}（已进行 {format_duration(elapsed_s)}，"
            f"剩余 {format_duration(remaining_s)}）"
        )

    title = f"[继续任务] 你有 {len(tasks)} 个进行中的任务"
    content = "以下任务仍在进行中，建议继续推进：\n" + "\n".join(lines)

    return central_db.insert_message(
        config.CENTRAL_DB,
        type_="todo.session_resume",
        title=title,
        content=content,
        props={"todo_ids": [t["id"] for t in tasks]},
        category="normal",
        source="todo",
    )


# ── 树形展示辅助 ────────────────────────────────────────────


def build_todo_tree(todos: list[dict]) -> list[dict]:
    """将扁平 todo 列表转为树形结构。"""
    by_id: dict[int, dict] = {t["id"]: {**t, "children": []} for t in todos}
    roots: list[dict] = []
    for t in todos:
        node = by_id[t["id"]]
        parent_id = t.get("parent_id")
        if parent_id and parent_id in by_id:
            by_id[parent_id]["children"].append(node)
        else:
            roots.append(node)
    return roots


def format_todo_tree(
    nodes: list[dict], indent: str = "", prefix: str = ""
) -> list[str]:
    """递归输出 todo 树形文本。"""
    lines: list[str] = []
    for i, node in enumerate(nodes):
        is_last = i == len(nodes) - 1
        branch = "└── " if is_last else "├── "
        status_mark = {
            "pending": "○",
            "active": "●",
            "paused": "◐",
            "done": "✓",
            "cancelled": "✗",
        }.get(node.get("status", "pending"), "○")
        title = node.get("title", "")
        duration = format_duration(node.get("duration_s", 0))
        lines.append(f"{indent}{prefix}{branch}[{status_mark}] {title} ({duration})")
        children = node.get("children", [])
        if children:
            child_indent = indent + ("    " if is_last else "│   ")
            lines.extend(format_todo_tree(children, indent=child_indent))
    return lines


def format_active_todos(db_path: str) -> str:
    """生成 ACTIVE_TODOS 模板变量的内容。"""
    try:
        tasks = get_current_tasks(db_path)
    except Exception:
        return ""
    if not tasks:
        return ""
    lines = ["📋 进行中任务："]
    for t in tasks:
        activated_at = _parse_iso(t.get("activated_at"))
        elapsed_s = 0
        if activated_at:
            elapsed_s = int((datetime.now(timezone.utc) - activated_at).total_seconds())
        remaining_s = max(0, t["duration_s"] - elapsed_s)
        lines.append(
            f"  • {t['title']} — 已进行 {format_duration(elapsed_s)}，"
            f"剩余 {format_duration(remaining_s)}"
        )
    return "\n".join(lines)
