"""Configuration loading and saving.

The config file lives at ``~/.config/jira-tool/config.json`` (override the
location with the ``JIRA_TOOL_CONFIG`` environment variable). The API token
can be kept out of the file entirely by setting ``JIRA_TOOL_TOKEN`` instead.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Union

DEFAULT_STATUSES = ["In Progress", "In Review"]


class ConfigError(Exception):
    """Raised when the configuration is missing or invalid."""


def config_path() -> Path:
    override = os.environ.get("JIRA_TOOL_CONFIG")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME", "~/.config")
    return Path(xdg).expanduser() / "jira-tool" / "config.json"


@dataclass
class Config:
    base_url: str
    auth_method: str = "pat"  # "pat" (Bearer token, Jira 8.14+) or "basic"
    username: str = ""
    token: str = ""
    statuses: List[str] = field(default_factory=lambda: list(DEFAULT_STATUSES))
    stale_after_days: float = 2.0
    verify_ssl: Union[bool, str] = True  # True/False, or a path to a CA bundle
    # Git-aware drafting: local repositories to scan for recent commits.
    repos: List[str] = field(default_factory=list)
    git_lookback_days: float = 14.0
    # Only commits by this author feed drafts; empty = each repo's user.email.
    git_author: str = ""

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        if self.auth_method not in ("pat", "basic"):
            raise ConfigError(
                f"Unknown auth_method {self.auth_method!r}; use 'pat' or 'basic'."
            )
        if not self.base_url:
            raise ConfigError("base_url must not be empty.")

    @property
    def effective_token(self) -> str:
        return os.environ.get("JIRA_TOOL_TOKEN") or self.token

    def browse_url(self, issue_key: str) -> str:
        return f"{self.base_url}/browse/{issue_key}"


def load_config() -> Config:
    path = config_path()
    if not path.exists():
        raise ConfigError(
            f"No config found at {path}. Run 'jira-tool setup' to create one."
        )
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Config at {path} is not valid JSON: {exc}") from exc
    known = {f for f in Config.__dataclass_fields__}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"Unknown config keys in {path}: {', '.join(sorted(unknown))}")
    try:
        return Config(**data)
    except TypeError as exc:
        raise ConfigError(f"Config at {path} is incomplete: {exc}") from exc


def save_config(config: Config) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(config), indent=2) + "\n")
    # The file may hold a token, so keep it readable by the owner only.
    os.chmod(path, 0o600)
    return path
