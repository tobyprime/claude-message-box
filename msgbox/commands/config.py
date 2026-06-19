"""Configuration management commands."""

import json
import sys

from ..filter import classify_message
from ..yaml_config import add_rule, get_config_value, list_rules, load_config, remove_rule, set_config_value
from ..template import render_brief
from .. import db as central_db
from .. import config as project_config


def cmd_config_get(args):
    val = get_config_value(args.key)
    if val is None:
        print(f"Key '{args.key}' not found", file=sys.stderr)
        sys.exit(1)
    if isinstance(val, (dict, list)):
        print(json.dumps(val, ensure_ascii=False, indent=2))
    else:
        print(val)


def cmd_config_set(args):
    val = args.value
    try:
        val = json.loads(val)
    except (json.JSONDecodeError, TypeError):
        pass
    set_config_value(args.key, val)
    print(f"Set {args.key} = {val}")


def cmd_config_rules(args):
    rules = list_rules()
    if not rules:
        print("No rules configured")
        return
    for r in rules:
        print(f"[{r['index']}] {r['type']:20s} type={r['pattern']:30s} props={json.dumps(r['props'], ensure_ascii=False)}")


def cmd_config_rules_add(args):
    props = {}
    if args.props:
        try:
            props = json.loads(args.props)
        except json.JSONDecodeError:
            print("Invalid JSON for --props", file=sys.stderr)
            sys.exit(1)
    add_rule(args.rule_type, args.pattern, props)
    print(f"Added {args.rule_type} rule: type={args.pattern} props={props}")


def cmd_config_rules_remove(args):
    remove_rule(args.rule_type, args.index)
    print(f"Removed rule [{args.index}] from {args.rule_type}")
