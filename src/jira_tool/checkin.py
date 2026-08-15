"""The interactive daily check-in flow.

Walks through each active ticket and lets you post a comment, log work,
transition/close the ticket with a resolution, or skip to the next one.
"""

from __future__ import annotations

import webbrowser
from typing import Any, Dict, List, Optional, Tuple

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table

from .config import Config
from .display import format_age, staleness_style
from .jira_client import JiraClient, JiraError
from .utils import build_default_jql, days_since, is_valid_duration

_MENU = (
    "[bold]c[/bold]omment  [bold]l[/bold]og work  [bold]d[/bold]one/close  "
    "[bold]o[/bold]pen in browser  [bold]n[/bold]ext  [bold]q[/bold]uit"
)

Action = Tuple[str, str, str]  # (issue key, action, detail)


def run_checkin(config: Config, console: Console, jql: Optional[str] = None) -> None:
    client = JiraClient(config)
    issues = client.search_issues(jql or build_default_jql(config))
    if not issues:
        console.print("[green]No active tickets found — nothing to check in on.[/green]")
        return

    console.print(f"\n[bold]{len(issues)} active ticket(s) to review.[/bold]")
    actions: List[Action] = []
    for index, issue in enumerate(issues, start=1):
        if not _review_issue(client, config, console, issue, index, len(issues), actions):
            break
    _print_summary(console, actions)


def _review_issue(
    client: JiraClient,
    config: Config,
    console: Console,
    issue: Dict[str, Any],
    index: int,
    total: int,
    actions: List[Action],
) -> bool:
    """Review one issue. Returns False when the user quits the check-in."""
    key = issue["key"]
    console.print(_issue_panel(config, issue, index, total))
    while True:
        console.print(_MENU)
        choice = Prompt.ask("Action", choices=["c", "l", "d", "o", "n", "q"], default="n")
        try:
            if choice == "c":
                _do_comment(client, console, key, actions)
            elif choice == "l":
                _do_worklog(client, console, key, actions)
            elif choice == "d":
                if _do_close(client, console, key, actions):
                    return True  # ticket closed, move on to the next one
            elif choice == "o":
                url = config.browse_url(key)
                console.print(f"[dim]{url}[/dim]")
                webbrowser.open(url)
            elif choice == "n":
                return True
            elif choice == "q":
                return False
        except JiraError as exc:
            console.print(f"[red]{exc}[/red]")


def _issue_panel(config: Config, issue: Dict[str, Any], index: int, total: int) -> Panel:
    fields = issue["fields"]
    key = issue["key"]
    age = days_since(fields["updated"])
    style = staleness_style(age, config)
    body = (
        f"[bold]{fields.get('summary', '')}[/bold]\n"
        f"{fields.get('issuetype', {}).get('name', '?')} · "
        f"{fields.get('status', {}).get('name', '?')} · "
        f"last updated [{style}]{format_age(age)}[/{style}]\n"
        f"[dim]{config.browse_url(key)}[/dim]"
    )
    return Panel(body, title=f"[bold cyan]{key}[/bold cyan] ({index}/{total})")


def _read_multiline(console: Console, intro: str) -> str:
    console.print(f"[dim]{intro} Finish with an empty line; empty input cancels.[/dim]")
    lines: List[str] = []
    while True:
        line = console.input()
        if not line:
            break
        lines.append(line)
    return "\n".join(lines).strip()


def _do_comment(client: JiraClient, console: Console, key: str, actions: List[Action]) -> None:
    body = _read_multiline(console, f"Comment on {key}.")
    if not body:
        console.print("[dim]Cancelled.[/dim]")
        return
    client.add_comment(key, body)
    actions.append((key, "comment", _truncate(body)))
    console.print(f"[green]Comment added to {key}.[/green]")


def _do_worklog(client: JiraClient, console: Console, key: str, actions: List[Action]) -> None:
    while True:
        time_spent = Prompt.ask("Time spent (e.g. 45m, 2h, 1h 30m; empty cancels)", default="")
        if not time_spent:
            console.print("[dim]Cancelled.[/dim]")
            return
        if is_valid_duration(time_spent):
            break
        console.print("[red]That doesn't look like a Jira duration (use w/d/h/m units).[/red]")
    description = Prompt.ask("What did you work on? (optional)", default="")
    client.add_worklog(key, time_spent, comment=description)
    actions.append((key, "worklog", time_spent))
    console.print(f"[green]Logged {time_spent} on {key}.[/green]")


def _do_close(client: JiraClient, console: Console, key: str, actions: List[Action]) -> bool:
    """Transition the issue, optionally with a resolution. Returns True if transitioned."""
    transitions = client.get_transitions(key)
    if not transitions:
        console.print("[yellow]No transitions available for this ticket.[/yellow]")
        return False
    console.print("Available transitions:")
    for number, transition in enumerate(transitions, start=1):
        target = transition.get("to", {}).get("name", "?")
        console.print(f"  {number}. {transition['name']} [dim]→ {target}[/dim]")
    choice = IntPrompt.ask("Transition (0 cancels)", default=0)
    if choice < 1 or choice > len(transitions):
        console.print("[dim]Cancelled.[/dim]")
        return False
    transition = transitions[choice - 1]

    resolution = _pick_resolution(client, console)
    comment = _read_multiline(console, "Closing comment (optional).")
    if comment:
        client.add_comment(key, comment)
        actions.append((key, "comment", _truncate(comment)))

    try:
        client.transition_issue(key, transition["id"], resolution_name=resolution)
    except JiraError:
        if not resolution:
            raise
        # Some workflows don't expose the resolution field on this
        # transition's screen; retry without it rather than failing.
        client.transition_issue(key, transition["id"])
        console.print(
            "[yellow]The workflow didn't accept a resolution on this transition; "
            "it was applied without one.[/yellow]"
        )
        resolution = None
    detail = transition["name"] + (f" ({resolution})" if resolution else "")
    actions.append((key, "transition", detail))
    console.print(f"[green]{key}: {detail}.[/green]")
    return True


def _pick_resolution(client: JiraClient, console: Console) -> Optional[str]:
    if not Confirm.ask("Set a resolution?", default=True):
        return None
    resolutions = client.get_resolutions()
    if not resolutions:
        console.print("[yellow]No resolutions defined on this Jira instance.[/yellow]")
        return None
    names = [resolution["name"] for resolution in resolutions]
    default = "Done" if "Done" in names else names[0]
    return Prompt.ask("Resolution", choices=names, default=default)


def _truncate(text: str, limit: int = 60) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _print_summary(console: Console, actions: List[Action]) -> None:
    if not actions:
        console.print("\n[dim]Check-in finished — no updates were made.[/dim]")
        return
    table = Table(title="Check-in summary", title_justify="left")
    table.add_column("Key", style="bold cyan", no_wrap=True)
    table.add_column("Action", no_wrap=True)
    table.add_column("Detail")
    for key, action, detail in actions:
        table.add_row(key, action, detail)
    console.print()
    console.print(table)
