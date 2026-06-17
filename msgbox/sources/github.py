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
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
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


def _map_comment(event_type: str, event_label: str, parent_label: str, parent: dict, payload: dict, repo: str, sender: str) -> dict | None:
    """共享的 issue_comment / discussion_comment 映射逻辑"""
    comment = payload.get("comment", {})
    number = parent.get("number", "?")
    body = (comment.get("body") or "")[:300]
    url = comment.get("html_url", "")
    mentions = re.findall(r"@([\w-]+)", body)

    return {
        "type": event_type,
        "title": f"{parent_label} on #{number}",
        "content": f"{sender} {event_label} on #{number} in {repo}\n{body}",
        "props": {
            "repo": repo,
            "number": str(number),
            "action": payload.get("action", ""),
            "sender": sender,
            "url": url,
            "mentions": ",".join(mentions),
            "event": event_type.split(".")[-1],
        },
    }


def _map_issue_comment(event_type: str, payload: dict, repo: str, sender: str) -> dict | None:
    return _map_comment(
        "github.issue_comment", "commented", "Comment",
        payload.get("issue", {}), payload, repo, sender,
    )


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
    return _map_comment(
        "github.discussion_comment", "commented on discussion", "Discussion comment",
        payload.get("discussion", {}), payload, repo, sender,
    )


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


# ── SSE Client (built-in smee-client replacement) ─────────────


def _sse_listen(smee_url: str, target_url: str, stop_event: threading.Event, proxy: str = ""):
    """Connect to smee.io SSE stream and forward events to the local server.

    Runs in a background thread with auto-reconnect on failure.
    """
    import http.client
    import urllib.parse

    parsed = urllib.parse.urlparse(smee_url)
    headers = {
        "Accept": "text/event-stream",
        "Cache-Control": "no-cache",
    }
    proxy_host = None
    proxy_port = None
    if proxy:
        p = urllib.parse.urlparse(proxy)
        proxy_host = p.hostname
        proxy_port = p.port or 7890

    while not stop_event.is_set():
        conn = None
        try:
            logger.info(f"SSE: connecting to {smee_url} ...")
            if proxy_host:
                conn = http.client.HTTPSConnection(proxy_host, proxy_port, timeout=30)
                conn.set_tunnel(parsed.hostname, parsed.port or 443)
            else:
                conn = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=30)
            conn.request("GET", parsed.path or "/", headers=headers)
            resp = conn.getresponse()
            logger.info(f"SSE: connected (status={resp.status})")

            # Set socket timeout for keep-alive detection
            sock = resp.fp.raw._sock if hasattr(resp.fp, 'raw') else None
            if sock:
                sock.settimeout(60)

            buffer = ""
            pos = 0
            last_event_time = time.time()
            while not stop_event.is_set():
                try:
                    chunk = resp.read(4096)
                except socket.timeout:
                    # No data for 60s — heartbeat check: send a ping request
                    elapsed = time.time() - last_event_time
                    if elapsed > 120:
                        logger.info(f"SSE: no data for {int(elapsed)}s, reconnecting...")
                        break
                    continue
                except Exception:
                    break

                if not chunk:
                    break
                buffer += chunk.decode("utf-8", errors="replace")
                while True:
                    delim = buffer.find("\n\n", pos)
                    if delim == -1:
                        break
                    event_block = buffer[pos:delim]
                    pos = delim + 2
                    last_event_time = time.time()
                    _handle_sse_event(event_block, target_url)
                # Trim consumed portion to prevent unbounded growth
                if pos > 8192:
                    buffer = buffer[pos:]
                    pos = 0

        except Exception as exc:
            if stop_event.is_set():
                break
            logger.warning(f"SSE: connection error: {exc}, reconnecting in 5s...")
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

        if not stop_event.is_set():
            stop_event.wait(5)


