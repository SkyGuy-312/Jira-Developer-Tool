import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from jira_tool.git_activity import (
    Commit,
    GitActivityError,
    TicketActivity,
    collect_activity,
    draft_comment,
    estimate_minutes,
    extract_keys,
    format_duration,
)


def ts(hour, minute=0, day=15):
    return datetime(2026, 8, day, hour, minute, tzinfo=timezone.utc)


# --- key extraction ------------------------------------------------------------


def test_extract_keys_from_subject():
    assert extract_keys("PROJ-123: fix login redirect") == ["PROJ-123"]


def test_extract_keys_lowercase_branch_uppercased():
    assert extract_keys("proj-123-fix-login") == ["PROJ-123"]


def test_extract_keys_multiple_deduped_ordered():
    assert extract_keys("PROJ-1 relates to ABC-2 and PROJ-1") == ["PROJ-1", "ABC-2"]


def test_extract_keys_none():
    assert extract_keys("fix flaky test") == []


# --- time estimation -----------------------------------------------------------


def test_estimate_single_commit_is_lead_in():
    assert estimate_minutes([ts(10)]) == 30


def test_estimate_one_session_spans_plus_lead_in():
    # 10:00 -> 10:20 span (20m) + 30m lead-in = 50m, rounded up to 60m.
    assert estimate_minutes([ts(10), ts(10, 20)]) == 60


def test_estimate_gap_splits_sessions():
    # Session 1: 10:00-10:20 (+30) = 50m; session 2: 14:00 (+30) = 30m; 80 -> 90m.
    assert estimate_minutes([ts(10), ts(10, 20), ts(14)]) == 90


def test_estimate_empty():
    assert estimate_minutes([]) == 0


def test_format_duration():
    assert format_duration(90) == "1h 30m"
    assert format_duration(60) == "1h"
    assert format_duration(45) == "45m"
    assert format_duration(0) == "0m"


# --- drafting ------------------------------------------------------------------


def make_commit(subject, repo="app", hour=10):
    return Commit(sha=subject, when=ts(hour), subject=subject, repo=repo, keys=["PROJ-1"])


def test_draft_comment_lists_subjects():
    activity = TicketActivity("PROJ-1", [make_commit("Fix redirect"), make_commit("Add test")])
    assert draft_comment(activity) == "Progress update:\n- Fix redirect\n- Add test"


def test_draft_comment_dedupes_and_annotates_multiple_repos():
    activity = TicketActivity(
        "PROJ-1",
        [make_commit("Fix redirect", "app"), make_commit("Fix redirect", "app"),
         make_commit("Bump client", "lib")],
    )
    text = draft_comment(activity)
    assert text.count("Fix redirect") == 1
    assert "- Fix redirect (app)" in text
    assert "- Bump client (lib)" in text


def test_newer_than_filters_commits():
    activity = TicketActivity("PROJ-1", [make_commit("old", hour=9), make_commit("new", hour=12)])
    recent = activity.newer_than(ts(10))
    assert [c.subject for c in recent.commits] == ["new"]


# --- end-to-end against a real git repository ----------------------------------


def git(repo, *args, when=None):
    env = None
    if when is not None:
        stamp = when.isoformat()
        env = {"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp,
               "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
               "HOME": str(repo), "PATH": "/usr/bin:/bin:/usr/local/bin"}
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, env=env
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "app"
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.email", "dev@example.com")
    git(path, "config", "user.name", "Dev")
    return path


def commit_file(repo_path, filename, subject, when):
    (repo_path / filename).write_text(subject)
    git(repo_path, "add", filename)
    git(repo_path, "commit", "-q", "-m", subject, when=when)


def test_collect_activity_from_real_repo(repo):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    commit_file(repo, "a.txt", "PROJ-7 fix the redirect", now - timedelta(hours=3))
    # A branch named after a ticket, with a keyless commit subject.
    git(repo, "checkout", "-q", "-b", "proj-9-add-cache")
    commit_file(repo, "b.txt", "wire up the cache layer", now - timedelta(hours=2))
    # A keyless commit on the default branch: attributed to nothing.
    git(repo, "checkout", "-q", "-")
    commit_file(repo, "c.txt", "chore: bump deps", now - timedelta(hours=1))

    activity = collect_activity([str(repo)], now - timedelta(days=1))

    assert set(activity) == {"PROJ-7", "PROJ-9"}
    assert [c.subject for c in activity["PROJ-7"].commits] == ["PROJ-7 fix the redirect"]
    assert [c.subject for c in activity["PROJ-9"].commits] == ["wire up the cache layer"]
    assert activity["PROJ-9"].commits[0].repo == "app"


def test_collect_activity_filters_by_author(repo):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    commit_file(repo, "a.txt", "PROJ-7 my work", now - timedelta(hours=2))
    git(repo, "config", "user.email", "someone-else@example.com")
    commit_file(repo, "b.txt", "PROJ-7 their work", now - timedelta(hours=1))

    activity = collect_activity([str(repo)], now - timedelta(days=1), author="dev@example.com")

    assert [c.subject for c in activity["PROJ-7"].commits] == ["PROJ-7 my work"]


def test_collect_activity_ignores_commits_outside_window(repo):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    commit_file(repo, "a.txt", "PROJ-7 ancient work", now - timedelta(days=30))
    commit_file(repo, "b.txt", "PROJ-7 recent work", now - timedelta(hours=1))

    activity = collect_activity([str(repo)], now - timedelta(days=14))

    assert [c.subject for c in activity["PROJ-7"].commits] == ["PROJ-7 recent work"]


def test_collect_activity_rejects_non_repo(tmp_path):
    with pytest.raises(GitActivityError, match="not a git repository"):
        collect_activity([str(tmp_path)], datetime.now(timezone.utc))
