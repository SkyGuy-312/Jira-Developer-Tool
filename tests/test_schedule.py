import io

import pytest
from rich.console import Console

from jira_tool import schedule
from jira_tool.schedule import (
    CRON_BEGIN,
    CRON_END,
    ScheduleError,
    build_crontab,
    cron_command,
    cron_day_field,
    cron_field_to_days,
    cron_line,
    format_days,
    normalize_times,
    parse_block_entries,
    parse_days,
    parse_task_times,
    schtasks_day_list,
    strip_managed_block,
    task_name,
    windows_action,
    windows_create_args,
)


# --- times ---------------------------------------------------------------------


def test_normalize_times_dedups_sorts_and_pads():
    assert normalize_times(["16:30", "9:05", "16:30"]) == ["09:05", "16:30"]


def test_normalize_times_splits_commas():
    assert normalize_times(["12:00,16:30"]) == ["12:00", "16:30"]


@pytest.mark.parametrize("bad", ["25:00", "12:60", "abc", "1230", "12:5"])
def test_normalize_times_rejects_invalid(bad):
    with pytest.raises(ScheduleError):
        normalize_times([bad])


# --- days ----------------------------------------------------------------------


def test_parse_days_keywords():
    assert parse_days("weekdays") == ["mon", "tue", "wed", "thu", "fri"]
    assert parse_days("weekends") == ["sat", "sun"]
    assert parse_days("daily") == ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def test_parse_days_list_is_ordered_and_deduped():
    assert parse_days("fri,mon,mon,wed") == ["mon", "wed", "fri"]


def test_parse_days_accepts_full_names():
    assert parse_days("sunday,monday") == ["mon", "sun"]


def test_parse_days_simple_range():
    assert parse_days("mon-fri") == ["mon", "tue", "wed", "thu", "fri"]


def test_parse_days_range_wraps_the_week():
    # Sun–Thu is the common Gulf working week; it wraps past Sunday.
    assert parse_days("sun-thu") == ["mon", "tue", "wed", "thu", "sun"]
    assert parse_days("fri-mon") == ["mon", "fri", "sat", "sun"]


def test_parse_days_rejects_unknown():
    with pytest.raises(ScheduleError):
        parse_days("funday")


def test_format_days_uses_friendly_labels():
    assert format_days(["mon", "tue", "wed", "thu", "fri"]) == "weekdays"
    assert format_days(["sat", "sun"]) == "weekends"
    assert format_days(schedule._ALL_DAYS) == "every day"
    # Explicit lists render Sunday-first.
    assert format_days(["sun", "mon", "tue"]) == "Sun, Mon, Tue"
    assert format_days(parse_days("sun-thu")) == "Sun, Mon, Tue, Wed, Thu"


def test_schtasks_day_list_is_upper_and_ordered():
    assert schtasks_day_list(["sun", "mon", "wed"]) == "MON,WED,SUN"


def test_cron_day_field_round_trip():
    assert cron_day_field(["mon", "tue", "wed", "thu", "fri"]) == "1,2,3,4,5"
    assert cron_day_field(schedule._ALL_DAYS) == "*"
    assert cron_field_to_days("1,2,3,4,0") == ["mon", "tue", "wed", "thu", "sun"]
    assert cron_field_to_days("*") == schedule._ALL_DAYS


# --- Windows command building --------------------------------------------------


def test_task_name_strips_colon():
    assert task_name("16:30") == "JiraCheckinReminder_1630"


def test_windows_action_includes_notify_and_module():
    action = windows_action(r"C:\Py\pythonw.exe", notify=True)
    assert action == r'"C:\Py\pythonw.exe" -m jira_tool remind --notify'


def test_windows_action_without_notify():
    assert windows_action("pythonw.exe", notify=False) == '"pythonw.exe" -m jira_tool remind'


def test_windows_create_args_uses_day_list():
    args = windows_create_args("16:30", "SUN,MON,TUE,WED,THU", "ACTION")
    assert args[:6] == ["schtasks", "/Create", "/F", "/TN", "JiraCheckinReminder_1630", "/TR"]
    assert args[args.index("/SC") + 1] == "WEEKLY"
    assert args[args.index("/D") + 1] == "SUN,MON,TUE,WED,THU"
    assert args[args.index("/ST") + 1] == "16:30"


def test_parse_task_times_reads_encoded_times():
    csv_text = (
        '"\\JiraCheckinReminder_1630","N/A","Ready"\n'
        '"\\JiraCheckinReminder_0900","N/A","Ready"\n'
        '"\\Some Other Task","N/A","Ready"\n'
    )
    assert parse_task_times(csv_text) == ["09:00", "16:30"]


# --- cron block building -------------------------------------------------------


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


def test_parse_block_entries_keeps_per_time_days():
    text = f"{CRON_BEGIN}\n30 16 * * 1,2,3,4,0 CMD\n0 9 * * 1-5 CMD\n{CRON_END}\n"
    assert parse_block_entries(text) == {"16:30": "1,2,3,4,0", "09:00": "1-5"}


def test_build_crontab_is_idempotent():
    first = build_crontab("", {"16:30": "1-5"}, "CMD")
    second = build_crontab(first, parse_block_entries(first), "CMD")
    assert first == second
    assert first.count(CRON_BEGIN) == 1
    assert "30 16 * * 1-5 CMD" in first


def test_build_crontab_preserves_foreign_lines():
    result = build_crontab("0 9 * * * backup\n", {"12:00": "*"}, "CMD")
    assert result.startswith("0 9 * * * backup\n")
    assert "0 12 * * * CMD" in result


def test_build_crontab_empty_entries_clears_block():
    existing = f"keep\n{CRON_BEGIN}\n30 16 * * 1-5 CMD\n{CRON_END}\n"
    assert build_crontab(existing, {}, "CMD") == "keep\n"


# --- end-to-end cron flow ------------------------------------------------------


class FakeCrontab:
    """In-memory stand-in for the user's crontab for flow testing."""

    def __init__(self):
        self.text = ""

    def read(self):
        return self.text

    def write(self, text):
        self.text = text


def _use_cron_backend(monkeypatch):
    fake = FakeCrontab()
    monkeypatch.setattr(schedule.sys, "platform", "linux")
    monkeypatch.setattr(schedule, "_read_crontab", fake.read)
    monkeypatch.setattr(schedule, "_write_crontab", fake.write)
    monkeypatch.setattr(schedule, "windowless_python", lambda: "/usr/bin/python3")
    return fake, Console(file=io.StringIO())


def test_cron_flow_preserves_each_reminders_days(monkeypatch):
    fake, console = _use_cron_backend(monkeypatch)

    # A Sun–Thu reminder and a Mon–Fri reminder, added separately.
    schedule.add_reminders(["09:00"], parse_days("sun-thu"), True, console)
    schedule.add_reminders(["16:30"], parse_days("mon-fri"), True, console)
    entries = parse_block_entries(fake.text)
    assert entries == {"09:00": "1,2,3,4,0", "16:30": "1,2,3,4,5"}

    # Removing one must not rewrite the other's days.
    schedule.add_reminders(["12:30"], parse_days("weekends"), True, console)
    schedule.remove_reminders(["12:30"], console)
    assert parse_block_entries(fake.text) == {"09:00": "1,2,3,4,0", "16:30": "1,2,3,4,5"}

    schedule.remove_reminders(None, console)
    assert parse_block_entries(fake.text) == {}
    assert CRON_BEGIN not in fake.text
