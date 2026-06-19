"""DingTalk source — polls dws CLI for notifications and feeds into msgbox central DB."""

import json
import logging
import os
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from .. import config
from .. import db as central_db
from ..filter import classify_message

# 轮询间隔，默认 5 秒，可通过 DINGTALK_POLL_INTERVAL 环境变量覆盖
DEFAULT_POLL_INTERVAL = int(os.environ.get("DINGTALK_POLL_INTERVAL", "5"))

# 会话已读水位线 DB（本地持久化，因为钉钉 OpenAPI 没有提供"设置会话已读"接口）
_STATE_DB = str(config.DINGTALK_STATE_DB)
_state_local = threading.local()


def _state_conn() -> sqlite3.Connection:
    cached = getattr(_state_local, "conn", None)
    if cached is not None:
        return cached
    Path(_STATE_DB).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_STATE_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS conversation_watermarks (
            conversation_id TEXT PRIMARY KEY,
            last_read_time  TEXT NOT NULL DEFAULT '',
            updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )
    conn.commit()
    _state_local.conn = conn
    return conn


def _get_watermark(conv_id: str) -> str | None:
    if not conv_id:
        return None
    row = _state_conn().execute(
        "SELECT last_read_time FROM conversation_watermarks WHERE conversation_id = ?",
        (conv_id,),
    ).fetchone()
    return row["last_read_time"] if row else None


def _set_watermark(conv_id: str, last_read_time: str):
    if not conv_id or not last_read_time:
        return
    _state_conn().execute(
        """INSERT INTO conversation_watermarks (conversation_id, last_read_time, updated_at)
           VALUES (?, ?, datetime('now'))
           ON CONFLICT(conversation_id) DO UPDATE SET
               last_read_time = excluded.last_read_time,
               updated_at = datetime('now')""",
        (conv_id, last_read_time),
    )
    _state_conn().commit()


def _is_newer_than_watermark(conv_id: str, create_time: str) -> bool:
    """判断消息是否比本地已读水位线更新。首次无水位线时视为新消息。"""
    if not create_time:
        return False
    watermark = _get_watermark(conv_id)
    if watermark is None:
        return True
    return create_time > watermark


def _filter_new_messages(msgs: list[dict]) -> list[dict]:
    """过滤出比本地水位线新的消息，并按时间正序排列。"""
    new_msgs = [
        m for m in msgs
        if _is_newer_than_watermark(
            m.get("openConversationId", "") or m.get("_conversation_id", ""),
            m.get("createTime", ""),
        )
    ]
    new_msgs.sort(key=lambda m: m.get("createTime", ""))
    return new_msgs


def _update_watermark_from_messages(msgs: list[dict]):
    """根据处理过的消息更新各会话的已读水位线。"""
    by_conv: dict[str, str] = {}
    for m in msgs:
        conv_id = m.get("openConversationId", "") or m.get("_conversation_id", "")
        ts = m.get("createTime", "")
        if not conv_id or not ts:
            continue
        if ts > by_conv.get(conv_id, ""):
            by_conv[conv_id] = ts
    for conv_id, ts in by_conv.items():
        _set_watermark(conv_id, ts)

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
            for key in ("conversations", "messages", "groups", "items", "list", "data", "records"):
                val = result.get(key)
                if isinstance(val, list):
                    return val
        if isinstance(result, list):
            return result
        # Maybe it's a single item
        if "id" in data or "processInstanceId" in data:
            return [data]
    return []


# 当前登录用户缓存（用于过滤自己发出的消息）
_SELF_USER: dict[str, str] = {}


