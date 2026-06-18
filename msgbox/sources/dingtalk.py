"""DingTalk source — polls dws CLI for notifications and feeds into msgbox DB."""

import json
import logging
import subprocess
import threading
import time
from datetime import datetime, timezone
from typing import Any

from .. import config
from .. import db as central_db
from ..filter import classify_message

logger = logging.getLogger("msgbox.sources.dingtalk")


def _dws(args: list[str]) -> list[dict] | dict | None:
    """Run a dws command and parse JSON output."""
    try:
        result = subprocess.run(
            ["dws"] + args + ["-f", "json", "-y"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            logger.debug(f"dws {' '.join(args)}: {result.stderr.strip()[:200]}")
            return None
        data = json.loads(result.stdout)
        return data
    except (json.JSONDecodeError, subprocess.TimeoutExpired, Exception) as exc:
        logger.debug(f"dws {' '.join(args)}: {exc}")
        return None


def _extract_items(data: Any) -> list[dict]:
    """Extract items/list from dws response (which may be wrapped)."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        # Common wrappers: {items: [...]}, {result: [...]}, {list: [...]}, {data: [...]}
        for key in ("items", "result", "list", "data", "records"):
            val = data.get(key)
            if isinstance(val, list):
                return val
        # Maybe it's a single item
        if "id" in data or "processInstanceId" in data:
            return [data]
    return []


# ── Pollers ──────────────────────────────────────────────


def poll_pending_approvals() -> list[dict]:
    """OA 待审批"""
    data = _dws(["oa", "approval", "list-pending"])
    return _extract_items(data)


def poll_cc_approvals() -> list[dict]:
    """OA 抄送我"""
    data = _dws(["oa", "approval", "list-cc"])
    return _extract_items(data)


def poll_mentions() -> list[dict]:
    """@我的消息"""
    data = _dws(["chat", "message", "list-mentions"])
    return _extract_items(data)


def poll_unread_conversations() -> list[dict]:
    """未读会话"""
    data = _dws(["chat", "message", "list-unread-conversations"])
    return _extract_items(data)


def poll_todo() -> list[dict]:
    """待办任务"""
    data = _dws(["todo", "list"])
    return _extract_items(data)


def poll_inbox_reports() -> list[dict]:
    """收到的日志/周报"""
    data = _dws(["report", "inbox", "list"])
    return _extract_items(data)


# ── Mappers ──────────────────────────────────────────────


def map_pending_approval(item: dict) -> dict | None:
    """审批 → msgbox 消息"""
    instance_id = item.get("processInstanceId") or item.get("businessId", "")
    title = item.get("title") or item.get("processCode", "审批")
    content = item.get("originatorName", "未知") + " 提交了审批"
    if item.get("formValues"):
        try:
            vals = json.loads(item["formValues"]) if isinstance(item["formValues"], str) else item["formValues"]
            if isinstance(vals, list) and len(vals) > 0:
                content += ": " + vals[0].get("value", "")
        except Exception:
            pass
    return {
        "type": "dingtalk.approval",
        "title": f"[待审批] {title}",
        "content": content[:300],
        "props": {
            "instanceId": instance_id,
            "source": "dingtalk",
            "dingtalk_type": "pending_approval",
        },
    }


def map_cc_approval(item: dict) -> dict | None:
    """抄送审批 → msgbox 消息"""
    title = item.get("title") or "审批抄送"
    originator = item.get("originatorName", "未知")
    return {
        "type": "dingtalk.approval_cc",
        "title": f"[抄送] {title}",
        "content": f"{originator} 提交的审批抄送了你",
        "props": {
            "instanceId": item.get("processInstanceId", ""),
            "source": "dingtalk",
            "dingtalk_type": "cc_approval",
        },
    }


def map_mention(item: dict) -> dict | None:
    """@消息 → msgbox 消息"""
    sender = item.get("senderNick") or item.get("senderId", "未知")
    conversation = item.get("conversationTitle") or item.get("conversationId", "聊天")
    content = item.get("textContent") or item.get("content", "")
    return {
        "type": "dingtalk.mention",
        "title": f"[@{sender}] 在 {conversation} 提到了你",
        "content": content[:300],
        "props": {
            "senderId": item.get("senderId", ""),
            "senderNick": sender,
            "conversationId": item.get("conversationId", ""),
            "conversationTitle": conversation,
            "msgId": item.get("msgId", ""),
            "source": "dingtalk",
            "dingtalk_type": "mention",
        },
    }


def map_unread_conversation(item: dict) -> dict | None:
    """未读会话 → msgbox 消息"""
    title = item.get("title") or item.get("conversationTitle", "未读会话")
    unread = item.get("unreadCount", 0)
    last_msg = item.get("lastMessage") or item.get("lastMsg", {})
    last_content = ""
    if isinstance(last_msg, dict):
        last_content = last_msg.get("textContent") or last_msg.get("content", "") or json.dumps(last_msg)[:100]
    return {
        "type": "dingtalk.unread",
        "title": f"[未读] {title} ({unread}条)",
        "content": last_content[:300] if last_content else f"{unread} 条未读消息",
        "props": {
            "conversationId": item.get("conversationId", ""),
            "title": title,
            "unreadCount": str(unread),
            "source": "dingtalk",
            "dingtalk_type": "unread",
        },
    }


def map_todo(item: dict) -> dict | None:
    """待办 → msgbox 消息"""
    title = item.get("title") or item.get("subject", "待办")
    content = item.get("description") or ""
    return {
        "type": "dingtalk.todo",
        "title": f"[待办] {title}",
        "content": content[:300] if content else "有一条待办任务",
        "props": {
            "taskId": item.get("taskId", ""),
            "source": "dingtalk",
            "dingtalk_type": "todo",
        },
    }


def map_report(item: dict) -> dict | None:
    """日志/周报 → msgbox 消息"""
    title = item.get("title") or item.get("templateName", "日志")
    creator = item.get("creatorName") or item.get("senderName", "未知")
    return {
        "type": "dingtalk.report",
        "title": f"[日志] {creator} 提交了 {title}",
        "content": item.get("content", "")[:300] or f"{creator} 提交了日志",
        "props": {
            "reportId": item.get("reportId", ""),
            "source": "dingtalk",
            "dingtalk_type": "report",
        },
    }


# ── Dedup ────────────────────────────────────────────────


def _dedup_key(msg: dict) -> str:
    """Generate dedup key from a message."""
    props = msg.get("props", {})
    dingtalk_type = props.get("dingtalk_type", "")
    if dingtalk_type == "pending_approval":
        return f"approval:{props.get('instanceId', '')}"
    if dingtalk_type == "cc_approval":
        return f"cc:{props.get('instanceId', '')}"
    if dingtalk_type == "mention":
        return f"mention:{props.get('msgId', '')}"
    if dingtalk_type == "unread":
        return f"unread:{props.get('conversationId', '')}"
    if dingtalk_type == "todo":
        return f"todo:{props.get('taskId', '')}"
    if dingtalk_type == "report":
        return f"report:{props.get('reportId', '')}"
    return ""


# ── Poller loop ──────────────────────────────────────────


def _poll_and_insert(poll_fn, mapper_fn, seen_keys: set[str], poll_name: str):
    """Run a single poll cycle for one source."""
    try:
        items = poll_fn()
        if not items:
            return
        for item in items:
            msg = mapper_fn(item)
            if msg is None:
                continue
            key = _dedup_key(msg)
            if key and key in seen_keys:
                continue

            category = "popup" if msg["type"] in (
                "dingtalk.approval", "dingtalk.mention", "dingtalk.todo"
            ) else "normal"
            try:
                msg_id = central_db.insert_message(
                    config.CENTRAL_DB,
                    type_=msg["type"],
                    title=msg["title"],
                    content=msg["content"],
                    props=msg.get("props", {}),
                    category=category,
                    source="dingtalk",
                )
                if msg_id:
                    if key:
                        seen_keys.add(key)
                    logger.info(f"DingTalk #{msg_id}: [{msg['type']}] {msg['title']} ({category})")
            except Exception as exc:
                logger.debug(f"DingTalk insert error: {exc}")
    except Exception as exc:
        logger.warning(f"DingTalk {poll_name} error: {exc}")


def poll_dingtalk(interval: int, stop_event: threading.Event):
    """Main polling loop for all DingTalk sources."""
    central_db.init_central_db(config.CENTRAL_DB)

    seen_keys: set[str] = set()

    pollers = [
        ("pending_approvals", poll_pending_approvals, map_pending_approval),
        ("cc_approvals", poll_cc_approvals, map_cc_approval),
        ("mentions", poll_mentions, map_mention),
        ("unread", poll_unread_conversations, map_unread_conversation),
        ("todo", poll_todo, map_todo),
        ("reports", poll_inbox_reports, map_report),
    ]

    # Warmup: mark all existing as seen on first run
    logger.info("DingTalk source warming up...")
    for poll_name, poll_fn, mapper_fn in pollers:
        try:
            items = poll_fn()
            if items:
                logger.info(f"  {poll_name}: {len(items)} items")
                for item in items:
                    msg = mapper_fn(item)
                    if msg:
                        key = _dedup_key(msg)
                        if key:
                            seen_keys.add(key)
        except Exception as exc:
            logger.debug(f"  {poll_name}: {exc}")
    logger.info(f"DingTalk source warmup done, {len(seen_keys)} known items")

    # Polling loop
    while not stop_event.is_set():
        for poll_name, poll_fn, mapper_fn in pollers:
            _poll_and_insert(poll_fn, mapper_fn, seen_keys, poll_name)
        stop_event.wait(interval)


def run_dingtalk_source(interval: int = 60, foreground: bool = True):
    """Start the DingTalk notification poller."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    logger.info(f"DingTalk source starting (interval={interval}s)")
    stop_event = threading.Event()
    t = threading.Thread(
        target=poll_dingtalk,
        args=(interval, stop_event),
        daemon=True,
    )
    t.start()

    if foreground:
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            logger.info("Shutting down DingTalk source...")
            stop_event.set()
    else:
        logger.info("DingTalk source started in background")
