"""A thin client for the Jira Server / Data Center REST API (v2)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

import requests

from .config import Config
from .utils import format_jira_datetime

DEFAULT_SEARCH_FIELDS = ("summary", "status", "updated", "issuetype", "priority")
_PAGE_SIZE = 50


class JiraError(Exception):
    """Raised when the Jira API returns an error response."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class JiraClient:
    def __init__(self, config: Config):
        self.config = config
        self.session = requests.Session()
        self.session.verify = config.verify_ssl
        if config.auth_method == "pat":
            self.session.headers["Authorization"] = f"Bearer {config.effective_token}"
        else:
            self.session.auth = (config.username, config.effective_token)
        self.session.headers["Accept"] = "application/json"

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self.config.base_url}{path}"
        try:
            response = self.session.request(method, url, timeout=30, **kwargs)
        except requests.RequestException as exc:
            raise JiraError(f"Could not reach Jira at {url}: {exc}") from exc
        if response.ok:
            if not response.content:
                return None
            return response.json()
        raise JiraError(
            self._error_message(response), status_code=response.status_code
        )

    @staticmethod
    def _error_message(response: requests.Response) -> str:
        prefix = f"Jira returned HTTP {response.status_code}"
        try:
            data = response.json()
        except ValueError:
            return f"{prefix}: {response.text[:200]}"
        messages = list(data.get("errorMessages", []))
        messages.extend(f"{field}: {msg}" for field, msg in data.get("errors", {}).items())
        if messages:
            return f"{prefix}: {'; '.join(messages)}"
        return prefix

    def myself(self) -> Dict[str, Any]:
        return self._request("GET", "/rest/api/2/myself")

    def search_issues(
        self,
        jql: str,
        fields: Sequence[str] = DEFAULT_SEARCH_FIELDS,
    ) -> List[Dict[str, Any]]:
        issues: List[Dict[str, Any]] = []
        start_at = 0
        while True:
            data = self._request(
                "GET",
                "/rest/api/2/search",
                params={
                    "jql": jql,
                    "fields": ",".join(fields),
                    "startAt": start_at,
                    "maxResults": _PAGE_SIZE,
                },
            )
            batch = data.get("issues", [])
            issues.extend(batch)
            start_at += len(batch)
            if not batch or start_at >= data.get("total", 0):
                return issues

    def add_comment(self, issue_key: str, body: str) -> Dict[str, Any]:
        return self._request(
            "POST", f"/rest/api/2/issue/{issue_key}/comment", json={"body": body}
        )

    def add_worklog(
        self,
        issue_key: str,
        time_spent: str,
        comment: str = "",
        started: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "timeSpent": time_spent,
            "started": format_jira_datetime(started or datetime.now().astimezone()),
        }
        if comment:
            payload["comment"] = comment
        return self._request(
            "POST", f"/rest/api/2/issue/{issue_key}/worklog", json=payload
        )

    def get_transitions(self, issue_key: str) -> List[Dict[str, Any]]:
        data = self._request("GET", f"/rest/api/2/issue/{issue_key}/transitions")
        return data.get("transitions", [])

    def transition_issue(
        self,
        issue_key: str,
        transition_id: str,
        resolution_name: Optional[str] = None,
    ) -> None:
        payload: Dict[str, Any] = {"transition": {"id": str(transition_id)}}
        if resolution_name:
            payload["fields"] = {"resolution": {"name": resolution_name}}
        self._request("POST", f"/rest/api/2/issue/{issue_key}/transitions", json=payload)

    def get_resolutions(self) -> List[Dict[str, Any]]:
        return self._request("GET", "/rest/api/2/resolution") or []