def _handle_sse_event(event_block: str, target_url: str):
    """Parse a single SSE event block and POST it to the local webhook server."""
    data_lines = []

    for line in event_block.strip().split("\n"):
        if line.startswith("data:"):
            data_lines.append(line[5:].strip())

    if not data_lines:
        return

    data = "\n".join(data_lines)

    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return

    gh_event = payload.get("x-github-event", "")
    delivery = payload.get("x-github-delivery", "")

    if not gh_event:
        return

    if gh_event == "ping":
        logger.info("SSE: received ping from smee.io")
        return

    # Extract the actual GitHub webhook body
    body = payload.get("body", {})
    repo = (body.get("repository") or {}).get("full_name", "unknown")
    sender = (body.get("sender") or {}).get("login", "unknown")

    logger.info(f"SSE: forwarding {gh_event} from {repo} (sender={sender})")

    # POST to local webhook handler
    body_bytes = json.dumps(body).encode()
    req = urllib.request.Request(
        target_url,
        data=body_bytes,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": gh_event,
            "X-GitHub-Delivery": delivery or "",
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        logger.debug(f"SSE: forwarded {gh_event} OK")
    except urllib.error.HTTPError as exc:
        logger.warning(f"SSE: forward {gh_event} returned {exc.code}")
    except Exception as exc:
        logger.warning(f"SSE: failed to forward event {gh_event}: {exc}")


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

    def _respond(self, status: int, body: bytes):
        """快捷响应：设置状态码、headers 并写入 body。"""
        self.send_response(status)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/webhook":
            self._respond(404, b'{"error":"not found"}\n')
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b"{}"

        event_type = self.headers.get("X-GitHub-Event", "unknown")
        delivery_id = self.headers.get("X-GitHub-Delivery", "")

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON body for {event_type} (delivery={delivery_id})")
            self._respond(400, b'{"error":"invalid json"}\n')
            return

        # ── Filtering ──────────────────────────────────────
        repo_name = payload.get("repository", {}).get("full_name", "")
        sender = (payload.get("sender") or {}).get("login", "")

        # Hard-ignore own events (bypasses all rule-based filtering)
        if self.self_user and sender == self.self_user:
            logger.debug(f"Ignored own event: {event_type} from {sender}")
            self._respond(200, b'{"status":"ignored (self)"}\n')
            return

        if self.repo_filter and repo_name not in self.repo_filter:
            logger.debug(f"Skipped {event_type} from {repo_name} (repo not in allowlist)")
            self._respond(200, b'{"status":"skipped (repo filter)"}\n')
            return

        if self.event_filter and event_type not in self.event_filter:
            logger.debug(f"Skipped {event_type} from {repo_name} (event not in allowlist)")
            self._respond(200, b'{"status":"skipped (event filter)"}\n')
            return

        # ── Map to message ─────────────────────────────────
        msg = map_github_event(event_type, payload)
        if msg is None:
            self._respond(200, b'{"status":"skipped (no mapping)"}\n')
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
            self._respond(500, b'{"error":"db insert failed"}\n')
            return

        self._respond(201, json.dumps({"status": "ok", "id": msg_id}).encode())

    def do_GET(self):
        if self.path == "/health":
            self._respond(200, b'{"status":"ok"}\n')
        elif self.path == "/webhook":
            self._respond(200, b'{"status":"github webhook endpoint ready"}\n')
        else:
            self._respond(404, b'{"error":"not found"}\n')


# ── Server lifecycle ───────────────────────────────────────────


def run_server(
    port: int = 3001,
    smee_url: str = "",
    repos: list[str] | None = None,
    events: list[str] | None = None,
    self_user: str = "",
    proxy: str = "",
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

    # Inject config into handler class (single server per process, so direct assignment is safe)
    WebhookHandler.event_filter = event_filter
    WebhookHandler.repo_filter = repos
    WebhookHandler.self_user = self_user
    WebhookHandler.server_started = started

    server = HTTPServer(("127.0.0.1", port), WebhookHandler)
    logger.info(f"GitHub source listening on http://127.0.0.1:{port}/webhook")

    if smee_url:
        logger.info(f"Smee proxy: {smee_url}")
        logger.info(f"Built-in SSE client enabled (no external smee-client needed)")
        stop_event = threading.Event()
        t = threading.Thread(
            target=_sse_listen,
            args=(smee_url, f"http://127.0.0.1:{port}/webhook", stop_event, proxy),
            daemon=True,
        )
        t.start()
    else:
        logger.info("No smee URL configured — waiting for manual POSTs")

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
