"""模板渲染引擎

支持:
  - {VAR}        内置变量替换
  - !{bash_cmd}  bash 命令执行（变量先展开再注入到命令中）
"""

import re
import subprocess
from datetime import datetime, timezone
from typing import Any


_BUILTIN_VARS: dict[str, str] = {}


def _relative_time(iso_str: str) -> str:
    """将 ISO 时间字符串转为相对时间描述（几秒前/几分钟前/几小时前等）"""
    try:
        ts = datetime.fromisoformat(iso_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - ts
        seconds = int(delta.total_seconds())
    except (ValueError, TypeError):
        return ""

    if seconds < 5:
        return "刚刚"
    elif seconds < 60:
        return f"{seconds}秒前"
    elif seconds < 3600:
        return f"{seconds // 60}分钟前"
    elif seconds < 86400:
        return f"{seconds // 3600}小时前"
    elif seconds < 2592000:
        return f"{seconds // 86400}天前"
    else:
        return f"{seconds // 2592000}月前"


def set_builtin_vars(vars: dict[str, str]):
    _BUILTIN_VARS.clear()
    _BUILTIN_VARS.update(vars)


def expand_vars(text: str, extra_vars: dict[str, str] | None = None) -> str:
    """先展开 {VAR} 变量（不碰 !{...}）"""
    vars = dict(_BUILTIN_VARS)
    if extra_vars:
        vars.update(extra_vars)

    def _replace_var(m: re.Match) -> str:
        key = m.group(1)
        return vars.get(key, m.group(0))

    text = re.sub(r"(?<!!)\{([A-Z_][A-Z0-9_]*)\}", _replace_var, text)
    return text


def expand_bash(text: str) -> str:
    """执行 !{bash_cmd} 并替换为 stdout"""

    def _exec_bash(m: re.Match) -> str:
        cmd = m.group(1).strip()
        if not cmd:
            return ""
        try:
            result = subprocess.run(
                ["bash", "-c", cmd],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result.stdout.strip()
        except subprocess.TimeoutExpired:
            return ""
        except subprocess.CalledProcessError:
            return ""

    text = re.sub(r"!\{([^}]+)\}", _exec_bash, text)
    return text


def render(text: str, extra_vars: dict[str, str] | None = None) -> str:
    """完整渲染流程: 变量展开 → bash 执行"""
    text = expand_vars(text, extra_vars)
    text = expand_bash(text)
    return text


# ── 消息简报变量构建 ────────────────────────────────────────


def _render_grouped(messages: list[dict], item_template: str, max_groups: int = 3, max_per_group: int = 3,
                    group_templates: dict[str, str] | None = None) -> list[str]:
    """按类型聚合展示消息，避免简报过长

    策略：
    - 按 type 分组，组内按 created_at 降序
    - 最多展示 max_groups 个类型组
    - 每组最多展示 max_per_group 条
      - 第1条：完整格式（标题 + 内容）
      - 第2条：标题（使用 group_item_title 模板）
      - 第3+条：计数（使用 group_item_remaining 模板）
    - 超出的组显示 group_overflow 模板

    group_templates 可配置模板：
      group_header:  类型组标题，变量 {GROUP_TYPE}，默认 "[{GROUP_TYPE}]"
      group_item_title: 标题行，变量 {MESSAGE_TITLE}，默认 "  ├ {MESSAGE_TITLE}"
      group_item_remaining: 剩余计数，变量 {GROUP_REMAINING}，默认 "  └ 还有 {GROUP_REMAINING} 条同类型"
      group_overflow: 溢出组，变量 {GROUP_OVERFLOW_GROUPS},{GROUP_OVERFLOW_TOTAL}，默认 "📎 还有 {GROUP_OVERFLOW_GROUPS} 类共 {GROUP_OVERFLOW_TOTAL} 条消息"
    """
    gt = group_templates or {}
    tpl_header = gt.get("group_header", "[{GROUP_TYPE}]")
    tpl_title = gt.get("group_item_title", "  ├ {MESSAGE_TITLE}")
    tpl_remaining = gt.get("group_item_remaining", "  └ 还有 {GROUP_REMAINING} 条同类型")
    tpl_overflow = gt.get("group_overflow", "📎 还有 {GROUP_OVERFLOW_GROUPS} 类共 {GROUP_OVERFLOW_TOTAL} 条消息")

    # Group by type
    groups: dict[str, list[dict]] = {}
    for m in messages:
        msg_type = m.get("type", "unknown")
        groups.setdefault(msg_type, []).append(m)

    # Sort groups: latest message time descending
    def _group_latest(msgs: list[dict]) -> str:
        times = [m.get("created_at", "") for m in msgs if m.get("created_at")]
        return max(times) if times else ""

    sorted_types = sorted(groups.keys(), key=lambda t: _group_latest(groups[t]), reverse=True)

    items: list[str] = []
    total_remaining = 0

    for i, msg_type in enumerate(sorted_types):
        msgs = groups[msg_type]
        msgs.sort(key=lambda m: m.get("created_at", ""), reverse=True)

        if i < max_groups:
            display_type = msg_type.replace("github.", "")
            items.append(expand_vars(tpl_header, {"GROUP_TYPE": display_type}))
            for j, m in enumerate(msgs):
                if j == 0:
                    items.append(_format_single_message(m, item_template))
                elif j < max_per_group - 1:
                    items.append(expand_vars(tpl_title, {"MESSAGE_TITLE": m.get("title", "")}))
                else:
                    remaining_in_group = len(msgs) - j
                    items.append(expand_vars(tpl_remaining, {"GROUP_REMAINING": str(remaining_in_group)}))
                    break
        else:
            total_remaining += len(msgs)

    if total_remaining > 0:
        remaining_groups = len(sorted_types) - max_groups
        items.append(expand_vars(tpl_overflow, {
            "GROUP_OVERFLOW_GROUPS": str(remaining_groups),
            "GROUP_OVERFLOW_TOTAL": str(total_remaining),
        }))

    return items


def _format_single_message(msg: dict, item_template: str, var_prefix: str = "") -> str:
    """按 item_template 渲染单条消息"""
    content = msg.get("content", "")
    content_cut = content[:200] + "..." if len(content) > 200 else content
    props = msg.get("props", {})
    vars = {
        f"{var_prefix}MESSAGE_ID": str(msg["id"]),
        f"{var_prefix}MESSAGE_TITLE": msg.get("title", ""),
        f"{var_prefix}MESSAGE_CONTENT": content,
        f"{var_prefix}MESSAGE_CONTENT_CUTTED": content_cut,
        f"{var_prefix}MESSAGE_TYPE": msg.get("type", ""),
        f"{var_prefix}MESSAGE_CATEGORY": msg.get("category", ""),
        f"{var_prefix}MESSAGE_TIME_AGO": _relative_time(msg.get("created_at", "")),
        f"{var_prefix}MESSAGE_CREATED_AT": msg.get("created_at", ""),
    }
    return render(item_template, vars)


def render_brief(
    brief_template: str,
    item_template: str,
    popup_messages: list[dict],
    normal_messages: list[dict],
    silent_messages: list[dict] | None = None,
    group_templates: dict[str, str] | None = None,
    extra_vars: dict[str, str] | None = None,
) -> str:
    """渲染消息简报（按类型聚合展示）"""
    popup_items = _render_grouped(popup_messages, item_template, group_templates=group_templates)
    normal_items = _render_grouped(normal_messages, item_template, group_templates=group_templates)
    silent_items = [_format_single_message(m, item_template) for m in (silent_messages or [])]

    vars = {
        "POPUP_MESSAGE_COUNT": str(len(popup_messages)),
        "MESSAGE_COUNT": str(len(normal_messages)),
        "SILENT_MESSAGE_COUNT": str(len(silent_messages or [])),
        "NEW_POPUP_MESSAGES": "\n".join(popup_items) if popup_items else "",
        "NEW_MESSAGES": "\n".join(normal_items) if normal_items else "",
        "NEW_SILENT_MESSAGES": "\n".join(silent_items) if silent_items else "",
    }
    if extra_vars:
        vars.update(extra_vars)
    return render(brief_template, vars)
