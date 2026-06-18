"""DingTalk source — polls dws CLI for notifications and feeds into msgbox DB."""

import json
import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Any

from .. import config
from .. import db as central_db
from ..filter import classify_message

# 轮询间隔，默认 15 秒，可通过 DINGTALK_POLL_INTERVAL 环境变量覆盖
DEFAULT_POLL_INTERVAL = int(os.environ.get("DINGTALK_POLL_INTERVAL", "15"))

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
        # Common wrappers: {items: [...]}, {result: [...]}, {list: [...]}, {data: [...]}, {records: [...]}
        for key in ("items", "list", "data", "records"):
            val = data.get(key)
            if isinstance(val, list):
                return val
        # {result: {conversations: [...]}} or {result: [...]} or {result: {messages: [...]}}
        result = data.get("result")
        if isinstance(result, dict):
            for key in ("conversations", "messages", "items", "list", "data", "records"):
                val = result.get(key)
                if isinstance(val, list):
                    return val
        if isinstance(result, list):
            return result
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
    """@我的消息（最近7天）"""
    now = int(time.time() * 1000)
    seven_days_ago = now - 7 * 86400 * 1000
    data = _dws(["chat", "message", "list-mentions", "--start", str(seven_days_ago), "--end", str(now)])
    return _extract_items(data)


def _resolve_user_id(title: str) -> str | None:
    """Resolve a contact name/phone to userId via dws contact search."""
    try:
        data = _dws(["contact", "user", "search", "--keyword", title])
        if data:
            users = _extract_items(data)
            if users:
                return users[0].get("userId", "")
    except Exception:
        pass
    return None


def poll_unread_conversations() -> list[dict]:
    """未读会话 — 同时拉取最新消息内容"""
    data = _dws(["chat", "message", "list-unread-conversations"])
    items = _extract_items(data)

    # Fetch latest message for each conversation using lastMsgCreateAt
    for item in items:
        conv_id = item.get("openConversationId", "")
        is_single = item.get("singleChat", False)
        last_ts = item.get("lastMsgCreateAt")
        if not conv_id or not last_ts:
            continue
        bj_time = datetime.fromtimestamp(last_ts / 1000, tz=timezone(timedelta(hours=8)))
        time_str = bj_time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            if is_single:
                title = item.get("title", "")
                user_id = _resolve_user_id(title)
                if not user_id:
                    continue
                msg_data = _dws(["chat", "message", "list-direct", "--user", user_id, "--time", time_str, "--forward", "false", "--limit", "1"])
                if msg_data:
                    msgs = _extract_items(msg_data)
                    if msgs:
                        newest = msgs[0]
                        item["_latest_content"] = (newest.get("content") or "")[:300]
                        item["_latest_sender"] = newest.get("sender", "")
                        item["_user_id"] = user_id
            else:
                msg_data = _dws(["chat", "message", "list", "--group", conv_id, "--time", time_str, "--forward", "false", "--limit", "1"])
                if msg_data:
                    msgs = _extract_items(msg_data)
                    if msgs:
                        newest = msgs[0]
                        item["_latest_content"] = (newest.get("content") or "")[:300]
                        item["_latest_sender"] = newest.get("sender", "")
        except Exception:
            pass
    return items


# 已知群聊列表（运行时积累，每次轮询拉取最新消息）
_KNOWN_GROUP_IDS: set[str] = set()
_KNOWN_GROUP_TITLES: dict[str, str] = {}


def _register_group(conv_id: str, title: str):
    """注册一个群聊，下次轮询会主动拉取它的最新消息。"""
    if conv_id:
        _KNOWN_GROUP_IDS.add(conv_id)
        if title:
            _KNOWN_GROUP_TITLES[conv_id] = title


def _init_known_groups():
    """初始化已知群聊（从 chat search 查找相关群聊）。"""
    try:
        # Search for groups containing our name
        data = _dws(["chat", "search", "--query", "Alkaid"])
        if data:
            for g in (_extract_items(data) or []):
                cid = g.get("openConversationId", "")
                if cid:
                    _register_group(cid, g.get("title", "群聊"))
    except Exception:
        pass


def poll_known_groups() -> list[dict]:
    """从已知群聊列表拉取最新消息"""
    items = []
    for conv_id in list(_KNOWN_GROUP_IDS):
        try:
            # Use current UTC time with forward=false (newest-first) and limit 5
            now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            msg_data = _dws(["chat", "message", "list", "--group", conv_id, "--time", now_utc, "--forward", "false", "--limit", "5"])
            if msg_data:
                msgs = _extract_items(msg_data)
                if msgs:
                    m = msgs[0]
                    items.append({
                        "openConversationId": conv_id,
                        "title": _KNOWN_GROUP_TITLES.get(conv_id, "群聊"),
                        "singleChat": False,
                        "_latest_content": (m.get("content") or "")[:300],
                        "_latest_sender": m.get("sender", ""),
                    })
        except Exception:
            pass
    return items


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
    sender_id = item.get("senderId", "")
    conv_id = item.get("conversationId", "")
    msg_id = item.get("msgId", "")
    return {
        "type": "dingtalk.mention",
        "title": f"[@{sender}] {conversation}",
        "content": content[:300],
        "props": {
            "senderId": sender_id,
            "senderNick": sender,
            "conversationId": conv_id,
            "conversationTitle": conversation,
            "msgId": msg_id,
            "source": "dingtalk",
            "dingtalk_type": "mention",
        },
    }


