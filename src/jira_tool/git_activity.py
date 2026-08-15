"""Collect recent git activity and draft ticket updates from it.

The drafting heuristics are deliberately simple and transparent:

- A commit belongs to a ticket when the ticket key appears in its subject
  line. A commit with no key of its own is attributed to a branch's key when
  it sits only on that branch (not reachable from any other local branch)
  and the branch name carries the key — e.g. ``PROJ-123-fix-login``.
- Time is estimated by clustering commit timestamps into work sessions: a
  gap of an hour or more starts a new session. Each session counts from its
  first to its last commit plus half an hour of lead-in, and the total is
  rounded up to a quarter hour.

Everything produced here is a *draft* — the check-in flow shows it for you
to accept, edit, or ignore.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import ceil
from pathlib import Path
from typing import Dict, List, Optional, Sequence

TICKET_KEY_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9]+-\d+)\b")

_FIELD_SEP = "\x1f"
_LOG_FORMAT = f"%H{_FIELD_SEP}%aI{_FIELD_SEP}%s"
_SESSION_GAP = timedelta(minutes=60)
_SESSION_LEAD_IN = timedelta(minutes=30)
_ROUND_TO_MINUTES = 15


class GitActivityError(Exception):
    """Raised when a configured repository can't be read."""


@dataclass
class Commit:
    sha: str
    when: datetime
    subject: str
    repo: str  # repository directory name, for display
    keys: List[str]  # ticket keys attributed to this commit


@dataclass
class TicketActivity:
    key: str
    commits: List[Commit]  # chronological

    def newer_than(self, cutoff: datetime) -> "TicketActivity":
        return TicketActivity(self.key, [c for c in self.commits if c.when > cutoff])

    @property
    def repos(self) -> List[str]:
        seen: List[str] = []
        for commit in self.commits:
            if commit.repo not in seen:
                seen.append(commit.repo)
        return seen

    @property
    def estimated_minutes(self) -> int:
        return estimate_minutes([c.when for c in self.commits])


def extract_keys(text: str) -> List[str]:
    """Ticket keys found in text, uppercased, ordered, deduplicated."""
    keys: List[str] = []
    for match in TICKET_KEY_RE.finditer(text):
        key = match.group(1).upper()
        if key not in keys:
            keys.append(key)
    return keys


def estimate_minutes(times: Sequence[datetime]) -> int:
    """Estimate minutes worked from commit timestamps (see module docstring)."""
    if not times:
        return 0
    ordered = sorted(times)
    total = timedelta()
    session_start = previous = ordered[0]
    for current in ordered[1:]:
        if current - previous > _SESSION_GAP:
            total += (previous - session_start) + _SESSION_LEAD_IN
            session_start = current
        previous = current
    total += (previous - session_start) + _SESSION_LEAD_IN
    minutes = total.total_seconds() / 60
    return ceil(minutes / _ROUND_TO_MINUTES) * _ROUND_TO_MINUTES


def format_duration(minutes: int) -> str:
    """Render minutes as a Jira duration, e.g. 90 -> '1h 30m'."""
    hours, remainder = divmod(minutes, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if remainder or not hours:
        parts.append(f"{remainder}m")
    return " ".join(parts)


def draft_comment(activity: TicketActivity) -> str:
    """A plain-text progress comment drafted from the commit subjects."""
    annotate_repo = len(activity.repos) > 1
    lines = ["Progress update:"]
    for commit in activity.commits:
        line = f"- {commit.subject}"
        if annotate_repo:
            line += f" ({commit.repo})"
        if line not in lines:
            lines.append(line)
    return "\n".join(lines)


def collect_activity(
    repo_paths: Sequence[str],
    since: datetime,
    author: Optional[str] = None,
) -> Dict[str, TicketActivity]:
    """Scan the given repositories and group recent commits by ticket key."""
    by_key: Dict[str, List[Commit]] = {}
    for raw_path in repo_paths:
        path = Path(raw_path).expanduser()
        if not (path / ".git").exists():
            raise GitActivityError(f"{path} is not a git repository.")
        for commit in _repo_commits(path, since, author):
            for key in commit.keys:
                by_key.setdefault(key, []).append(commit)
    return {
        key: TicketActivity(key, sorted(commits, key=lambda c: c.when))
        for key, commits in by_key.items()
    }


def _repo_commits(path: Path, since: datetime, author: Optional[str]) -> List[Commit]:
    repo_name = path.resolve().name
    # Unless overridden, only the repo's own author shows up in drafts —
    # a teammate's commits on a shared branch are not your status update.
    repo_author = author or _config_default(path, "user.email")

    commits = _log(path, ["--all"], since, repo_author, repo_name)
    for commit in commits:
        commit.keys = extract_keys(commit.subject)

    # Attribute keyless commits via branch names: a commit that exists only
    # on PROJ-123-fix-login was work on PROJ-123 even if its subject doesn't
    # say so. Commits reachable from other branches (e.g. merged to main or
    # inherited from the branch point) are left out to avoid mis-attribution.
    by_sha = {commit.sha: commit for commit in commits}
    branches = [
        line.strip()
        for line in _git(path, "for-each-ref", "refs/heads", "--format=%(refname:short)").splitlines()
        if line.strip()
    ]
    for branch in branches:
        branch_keys = extract_keys(branch)
        if not branch_keys:
            continue
        exclusive = [branch, "--not"] + [other for other in branches if other != branch]
        for entry in _log(path, exclusive, since, repo_author, repo_name):
            commit = by_sha.get(entry.sha)
            if commit is not None and not commit.keys:
                commit.keys = list(branch_keys)
    return [commit for commit in commits if commit.keys]


def _log(
    path: Path,
    ref_args: List[str],
    since: datetime,
    author: str,
    repo_name: str,
) -> List[Commit]:
    args = [
        "log",
        *ref_args,
        "--no-merges",
        f"--since={since.isoformat()}",
        f"--format={_LOG_FORMAT}",
    ]
    if author:
        args += [f"--author={author}", "--regexp-ignore-case"]
    commits: List[Commit] = []
    for line in _git(path, *args).splitlines():
        parts = line.split(_FIELD_SEP, 2)
        if len(parts) != 3:
            continue
        sha, when_raw, subject = parts
        try:
            when = datetime.fromisoformat(when_raw)
        except ValueError:
            continue
        commits.append(Commit(sha=sha, when=when, subject=subject.strip(), repo=repo_name, keys=[]))
    return commits


def _git(path: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitActivityError(f"Could not run git in {path}: {exc}") from exc
    if result.returncode != 0:
        raise GitActivityError(f"git {args[0]} failed in {path}: {result.stderr.strip()}")
    return result.stdout


def _config_default(path: Path, key: str) -> str:
    """A git config value, or empty when unset (unlike _git, not an error)."""
    result = subprocess.run(
        ["git", "-C", str(path), "config", key],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout.strip() if result.returncode == 0 else ""
