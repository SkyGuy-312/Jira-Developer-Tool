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


def test_add_worklog_payload(monkeypatch):
    client = JiraClient(make_config())
    request = Mock(return_value=ok_response({}))
    monkeypatch.setattr(client.session, "request", request)

    client.add_worklog("PROJ-1", "2h", comment="reviewed the fix")

    payload = request.call_args.kwargs["json"]
    assert payload["timeSpent"] == "2h"
    assert payload["comment"] == "reviewed the fix"
    assert "started" in payload
