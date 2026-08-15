"""Rendering helpers shared by the CLI commands."""

from __future__ import annotations

from typing import Any, Dict, List

from rich.table import Table

from .config import Config
from .utils import days_since


def staleness_style(days: float, config: Config) -> str:
    if days >= config.stale_after_days:
        return "bold red"
    if days >= 1:
        return "yellow"
    return "green"


def format_age(days: float) -> str:
    if days < 1:
        hours = days * 24
        return f"{hours:.0f}h ago"
    return f"{days:.1f}d ago"


def issue_table(issues: List[Dict[str, Any]], config: Config) -> Table:
    table = Table(title="Active tickets", title_justify="left")
    table.add_column("Key", style="bold cyan", no_wrap=True)
    table.add_column("Type", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Last updated", no_wrap=True)
    table.add_column("Summary")
    for issue in issues:
        fields = issue["fields"]
        age = days_since(fields["updated"])
        style = staleness_style(age, config)
        table.add_row(
            issue["key"],
            fields.get("issuetype", {}).get("name", "?"),
            fields.get("status", {}).get("name", "?"),
            f"[{style}]{format_age(age)}[/{style}]",
            fields.get("summary", ""),
        )
    return table
