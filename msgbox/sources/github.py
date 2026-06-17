"""GitHub webhook source — receives webhooks forwarded by smee-client and feeds them into the message-box central DB.

Architecture:
    GitHub → smee.io → smee-client (SSE) → localhost:3001/webhook → msgbox DB

Usage:
    msgbox source-github [--port PORT] [--smee-url URL]
"""

import json
import logging
import re
import signal
import sys
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

from .. import config
from .. import db as central_db
from ..filter import classify_message
from ..yaml_config import load_config

logger = logging.getLogger("msgbox.sources.github")

# ── Event → Message mapping ────────────────────────────────────


def _short_sha(sha: str) -> str:
    return sha[:7]


def _ref_branch(ref: str) -> str:
    """refs/heads/main → main, refs/tags/v1.0 → v1.0"""
    return re.sub(r"^refs/(heads|tags)/", "", ref)


def map_github_event(event_type: str, payload: dict) -> dict | None:
    """Map a GitHub webhook event to a message-box message dict.

    Returns None if the event should be skipped.
    """
    repo_name = payload.get("repository", {}).get("full_name", "unknown")
    sender = (payload.get("sender") or {}).get("login", "unknown")

    mapping = {
        "push": _map_push,
        "issues": _map_issues,
        "issue_comment": _map_issue_comment,
        "pull_request": _map_pull_request,
        "star": _map_star,
        "create": _map_create,
        "delete": _map_delete,
        "fork": _map_fork,
        "release": _map_release,
        "pull_request_review": _map_pr_review,
        "pull_request_review_comment": _map_pr_review_comment,
        "check_run": _map_check_run,
        "check_suite": _map_check_suite,
        "status": _map_status,
        "workflow_run": _map_workflow_run,
        "watch": _map_star,
        "ping": _map_ping,
        "discussion": _map_discussion,
        "discussion_comment": _map_discussion_comment,
    }

    handler = mapping.get(event_type, _map_generic)
    return handler(event_type, payload, repo_name, sender)


# ── Per-event mappers ──────────────────────────────────────────


def _map_push(event_type: str, payload: dict, repo: str, sender: str) -> dict | None:
    ref = payload.get("ref", "")
    branch = _ref_branch(ref)
    commits = payload.get("commits", [])
    compare = payload.get("compare", "")
    forced = payload.get("forced", False)
    deleted = payload.get("deleted", False)

    if deleted:
        title = f"Branch deleted: {branch}"
        content = f"{sender} deleted {branch} on {repo}"
    elif forced:
        title = f"Force push to {repo}:{branch}"
        summary = "; ".join(c.get("message", "").split("\n")[0] for c in commits[:3])
        content = f"{sender} force-pushed {len(commits)} commit(s) to {branch}\n{summary}"
    else:
        title = f"Push to {repo}:{branch}"
        summary = "; ".join(c.get("message", "").split("\n")[0] for c in commits[:3])
        content = f"{sender} pushed {len(commits)} commit(s) to {branch}"
        if summary:
            content += f"\n{summary}"
        if compare:
            content += f"\n{compare}"

    return {
        "type": "github.push",
        "title": title,
        "content": content,
        "props": {
            "repo": repo,
            "branch": branch,
            "commits": str(len(commits)),
            "sender": sender,
            "event": "push",
        },
    }


def _map_issues(event_type: str, payload: dict, repo: str, sender: str) -> dict | None:
    issue = payload.get("issue", {})
    action = payload.get("action", "unknown")
    number = issue.get("number", "?")
    title = issue.get("title", "")
    body = (issue.get("body") or "")[:300]
    url = issue.get("html_url", "")

    return {
        "type": "github.issue",
        "title": f"Issue {action}: #{number} {title}",
        "content": f"{sender} {action} issue #{number} on {repo}\n{body}",
        "props": {
            "repo": repo,
            "number": str(number),
            "action": action,
            "sender": sender,
            "url": url,
            "event": "issues",
        },
    }


def _map_issue_comment(event_type: str, payload: dict, repo: str, sender: str) -> dict | None:
    issue = payload.get("issue", {})
    comment = payload.get("comment", {})
    number = issue.get("number", "?")
    body = (comment.get("body") or "")[:300]
    url = comment.get("html_url", "")
    mentions = re.findall(r"@([\w-]+)", body)

    return {
        "type": "github.issue_comment",
        "title": f"Comment on #{number}",
        "content": f"{sender} commented on #{number} in {repo}\n{body}",
        "props": {
            "repo": repo,
            "number": str(number),
            "action": payload.get("action", ""),
            "sender": sender,
            "url": url,
            "mentions": ",".join(mentions),
            "event": "issue_comment",
        },
    }


