"""MCP Channel server for msgbox — push message notifications to Claude Code."""

import asyncio
import logging
import sys
from pathlib import Path

from mcp.server.lowlevel import Server, NotificationOptions
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCNotification, JSONRPCMessage

from . import config, db as central_db

logger = logging.getLogger(__name__)

CURSOR_FILE = config.PLUGIN_DIR / "channel_cursor"
CHANNEL_CATEGORIES = ("normal", "popup", "silent")
POLL_INTERVAL = 1.5  # seconds between DB polls


def _read_cursor() -> int:
    try:
        return int(CURSOR_FILE.read_text().strip())
    except (FileNotFoundError, ValueError, OSError):
        return 0


def _write_cursor(cursor: int):
    config.PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
    CURSOR_FILE.write_text(str(cursor))


def _init_cursor() -> int:
    """Initialize cursor from global max message id."""
    max_id = central_db.get_max_message_id(config.CENTRAL_DB)
    _write_cursor(max_id)
    return max_id


def _build_channel_text(msg: dict) -> str:
    """Format a DB message row into channel notification content."""
    content_parts = [f"[#{msg['id']}]"]
    if msg["title"]:
        content_parts.append(msg["title"])
    if msg["content"]:
        content_parts.append(msg["content"])
    return ": ".join(content_parts)


async def poll_loop(write_stream):
    """Poll central DB and push new messages via MCP channel notification."""
    central_db.init_central_db(config.CENTRAL_DB)

    cursor = _read_cursor()
    if cursor == 0:
        cursor = _init_cursor()

    logger.info("channel poll starting, cursor=%d", cursor)

    while True:
        try:
            msgs = central_db.get_messages_after(
                config.CENTRAL_DB, cursor, CHANNEL_CATEGORIES, limit=10,
            )
        except Exception:
            msgs = []

        if msgs:
            for msg in msgs:
                meta = {
                    "id": str(msg["id"]),
                    "type": msg["type"] or "",
                    "category": msg["category"] or "normal",
                }
                if msg.get("source"):
                    meta["source"] = msg["source"]

                notif = JSONRPCNotification(
                    jsonrpc="2.0",
                    method="notifications/claude/channel",
                    params={"content": _build_channel_text(msg), "meta": meta},
                )
                await write_stream.send(SessionMessage(message=JSONRPCMessage(notif)))

            new_cursor = max(m["id"] for m in msgs)
            if new_cursor > cursor:
                cursor = new_cursor
                _write_cursor(cursor)

        await asyncio.sleep(POLL_INTERVAL)


async def channel_main():
    """Channel server entry point."""
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )

    server = Server(
        "msgbox",
        version="0.1.0",
        instructions=(
            'Messages from msgbox channel arrive as <channel source="msgbox" ...>. '
            "Read the content and act on it. No reply tool available."
        ),
    )

    opts = server.create_initialization_options(
        notification_options=NotificationOptions(),
        experimental_capabilities={"claude/channel": {}},
    )

    async with stdio_server() as (read_stream, write_stream):
        async with asyncio.TaskGroup() as tg:
            tg.create_task(server.run(read_stream, write_stream, opts))
            tg.create_task(poll_loop(write_stream))


def cmd_channel(args):
    """CLI entry point for 'msgbox channel'."""
    asyncio.run(channel_main())
