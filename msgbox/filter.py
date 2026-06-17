"""过滤引擎 - 消息分类"""

import json
import re
from typing import Any

from . import config
from .yaml_config import load_config


def classify_message(type_: str, props: dict[str, str]) -> str:
    """按规则分类消息: popup / silent / normal"""
    cfg = load_config()
    rules = cfg.get("rules", {})

    # 构建匹配上下文
    ctx = {"type": type_, "props": props}

    # 1. popup_excluded — 命中则跳过 popup
    for rule in rules.get("popup_excluded", []):
        if _match_rule(rule, ctx):
            # 跳到 silent_excluded
            break
    else:
        # 没被 popup_excluded 挡住 → 检查 popup
        for rule in rules.get("popup", []):
            if _match_rule(rule, ctx):
                return "popup"
        # 命中 popup_excluded 但未命中 popup → 继续检查

    # 2. silent_excluded — 命中则跳过 silent
    for rule in rules.get("silent_excluded", []):
        if _match_rule(rule, ctx):
            return "normal"

    # 3. silent
    for rule in rules.get("silent", []):
        if _match_rule(rule, ctx):
            return "silent"

    return "normal"


def _match_rule(rule: dict[str, Any], ctx: dict) -> bool:
    """检查一条规则是否匹配"""
    # type 匹配
    type_pattern = rule.get("type", ".*")
    if not re.search(type_pattern, ctx["type"]):
        return False

    # props 匹配
    props_patterns = rule.get("props", {})
    for key, pattern in props_patterns.items():
        val = ctx["props"].get(key, "")
        if not re.search(pattern, val):
            return False

    return True