def map_direct_message(item: dict) -> dict | None:
    """单聊私信 → msgbox 消息"""
    title = item.get("title") or "私聊"
    sender = item.get("_latest_sender", "")
    content = item.get("_latest_content", "")
    conv_id = item.get("openConversationId", "")
    user_id = item.get("_user_id", "")
    unread = item.get("unreadPoint") or item.get("unreadCount", 0)
    if not content:
        content = f"{unread} 条未读消息"
    return {
        "type": "dingtalk.direct",
        "title": f"[私聊] {title} ({unread})",
        "content": f"[{sender}] {content}"[:300] if sender else content[:300],
        "props": {
            "conversationId": conv_id,
            "userId": user_id,
            "senderNick": title,
            "unreadCount": str(unread),
            "source": "dingtalk",
            "dingtalk_type": "direct",
        },
    }


def map_group_message(item: dict) -> dict | None:
    """群聊消息 → msgbox 消息"""
    title = item.get("title") or "群聊"
    sender = item.get("_latest_sender", "")
    content = item.get("_latest_content", "")
    conv_id = item.get("openConversationId", "")
    unread = item.get("unreadPoint") or item.get("unreadCount", 0)
    if not content:
        content = f"{unread} 条未读消息"
    return {
        "type": "dingtalk.group",
        "title": f"[群聊] {title} ({unread})",
        "content": f"[{sender}] {content}"[:300] if sender else content[:300],
        "props": {
            "conversationId": conv_id,
            "unreadCount": str(unread),
            "source": "dingtalk",
            "dingtalk_type": "group",
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
    if dingtalk_type == "direct":
        return f"direct:{props.get('conversationId', '')}:{props.get('unreadCount', '0')}"
    if dingtalk_type == "group":
        return f"group:{props.get('conversationId', '')}:{props.get('unreadCount', '0')}"
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


def _poll_unread(seen_keys: set[str]):
    """Poll unread conversations."""
    try:
        items = poll_unread_conversations()
        if not items:
            return
        _insert_conversation_items(items, seen_keys)
    except Exception as exc:
        logger.warning(f"DingTalk unread error: {exc}")


def _poll_known_groups(seen_keys: set[str]):
    """Poll known group chats (catch groups not in unread list)."""
    try:
        items = poll_known_groups()
        if not items:
            return
        _insert_conversation_items(items, seen_keys)
    except Exception as exc:
        logger.warning(f"DingTalk known groups error: {exc}")


def _insert_conversation_items(items: list[dict], seen_keys: set[str]):
    """Insert conversation items (direct/group) into DB."""
    for item in items:
        is_single = item.get("singleChat", False)
        if not is_single:
            # Register group for future polling
            cid = item.get("openConversationId", "")
            title = item.get("title", "群聊")
            _register_group(cid, title)
        if is_single:
            msg = map_direct_message(item)
        else:
            msg = map_group_message(item)
        if msg is None:
            continue
        key = _dedup_key(msg)
        if key and key in seen_keys:
            continue
        category = "normal"
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


def poll_dingtalk(interval: int, stop_event: threading.Event):
    """Main polling loop for all DingTalk sources."""
    central_db.init_central_db(config.CENTRAL_DB)
    _init_known_groups()

    seen_keys: set[str] = set()
    _init_known_groups()

    pollers: list[tuple[str, Any, Any]] = [
        ("pending_approvals", poll_pending_approvals, map_pending_approval),
        ("cc_approvals", poll_cc_approvals, map_cc_approval),
        ("mentions", poll_mentions, map_mention),
        ("todo", poll_todo, map_todo),
        ("reports", poll_inbox_reports, map_report),
    ]

    # First run: init known groups and insert all items
    _init_known_groups()
    logger.info("DingTalk source first run...")
    for poll_name, poll_fn, mapper_fn in pollers:
        _poll_and_insert(poll_fn, mapper_fn, seen_keys, poll_name)
    _poll_unread(seen_keys)
    _poll_known_groups(seen_keys)

    # Polling loop
    while not stop_event.is_set():
        for poll_name, poll_fn, mapper_fn in pollers:
            _poll_and_insert(poll_fn, mapper_fn, seen_keys, poll_name)
        _poll_unread(seen_keys)
        _poll_known_groups(seen_keys)
        stop_event.wait(interval)


def run_dingtalk_source(interval: int | None = None, foreground: bool = True):
    """Start the DingTalk notification poller."""
    if interval is None or interval <= 0:
        interval = DEFAULT_POLL_INTERVAL
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
