"""模板渲染引擎

支持:
  - {VAR}        内置变量替换
  - !{bash_cmd}  bash 命令执行（变量先展开再注入到命令中）
"""

import re
import subprocess
from typing import Any


_BUILTIN_VARS: dict[str, str] = {}


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
    }
    return render(item_template, vars)


def render_brief(
    brief_template: str,
    item_template: str,
    popup_messages: list[dict],
    normal_messages: list[dict],
) -> str:
    """渲染消息简报"""
    popup_items = [_format_single_message(m, item_template) for m in popup_messages]
    normal_items = [_format_single_message(m, item_template) for m in normal_messages]

    vars = {
        "POPUP_MESSAGE_COUNT": str(len(popup_messages)),
        "MESSAGE_COUNT": str(len(normal_messages)),
        "NEW_POPUP_MESSAGES": "\n".join(popup_items) if popup_items else "",
        "NEW_MESSAGES": "\n".join(normal_items) if normal_items else "",
    }
    return render(brief_template, vars)
