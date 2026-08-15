import base64
from io import StringIO

import pytest
from rich.console import Console

from jira_tool import remind
from jira_tool.config import Config
from jira_tool.jira_client import JiraError
from jira_tool.remind import (
    _applescript_string,
    _desktop_notify,
    _failure_notification,
    _notify_command,
    _powershell_string,
    run_remind,
    send_test_notification,
)


class FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def make_config():
    return Config(base_url="https://jira.example.com", auth_method="pat", token="tok")


def which_returning(*available):
    def fake_which(name):
        return f"/usr/bin/{name}" if name in available else None

    return fake_which


def decode_powershell(encoded):
    return base64.b64decode(encoded).decode("utf-16-le")


def test_prefers_notify_send_when_available(monkeypatch):
    monkeypatch.setattr(remind.shutil, "which", which_returning("notify-send"))
    command = _notify_command("Title", "Message")
    assert command[0] == "notify-send"
    assert "Title" in command and "Message" in command


def test_macos_uses_osascript(monkeypatch):
    monkeypatch.setattr(remind.sys, "platform", "darwin")
    monkeypatch.setattr(remind.shutil, "which", which_returning("osascript"))
    command = _notify_command("Jira", 'Say "hi"')
    assert command[0] == "osascript"
    assert 'display notification "Say \\"hi\\""' in command[2]


def test_windows_uses_powershell_toast(monkeypatch):
    monkeypatch.setattr(remind.sys, "platform", "win32")
    monkeypatch.setattr(remind.shutil, "which", which_returning("powershell"))
    command = _notify_command("Jira check-in", "It's time")
    assert command[0] == "/usr/bin/powershell"
    assert "-EncodedCommand" in command
    script = decode_powershell(command[-1])
    assert "'Jira check-in'" in script
    assert "'It''s time'" in script  # single quotes doubled for PowerShell
    assert "ToastNotificationManager" in script


def test_wsl_falls_back_to_powershell_exe(monkeypatch):
    monkeypatch.setattr(remind.sys, "platform", "linux")
    monkeypatch.setattr(remind.shutil, "which", which_returning("powershell.exe"))
    command = _notify_command("Jira", "stale tickets")
    assert command[0] == "/usr/bin/powershell.exe"


def test_no_backend_prints_linux_hint(monkeypatch):
    monkeypatch.setattr(remind.sys, "platform", "linux")
    monkeypatch.setattr(remind.shutil, "which", which_returning())
    assert _notify_command("a", "b") is None

    output = StringIO()
    _desktop_notify("a", "b", Console(file=output, width=200))
    assert "libnotify" in output.getvalue()


def test_string_escaping_helpers():
    assert _applescript_string('a "quoted" \\ string') == '"a \\"quoted\\" \\\\ string"'
    assert _powershell_string("it's") == "'it''s'"


def test_failure_notification_for_network_error_mentions_vpn():
    title, message = _failure_notification(make_config(), JiraError("boom", status_code=None))
    assert "can't connect" in title
    assert "VPN" in message
    assert "jira.example.com" in message


def test_failure_notification_for_http_error_mentions_status():
    title, message = _failure_notification(make_config(), JiraError("nope", status_code=401))
    assert "401" in title
    assert "401" in message


def test_run_remind_notifies_on_connection_failure(monkeypatch):
    error = JiraError("Could not reach Jira", status_code=None)
    monkeypatch.setattr(
        remind.JiraClient, "search_issues", lambda self, jql: (_ for _ in ()).throw(error)
    )
    calls = []
    monkeypatch.setattr(remind, "_desktop_notify", lambda t, m, c: calls.append((t, m)))

    with pytest.raises(JiraError):
        run_remind(make_config(), Console(file=StringIO()), notify=True)

    assert len(calls) == 1
    assert "VPN" in calls[0][1]


def test_desktop_notify_redirects_stdio_and_hides_window(monkeypatch):
    monkeypatch.setattr(remind, "_notify_command", lambda t, m: ["notifier", t, m])
    captured = {}

    def fake_run(command, **kwargs):
        captured.update(kwargs)
        return FakeCompleted(returncode=0)

    monkeypatch.setattr(remind.subprocess, "run", fake_run)
    _desktop_notify("t", "m", Console(file=StringIO()))

    # stdin from DEVNULL so a windowless (pythonw) parent's invalid handles
    # aren't inherited; output captured; window hidden.
    assert captured["stdin"] == remind.subprocess.DEVNULL
    assert captured["capture_output"] is True
    assert "creationflags" in captured


def test_desktop_notify_surfaces_failure(monkeypatch):
    monkeypatch.setattr(remind, "_notify_command", lambda t, m: ["notifier"])
    monkeypatch.setattr(
        remind.subprocess, "run",
        lambda command, **kwargs: FakeCompleted(returncode=1, stderr="toast API blew up"),
    )
    output = StringIO()
    _desktop_notify("t", "m", Console(file=output, width=200))
    assert "toast API blew up" in output.getvalue()


def test_desktop_notify_silent_on_success(monkeypatch):
    monkeypatch.setattr(remind, "_notify_command", lambda t, m: ["notifier"])
    monkeypatch.setattr(
        remind.subprocess, "run", lambda command, **kwargs: FakeCompleted(returncode=0)
    )
    output = StringIO()
    _desktop_notify("t", "m", Console(file=output, width=200))
    assert "failed" not in output.getvalue()


def test_send_test_notification_uses_desktop_notify(monkeypatch):
    calls = []
    monkeypatch.setattr(remind, "_desktop_notify", lambda t, m, c: calls.append((t, m)))
    send_test_notification(Console(file=StringIO()))
    assert len(calls) == 1
    assert "working" in calls[0][1].lower()


def test_run_remind_reraises_without_notify_when_flag_off(monkeypatch):
    error = JiraError("Could not reach Jira", status_code=None)
    monkeypatch.setattr(
        remind.JiraClient, "search_issues", lambda self, jql: (_ for _ in ()).throw(error)
    )
    calls = []
    monkeypatch.setattr(remind, "_desktop_notify", lambda t, m, c: calls.append((t, m)))

    with pytest.raises(JiraError):
        run_remind(make_config(), Console(file=StringIO()), notify=False)

    assert calls == []
