"""GitHub Inbox source — polls GitHub Notifications API and feeds into msgbox central DB.

Architecture:
    GitHub Notifications API → gh api notifications → inbox poller → msgbox DB

Usage:
    msgbox source-inbox [--interval SECONDS]
"""

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

logger = logging.getLogger("msgbox.sources.inbox")

_NOTIFICATION_TYPES = {
    "Issue": "github.issue",
    "PullRequest": "github.pr",
    "Discussion": "github.discussion",
    "RepositoryInvitation": "github.invitation",
    "Release": "github.release",
    "CheckSuite": "github.check_suite",
    "CheckRun": "github.check_run",
    "WorkflowRun": "github.workflow_run",
}

_REASON_CATEGORY = {
    "mention": "popup",
    "assign": "popup",
    "review_requested": "popup",
    "author": "normal",
    "comment": "normal",
    "subscribed": "silent",
    "manual": "silent",
    "team_mention": "popup",
    "state_change": "normal",
    "security_alert": "popup",
    "invitation": "popup",
}


def _get_self_user() -> str:
    """Detect bot's GitHub username."""
    try:
        result = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def _fetch_notifications(since: str | None = None) -> list[dict]:
    """Fetch notifications from GitHub API.

    Args:
        since: ISO timestamp — only fetch notifications updated after this time.

    Returns:
        List of notification dicts.
    """
    args = ["gh", "api", "--method", "GET", "/notifications", "--jq", "."]
    if since:
        args += ["--raw-field", f"since={since}"]

    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            logger.warning(f"gh api notifications failed: {result.stderr.strip()}")
            return []
        notifications = json.loads(result.stdout)
        if not isinstance(notifications, list):
            return []
        return notifications
    except (json.JSONDecodeError, subprocess.TimeoutExpired, Exception) as exc:
        logger.warning(f"Failed to fetch notifications: {exc}")
        return []


def _map_notification(n: dict) -> dict | None:
    """Map a GitHub notification to a msgbox message dict."""
    repo = n.get("repository", {}).get("full_name", "unknown")
    subject = n.get("subject", {})
    title = subject.get("title", "")
    raw_type = subject.get("type", "")
    reason = n.get("reason", "")
    updated_at = n.get("updated_at", "")
    url = subject.get("url", "")

    # Map type
    msg_type = _NOTIFICATION_TYPES.get(raw_type, f"github.{raw_type.lower()}")

    # Determine category based on reason
    category = _REASON_CATEGORY.get(reason, "normal")

    # Build content
    content_parts = [f"{reason} on {repo}"]
    if raw_type == "Issue":
        # Extract issue/PR number from URL
        if url:
            parts = url.rstrip("/").split("/")
            number = parts[-1] if parts[-1].isdigit() else ""
            if number:
                content_parts.insert(0, f"[#{number}]")
    elif raw_type == "PullRequest":
        if url:
            parts = url.rstrip("/").split("/")
            number = parts[-1] if parts[-1].isdigit() else ""
            if number:
                content_parts.insert(0, f"[#{number}]")
    elif raw_type == "Discussion":
        if url:
            parts = url.rstrip("/").split("/")
            number = parts[-1] if parts[-1].isdigit() else ""
            if number:
                content_parts.insert(0, f"[#{number}]")

    content = " ".join(content_parts)

    return {
        "type": msg_type,
        "title": title,
        "content": content,
        "props": {
            "repo": repo,
            "type": raw_type,
            "reason": reason,
            "url": url,
            "notif_id": n.get("id", ""),
            "updated_at": updated_at,
            "source": "inbox",
            "event": raw_type.lower(),
        },
    }


def poll_inbox(interval: int, stop_event: threading.Event):
    """Poll GitHub notifications periodically and insert into central DB.

    Runs in a background thread.
    """
    self_user = _get_self_user()
    if self_user:
        logger.info(f"Self user: {self_user} (own notifications will be filtered)")

    # Initialize DB
    central_db.init_central_db(config.CENTRAL_DB)

    while not stop_event.is_set():
        try:
            notifications = _fetch_notifications()
            now = datetime.now(timezone.utc)
            last_check = now

            for n in notifications:
                msg = _map_notification(n)
                if msg is None:
                    continue

                # DB-level dedup: skip if same source + URL already exists
                url = msg.get("props", {}).get("url", "")
                if url and central_db.message_exists_by_url(config.CENTRAL_DB, "github_notif", url):
                    continue

                # Insert into DB
                category = classify_message(msg["type"], msg.get("props", {}))
                msg_id = central_db.insert_message(
                    config.CENTRAL_DB,
                    type_=msg["type"],
                    title=msg["title"],
                    content=msg["content"],
                    props=msg.get("props", {}),
                    category=category,
                    source="github_notif",
                )
                if msg_id:
                    logger.debug(f"Inbox #{msg_id}: [{msg['type']}] {msg['title']} ({category})")

        except Exception as exc:
            logger.warning(f"Inbox poll error: {exc}")

        # Wait for next poll
        stop_event.wait(interval)


def run_inbox_source(interval: int = 30, foreground: bool = True):
    """Start the inbox notification poller."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    logger.info(f"GitHub Inbox source starting (interval={interval}s)")
    stop_event = threading.Event()
    t = threading.Thread(
        target=poll_inbox,
        args=(interval, stop_event),
        daemon=True,
    )
    t.start()

    if foreground:
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            logger.info("Shutting down inbox source...")
            stop_event.set()
    else:
        logger.info("Inbox source started in background")