def _load_self_user() -> dict[str, str]:
    """加载当前登录用户信息（name / userId / openDingTalkId）。"""
    global _SELF_USER
    if _SELF_USER:
        return _SELF_USER

    data = _dws(["contact", "user", "get-self"])
    items = _extract_items(data)
    if not items:
        logger.warning("Failed to load DingTalk self user info")
        return {}

    model = items[0].get("orgEmployeeModel", {})
    name = model.get("orgUserName", "")
    user_id = model.get("userId", "") or model.get("orgUserId", "")
    open_id = ""

    # 通过 aisearch 获取当前用户的 openDingTalkId
    if name:
        search_data = _dws([
            "aisearch", "person",
            "--keyword", name,
            "--dimension", "name",
        ])
        search_items = _extract_items(search_data)
        for item in search_items:
            if item.get("openDingTalkId") and item.get("userId") == user_id:
                open_id = item["openDingTalkId"]
                break

    _SELF_USER = {
        "name": name,
        "userId": user_id,
        "openDingTalkId": open_id,
    }
    if _SELF_USER["name"]:
        logger.info(
            f"DingTalk self user: name={name}, userId={user_id}, "
            f"openDingTalkId={open_id[:20]}..."
        )
    return _SELF_USER


def _is_self_message(msg: dict) -> bool:
    """判断是否为当前登录用户自己发出的消息。"""
    if not _SELF_USER:
        return False

    props = msg.get("props", {})
    sender_open_id = props.get("senderOpenDingTalkId", "")
    sender_name = props.get("senderNick", "") or msg.get("sender", "")

    self_open_id = _SELF_USER.get("openDingTalkId", "")
    self_name = _SELF_USER.get("name", "")

    if self_open_id and sender_open_id == self_open_id:
        return True
    if self_name and sender_name == self_name:
        return True
    return False


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


def _ts_to_bj_str(ts_ms: int | None) -> str | None:
    """把毫秒时间戳转为北京时间字符串。"""
    if not ts_ms:
        return None
    try:
        bj_time = datetime.fromtimestamp(ts_ms / 1000, tz=timezone(timedelta(hours=8)))
        return bj_time.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _fetch_conversation_messages(conv: dict, limit: int | None = None) -> list[dict]:
    """根据会话信息拉取该会话的最新多条消息，并注入会话上下文。"""
    conv_id = conv.get("openConversationId", "")
    is_single = conv.get("singleChat", False)
    last_ts = conv.get("lastMsgCreateAt")
    title = conv.get("title", "")
    unread = conv.get("unreadPoint") or conv.get("unreadCount", 0)

    if not conv_id or not last_ts:
        return []

    time_str = _ts_to_bj_str(last_ts)
    if not time_str:
        return []

    # 有多少未读就拉多少；未读为 0 时兜底拉 1 条用于更新水位线
    fetch_limit = max(1, limit if limit is not None else (int(unread) if unread else 1))
    msgs: list[dict] = []
    try:
        if is_single:
            user_id = _resolve_user_id(title)
            if not user_id:
                return []
            msg_data = _dws([
                "chat", "message", "list-direct",
                "--user", user_id,
                "--time", time_str,
                "--forward", "false",
                "--limit", str(fetch_limit),
            ])
            if msg_data:
                msgs = _extract_items(msg_data)
                for m in msgs:
                    m["_user_id"] = user_id
        else:
            msg_data = _dws([
                "chat", "message", "list",
                "--group", conv_id,
                "--time", time_str,
                "--forward", "false",
                "--limit", str(fetch_limit),
            ])
            if msg_data:
                msgs = _extract_items(msg_data)
    except Exception:
        pass

    # 注入会话上下文，方便 mapper 和回复
    for m in msgs:
        m["_conversation_title"] = title
        m["_single_chat"] = is_single
        m["_unread_count"] = unread
        m["_conversation_id"] = conv_id
    return msgs


def poll_unread_conversations() -> list[dict]:
    """未读会话 — 拉取每个会话内的多条新增消息。"""
    data = _dws(["chat", "message", "list-unread-conversations"])
    items = _extract_items(data)
    messages: list[dict] = []
    for conv in items:
        # 按会话未读数拉取，保证 15s 内 n 条新消息全部拿到
        messages.extend(_fetch_conversation_messages(conv, limit=None))
    return _filter_new_messages(messages)


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
    messages: list[dict] = []
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    for conv_id in list(_KNOWN_GROUP_IDS):
        try:
            msg_data = _dws([
                "chat", "message", "list",
                "--group", conv_id,
                "--time", now_utc,
                "--forward", "false",
                "--limit", "50",
            ])
            if msg_data:
                msgs = _extract_items(msg_data)
                for m in msgs:
                    m["_conversation_title"] = _KNOWN_GROUP_TITLES.get(conv_id, "群聊")
                    m["_single_chat"] = False
                    m["_unread_count"] = 0
                    m["_conversation_id"] = conv_id
                messages.extend(msgs)
        except Exception:
            pass
    return _filter_new_messages(messages)


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


