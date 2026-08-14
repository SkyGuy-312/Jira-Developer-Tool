from datetime import datetime, timedelta, timezone

import pytest

from jira_tool.config import Config
from jira_tool.utils import (
    build_default_jql,
    days_since,
    format_jira_datetime,
    is_valid_duration,
    parse_jira_datetime,
)


@pytest.mark.parametrize("value", ["45m", "2h", "1h 30m", "1d 2h", "3w", " 90m "])
def test_valid_durations(value):
    assert is_valid_duration(value)


@pytest.mark.parametrize("value", ["", "abc", "2 hours", "h30", "30x", "1h30m"])
def test_invalid_durations(value):
    assert not is_valid_duration(value)


def test_days_since():
    two_days_ago = datetime.now(timezone.utc) - timedelta(days=2)
    value = two_days_ago.strftime("%Y-%m-%dT%H:%M:%S.000%z")
    assert days_since(value) == pytest.approx(2.0, abs=0.01)


def test_jira_datetime_roundtrip():
    stamp = "2026-08-14T10:22:33.000+0300"
    assert format_jira_datetime(parse_jira_datetime(stamp)) == stamp


def test_build_default_jql_quotes_statuses():
    config = Config(base_url="https://jira.example.com", statuses=["In Progress", "In Review"])
    jql = build_default_jql(config)
    assert 'status in ("In Progress", "In Review")' in jql
    assert "assignee = currentUser()" in jql
