"""The daily reminder: report tickets that have gone quiet.

Designed to be run from cron / a scheduled task; ``--notify`` additionally
raises a desktop notification when stale tickets exist, or when the tool
can't reach Jira at all (e.g. the VPN is down) so the failure isn't silent.
"""

from __future__ import annotations

import base64
import shutil
import subprocess
import sys
from typing import List, Optional, Tuple

from rich.console import Console

from .config import Config
from .display import issue_table
from .jira_client import JiraClient, JiraError
from .utils import build_default_jql, days_since


def run_remind(config: Config, console: Console, notify: bool = False) -> int:
    """Print the active-ticket report. Returns the number of stale tickets.

    Raises JiraError if Jira can't be reached; when ``notify`` is set, a
    desktop notification about the failure is raised before re-raising so an
    unattended scheduled run still surfaces the problem.
    """
    client = JiraClient(config)
    try:
        issues = client.search_issues(build_default_jql(config))
    except JiraError as exc:
        if notify:
            title, message = _failure_notification(config, exc)
            _desktop_notify(title, message, console)
        raise
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


def _failure_notification(config: Config, error: JiraError) -> Tuple[str, str]:
    """Craft a toast title/body for a failed Jira fetch.

    A missing HTTP status means the request never got a response — most often
    the VPN or network is down — so we nudge toward that. A real status code
    means Jira answered but rejected us (auth, permissions, bad JQL).
    """
    host = config.base_url.split("://")[-1]
    if error.status_code is None:
        return (
            "Jira reminder: can't connect",
            f"Couldn't reach {host}. Is the VPN on? "
            "Reconnect, then run jira-tool checkin.",
        )
    return (
        f"Jira reminder: error {error.status_code}",
        f"{host} rejected the request (HTTP {error.status_code}). "
        "Check your token, then run jira-tool checkin.",
    )


def _desktop_notify(title: str, message: str, console: Console) -> None:
    command = _notify_command(title, message)
    if command is None:
        console.print(f"[dim]{_no_notifier_hint()}[/dim]")
        return
    try:
        subprocess.run(command, check=False, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        console.print(f"[dim]Desktop notification failed: {exc}[/dim]")


def _notify_command(title: str, message: str) -> Optional[List[str]]:
    """Build the notification command for this platform, or None if unsupported."""
    if shutil.which("notify-send"):
        return ["notify-send", "-a", "jira-tool", title, message]
    if sys.platform == "darwin" and shutil.which("osascript"):
        script = (
            f"display notification {_applescript_string(message)} "
            f"with title {_applescript_string(title)}"
        )
        return ["osascript", "-e", script]
    powershell = _find_powershell()
    if powershell:
        return [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            _encode_powershell(_toast_script(title, message)),
        ]
    return None


def _find_powershell() -> Optional[str]:
    if sys.platform == "win32":
        # Prefer Windows PowerShell 5.1: it ships with Windows and can load
        # the WinRT toast types directly, which pwsh 7 cannot.
        return shutil.which("powershell") or shutil.which("pwsh")
    # Inside WSL, Windows PowerShell is usually reachable as powershell.exe.
    return shutil.which("powershell.exe")


def _no_notifier_hint() -> str:
    if sys.platform.startswith("linux"):
        return (
            "No notification tool found; install libnotify to enable --notify "
            "(e.g. 'sudo apt install libnotify-bin')."
        )
    if sys.platform == "win32":
        return "PowerShell was not found on PATH; cannot raise a desktop notification."
    return "No desktop notification tool found; skipping --notify."


def _applescript_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _powershell_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _toast_script(title: str, message: str) -> str:
    # Uses Windows PowerShell's registered AppUserModelID so the toast is
    # allowed to display without registering our own app identity.
    app_id = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
    return "\n".join(
        [
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null",
            "$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)",
            "$texts = $template.GetElementsByTagName('text')",
            f"$null = $texts.Item(0).AppendChild($template.CreateTextNode({_powershell_string(title)}))",
            f"$null = $texts.Item(1).AppendChild($template.CreateTextNode({_powershell_string(message)}))",
            f"$appId = '{app_id}'",
            "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show([Windows.UI.Notifications.ToastNotification]::new($template))",
        ]
    )


def _encode_powershell(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")