def _map_pull_request(event_type: str, payload: dict, repo: str, sender: str) -> dict | None:
    pr = payload.get("pull_request", {})
    action = payload.get("action", "unknown")
    number = pr.get("number", "?")
    title = pr.get("title", "")
    body = (pr.get("body") or "")[:300]
    url = pr.get("html_url", "")
    merged = pr.get("merged", False)

    if action == "closed" and merged:
        action_label = "merged"
    else:
        action_label = action

    return {
        "type": "github.pr",
        "title": f"PR {action_label}: #{number} {title}",
        "content": f"{sender} {action_label} PR #{number} on {repo}\n{body}",
        "props": {
            "repo": repo,
            "number": str(number),
            "action": action,
            "merged": str(merged).lower(),
            "sender": sender,
            "url": url,
            "event": "pull_request",
        },
    }


def _map_star(event_type: str, payload: dict, repo: str, sender: str) -> dict | None:
    return {
        "type": "github.star",
        "title": f"⭐ Star: {repo}",
        "content": f"{sender} starred {repo}",
        "props": {"repo": repo, "sender": sender, "event": "star"},
    }


def _map_create(event_type: str, payload: dict, repo: str, sender: str) -> dict | None:
    ref_type = payload.get("ref_type", "")
    ref_name = payload.get("ref", "")

    return {
        "type": "github.create",
        "title": f"Created {ref_type}: {ref_name}",
        "content": f"{sender} created {ref_type} '{ref_name}' on {repo}",
        "props": {"repo": repo, "ref_type": ref_type, "ref": ref_name, "sender": sender, "event": "create"},
    }


def _map_delete(event_type: str, payload: dict, repo: str, sender: str) -> dict | None:
    ref_type = payload.get("ref_type", "")
    ref_name = payload.get("ref", "")

    return {
        "type": "github.delete",
        "title": f"Deleted {ref_type}: {ref_name}",
        "content": f"{sender} deleted {ref_type} '{ref_name}' on {repo}",
        "props": {"repo": repo, "ref_type": ref_type, "ref": ref_name, "sender": sender, "event": "delete"},
    }


def _map_fork(event_type: str, payload: dict, repo: str, sender: str) -> dict | None:
    forkee = payload.get("forkee", {})
    fork_name = forkee.get("full_name", "unknown")

    return {
        "type": "github.fork",
        "title": f"Fork: {repo}",
        "content": f"{sender} forked {repo} → {fork_name}",
        "props": {"repo": repo, "fork": fork_name, "sender": sender, "event": "fork"},
    }


def _map_release(event_type: str, payload: dict, repo: str, sender: str) -> dict | None:
    release = payload.get("release", {})
    tag = release.get("tag_name", "")
    name = release.get("name", "")
    action = payload.get("action", "published")
    url = release.get("html_url", "")

    return {
        "type": "github.release",
        "title": f"Release {action}: {tag} {name}",
        "content": f"{sender} {action} release {tag} on {repo}\n{(release.get('body') or '')[:300]}",
        "props": {"repo": repo, "tag": tag, "action": action, "sender": sender, "url": url, "event": "release"},
    }


def _map_pr_review(event_type: str, payload: dict, repo: str, sender: str) -> dict | None:
    pr = payload.get("pull_request", {})
    review = payload.get("review", {})
    number = pr.get("number", "?")
    state = review.get("state", "")
    url = review.get("html_url", "")

    return {
        "type": "github.review",
        "title": f"PR review {state}: #{number}",
        "content": f"{sender} {state} PR #{number} on {repo}\n{(review.get('body') or '')[:300]}",
        "props": {"repo": repo, "number": str(number), "state": state, "sender": sender, "url": url, "event": "pull_request_review"},
    }


def _map_pr_review_comment(event_type: str, payload: dict, repo: str, sender: str) -> dict | None:
    pr = payload.get("pull_request", {})
    comment = payload.get("comment", {})
    number = pr.get("number", "?")
    url = comment.get("html_url", "")

    return {
        "type": "github.review_comment",
        "title": f"Review comment on PR #{number}",
        "content": f"{sender} left a review comment on PR #{number} in {repo}\n{(comment.get('body') or '')[:300]}",
        "props": {"repo": repo, "number": str(number), "sender": sender, "url": url, "event": "pull_request_review_comment"},
    }


def _map_check_run(event_type: str, payload: dict, repo: str, sender: str) -> dict | None:
    check_run = payload.get("check_run", {})
    name = check_run.get("name", "")
    status = check_run.get("status", "")
    conclusion = check_run.get("conclusion", "")
    url = check_run.get("html_url", "")

    if status == "completed":
        title = f"Check {conclusion}: {name}"
    else:
        title = f"Check {status}: {name}"

    return {
        "type": "github.check_run",
        "title": title,
        "content": f"Check run '{name}' {status}" + (f" ({conclusion})" if conclusion else "") + f" on {repo}",
        "props": {"repo": repo, "name": name, "status": status, "conclusion": conclusion or "", "sender": sender, "url": url, "event": "check_run"},
    }


