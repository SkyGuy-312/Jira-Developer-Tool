"""The daily reminder: report tickets that have gone quiet.

Designed to be run from cron / a scheduled task; ``--notify`` additionally
raises a desktop notification when stale tickets exist.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from typing import Any, Dict, List

from rich.console import Console

from .config import Config
from .display import issue_table
from .jira_client import JiraClient
from .utils import build_default_jql, days_since


def run_remind(config: Config, console: Console, notify: bool = False) -> int:
    """Print the active-ticket report. Returns the number of stale tickets."""
    client = JiraClient(config)
    issues = client.search_issues(build_default_jql(config))
    if not issues:
        console.print("[green]No active tickets.[/green]")
        return 0

    stale = [
        issue
        for issue in issues
        if days_since(issue["fields"]["updated"]) >= config.stale_after_days
    ]
    console.print(issue_table(issues, config))
    if stale:
        keys = ", ".join(issue["key"] for issue in stale)
        console.print(
            f"\n[bold red]{len(stale)} ticket(s) with no update for "
            f"{config.stale_after_days:g}+ days:[/bold red] {keys}"
        )
        console.print("Run [bold]jira-tool checkin[/bold] to update them.")
        if notify:
            _desktop_notify(
                "Jira check-in needed",
                f"{len(stale)} ticket(s) need a status update: {keys}",
                console,
            )
    else:
        console.print("\n[green]All active tickets have recent updates. Nice.[/green]")
    return len(stale)


def _desktop_notify(title: str, message: str, console: Console) -> None:
    if shutil.which("notify-send"):
        command = ["notify-send", title, message]
    elif sys.platform == "darwin" and shutil.which("osascript"):
        script = f'display notification "{message}" with title "{title}"'
        command = ["osascript", "-e", script]
    else:
        console.print("[dim]No desktop notification tool found; skipping --notify.[/dim]")
        return
    try:
        subprocess.run(command, check=False, timeout=10)
    except OSError as exc:
        console.print(f"[dim]Desktop notification failed: {exc}[/dim]")
