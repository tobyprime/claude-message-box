"""测试模板渲染引擎"""

import pytest

from msgbox.template import (
    expand_vars,
    expand_bash,
    render,
    _format_single_message,
    render_brief,
    set_builtin_vars,
)


class TestExpandVars:
    def test_simple_var(self):
        set_builtin_vars({"NAME": "World"})
        assert expand_vars("Hello {NAME}") == "Hello World"

    def test_unknown_var_kept(self):
        assert expand_vars("{UNKNOWN}") == "{UNKNOWN}"

    def test_extra_vars_override(self):
        set_builtin_vars({"X": "default"})
        assert expand_vars("{X}", {"X": "override"}) == "override"

    def test_bash_syntax_not_touched(self):
        assert expand_vars("time: !{date}") == "time: !{date}"

    def test_multiple_vars(self):
        set_builtin_vars({"A": "1", "B": "2"})
        assert expand_vars("{A} + {B} = 3") == "1 + 2 = 3"

    def test_empty_vars_empty_result(self):
        set_builtin_vars({})
        assert expand_vars("") == ""


class TestExpandBash:
    def test_simple_cmd(self):
        result = expand_bash("hello !{echo world}")
        assert result == "hello world"

    def test_unknown_cmd_empty(self):
        result = expand_bash("!{nonexistent_cmd_xyz 2>/dev/null || true}")
        assert result == ""

    def test_no_bash_kept(self):
        assert expand_bash("plain text") == "plain text"

    def test_empty_cmd_stays_as_is(self):
        assert expand_bash("!{}") == "!{}"


class TestRender:
    def test_full_render(self):
        set_builtin_vars({"NAME": "Claude"})
        result = render("Hello {NAME}, today is !{echo Monday}")
        assert "Hello Claude" in result
        assert "Monday" in result

    def test_extra_vars(self):
        result = render("{MSG}", {"MSG": "hi"})
        assert result == "hi"


class TestFormatSingleMessage:
    def test_basic(self):
        msg = {"id": 1, "type": "test", "title": "Hello", "content": "World", "category": "normal", "props": "{}"}
        result = _format_single_message(msg, "[{MESSAGE_TYPE}] {MESSAGE_TITLE}: {MESSAGE_CONTENT}")
        assert result == "[test] Hello: World"

    def test_content_cut(self):
        msg = {"id": 2, "type": "a", "title": "b", "content": "x" * 300, "category": "normal", "props": "{}"}
        result = _format_single_message(msg, "{MESSAGE_CONTENT_CUTTED}")
        assert result.endswith("...")
        assert len(result) == 203  # 200 + ...

    def test_short_content_not_cut(self):
        msg = {"id": 3, "type": "a", "title": "b", "content": "short", "category": "normal", "props": "{}"}
        result = _format_single_message(msg, "{MESSAGE_CONTENT_CUTTED}")
        assert result == "short"

    def test_missing_fields(self):
        msg = {"id": 4, "type": "", "title": "", "content": "", "category": "", "props": "{}"}
        result = _format_single_message(msg, "{MESSAGE_TITLE}")
        assert result == ""


class TestRenderBrief:
    def test_all_categories(self):
        popups = [{"id": 1, "type": "alert", "title": "P1", "content": "pop", "category": "popup", "props": "{}"}]
        normals = [{"id": 2, "type": "info", "title": "N1", "content": "norm", "category": "normal", "props": "{}"}]
        silents = [{"id": 3, "type": "hb", "title": "S1", "content": "sil", "category": "silent", "props": "{}"}]

        brief_tpl = "POPUP({POPUP_MESSAGE_COUNT}):{NEW_POPUP_MESSAGES}\nNORM({MESSAGE_COUNT}):{NEW_MESSAGES}\nSIL({SILENT_MESSAGE_COUNT}):{NEW_SILENT_MESSAGES}"
        item_tpl = " [{MESSAGE_TITLE}]"

        result = render_brief(brief_tpl, item_tpl, popups, normals, silents)
        assert "POPUP(1): [P1]" in result
        assert "NORM(1): [N1]" in result
        assert "SIL(1): [S1]" in result

    def test_empty(self):
        result = render_brief("empty", "x", [], [], [])
        assert result == "empty"

    def test_bash_in_brief(self):
        popups = [{"id": 1, "type": "a", "title": "t", "content": "c", "category": "popup", "props": "{}"}]
        result = render_brief("{POPUP_MESSAGE_COUNT} !{echo OK}", "{MESSAGE_TITLE}", popups, [], [])
        assert "1" in result
        assert "OK" in result
