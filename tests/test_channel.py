"""Tests for MCP Channel server — cursor persistence and message push logic."""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from msgbox import config, db as central_db
from msgbox.channel import (
    _read_cursor,
    _write_cursor,
    _init_cursor,
    poll_loop,
)
from mcp.types import JSONRPCNotification, JSONRPCMessage
from mcp.shared.message import SessionMessage


@pytest.fixture
def temp_cursor_dir():
    """Patch CURSOR_FILE into a temp directory so tests don't touch real plugin dir."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        fake_cursor = tmp_dir / "channel_cursor"
        with patch("msgbox.channel.CURSOR_FILE", fake_cursor):
            yield tmp_dir, fake_cursor


@pytest.fixture
def temp_central_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = Path(f.name)
    with patch.object(config, "CENTRAL_DB", path):
        central_db.init_central_db(str(path))
        yield str(path)
    central_db._local.conn = None
    central_db._local.conn_path = None
    if path.exists():
        path.unlink()


# ── Cursor persistence ────────────────────────────────────────


class TestCursorPersistence:
    def test_read_write_cursor(self, temp_cursor_dir):
        assert _read_cursor() == 0, "empty file should return 0"
        _write_cursor(42)
        assert _read_cursor() == 42
        _write_cursor(0)
        assert _read_cursor() == 0

    def test_cursor_file_created(self, temp_cursor_dir):
        tmp_dir, fake_cursor = temp_cursor_dir
        _write_cursor(99)
        assert fake_cursor.exists()
        assert fake_cursor.read_text().strip() == "99"

    def test_corrupted_cursor_returns_zero(self, temp_cursor_dir):
        _, fake_cursor = temp_cursor_dir
        fake_cursor.write_text("not-a-number")
        assert _read_cursor() == 0

    def test_missing_cursor_returns_zero(self, temp_cursor_dir):
        assert _read_cursor() == 0


class TestCursorInit:
    def test_init_from_empty_db(self, temp_central_db, temp_cursor_dir):
        cursor = _init_cursor()
        assert cursor == 0
        assert _read_cursor() == 0

    def test_init_from_nonempty_db(self, temp_central_db, temp_cursor_dir):
        central_db.insert_message(str(config.CENTRAL_DB), "t", "a", "b")
        central_db.insert_message(str(config.CENTRAL_DB), "t", "c", "d")
        cursor = _init_cursor()
        assert cursor == 2
        assert _read_cursor() == 2


# ── Poll loop: message discovery and push ─────────────────────


class TestPollLoop:
    @pytest.mark.asyncio
    async def _run_poll_one_cycle(self, write_stream, duration=0.35):
        task = asyncio.create_task(poll_loop(write_stream))
        await asyncio.sleep(duration)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    @pytest.mark.asyncio
    async def test_poll_no_new_messages(self, temp_central_db, temp_cursor_dir):
        central_db.insert_message(str(config.CENTRAL_DB), "t", "Dummy", "", category="normal")
        _write_cursor(1)

        sent = []

        async def capture_send(msg):
            sent.append(msg)

        write_stream = MagicMock()
        write_stream.send = capture_send

        await self._run_poll_one_cycle(write_stream)
        assert sent == []

    @pytest.mark.asyncio
    async def test_poll_new_messages_triggered(self, temp_central_db, temp_cursor_dir):
        central_db.insert_message(str(config.CENTRAL_DB), "dummy", "X", "", category="normal")
        _write_cursor(1)
        central_db.insert_message(
            str(config.CENTRAL_DB), "test.type", "Test Title", "Test Content",
            category="normal",
        )

        sent = []

        async def capture_send(msg):
            sent.append(msg)

        write_stream = MagicMock()
        write_stream.send = capture_send

        await self._run_poll_one_cycle(write_stream)

        assert len(sent) == 1
        sm = sent[0]
        assert isinstance(sm, SessionMessage)
        notif = sm.message.root
        assert isinstance(notif, JSONRPCNotification)
        assert notif.method == "notifications/claude/channel"
        assert notif.params["content"] == "[#2]: Test Title: Test Content"
        assert notif.params["meta"]["id"] == "2"
        assert notif.params["meta"]["type"] == "test.type"
        assert notif.params["meta"]["category"] == "normal"

    @pytest.mark.asyncio
    async def test_poll_updates_cursor(self, temp_central_db, temp_cursor_dir):
        central_db.insert_message(str(config.CENTRAL_DB), "dummy", "X", "", category="normal")
        _write_cursor(1)
        central_db.insert_message(str(config.CENTRAL_DB), "t", "A", "B", category="normal")
        central_db.insert_message(str(config.CENTRAL_DB), "t", "C", "D", category="normal")

        sent = []

        async def capture_send(msg):
            sent.append(msg)

        write_stream = MagicMock()
        write_stream.send = capture_send

        await self._run_poll_one_cycle(write_stream)

        assert len(sent) == 2
        assert _read_cursor() == 3

    @pytest.mark.asyncio
    async def test_poll_popup_category(self, temp_central_db, temp_cursor_dir):
        central_db.insert_message(str(config.CENTRAL_DB), "dummy", "X", "", category="normal")
        _write_cursor(1)
        central_db.insert_message(
            str(config.CENTRAL_DB), "alert", "Urgent!", "Something happened",
            category="popup",
        )

        sent = []

        async def capture_send(msg):
            sent.append(msg)

        write_stream = MagicMock()
        write_stream.send = capture_send

        await self._run_poll_one_cycle(write_stream)

        assert len(sent) == 1
        notif = sent[0].message.root
        assert notif.params["meta"]["category"] == "popup"

    @pytest.mark.asyncio
    async def test_poll_incremental_discovery(self, temp_central_db, temp_cursor_dir):
        central_db.insert_message(str(config.CENTRAL_DB), "dummy", "X", "", category="normal")
        _write_cursor(1)

        sent = []

        async def capture_send(msg):
            sent.append(msg)

        write_stream = MagicMock()
        write_stream.send = capture_send

        central_db.insert_message(str(config.CENTRAL_DB), "t", "First", "", category="normal")
        await self._run_poll_one_cycle(write_stream)
        assert len(sent) >= 1
        assert _read_cursor() >= 2
        first_len = len(sent)

        central_db.insert_message(str(config.CENTRAL_DB), "t", "Second", "", category="normal")
        await self._run_poll_one_cycle(write_stream)
        assert len(sent) >= first_len + 1
        assert _read_cursor() >= 3
