"""测试 YAML 配置管理"""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from msgbox.yaml_config import (
    add_rule,
    get_config_value,
    list_rules,
    load_config,
    remove_rule,
    save_config,
    set_config_value,
)


@pytest.fixture
def cfg_dir():
    with tempfile.TemporaryDirectory() as d:
        with patch("msgbox.yaml_config.config.PLUGIN_DIR", Path(d)):
            with patch("msgbox.yaml_config.config.CONFIG_FILE", Path(d) / "config.yaml"):
                yield d


class TestLoadSave:
    def test_default_config(self, cfg_dir):
        cfg = load_config()
        assert "rules" in cfg
        assert "templates" in cfg
        assert cfg["rules"]["popup"] == []

    def test_save_and_load(self, cfg_dir):
        save_config({"rules": {"popup": [{"type": "test"}]}, "templates": {}})
        cfg = load_config()
        assert cfg["rules"]["popup"] == [{"type": "test"}]


class TestGetSet:
    def test_get_value(self, cfg_dir):
        save_config({"rules": {"popup": [{"type": "alert"}]}, "templates": {}})
        assert get_config_value("rules.popup") == [{"type": "alert"}]

    def test_set_value(self, cfg_dir):
        set_config_value("rules.popup", [{"type": "test"}])
        assert get_config_value("rules.popup") == [{"type": "test"}]

    def test_unknown_key(self, cfg_dir):
        assert get_config_value("nonexistent") is None


class TestRules:
    def test_add_and_list(self, cfg_dir):
        add_rule("popup", "alert\\.critical", {"env": "prod"})
        rules = list_rules()
        assert len(rules) == 1
        assert rules[0]["type"] == "popup"
        assert rules[0]["pattern"] == "alert\\.critical"

    def test_add_no_props(self, cfg_dir):
        add_rule("silent", "heartbeat")
        rules = list_rules()
        assert rules[0]["props"] == {}

    def test_remove(self, cfg_dir):
        add_rule("popup", "a")
        add_rule("popup", "b")
        remove_rule("popup", 0)
        rules = list_rules()
        assert len(rules) == 1
        assert rules[0]["pattern"] == "b"

    def test_remove_out_of_range(self, cfg_dir):
        add_rule("popup", "a")
        remove_rule("popup", 99)  # 不应报错
        assert len(list_rules()) == 1