def _format_identifiers(props: dict) -> str:
    """把 DingTalk 唯一标识格式化成可复制的回复信息。"""
    parts = []
    conv_id = props.get("conversationId", "")
    msg_id = props.get("openMessageId", "")
    sender_id = props.get("senderOpenDingTalkId", "")
    user_id = props.get("userId", "")
    if user_id:
        parts.append(f"userId={user_id}")
    if sender_id:
        parts.append(f"senderOpenDingTalkId={sender_id}")
    if conv_id:
        parts.append(f"openConversationId={conv_id}")
    if msg_id:
        parts.append(f"openMessageId={msg_id}")
    return " | ".join(parts)


def _normalize_message_text(text: str) -> str:
    """把钉钉消息里常见的字面量转义序列还原为真实字符。

    钉钉服务端返回的文本中，换行符经常以字面量 \\n 形式存在
    （例如 Markdown 消息），这里统一还原为真实换行，
    让简报显示更自然。
    """
    return text.replace("\\n", "\n").replace("\\t", "\t").replace("\\r", "\r")


def map_direct_message(msg: dict) -> dict | None:
    """单聊私信 → msgbox 消息（逐条）"""
    title = msg.get("_conversation_title") or "私聊"
    sender = msg.get("sender") or "未知"
    content = _normalize_message_text((msg.get("content") or "").strip())
    conv_id = msg.get("openConversationId", "") or msg.get("_conversation_id", "")
    user_id = msg.get("_user_id", "")
    msg_id = msg.get("openMessageId", "")
    sender_open_id = msg.get("senderOpenDingTalkId", "")
    create_time = msg.get("createTime", "")

    # 标题展示消息内容摘要；正文展示完整消息 + 回复所需标识
    body_parts = []
    if create_time:
        body_parts.append(f"时间: {create_time}")
    if sender:
        body_parts.append(f"发送者: {sender}")
    if content:
        body_parts.append(f"内容: {content}")
    identifiers = _format_identifiers({
        "conversationId": conv_id,
        "openMessageId": msg_id,
        "userId": user_id,
        "senderOpenDingTalkId": sender_open_id,
    })
    if identifiers:
        body_parts.append(f"标识: {identifiers}")

    body = "\n".join(body_parts)
    title_summary = content[:60] if content else "收到一条私聊消息"
    if create_time:
        title_summary = f"{create_time}  {title_summary}"

    props = {
        "conversationId": conv_id,
        "conversationTitle": title,
        "openMessageId": msg_id,
        "userId": user_id,
        "senderNick": sender,
        "senderOpenDingTalkId": sender_open_id,
        "dingtalk_type": "direct",
        "source": "dingtalk",
    }
    return {
        "type": "dingtalk.direct",
        "title": f"[私聊] {title} — {title_summary}",
        "content": body[:500] if body else "收到一条私聊消息",
        "props": props,
    }


