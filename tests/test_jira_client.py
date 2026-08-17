from unittest.mock import Mock

import pytest

from jira_tool.config import Config
from jira_tool.jira_client import JiraClient, JiraError


def make_config(**overrides):
    defaults = dict(base_url="https://jira.example.com", auth_method="pat", token="tok")
    defaults.update(overrides)
    return Config(**defaults)


def ok_response(payload):
    response = Mock()
    response.ok = True
    response.content = b"{}"
    response.json.return_value = payload
    return response


def error_response(status_code, payload):
    response = Mock()
    response.ok = False
    response.status_code = status_code
    response.json.return_value = payload
    return response


def test_pat_auth_sets_bearer_header():
    client = JiraClient(make_config())
    assert client.session.headers["Authorization"] == "Bearer tok"
    assert client.session.auth is None


def test_basic_auth_sets_session_auth():
    client = JiraClient(make_config(auth_method="basic", username="me", token="pw"))
    assert client.session.auth == ("me", "pw")
    assert "Authorization" not in client.session.headers


def test_search_paginates_until_total(monkeypatch):
    client = JiraClient(make_config())
    pages = [
        ok_response({"total": 3, "issues": [{"key": "A-1"}, {"key": "A-2"}]}),
        ok_response({"total": 3, "issues": [{"key": "A-3"}]}),
    ]
    request = Mock(side_effect=pages)
    monkeypatch.setattr(client.session, "request", request)

    issues = client.search_issues("assignee = currentUser()")

    assert [issue["key"] for issue in issues] == ["A-1", "A-2", "A-3"]
    assert request.call_count == 2
    assert request.call_args_list[1].kwargs["params"]["startAt"] == 2


def test_error_response_raises_jira_error(monkeypatch):
    client = JiraClient(make_config())
    response = error_response(
        400,
        {
            "errorMessages": ["Field 'resolution' cannot be set."],
            "errors": {"resolution": "Unknown value"},
        },
    )
    monkeypatch.setattr(client.session, "request", Mock(return_value=response))

    with pytest.raises(JiraError) as excinfo:
        client.myself()
    assert "HTTP 400" in str(excinfo.value)
    assert "Field 'resolution' cannot be set." in str(excinfo.value)
    assert "resolution: Unknown value" in str(excinfo.value)
    assert excinfo.value.status_code == 400


def test_transition_includes_resolution(monkeypatch):
    client = JiraClient(make_config())
    request = Mock(return_value=ok_response({}))
    monkeypatch.setattr(client.session, "request", request)

    client.transition_issue("PROJ-1", "31", resolution_name="Done")

    payload = request.call_args.kwargs["json"]
    assert payload == {
        "transition": {"id": "31"},
        "fields": {"resolution": {"name": "Done"}},
    }


def test_transition_without_resolution_omits_fields(monkeypatch):
    client = JiraClient(make_config())
    request = Mock(return_value=ok_response({}))
    monkeypatch.setattr(client.session, "request", request)

    client.transition_issue("PROJ-1", "31")

    assert request.call_args.kwargs["json"] == {"transition": {"id": "31"}}


def test_search_stops_at_the_limit(monkeypatch):
    client = JiraClient(make_config())
    response = ok_response(
        {"total": 200, "issues": [{"key": f"A-{i}"} for i in range(10)]}
    )
    request = Mock(return_value=response)
    monkeypatch.setattr(client.session, "request", request)

    issues = client.search_issues("project = A", limit=10)

    assert len(issues) == 10
    assert request.call_count == 1
    assert request.call_args.kwargs["params"]["maxResults"] == 10


def test_get_issue_expands_the_changelog(monkeypatch):
    client = JiraClient(make_config())
    request = Mock(return_value=ok_response({"key": "A-1"}))
    monkeypatch.setattr(client.session, "request", request)

    client.get_issue("A-1")

    args = request.call_args
    assert args.args[1].endswith("/rest/api/2/issue/A-1")
    assert args.kwargs["params"]["expand"] == "changelog"
    assert "fields" not in args.kwargs["params"]  # all fields, incl. custom ones


def test_get_issue_updated_asks_for_one_field(monkeypatch):
    client = JiraClient(make_config())
    request = Mock(
        return_value=ok_response({"fields": {"updated": "2026-08-14T09:30:00.000+0300"}})
    )
    monkeypatch.setattr(client.session, "request", request)

    assert client.get_issue_updated("A-1") == "2026-08-14T09:30:00.000+0300"
    assert request.call_args.kwargs["params"] == {"fields": "updated"}


def test_get_comments_paginates(monkeypatch):
    client = JiraClient(make_config())
    pages = [
        ok_response({"total": 3, "comments": [{"id": "1"}, {"id": "2"}]}),
        ok_response({"total": 3, "comments": [{"id": "3"}]}),
    ]
    request = Mock(side_effect=pages)
    monkeypatch.setattr(client.session, "request", request)

    comments = client.get_comments("A-1")

    assert [c["id"] for c in comments] == ["1", "2", "3"]
    assert request.call_args_list[1].kwargs["params"]["startAt"] == 2


def test_download_writes_the_file(monkeypatch, tmp_path):
    client = JiraClient(make_config())
    response = Mock()
    response.ok = True
    response.iter_content.return_value = [b"0x50 ", b"stuck"]
    monkeypatch.setattr(client.session, "get", Mock(return_value=response))

    dest = client.download("https://jira/secure/attachment/1/t.log", tmp_path / "t.log")

    assert dest.read_bytes() == b"0x50 stuck"


def test_add_worklog_payload(monkeypatch):
    client = JiraClient(make_config())
    request = Mock(return_value=ok_response({}))
    monkeypatch.setattr(client.session, "request", request)

    client.add_worklog("PROJ-1", "2h", comment="reviewed the fix")

    payload = request.call_args.kwargs["json"]
    assert payload["timeSpent"] == "2h"
    assert payload["comment"] == "reviewed the fix"
    assert "started" in payload