def _map_check_suite(event_type: str, payload: dict, repo: str, sender: str) -> dict | None:
    suite = payload.get("check_suite", {})
    app = suite.get("app", {})
    app_name = app.get("name", "unknown")
    status = suite.get("status", "")
    conclusion = suite.get("conclusion", "")

    if status == "completed":
        title = f"Check suite {conclusion}: {app_name}"
    else:
        title = f"Check suite {status}: {app_name}"

    return {
        "type": "github.check_suite",
        "title": title,
        "content": f"Check suite from '{app_name}' {status}" + (f" ({conclusion})" if conclusion else "") + f" on {repo}",
        "props": {"repo": repo, "app": app_name, "status": status, "conclusion": conclusion or "", "sender": sender, "event": "check_suite"},
    }


def _map_status(event_type: str, payload: dict, repo: str, sender: str) -> dict | None:
    state = payload.get("state", "")
    branches = [b.get("name", "") for b in payload.get("branches", [])]
    description = payload.get("description", "")

    return {
        "type": "github.status",
        "title": f"Status {state}: {', '.join(branches[:3])}",
        "content": f"Commit status '{state}' on {repo}\n{description}" if description else f"Commit status '{state}' on {repo}",
        "props": {"repo": repo, "state": state, "branches": ", ".join(branches), "sender": sender, "event": "status"},
    }


def _map_workflow_run(event_type: str, payload: dict, repo: str, sender: str) -> dict | None:
    workflow = payload.get("workflow_run", {})
    name = workflow.get("name", "")
    status = workflow.get("status", "")
    conclusion = workflow.get("conclusion", "")
    url = workflow.get("html_url", "")
    action = payload.get("action", "")

    if status == "completed":
        title = f"Workflow {conclusion}: {name}"
    else:
        title = f"Workflow {action}: {name}"

    return {
        "type": "github.workflow_run",
        "title": title,
        "content": f"Workflow '{name}' {status}" + (f" ({conclusion})" if conclusion else "") + f" on {repo}",
        "props": {"repo": repo, "name": name, "status": status, "conclusion": conclusion or "", "action": action, "sender": sender, "url": url, "event": "workflow_run"},
    }


def _map_discussion(event_type: str, payload: dict, repo: str, sender: str) -> dict | None:
    discussion = payload.get("discussion", {})
    action = payload.get("action", "unknown")
    number = discussion.get("number", "?")
    title = discussion.get("title", "")
    body = (discussion.get("body") or "")[:300]
    url = discussion.get("html_url", "")
    category = (discussion.get("category") or {}).get("name", "")

    return {
        "type": "github.discussion",
        "title": f"Discussion {action}: #{number} {title}",
        "content": f"{sender} {action} discussion #{number} in {repo}" + (f" [{category}]" if category else "") + f"\n{body}",
        "props": {
            "repo": repo,
            "number": str(number),
            "action": action,
            "category": category,
            "sender": sender,
            "url": url,
            "event": "discussion",
        },
    }


def _map_discussion_comment(event_type: str, payload: dict, repo: str, sender: str) -> dict | None:
    discussion = payload.get("discussion", {})
    comment = payload.get("comment", {})
    number = discussion.get("number", "?")
    body = (comment.get("body") or "")[:300]
    url = comment.get("html_url", "")
    mentions = re.findall(r"@([\w-]+)", body)

    return {
        "type": "github.discussion_comment",
        "title": f"Discussion comment on #{number}",
        "content": f"{sender} commented on discussion #{number} in {repo}\n{body}",
        "props": {
            "repo": repo,
            "number": str(number),
            "action": payload.get("action", ""),
            "sender": sender,
            "url": url,
            "mentions": ",".join(mentions),
            "event": "discussion_comment",
        },
    }


def _map_ping(event_type: str, payload: dict, repo: str, sender: str) -> dict | None:
    zen = payload.get("zen", "")
    hook_id = payload.get("hook_id", "")

    return {
        "type": "github.ping",
        "title": "Webhook connected",
        "content": f"GitHub webhook ping received (hook #{hook_id})\n{zen}",
        "props": {"hook_id": str(hook_id), "event": "ping"},
    }


def _map_generic(event_type: str, payload: dict, repo: str, sender: str) -> dict | None:
    return {
        "type": f"github.{event_type}",
        "title": f"GitHub {event_type} on {repo}",
        "content": f"{sender} triggered '{event_type}' on {repo}",
        "props": {"repo": repo, "sender": sender, "event": event_type},
    }


# ── HTTP Server ────────────────────────────────────────────────


