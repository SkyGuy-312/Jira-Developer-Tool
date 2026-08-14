"""Command-line entry points for jira-tool."""

from __future__ import annotations

from typing import List, Optional

import typer
from rich.console import Console
from rich.prompt import Confirm, FloatPrompt, Prompt

from . import __version__
from .checkin import run_checkin
from .config import DEFAULT_STATUSES, Config, ConfigError, load_config, save_config
from .display import issue_table
from .jira_client import JiraClient, JiraError
from .remind import run_remind
from .utils import build_default_jql

app = typer.Typer(
    help="Keep your Jira tickets updated with a daily interactive check-in.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


def _load_config_or_exit() -> Config:
    try:
        return load_config()
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)


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
