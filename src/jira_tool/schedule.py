"""Create and manage scheduled ``jira-tool remind`` reminders.

On Windows this drives ``schtasks`` (one task per reminder time); on
Linux/macOS it manages a delimited block in the user's crontab. Reminders
run windowlessly: the scheduled action invokes ``pythonw -m jira_tool`` on
Windows, so no console window flashes when the reminder fires.

Each reminder carries its own set of weekdays, so a Sun–Thu week and a
Mon–Fri week can coexist, and removing one reminder never rewrites another's
days.
"""

from __future__ import annotations

import csv
import io
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from rich.console import Console

TASK_PREFIX = "JiraCheckinReminder"
_TASK_NAME_RE = re.compile(rf"{TASK_PREFIX}_(\d{{2}})(\d{{2}})$")
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")

CRON_BEGIN = "# >>> jira-tool reminders >>>"
CRON_END = "# <<< jira-tool reminders <<<"

# Canonical week order used for output and range expansion.
_DAY_ORDER = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_ALL_DAYS = list(_DAY_ORDER)
_WEEKDAYS = ["mon", "tue", "wed", "thu", "fri"]
_WEEKENDS = ["sat", "sun"]

_DAY_ALIASES = {
    "mon": "mon", "monday": "mon",
    "tue": "tue", "tues": "tue", "tuesday": "tue",
    "wed": "wed", "weds": "wed", "wednesday": "wed",
    "thu": "thu", "thur": "thu", "thurs": "thu", "thursday": "thu",
    "fri": "fri", "friday": "fri",
    "sat": "sat", "saturday": "sat",
    "sun": "sun", "sunday": "sun",
}
_DAY_KEYWORDS = {
    "weekday": _WEEKDAYS, "weekdays": _WEEKDAYS,
    "weekend": _WEEKENDS, "weekends": _WEEKENDS,
    "daily": _ALL_DAYS, "all": _ALL_DAYS, "everyday": _ALL_DAYS,
}
_DAY_TO_SCHTASKS = {d: d.upper() for d in _DAY_ORDER}
# cron day-of-week: Sunday is 0 (0–6, Mon=1 … Sat=6).
_DAY_TO_CRON = {"mon": "1", "tue": "2", "wed": "3", "thu": "4", "fri": "5", "sat": "6", "sun": "0"}


class ScheduleError(Exception):
    """Raised when a scheduler backend can't be driven, or input is invalid."""


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
    return sorted(set(times))


def _resolve_day(token: str) -> str:
    try:
        return _DAY_ALIASES[token]
    except KeyError:
        raise ScheduleError(
            f"{token!r} is not a day. Use mon/tue/.../sun, a range like sun-thu, "
            "or a keyword (weekdays, weekends, daily)."
        )


def parse_days(spec: str) -> List[str]:
    """Turn a day spec into canonical day keys (Mon-first order).

    Accepts a keyword (``weekdays``/``weekends``/``daily``), a comma list of day
    names (``sun,mon,tue``), or ranges that may wrap the week (``sun-thu``,
    ``fri-mon``). Full names and common abbreviations both work.
    """
    value = spec.strip().lower()
    if not value:
        raise ScheduleError("No days given.")
    if value in _DAY_KEYWORDS:
        return list(_DAY_KEYWORDS[value])

    selected = set()
    for token in value.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            start, end = (part.strip() for part in token.split("-", 1))
            selected.update(_expand_range(_resolve_day(start), _resolve_day(end)))
        else:
            selected.add(_resolve_day(token))
    if not selected:
        raise ScheduleError("No days given.")
    return [day for day in _DAY_ORDER if day in selected]


def _expand_range(start: str, end: str) -> List[str]:
    start_i, end_i = _DAY_ORDER.index(start), _DAY_ORDER.index(end)
    span = (end_i - start_i) % 7
    return [_DAY_ORDER[(start_i + step) % 7] for step in range(span + 1)]


def format_days(days: Sequence[str]) -> str:
    """Human-readable day summary for listings."""
    days = [d for d in _DAY_ORDER if d in set(days)]
    if days == _ALL_DAYS:
        return "every day"
    if days == _WEEKDAYS:
        return "weekdays"
    if days == _WEEKENDS:
        return "weekends"
    return ", ".join(d.capitalize() for d in days)


def schtasks_day_list(days: Sequence[str]) -> str:
    return ",".join(_DAY_TO_SCHTASKS[d] for d in _DAY_ORDER if d in set(days))


def cron_day_field(days: Sequence[str]) -> str:
    chosen = [d for d in _DAY_ORDER if d in set(days)]
    if chosen == _ALL_DAYS:
        return "*"
    return ",".join(_DAY_TO_CRON[d] for d in chosen)


def cron_field_to_days(field: str) -> List[str]:
    """Inverse of cron_day_field, for reading a reminder's days back."""
    if field.strip() == "*":
        return list(_ALL_DAYS)
    number_to_day = {v: k for k, v in _DAY_TO_CRON.items()}
    days = {number_to_day[num] for num in field.split(",") if num in number_to_day}
    return [d for d in _DAY_ORDER if d in days]


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


