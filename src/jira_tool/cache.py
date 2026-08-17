"""On-disk cache for fetched issues.

Jira is live, so a cache that only expires on a timer would happily serve a
ticket that moved five minutes ago. Instead the TTL only decides how long an
entry is served *blind*; past it, one cheap request for the issue's ``updated``
timestamp says whether the expensive fetch is needed at all.

The cache also carries the tool through a dropped VPN: a stale entry is served
with a note rather than failing outright.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional

_SAFE_KEY = re.compile(r"[^A-Za-z0-9_.-]")


def cache_dir() -> Path:
    override = os.environ.get("JIRA_TOOL_CACHE")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CACHE_HOME") or os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base).expanduser() / "jira-tool"
    return Path("~/.cache/jira-tool").expanduser()


def attachment_dir(issue_key: str) -> Path:
    return cache_dir() / "attachments" / _SAFE_KEY.sub("_", issue_key)


class Cache:
    """A tiny JSON file cache. Any read error is treated as a miss."""

    def __init__(self, ttl_seconds: float, directory: Optional[Path] = None):
        self.ttl_seconds = ttl_seconds
        self.directory = directory or (cache_dir() / "issues")

    def _path(self, key: str) -> Path:
        return self.directory / f"{_SAFE_KEY.sub('_', key)}.json"

    def load(self, key: str) -> Optional[Dict[str, Any]]:
        path = self._path(key)
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(entry, dict) or "payload" not in entry:
            return None
        entry["age_seconds"] = max(0.0, time.time() - entry.get("fetched_at", 0))
        entry["fresh"] = entry["age_seconds"] < self.ttl_seconds
        return entry

    def store(self, key: str, payload: Any, updated: Optional[str] = None) -> None:
        path = self._path(key)
        entry = {"fetched_at": time.time(), "updated": updated, "payload": payload}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(entry), encoding="utf-8")
        except OSError:
            pass  # a cache that cannot be written must not break the command

    def touch(self, key: str) -> None:
        """Reset the TTL after confirming an entry is still current."""
        entry = self.load(key)
        if entry is not None:
            self.store(key, entry["payload"], entry.get("updated"))

    def clear(self, key: Optional[str] = None) -> int:
        if key:
            paths = [self._path(key)]
        else:
            paths = list(self.directory.glob("*.json")) if self.directory.exists() else []
        removed = 0
        for path in paths:
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        return removed
