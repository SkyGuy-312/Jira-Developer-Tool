import json
import os
import stat

import pytest

from jira_tool.config import Config, ConfigError, load_config, save_config


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setenv("JIRA_TOOL_CONFIG", str(path))
    monkeypatch.delenv("JIRA_TOOL_TOKEN", raising=False)
    return path


def test_save_and_load_roundtrip(config_file):
    original = Config(
        base_url="https://jira.example.com",
        auth_method="pat",
        token="secret",
        statuses=["In Progress"],
        stale_after_days=3,
    )
    save_config(original)
    assert load_config() == original


def test_saved_file_is_owner_only(config_file):
    save_config(Config(base_url="https://jira.example.com"))
    mode = stat.S_IMODE(os.stat(config_file).st_mode)
    assert mode == 0o600


def test_load_missing_config_raises(config_file):
    with pytest.raises(ConfigError, match="jira-tool setup"):
        load_config()


def test_load_rejects_unknown_keys(config_file):
    config_file.write_text(json.dumps({"base_url": "https://x", "surprise": 1}))
    with pytest.raises(ConfigError, match="surprise"):
        load_config()


def test_trailing_slash_is_stripped():
    config = Config(base_url="https://jira.example.com/")
    assert config.base_url == "https://jira.example.com"
    assert config.browse_url("PROJ-1") == "https://jira.example.com/browse/PROJ-1"


def test_invalid_auth_method_rejected():
    with pytest.raises(ConfigError):
        Config(base_url="https://jira.example.com", auth_method="oauth")


def test_env_token_overrides_config(config_file, monkeypatch):
    config = Config(base_url="https://jira.example.com", token="from-file")
    assert config.effective_token == "from-file"
    monkeypatch.setenv("JIRA_TOOL_TOKEN", "from-env")
    assert config.effective_token == "from-env"
