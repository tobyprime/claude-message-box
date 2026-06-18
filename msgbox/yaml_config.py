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
            "item": "• [{MESSAGE_TYPE}] {MESSAGE_TITLE} ({MESSAGE_TIME_AGO}): {MESSAGE_CONTENT_CUTTED}",
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
