"""YAML 配置管理"""

import os
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from . import config

# load_config 缓存：mtime 不变时复用缓存结果
_config_cache: dict[str, Any] | None = None
_config_mtime: float = 0


def _ensure_dir():
    config.PLUGIN_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> dict[str, Any]:
    global _config_cache, _config_mtime
    _ensure_dir()
    if not config.CONFIG_FILE.exists():
        _config_cache = None
        return _default_config()
    try:
        mtime = config.CONFIG_FILE.stat().st_mtime
    except OSError:
        mtime = 0
    if _config_cache is not None and _config_mtime == mtime:
        return _config_cache
    with open(config.CONFIG_FILE) as f:
        _config_cache = yaml.safe_load(f) or _default_config()
        _config_mtime = mtime
        return _config_cache


def save_config(cfg: dict[str, Any]):
    global _config_cache, _config_mtime
    _ensure_dir()
    with open(config.CONFIG_FILE, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
    _config_cache = cfg
    _config_mtime = config.CONFIG_FILE.stat().st_mtime


def _default_config() -> dict[str, Any]:
    return {
        "rules": {
            "popup": [],
            "popup_excluded": [],
            "silent": [],
            "silent_excluded": [],
        },
        "templates": {
            "brief": """## 📬 消息简报
弹窗消息 ({POPUP_MESSAGE_COUNT}):
{NEW_POPUP_MESSAGES}

新消息 ({MESSAGE_COUNT}):
{NEW_MESSAGES}

!{date "+%Y-%m-%d %H:%M:%S"}
💡 向消息源回复，不要在对话中直接输出
""",
            "brief_wait": """## 📬 消息简报
弹窗消息 ({POPUP_MESSAGE_COUNT}):
{NEW_POPUP_MESSAGES}

新消息 ({MESSAGE_COUNT}):
{NEW_MESSAGES}

{ACTIVE_TODOS}

!{date "+%Y-%m-%d %H:%M:%S"}
💡 处理原则：
1. 被私聊或在群聊中被 @ 时，先第一时间简短回应「收到，正在处理」，再执行任务。
2. 处理消息时查看最近完整上下文，不要只看最新一条。
3. 向消息源回复，不要在对话中直接输出。
4. 复杂任务交给 subagent 或非交互式 claude 后台处理。
   例如：复杂 Excel 分析、文档生成、代码实现/重构、多步骤调研等，建议优先走 subagent。
5. 忙更高优先级事项时，首次回复说明状态，并视情况决定是否透露具体内容。
   当需要 subagent/后台处理时，首次回应应包含预计完成时间。
6. 善用 todo/task 系统跟踪和管理任务。
   必须创建 todo 的场景：
   • 预计耗时较长（超过 5–10 分钟）的任务；
   • 当前任务执行过程中，收到新的、与当前任务不相关且需要处理的消息；
   • 多步骤或容易中断的任务。
""",
            "brief_peek": """## 📬 消息简报
弹窗消息 ({POPUP_MESSAGE_COUNT}):
{NEW_POPUP_MESSAGES}

新消息 ({MESSAGE_COUNT}):
{NEW_MESSAGES}
""",
            "item": "• [{MESSAGE_TYPE}] {MESSAGE_TITLE} ({MESSAGE_TIME_AGO}): {MESSAGE_CONTENT_CUTTED}",
            "groups": {
                "group_header": "[{GROUP_TYPE}]",
                "group_item_title": "  ├ {MESSAGE_TITLE}",
                "group_item_remaining": "  └ 还有 {GROUP_REMAINING} 条同类型",
                "group_overflow": "📎 还有 {GROUP_OVERFLOW_GROUPS} 类共 {GROUP_OVERFLOW_TOTAL} 条消息",
            },
        },
    }


def get_config_value(key_path: str) -> Any:
    """获取配置值，如 'rules.popup' """
    cfg = load_config()
    parts = key_path.split(".")
    val = cfg
    for p in parts:
        if isinstance(val, dict):
            val = val.get(p)
        else:
            return None
    return val


def set_config_value(key_path: str, value: Any):
    cfg = load_config()
    parts = key_path.split(".")
    parent = cfg
    for p in parts[:-1]:
        if p not in parent:
            parent[p] = {}
        parent = parent[p]
    parent[parts[-1]] = value
    save_config(cfg)


def add_rule(rule_type: str, type_pattern: str, props: dict[str, str] | None = None):
    """添加过滤规则"""
    cfg = load_config()
    rules = cfg.setdefault("rules", {})
    rule_list = rules.setdefault(rule_type, [])
    rule = {"type": type_pattern}
    if props:
        rule["props"] = props
    rule_list.append(rule)
    save_config(cfg)


def remove_rule(rule_type: str, index: int):
    """按索引删除规则"""
    cfg = load_config()
    rules = cfg.get("rules", {})
    rule_list = rules.get(rule_type, [])
    if 0 <= index < len(rule_list):
        rule_list.pop(index)
        save_config(cfg)


def list_rules() -> list[dict]:
    cfg = load_config()
    result = []
    for rule_type in ("popup", "popup_excluded", "silent", "silent_excluded"):
        for i, rule in enumerate(cfg.get("rules", {}).get(rule_type, [])):
            result.append({"index": i, "type": rule_type, "pattern": rule.get("type", ""), "props": rule.get("props", {})})
    return result
