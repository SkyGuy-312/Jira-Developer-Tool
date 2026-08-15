import base64
from io import StringIO

from rich.console import Console

from jira_tool import remind
from jira_tool.remind import (
    _applescript_string,
    _desktop_notify,
    _notify_command,
    _powershell_string,
)


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
