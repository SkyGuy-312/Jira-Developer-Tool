"""Command-line entry points for jira-tool."""

from __future__ import annotations

import json as jsonlib
import sys
from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console
from rich.prompt import Confirm, FloatPrompt, Prompt

from . import __version__, read, render
from .cache import Cache
from .checkin import run_checkin
from .config import DEFAULT_STATUSES, Config, ConfigError, load_config, save_config
from .display import issue_table
from .jira_client import JiraClient, JiraError
from .mcp_server import serve
from .remind import run_remind, send_test_notification
from .schedule import (
    ScheduleError,
    add_reminders,
    list_reminders,
    normalize_times,
    parse_days,
    remove_reminders,
)
from .utils import build_default_jql

app = typer.Typer(
    help="Keep your Jira tickets updated with a daily interactive check-in.",
    no_args_is_help=True,
    add_completion=False,
)
schedule_app = typer.Typer(
    help="Create and manage scheduled daily reminders.",
    no_args_is_help=True,
)
app.add_typer(schedule_app, name="schedule")
console = Console()


def _load_config_or_exit() -> Config:
    try:
        return load_config()
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)


def _emit(text: str) -> None:
    """Write rendered markdown out verbatim.

    Not console.print: the render is full of '[In Progress]' and rich would
    read those as style tags. Reconfiguring first keeps the arrows and dashes
    from blowing up on a legacy Windows code page.
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    print(text)


@app.command()
def setup() -> None:
    """Create the config file interactively and test the connection."""
    base_url = Prompt.ask("Jira base URL (e.g. https://jira.mycompany.com)").strip()
    auth_method = Prompt.ask(
        "Auth method ('pat' = personal access token, Jira 8.14+; 'basic' = username/password)",
        choices=["pat", "basic"],
        default="pat",
    )
    username = ""
    if auth_method == "basic":
        username = Prompt.ask("Username")
    secret_label = "Personal access token" if auth_method == "pat" else "Password"
    token = Prompt.ask(f"{secret_label} (stored in the config file, or leave empty and set JIRA_TOOL_TOKEN)", password=True, default="")
    statuses_raw = Prompt.ask(
        "Statuses to track (comma-separated)", default=", ".join(DEFAULT_STATUSES)
    )
    statuses: List[str] = [part.strip() for part in statuses_raw.split(",") if part.strip()]
    stale_after_days = FloatPrompt.ask(
        "Days without an update before a ticket counts as stale", default=2.0
    )

    config = Config(
        base_url=base_url,
        auth_method=auth_method,
        username=username,
        token=token,
        statuses=statuses or list(DEFAULT_STATUSES),
        stale_after_days=stale_after_days,
    )

    console.print("Testing the connection…")
    try:
        me = JiraClient(config).myself()
    except JiraError as exc:
        console.print(f"[red]Connection failed: {exc}[/red]")
        console.print(
            "[dim]Hint: for an internal certificate authority, set \"verify_ssl\" "
            "in the config file to the path of your CA bundle.[/dim]"
        )
        if not Confirm.ask("Save the config anyway?", default=True):
            raise typer.Exit(code=1)
    else:
        name = me.get("displayName") or me.get("name") or "unknown user"
        console.print(f"[green]Connected as {name}.[/green]")

    path = save_config(config)
    console.print(f"Config saved to [bold]{path}[/bold].")


@app.command("list")
def list_cmd(
    jql: Optional[str] = typer.Option(None, "--jql", help="Override the default JQL query."),
) -> None:
    """List your active tickets and how stale each one is."""
    config = _load_config_or_exit()
    try:
        issues = JiraClient(config).search_issues(jql or build_default_jql(config))
    except JiraError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)
    if not issues:
        console.print("[green]No active tickets.[/green]")
        return
    console.print(issue_table(issues, config))


@app.command()
def checkin(
    jql: Optional[str] = typer.Option(None, "--jql", help="Override the default JQL query."),
) -> None:
    """Walk through your active tickets: comment, log work, or close each one."""
    config = _load_config_or_exit()
    try:
        run_checkin(config, console, jql=jql)
    except JiraError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)


@app.command()
def show(
    key: str = typer.Argument(..., help="Issue key, e.g. PROJ-1234."),
    comments: bool = typer.Option(True, "--comments/--no-comments"),
    history: bool = typer.Option(True, "--history/--no-history"),
    max_comments: int = typer.Option(
        0, "--max-comments", help="Show only the newest N comments (0 = all)."
    ),
    refresh: bool = typer.Option(False, "--refresh", help="Bypass the cache."),
    as_json: bool = typer.Option(False, "--json", help="Emit the normalised issue."),
) -> None:
    """Print a full issue as markdown: fields, description, comments, history."""
    config = _load_config_or_exit()
    try:
        issue = read.fetch_issue(
            JiraClient(config), config, key.strip().upper(), refresh=refresh
        )
    except JiraError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)
    if as_json:
        _emit(jsonlib.dumps(issue, indent=2, default=str))
        return
    _emit(
        render.render_issue(
            issue,
            comments=comments,
            history=history,
            max_comments=max_comments or None,
        )
    )


@app.command()
def search(
    jql: str = typer.Argument(..., help="A JQL query."),
    limit: int = typer.Option(25, "--limit", help="Maximum issues to return."),
) -> None:
    """Run a JQL query and list the matches."""
    config = _load_config_or_exit()
    try:
        rows = read.search(JiraClient(config), jql, limit=limit)
    except JiraError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)
    _emit(render.render_search(rows, jql))


@app.command()
def attach(
    key: str = typer.Argument(..., help="Issue key, e.g. PROJ-1234."),
    filename: Optional[str] = typer.Argument(
        None, help="Attachment to download; omit to list them."
    ),
    out: Optional[Path] = typer.Option(
        None, "--out", help="Directory to save into (default: the cache dir)."
    ),
) -> None:
    """List an issue's attachments, or download one of them."""
    config = _load_config_or_exit()
    client = JiraClient(config)
    try:
        issue = read.fetch_issue(client, config, key.strip().upper())
        if filename is None:
            if not issue["attachments"]:
                console.print("[dim]No attachments.[/dim]")
                return
            for att in issue["attachments"]:
                console.print(
                    f"{att['filename']}  ({read.human_size(att.get('size'))},"
                    f" {att.get('mime') or '?'})"
                )
            return
        result = read.fetch_attachment(client, issue, filename, out_dir=out)
    except JiraError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)
    _emit(render.render_attachment(result))


