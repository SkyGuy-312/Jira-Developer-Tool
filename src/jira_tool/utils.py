"""Small helpers shared across commands."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from .config import Config

# Jira duration format: one or more <number><unit> groups, e.g. "45m", "2h", "1h 30m", "1d 2h".
_DURATION_RE = re.compile(r"^\s*\d+\s*[wdhm](\s+\d+\s*[wdhm])*\s*$", re.IGNORECASE)

# Jira Server timestamps look like 2026-08-14T10:22:33.000+0300.
_JIRA_DATETIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%f%z"


def is_valid_duration(value: str) -> bool:
    return bool(_DURATION_RE.match(value))


def parse_jira_datetime(value: str) -> datetime:
    return datetime.strptime(value, _JIRA_DATETIME_FORMAT)


def format_jira_datetime(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000%z")


def days_since(value: str, now: Optional[datetime] = None) -> float:
    now = now or datetime.now(timezone.utc)
    delta = now - parse_jira_datetime(value)
    return delta.total_seconds() / 86400


def format_timestamp(value: Optional[str]) -> str:
    """A Jira timestamp as 'YYYY-MM-DD HH:MM', or the raw value if unparsable.

    Unlike parse_jira_datetime this never raises: rendering a whole issue must
    not fail because one instance emits a timestamp in an unexpected shape.
    """
    if not value:
        return "—"
    try:
        return parse_jira_datetime(value).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return str(value)


def build_default_jql(config: Config) -> str:
    quoted = ", ".join(f'"{status}"' for status in config.statuses)
    return (
        f"assignee = currentUser() AND status in ({quoted}) ORDER BY updated ASC"
    )
