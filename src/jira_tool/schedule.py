"""Create and manage scheduled ``jira-tool remind`` reminders.

On Windows this drives ``schtasks`` (one task per reminder time); on
Linux/macOS it manages a delimited block in the user's crontab. Reminders
run windowlessly: the scheduled action invokes ``pythonw -m jira_tool`` on
Windows, so no console window flashes when the reminder fires.
"""

from __future__ import annotations

import csv
import io
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from rich.console import Console

TASK_PREFIX = "JiraCheckinReminder"
_TASK_NAME_RE = re.compile(rf"{TASK_PREFIX}_(\d{{2}})(\d{{2}})$")
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")

CRON_BEGIN = "# >>> jira-tool reminders >>>"
CRON_END = "# <<< jira-tool reminders <<<"

# days key -> (schtasks schedule, schtasks /D list or None, cron day-of-week field)
_DAYS = {
    "weekdays": ("WEEKLY", "MON,TUE,WED,THU,FRI", "1-5"),
    "daily": ("DAILY", None, "*"),
}


class ScheduleError(Exception):
    """Raised when a scheduler backend can't be driven."""


# --- pure helpers (unit-tested) ------------------------------------------------


def normalize_times(raw: Sequence[str]) -> List[str]:
    """Split, validate and canonicalize ``HH:MM`` times (comma-splitting each)."""
    times: List[str] = []
    for chunk in raw:
        for part in chunk.split(","):
            part = part.strip()
            if not part:
                continue
            match = _TIME_RE.match(part)
            if not match:
                raise ScheduleError(f"{part!r} is not a valid HH:MM time.")
            hour, minute = int(match.group(1)), int(match.group(2))
            if hour > 23 or minute > 59:
                raise ScheduleError(f"{part!r} is out of range (use 00:00–23:59).")
            times.append(f"{hour:02d}:{minute:02d}")
    # de-duplicate while keeping chronological order
    return sorted(set(times))


def task_name(time_str: str) -> str:
    return f"{TASK_PREFIX}_{time_str.replace(':', '')}"


def windowless_python() -> str:
    """Path to the interpreter the scheduled action should run.

    Prefers ``pythonw.exe`` on Windows so the reminder runs with no console.
    """
    exe = Path(sys.executable)
    if sys.platform == "win32":
        windowless = exe.with_name("pythonw.exe")
        if windowless.exists():
            return str(windowless)
    return str(exe)


def windows_action(python_exe: str, notify: bool) -> str:
    """The command line stored in the scheduled task's /TR field."""
    flags = " remind" + (" --notify" if notify else "")
    return f'"{python_exe}" -m jira_tool{flags}'


def windows_create_args(time_str: str, days_key: str, action: str) -> List[str]:
    schedule, day_list, _ = _DAYS[days_key]
    args = ["schtasks", "/Create", "/F", "/TN", task_name(time_str), "/TR", action, "/SC", schedule]
    if day_list:
        args += ["/D", day_list]
    args += ["/ST", time_str]
    return args


def parse_task_times(query_csv: str) -> List[str]:
    """Extract reminder times from ``schtasks /Query /FO CSV /NH`` output."""
    times: List[str] = []
    for row in csv.reader(io.StringIO(query_csv)):
        if not row:
            continue
        name = row[0].strip().lstrip("\\")
        match = _TASK_NAME_RE.match(name)
        if match:
            times.append(f"{match.group(1)}:{match.group(2)}")
    return sorted(set(times))


def cron_command(python_exe: str, notify: bool) -> str:
    flags = " remind" + (" --notify" if notify else "")
    prefix = "DISPLAY=:0 " if sys.platform.startswith("linux") else ""
    return f'{prefix}"{python_exe}" -m jira_tool{flags}'


def cron_line(time_str: str, days_field: str, command: str) -> str:
    hour, minute = time_str.split(":")
    return f"{int(minute)} {int(hour)} * * {days_field} {command}"


def strip_managed_block(crontab_text: str) -> str:
    """Remove the jira-tool managed block from crontab text."""
    lines = crontab_text.splitlines()
    kept: List[str] = []
    inside = False
    for line in lines:
        if line.strip() == CRON_BEGIN:
            inside = True
            continue
        if line.strip() == CRON_END:
            inside = False
            continue
        if not inside:
            kept.append(line)
    text = "\n".join(kept).strip("\n")
    return text + "\n" if text else ""


def parse_block_times(crontab_text: str) -> List[str]:
    """Read the reminder times currently in the managed block."""
    times: List[str] = []
    inside = False
    for line in crontab_text.splitlines():
        if line.strip() == CRON_BEGIN:
            inside = True
            continue
        if line.strip() == CRON_END:
            inside = False
            continue
        if inside:
            fields = line.split()
            if len(fields) >= 2 and fields[0].isdigit() and fields[1].isdigit():
                times.append(f"{int(fields[1]):02d}:{int(fields[0]):02d}")
    return sorted(set(times))