class WebhookHandler(BaseHTTPRequestHandler):
    """Handles POST /webhook from smee-client forwarding."""

    # Class-level config, set before server starts
    event_filter: set[str] | None = None  # None = accept all
    repo_filter: list[str] | None = None  # None = accept all
    self_user: str = ""  # Username to hard-ignore (bot's own events)
    server_started: threading.Event | None = None

    def log_message(self, format, *args):
        logger.info(f"{self.client_address[0]} - {format % args}")

    def do_POST(self):
        if self.path != "/webhook":
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"error":"not found"}\n')
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b"{}"

        event_type = self.headers.get("X-GitHub-Event", "unknown")
        delivery_id = self.headers.get("X-GitHub-Delivery", "")

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON body for {event_type} (delivery={delivery_id})")
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error":"invalid json"}\n')
            return

        # ── Filtering ──────────────────────────────────────
        repo_name = payload.get("repository", {}).get("full_name", "")
        sender = (payload.get("sender") or {}).get("login", "")

        # Hard-ignore own events (bypasses all rule-based filtering)
        if self.self_user and sender == self.self_user:
            logger.debug(f"Ignored own event: {event_type} from {sender}")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ignored (self)"}\n')
            return

        if self.repo_filter and repo_name not in self.repo_filter:
            logger.debug(f"Skipped {event_type} from {repo_name} (repo not in allowlist)")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"skipped (repo filter)"}\n')
            return

        if self.event_filter and event_type not in self.event_filter:
            logger.debug(f"Skipped {event_type} from {repo_name} (event not in allowlist)")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"skipped (event filter)"}\n')
            return

        # ── Map to message ─────────────────────────────────
        msg = map_github_event(event_type, payload)
        if msg is None:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"skipped (no mapping)"}\n')
            return

        # ── Map and insert ─────────────────────────────────
        try:
            category = classify_message(msg["type"], msg.get("props", {}))
            msg_id = central_db.insert_message(
                config.CENTRAL_DB,
                type_=msg["type"],
                title=msg["title"],
                content=msg["content"],
                props=msg.get("props", {}),
                category=category,
            )
            logger.info(f"Stored #{msg_id}: [{msg['type']}] {msg['title']}")
        except Exception as exc:
            logger.error(f"Failed to store message: {exc}")
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b'{"error":"db insert failed"}\n')
            return

        self.send_response(201)
        self.end_headers()
        self.wfile.write(json.dumps({"status": "ok", "id": msg_id}).encode())

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}\n')
        elif self.path == "/webhook":
            # Respond to smee-client pre-flight / info requests
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"github webhook endpoint ready"}\n')
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"error":"not found"}\n')


# ── Server lifecycle ───────────────────────────────────────────


def _make_handler(
    event_filter: set[str] | None,
    repo_filter: list[str] | None,
    started_event: threading.Event,
    self_user: str = "",
):
    """Factory to create a WebhookHandler subclass with injected config."""

    class ConfiguredHandler(WebhookHandler):
        pass

    ConfiguredHandler.event_filter = event_filter
    ConfiguredHandler.repo_filter = repo_filter
    ConfiguredHandler.self_user = self_user
    ConfiguredHandler.server_started = started_event
    return ConfiguredHandler


def run_server(
    port: int = 3001,
    smee_url: str = "",
    repos: list[str] | None = None,
    events: list[str] | None = None,
    self_user: str = "",
    foreground: bool = True,
) -> HTTPServer:
    """Start the GitHub webhook HTTP server.

    Returns the HTTPServer instance. Call server.serve_forever() to block.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    # Parse event filter
    event_filter: set[str] | None = None
    if events and "*" not in events:
        event_filter = set(events)

    started = threading.Event()
    handler = _make_handler(event_filter, repos, started, self_user)

    server = HTTPServer(("127.0.0.1", port), handler)
    logger.info(f"GitHub source listening on http://127.0.0.1:{port}/webhook")

    if smee_url:
        logger.info(f"Smee proxy: {smee_url}")
        logger.info(f"Run: smee-client --url {smee_url} --target http://127.0.0.1:{port}/webhook")

    if repos:
        logger.info(f"Repo allowlist: {repos}")
    if event_filter:
        logger.info(f"Event allowlist: {sorted(event_filter)}")

    # Ensure central DB is initialized
    central_db.init_central_db(config.CENTRAL_DB)

    if foreground:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            logger.info("Shutting down...")
            server.shutdown()
    else:
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        started.wait(timeout=3)
        logger.info("Server started in background thread")

    return server


# ── Config helpers ─────────────────────────────────────────────


def get_github_config() -> dict:
    """Load GitHub source configuration from config.yaml."""
    cfg = load_config()
    return cfg.get("sources", {}).get("github", {})