def windows_create_args(time_str: str, day_list: str, action: str) -> List[str]:
    return [
        "schtasks", "/Create", "/F",
        "/TN", task_name(time_str),
        "/TR", action,
        "/SC", "WEEKLY",
        "/D", day_list,
        "/ST", time_str,
    ]


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
    kept: List[str] = []
    inside = False
    for line in crontab_text.splitlines():
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


def parse_block_entries(crontab_text: str) -> Dict[str, str]:
    """Read the managed block as an ordered {time: cron-day-field} mapping."""
    entries: Dict[str, str] = {}
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
            if len(fields) >= 6 and fields[0].isdigit() and fields[1].isdigit():
                time_str = f"{int(fields[1]):02d}:{int(fields[0]):02d}"
                entries[time_str] = fields[4]
    return entries


def build_crontab(existing: str, entries: Dict[str, str], command: str) -> str:
    """Rebuild the crontab, replacing the managed block with ``entries``."""
    base = strip_managed_block(existing)
    if not entries:
        return base
    block = [CRON_BEGIN]
    block += [cron_line(time_str, entries[time_str], command) for time_str in sorted(entries)]
    block.append(CRON_END)
    return base + "\n".join(block) + "\n"


# --- execution wrappers --------------------------------------------------------


def _run(args: Sequence[str]) -> Tuple[int, str]:
    try:
        result = subprocess.run(list(args), capture_output=True, text=True, timeout=30)
    except FileNotFoundError as exc:
        raise ScheduleError(f"Could not run {args[0]!r}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ScheduleError(f"{args[0]} timed out.") from exc
    return result.returncode, (result.stdout + result.stderr).strip()


def add_reminders(times: List[str], days: List[str], notify: bool, console: Console) -> None:
    if sys.platform == "win32":
        _windows_add(times, days, notify, console)
    else:
        _cron_add(times, days, notify, console)


def remove_reminders(times: Optional[List[str]], console: Console) -> None:
    if sys.platform == "win32":
        _windows_remove(times, console)
    else:
        _cron_remove(times, console)


def list_reminders(console: Console) -> None:
    if sys.platform == "win32":
        times = _windows_list()
        if not times:
            console.print("[dim]No jira-tool reminders scheduled.[/dim]")
            return
        console.print("[bold]Scheduled jira-tool reminders:[/bold]")
        for time_str in times:
            console.print(f"  • {time_str}")
        console.print("[dim](run 'schedule add' again to change a time's days)[/dim]")
    else:
        entries = parse_block_entries(_read_crontab())
        if not entries:
            console.print("[dim]No jira-tool reminders scheduled.[/dim]")
            return
        console.print("[bold]Scheduled jira-tool reminders:[/bold]")
        for time_str in sorted(entries):
            days = format_days(cron_field_to_days(entries[time_str]))
            console.print(f"  • {time_str}  [dim]({days})[/dim]")


# --- Windows backend -----------------------------------------------------------


def _windows_add(times: List[str], days: List[str], notify: bool, console: Console) -> None:
    action = windows_action(windowless_python(), notify)
    day_list = schtasks_day_list(days)
    for time_str in times:
        code, output = _run(windows_create_args(time_str, day_list, action))
        if code == 0:
            console.print(f"[green]Scheduled reminder at {time_str} ({format_days(days)}).[/green]")
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


def _write_crontab(text: str) -> None:
    result = subprocess.run(["crontab", "-"], input=text, text=True, capture_output=True)
    if result.returncode != 0:
        raise ScheduleError(f"Could not update crontab: {result.stderr.strip()}")


def _cron_add(times: List[str], days: List[str], notify: bool, console: Console) -> None:
    existing = _read_crontab()
    entries = parse_block_entries(existing)
    field = cron_day_field(days)
    for time_str in times:
        entries[time_str] = field
    _write_crontab(build_crontab(existing, entries, cron_command(windowless_python(), notify)))
    for time_str in times:
        console.print(f"[green]Scheduled reminder at {time_str} ({format_days(days)}).[/green]")
    if sys.platform.startswith("linux"):
        console.print(
            "[dim]cron runs headless; DISPLAY=:0 is set so notify-send can reach "
            "your desktop session.[/dim]"
        )


def _cron_remove(times: Optional[List[str]], console: Console) -> None:
    existing = _read_crontab()
    entries = parse_block_entries(existing)
    if not entries:
        console.print("[dim]No jira-tool reminders to remove.[/dim]")
        return
    removed = list(entries) if not times else [t for t in times if t in entries]
    if times:
        for time_str in times:
            entries.pop(time_str, None)
    else:
        entries = {}
    _write_crontab(build_crontab(existing, entries, cron_command(windowless_python(), True)))
    for time_str in sorted(removed):
        console.print(f"[green]Removed reminder at {time_str}.[/green]")
