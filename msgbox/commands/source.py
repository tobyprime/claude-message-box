"""Source commands — GitHub webhook, GitHub inbox, DingTalk poller."""

import os
import sys
import subprocess

from ..sources.github import run_server, get_github_config
from ..sources.inbox import run_inbox_source
from ..sources.dingtalk import run_dingtalk_source
from ..yaml_config import add_rule, list_rules, load_config, remove_rule


_THREAD_TYPE_PATTERNS = {
    "discussion": ("github.discussion_comment",),
    "issue": ("github.issue_comment",),
    "pr": ("github.review_comment", "github.review"),
}


def cmd_source_github(args):
    gh_config = get_github_config()

    port = args.port or gh_config.get("port", 3001)
    smee_url = args.smee_url or gh_config.get("smee_url", "")
    repos = args.repos or gh_config.get("repos", [])
    events = args.events or gh_config.get("events", ["*"])
    foreground = args.foreground

    self_user = gh_config.get("self_user", "")
    try:
        detected = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            capture_output=True, text=True, timeout=5
        ).stdout.strip()
        if detected:
            self_user = detected
            print(f"Bot user detected: {self_user} (own events will be ignored)")
    except Exception:
        pass

    if not repos:
        repos = None
    if not events:
        events = None

    proxy = os.environ.get("HTTP_PROXY") or os.environ.get("https_proxy") or ""

    run_server(
        port=port,
        smee_url=smee_url,
        repos=repos,
        events=events,
        self_user=self_user,
        proxy=proxy,
        foreground=foreground,
    )


def cmd_source_inbox(args):
    run_inbox_source(interval=args.interval, foreground=args.foreground)


def cmd_source_dingtalk(args):
    interval = args.interval if args.interval and args.interval > 0 else None
    run_dingtalk_source(interval=interval, foreground=args.foreground)


def cmd_subscribe(args):
    thread_type = args.thread_type
    number = args.number

    patterns = _THREAD_TYPE_PATTERNS.get(thread_type)
    if patterns is None:
        print(f"Unknown type: {thread_type}", file=sys.stderr)
        sys.exit(1)

    ignore_pattern = "|".join(patterns)
    props = {"number": str(number)}

    add_rule("silent_excluded", ignore_pattern, props)
    print(f"Subscribed to {thread_type} #{number} comments (silent_excluded)")

    if args.popup:
        add_rule("popup", ignore_pattern, props)
        print(f"  → {thread_type} #{number} comments will be popup")


def cmd_unsubscribe(args):
    thread_type = args.thread_type
    number = args.number

    patterns = _THREAD_TYPE_PATTERNS.get(thread_type)
    if patterns is None:
        print(f"Unknown type: {thread_type}", file=sys.stderr)
        sys.exit(1)

    props = {"number": str(number)}
    cfg = load_config()
    removed = 0

    for rule_type in ("silent_excluded", "popup"):
        rules = cfg.get("rules", {}).get(rule_type, [])
        to_remove = []
        for i, rule in enumerate(rules):
            if rule.get("type") in patterns and rule.get("props", {}).get("number") == str(number):
                to_remove.append(i)
        for i in reversed(to_remove):
            remove_rule(rule_type, i)
            removed += 1

    print(f"Unsubscribed from {thread_type} #{number} (removed {removed} rules)")


def cmd_subscriptions(args):
    rules = list_rules()
    subs = []
    for r in rules:
        if r["type"] in ("silent_excluded", "popup"):
            props = r.get("props", {})
            if "number" in props:
                subs.append(r)
    if not subs:
        print("No active subscriptions")
        return
    print("Active subscriptions:")
    for s in subs:
        print(f"  [{s['type']}] {s['pattern']:35s} props={json.dumps(s['props'], ensure_ascii=False)}")
