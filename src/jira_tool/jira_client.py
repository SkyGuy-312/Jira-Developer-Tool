"""A thin client for the Jira Server / Data Center REST API (v2)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import requests

from .config import Config
from .utils import format_jira_datetime

DEFAULT_SEARCH_FIELDS = ("summary", "status", "updated", "issuetype", "priority")
_PAGE_SIZE = 50
# Fetching an issue with expand=changelog is one round trip and works on every
# Jira version; the dedicated /changelog endpoint is 8.14+ only and is used as
# a follow-up when the embedded history comes back truncated.
ISSUE_EXPAND = ("changelog",)


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
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        issues: List[Dict[str, Any]] = []
        start_at = 0
        while True:
            page_size = _PAGE_SIZE
            if limit is not None:
                page_size = min(_PAGE_SIZE, limit - len(issues))
            data = self._request(
                "GET",
                "/rest/api/2/search",
                params={
                    "jql": jql,
                    "fields": ",".join(fields),
                    "startAt": start_at,
                    "maxResults": page_size,
                },
            )
            batch = data.get("issues", [])
            issues.extend(batch)
            start_at += len(batch)
            if limit is not None and len(issues) >= limit:
                return issues[:limit]
            if not batch or start_at >= data.get("total", 0):
                return issues

    # -- read layer ----------------------------------------------------------

    def get_issue(
        self,
        issue_key: str,
        fields: Optional[Sequence[str]] = None,
        expand: Sequence[str] = ISSUE_EXPAND,
    ) -> Dict[str, Any]:
        params: Dict[str, str] = {}
        if fields is not None:
            params["fields"] = ",".join(fields)
        if expand:
            params["expand"] = ",".join(expand)
        return self._request("GET", f"/rest/api/2/issue/{issue_key}", params=params)

    def get_issue_updated(self, issue_key: str) -> Optional[str]:
        """The 'updated' timestamp alone - a cheap cache freshness probe."""
        data = self.get_issue(issue_key, fields=["updated"], expand=())
        return (data.get("fields") or {}).get("updated")

    def get_comments(self, issue_key: str) -> List[Dict[str, Any]]:
        comments: List[Dict[str, Any]] = []
        start_at = 0
        while True:
            data = self._request(
                "GET",
                f"/rest/api/2/issue/{issue_key}/comment",
                params={
                    "startAt": start_at,
                    "maxResults": _PAGE_SIZE,
                    "orderBy": "created",
                },
            )
            batch = data.get("comments", [])
            comments.extend(batch)
            start_at += len(batch)
            if not batch or start_at >= data.get("total", 0):
                return comments

    def get_changelog(self, issue_key: str) -> List[Dict[str, Any]]:
        """Full change history via the paginated endpoint (Jira 8.14+)."""
        entries: List[Dict[str, Any]] = []
        start_at = 0
        while True:
            data = self._request(
                "GET",
                f"/rest/api/2/issue/{issue_key}/changelog",
                params={"startAt": start_at, "maxResults": _PAGE_SIZE},
            )
            batch = data.get("values", [])
            entries.extend(batch)
            start_at += len(batch)
            if not batch or start_at >= data.get("total", 0):
                return entries

    def get_remote_links(self, issue_key: str) -> List[Dict[str, Any]]:
        return self._request(
            "GET", f"/rest/api/2/issue/{issue_key}/remotelink"
        ) or []

    def get_fields(self) -> List[Dict[str, Any]]:
        """Field metadata, used to turn customfield_10234 into a real name."""
        return self._request("GET", "/rest/api/2/field") or []

    def download(self, url: str, dest: Path) -> Path:
        """Download an attachment by its absolute content URL."""
        try:
            response = self.session.get(
                url, stream=True, timeout=120, headers={"Accept": "*/*"}
            )
        except requests.RequestException as exc:
            raise JiraError(f"Could not download {url}: {exc}") from exc
        if not response.ok:
            raise JiraError(
                self._error_message(response), status_code=response.status_code
            )
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as handle:
            for chunk in response.iter_content(chunk_size=65536):
                handle.write(chunk)
        return dest

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
