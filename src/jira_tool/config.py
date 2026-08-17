"""Configuration loading and saving.

The config file lives at ``~/.config/jira-tool/config.json`` (override the
location with the ``JIRA_TOOL_CONFIG`` environment variable). The API token
can be kept out of the file entirely by setting ``JIRA_TOOL_TOKEN`` instead.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Union

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

    # -- read layer (jira-tool show / search / mcp) --------------------------
    # Custom fields to render, by display name. Empty = every non-empty one.
    custom_fields: List[str] = field(default_factory=list)
    # Field display names to drop from the rendered issue, allowlist or not.
    hide_fields: List[str] = field(default_factory=list)
    # Extra cross-references to pull out of issue text, {label: regex}. Issue
    # keys are always extracted; this is for domain codes, e.g.
    # {"DTCs": "\\b[BCPU][0-9A-F]{5}\\b"}.
    ref_patterns: Dict[str, str] = field(default_factory=dict)
    # How long a cached issue is served without asking Jira whether it moved.
    cache_ttl_minutes: float = 15.0

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        if self.auth_method not in ("pat", "basic"):
            raise ConfigError(
                f"Unknown auth_method {self.auth_method!r}; use 'pat' or 'basic'."
            )
        if not self.base_url:
            raise ConfigError("base_url must not be empty.")
        for label, pattern in self.ref_patterns.items():
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ConfigError(
                    f"ref_patterns[{label!r}] is not a valid regex: {exc}"
                ) from exc

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
