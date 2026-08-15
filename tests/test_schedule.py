import pytest

from jira_tool import schedule
from jira_tool.schedule import (
    CRON_BEGIN,
    CRON_END,
    ScheduleError,
    build_crontab,
    cron_command,
    cron_line,
    normalize_times,
    parse_block_times,
    parse_task_times,
    strip_managed_block,
    task_name,
    windows_action,
    windows_create_args,
)


def test_normalize_times_dedups_sorts_and_pads():
    assert normalize_times(["16:30", "9:05", "16:30"]) == ["09:05", "16:30"]


def test_normalize_times_splits_commas():
    assert normalize_times(["12:00,16:30"]) == ["12:00", "16:30"]


@pytest.mark.parametrize("bad", ["25:00", "12:60", "abc", "1230", "12:5"])
def test_normalize_times_rejects_invalid(bad):
    with pytest.raises(ScheduleError):
        normalize_times([bad])


def test_task_name_strips_colon():
    assert task_name("16:30") == "JiraCheckinReminder_1630"


def test_windows_action_includes_notify_and_module():
    action = windows_action(r"C:\Py\pythonw.exe", notify=True)
    assert action == r'"C:\Py\pythonw.exe" -m jira_tool remind --notify'


def test_windows_action_without_notify():
    action = windows_action("pythonw.exe", notify=False)
    assert action == '"pythonw.exe" -m jira_tool remind'


def test_windows_create_args_weekdays():
    args = windows_create_args("16:30", "weekdays", "ACTION")
    assert args[:6] == ["schtasks", "/Create", "/F", "/TN", "JiraCheckinReminder_1630", "/TR"]
    assert "/D" in args and "MON,TUE,WED,THU,FRI" in args
    assert args[args.index("/SC") + 1] == "WEEKLY"
    assert args[args.index("/ST") + 1] == "16:30"


def test_windows_create_args_daily_omits_day_list():
    args = windows_create_args("08:00", "daily", "ACTION")
    assert "/D" not in args
    assert args[args.index("/SC") + 1] == "DAILY"


def test_parse_task_times_reads_encoded_times():
    csv_text = (
        '"\\JiraCheckinReminder_1630","N/A","Ready"\n'
        '"\\JiraCheckinReminder_0900","N/A","Ready"\n'
        '"\\Some Other Task","N/A","Ready"\n'
    )
    assert parse_task_times(csv_text) == ["09:00", "16:30"]


def test_cron_line_orders_minute_then_hour():
    assert cron_line("16:30", "1-5", "CMD") == "30 16 * * 1-5 CMD"


def test_cron_command_quotes_python(monkeypatch):
    monkeypatch.setattr(schedule.sys, "platform", "darwin")
    assert cron_command("/usr/bin/python3", True) == '"/usr/bin/python3" -m jira_tool remind --notify'


def test_cron_command_adds_display_on_linux(monkeypatch):
    monkeypatch.setattr(schedule.sys, "platform", "linux")
    assert cron_command("/usr/bin/python3", True).startswith("DISPLAY=:0 ")


def test_strip_managed_block_removes_only_the_block():
    text = f"0 9 * * * other\n{CRON_BEGIN}\n30 16 * * 1-5 CMD\n{CRON_END}\n"
    assert strip_managed_block(text) == "0 9 * * * other\n"


def test_parse_block_times_reads_the_block():
    text = f"{CRON_BEGIN}\n30 16 * * 1-5 CMD\n0 9 * * 1-5 CMD\n{CRON_END}\n"
    assert parse_block_times(text) == ["09:00", "16:30"]


def test_build_crontab_is_idempotent():
    first = build_crontab("", ["16:30"], "1-5", "CMD")
    second = build_crontab(first, ["16:30"], "1-5", "CMD")
    assert first == second
    assert first.count(CRON_BEGIN) == 1
    assert "30 16 * * 1-5 CMD" in first


def test_build_crontab_preserves_foreign_lines():
    existing = "0 9 * * * backup\n"
    result = build_crontab(existing, ["12:00"], "*", "CMD")
    assert result.startswith("0 9 * * * backup\n")
    assert "0 12 * * * CMD" in result


def test_build_crontab_empty_times_clears_block():
    existing = f"keep\n{CRON_BEGIN}\n30 16 * * 1-5 CMD\n{CRON_END}\n"
    assert build_crontab(existing, [], "1-5", "CMD") == "keep\n"


class FakeCrontab:
    """In-memory stand-in for the user's crontab for flow testing."""

    def __init__(self):
        self.text = ""

    def read(self):
        return self.text

    def write(self, text, console):
        self.text = text


def _use_cron_backend(monkeypatch):
    """Force the non-Windows (cron) backend with an in-memory crontab."""
    from rich.console import Console

    from jira_tool import schedule as sched

    fake = FakeCrontab()
    monkeypatch.setattr(sched.sys, "platform", "linux")
    monkeypatch.setattr(sched, "_read_crontab", fake.read)
    monkeypatch.setattr(sched, "_write_crontab", fake.write)
    monkeypatch.setattr(sched, "windowless_python", lambda: "/usr/bin/python3")
    return sched, fake, Console(file=__import__("io").StringIO())


def test_cron_add_list_remove_flow(monkeypatch):
    sched, fake, console = _use_cron_backend(monkeypatch)

    sched.add_reminders(["16:30", "12:30"], "weekdays", True, console)
    assert parse_block_times(fake.text) == ["12:30", "16:30"]

    # Adding another time unions with the existing ones.
    sched.add_reminders(["09:00"], "weekdays", True, console)
    assert parse_block_times(fake.text) == ["09:00", "12:30", "16:30"]

    # Removing a single time leaves the rest.
    sched.remove_reminders(["12:30"], console)
    assert parse_block_times(fake.text) == ["09:00", "16:30"]

    # Removing with no times clears everything.
    sched.remove_reminders(None, console)
    assert parse_block_times(fake.text) == []
    assert CRON_BEGIN not in fake.text
