from unittest.mock import Mock

from jira_tool import mcp_server, read
from jira_tool.config import ConfigError
from jira_tool.jira_client import JiraError

from test_read import FIELD_MAP, FakeClient, bundle, make_config


def make_server(monkeypatch, issue=None):
    """A server wired to the fixture issue instead of a live Jira."""
    server = mcp_server.Server(make_config())
    monkeypatch.setattr(mcp_server.JiraClient, "__init__", lambda self, config: None)
    monkeypatch.setattr(server, "_client", lambda: (FakeClient(), make_config()))
    monkeypatch.setattr(
        server,
        "_issue",
        lambda key, refresh=False: issue
        if issue is not None
        else read.normalize(bundle(), make_config(), FIELD_MAP),
    )
    return server


def body(result):
    return result["content"][0]["text"]


def test_every_advertised_tool_has_a_handler():
    advertised = {tool["name"] for tool in mcp_server.TOOLS}
    assert advertised == set(mcp_server.Server._HANDLERS)


def test_the_tool_surface_is_read_only():
    names = " ".join(tool["name"] for tool in mcp_server.TOOLS)
    for forbidden in ("comment_add", "transition", "worklog", "create", "update",
                      "delete"):
        assert forbidden not in names


def test_tools_declare_their_required_arguments():
    for tool in mcp_server.TOOLS:
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        for name in schema.get("required", []):
            assert name in schema["properties"], (tool["name"], name)


def test_jira_issue_renders_the_ticket(monkeypatch):
    server = make_server(monkeypatch)
    result = server.call("jira_issue", {"key": "PROJ-2715"})

    assert not result.get("isError")
    assert "# PROJ-2715" in body(result)
    assert "## Comments" in body(result)


def test_jira_issue_honours_section_flags(monkeypatch):
    server = make_server(monkeypatch)
    text = body(server.call("jira_issue", {"key": "PROJ-2715", "history": False}))
    assert "## History" not in text


def test_jira_history_returns_only_the_history(monkeypatch):
    server = make_server(monkeypatch)
    text = body(server.call("jira_history", {"key": "PROJ-2715"}))

    assert "PROJ-2715 history" in text
    assert "status: Fixed → Reopened" in text
    assert "## Description" not in text


def test_jira_comments_returns_only_the_comments(monkeypatch):
    server = make_server(monkeypatch)
    text = body(server.call("jira_comments", {"key": "PROJ-2715"}))

    assert "PROJ-2715 comments" in text
    assert "Reproduced on rig 2" in text


def test_unknown_tool_is_an_error_result_not_a_crash():
    result = mcp_server.Server(make_config()).call("jira_nope", {})
    assert result["isError"]
    assert "unknown tool" in body(result)


def test_missing_argument_is_an_error_result():
    result = mcp_server.Server(make_config()).call("jira_issue", {})
    assert result["isError"]
    assert "missing required argument" in body(result)


def test_jira_error_becomes_an_error_result(monkeypatch):
    server = make_server(monkeypatch)
    monkeypatch.setattr(
        server, "_issue", Mock(side_effect=JiraError("Could not reach Jira"))
    )
    result = server.call("jira_issue", {"key": "PROJ-2715"})

    assert result["isError"]
    assert "Could not reach Jira" in body(result)


def test_config_error_points_at_setup(monkeypatch):
    server = mcp_server.Server(None)
    monkeypatch.setattr(mcp_server, "load_config", Mock(side_effect=ConfigError("no config")))
    result = server.call("jira_search", {"jql": "project = X"})

    assert result["isError"]
    assert "jira-tool setup" in body(result)


def test_search_renders_rows(monkeypatch):
    server = make_server(monkeypatch)
    monkeypatch.setattr(
        read,
        "search",
        lambda client, jql, limit=25: [
            {"key": "PROJ-1", "summary": "A thing", "type": "Bug", "status": "Open",
             "resolution": None, "priority": "Major", "assignee": None,
             "updated": "2026-08-14T09:30:00.000+0300"}
        ],
    )
    text = body(server.call("jira_search", {"jql": "project = PROJ"}))

    assert "PROJ-1 [Open] — A thing" in text