def map_group_message(msg: dict) -> dict | None:
    """群聊消息 → msgbox 消息（逐条）"""
    title = msg.get("_conversation_title") or "群聊"
    sender = msg.get("sender") or "未知"
    content = _normalize_message_text((msg.get("content") or "").strip())
    conv_id = msg.get("openConversationId", "") or msg.get("_conversation_id", "")
    msg_id = msg.get("openMessageId", "")
    sender_open_id = msg.get("senderOpenDingTalkId", "")
    create_time = msg.get("createTime", "")

    body_parts = []
    if create_time:
        body_parts.append(f"时间: {create_time}")
    if sender:
        body_parts.append(f"发送者: {sender}")
    if content:
        body_parts.append(f"内容: {content}")
    identifiers = _format_identifiers({
        "conversationId": conv_id,
        "openMessageId": msg_id,
        "senderOpenDingTalkId": sender_open_id,
    })
    if identifiers:
        body_parts.append(f"标识: {identifiers}")

    body = "\n".join(body_parts)
    title_summary = content[:60] if content else "收到一条群聊消息"
    if create_time:
        title_summary = f"{create_time}  {title_summary}"

    props = {
        "conversationId": conv_id,
        "conversationTitle": title,
        "openMessageId": msg_id,
        "senderNick": sender,
        "senderOpenDingTalkId": sender_open_id,
        "dingtalk_type": "group",
        "source": "dingtalk",
    }
    return {
        "type": "dingtalk.group",
        "title": f"[群聊] {title} — {title_summary}",
        "content": body[:500] if body else "收到一条群聊消息",
        "props": props,
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
    if dingtalk_type in ("direct", "group"):
        return f"{dingtalk_type}:{props.get('openMessageId', '')}"
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


def _insert_conversation_messages(messages: list[dict], seen_keys: set[str]):
    """Insert individual conversation messages into DB, then update read watermark."""
    inserted: list[dict] = []
    for msg in messages:
        is_single = msg.get("_single_chat", False)
        if not is_single:
            # Register group for future polling
            cid = msg.get("openConversationId", "") or msg.get("_conversation_id", "")
            title = msg.get("_conversation_title", "群聊")
            _register_group(cid, title)

        mapper = map_direct_message if is_single else map_group_message
        mapped = mapper(msg)
        if mapped is None:
            continue

        if _is_self_message(mapped):
            logger.debug(f"Skipping self message: {mapped['title'][:60]}")
            continue

        key = _dedup_key(mapped)
        if key and key in seen_keys:
            continue

        category = "normal"
        try:
            msg_id = central_db.insert_message(
                config.CENTRAL_DB,
                type_=mapped["type"],
                title=mapped["title"],
                content=mapped["content"],
                props=mapped.get("props", {}),
                category=category,
                source="dingtalk",
            )
            if msg_id:
                if key:
                    seen_keys.add(key)
                inserted.append(msg)
                logger.info(f"DingTalk #{msg_id}: [{mapped['type']}] {mapped['title']} ({category})")
        except Exception as exc:
            logger.debug(f"DingTalk insert error: {exc}")

    # 插入成功后再更新已读水位线，下次不再重复拉取
    _update_watermark_from_messages(inserted)


def _poll_unread(seen_keys: set[str]):
    """Poll unread conversations."""
    try:
        messages = poll_unread_conversations()
        if messages:
            _insert_conversation_messages(messages, seen_keys)
    except Exception as exc:
        logger.warning(f"DingTalk unread error: {exc}")


def _poll_known_groups(seen_keys: set[str]):
    """Poll known group chats (catch groups not in unread list)."""
    try:
        messages = poll_known_groups()
        if messages:
            _insert_conversation_messages(messages, seen_keys)
    except Exception as exc:
        logger.warning(f"DingTalk known groups error: {exc}")


def poll_dingtalk(interval: int, stop_event: threading.Event):
    """Main polling loop for all DingTalk sources."""
    central_db.init_central_db(config.CENTRAL_DB)
    _init_known_groups()

    # 加载当前登录用户信息，用于过滤自己发出的消息
    _load_self_user()

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

    logger.info(f"DingTalk source starting (interval={interval}s, default={DEFAULT_POLL_INTERVAL}s)")
    stop_event = threading.Event()
    t = threading.Thread(
        target=poll_dingtalk,
        args=(interval, stop_event),
        daemon=False,
    )
    t.start()

    if foreground:
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            logger.info("Shutting down DingTalk source...")
            stop_event.set()
            t.join(timeout=5)
    else:
        # background 模式下保持主进程存活，避免 daemon 线程被回收
        logger.info("DingTalk source started in background")
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            logger.info("Shutting down DingTalk source...")
            stop_event.set()
            t.join(timeout=5)