def build_crontab(existing: str, times: Sequence[str], days_field: str, command: str) -> str:
    base = strip_managed_block(existing)
    if not times:
        return base
    block = [CRON_BEGIN]
    block += [cron_line(time_str, days_field, command) for time_str in times]
    block.append(CRON_END)
    prefix = base if base.endswith("\n") or not base else base + "\n"
    return prefix + "\n".join(block) + "\n"


# --- execution wrappers --------------------------------------------------------


def _run(args: Sequence[str], stdin: Optional[str] = None) -> Tuple[int, str]:
    try:
        result = subprocess.run(
            list(args),
            input=stdin,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError as exc:
        raise ScheduleError(f"Could not run {args[0]!r}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ScheduleError(f"{args[0]} timed out.") from exc
    return result.returncode, (result.stdout + result.stderr).strip()


def add_reminders(times: List[str], days_key: str, notify: bool, console: Console) -> None:
    if sys.platform == "win32":
        _windows_add(times, days_key, notify, console)
    else:
        _cron_add(times, days_key, notify, console)


def remove_reminders(times: Optional[List[str]], console: Console) -> None:
    if sys.platform == "win32":
        _windows_remove(times, console)
    else:
        _cron_remove(times, console)


def list_reminders(console: Console) -> None:
    if sys.platform == "win32":
        current = _windows_list()
    else:
        current = parse_block_times(_read_crontab())
    if not current:
        console.print("[dim]No jira-tool reminders scheduled.[/dim]")
        return
    console.print("[bold]Scheduled jira-tool reminders:[/bold]")
    for time_str in current:
        console.print(f"  • {time_str}")


# --- Windows backend -----------------------------------------------------------


def _windows_add(times: List[str], days_key: str, notify: bool, console: Console) -> None:
    action = windows_action(windowless_python(), notify)
    for time_str in times:
        code, output = _run(windows_create_args(time_str, days_key, action))
        if code == 0:
            console.print(f"[green]Scheduled reminder at {time_str}.[/green]")
        else:
            console.print(f"[red]Failed to schedule {time_str}: {output}[/red]")
    console.print(
        "[dim]Reminders run windowless via pythonw. They fire only while you're "
        "logged in (so the toast can reach your desktop).[/dim]"
    )


def _windows_list() -> List[str]:
    code, output = _run(["schtasks", "/Query", "/FO", "CSV", "/NH"])
    if code != 0:
        raise ScheduleError(f"Could not query scheduled tasks: {output}")
    return parse_task_times(output)


def _windows_remove(times: Optional[List[str]], console: Console) -> None:
    targets = times if times else _windows_list()
    if not targets:
        console.print("[dim]No jira-tool reminders to remove.[/dim]")
        return
    for time_str in targets:
        code, output = _run(["schtasks", "/Delete", "/F", "/TN", task_name(time_str)])
        if code == 0:
            console.print(f"[green]Removed reminder at {time_str}.[/green]")
        else:
            console.print(f"[yellow]Could not remove {time_str}: {output}[/yellow]")


# --- cron backend --------------------------------------------------------------


def _read_crontab() -> str:
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    # A missing crontab exits non-zero with "no crontab for <user>" — treat as empty.
    return result.stdout if result.returncode == 0 else ""


def _write_crontab(text: str, console: Console) -> None:
    result = subprocess.run(["crontab", "-"], input=text, text=True, capture_output=True)
    if result.returncode != 0:
        raise ScheduleError(f"Could not update crontab: {result.stderr.strip()}")


def _cron_add(times: List[str], days_key: str, notify: bool, console: Console) -> None:
    _, _, days_field = _DAYS[days_key]
    existing = _read_crontab()
    merged = sorted(set(parse_block_times(existing)) | set(times))
    command = cron_command(windowless_python(), notify)
    _write_crontab(build_crontab(existing, merged, days_field, command), console)
    for time_str in times:
        console.print(f"[green]Scheduled reminder at {time_str}.[/green]")
    if sys.platform.startswith("linux"):
        console.print(
            "[dim]cron runs headless; DISPLAY=:0 is set so notify-send can reach "
            "your desktop session.[/dim]"
        )


def _cron_remove(times: Optional[List[str]], console: Console) -> None:
    existing = _read_crontab()
    current = parse_block_times(existing)
    if not current:
        console.print("[dim]No jira-tool reminders to remove.[/dim]")
        return
    remaining = [t for t in current if t not in times] if times else []
    _, _, days_field = _DAYS["weekdays"]
    command = cron_command(windowless_python(), True)
    _write_crontab(build_crontab(existing, remaining, days_field, command), console)
    removed = [t for t in current if t not in remaining]
    for time_str in removed:
        console.print(f"[green]Removed reminder at {time_str}.[/green]")