@app.command("cache-clear")
def cache_clear(
    key: Optional[str] = typer.Argument(None, help="Issue key; omit to clear all."),
) -> None:
    """Drop cached issues so the next read comes straight from Jira."""
    config = _load_config_or_exit()
    removed = Cache(config.cache_ttl_minutes * 60).clear(key.upper() if key else None)
    console.print(f"Cleared {removed} cached issue(s).")


@app.command()
def mcp() -> None:
    """Serve the read-only Jira tools over MCP stdio (for Claude Code etc.)."""
    serve()


@app.command()
def remind(
    notify: bool = typer.Option(
        False, "--notify", help="Also raise a desktop notification when tickets are stale."
    ),
) -> None:
    """Report tickets that have gone quiet. Meant for cron / scheduled tasks."""
    config = _load_config_or_exit()
    try:
        run_remind(config, console, notify=notify)
    except JiraError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)


@schedule_app.command("add")
def schedule_add(
    at: List[str] = typer.Option(
        ...,
        "--at",
        help="Reminder time HH:MM (24h). Repeat or comma-separate for several, "
        "e.g. --at 12:30 --at 16:30.",
    ),
    days: Optional[str] = typer.Option(
        None,
        "--days",
        help="Which days to run: a keyword (weekdays, weekends, daily), a list "
        "(sun,mon,tue), or a range (sun-thu). Default: weekdays.",
    ),
    daily: bool = typer.Option(
        False, "--daily", help="Shortcut for --days daily (every day)."
    ),
    notify: bool = typer.Option(
        True, "--notify/--no-notify", help="Raise a desktop notification (default: on)."
    ),
) -> None:
    """Schedule one or more daily reminders (runs 'remind' in the background)."""
    day_spec = days if days else ("daily" if daily else "weekdays")
    try:
        times = normalize_times(at)
        day_list = parse_days(day_spec)
    except ScheduleError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)
    if not times:
        console.print("[red]Pass at least one --at HH:MM.[/red]")
        raise typer.Exit(code=1)
    try:
        add_reminders(times, day_list, notify, console)
    except ScheduleError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)


@schedule_app.command("list")
def schedule_list() -> None:
    """List the reminders jira-tool has scheduled."""
    try:
        list_reminders(console)
    except ScheduleError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)


@schedule_app.command("remove")
def schedule_remove(
    at: Optional[List[str]] = typer.Option(
        None,
        "--at",
        help="Remove only these times; omit to remove all jira-tool reminders.",
    ),
) -> None:
    """Remove scheduled reminders (all of them, or just the given --at times)."""
    try:
        times = normalize_times(at) if at else None
        remove_reminders(times, console)
    except ScheduleError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)


@app.command("notify-test")
def notify_test() -> None:
    """Raise a sample desktop notification to verify --notify works."""
    send_test_notification(console)
    console.print("Sent a test notification. If none appeared, see the note above.")


@app.command()
def version() -> None:
    """Print the jira-tool version."""
    console.print(__version__)


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:
        console.print("\n[dim]Aborted.[/dim]")
        raise SystemExit(130)


if __name__ == "__main__":
    main()
