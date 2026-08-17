"""Minimal MCP (Model Context Protocol) stdio server for reading Jira.

Exposes the read layer to MCP hosts - Claude Code, GitHub Copilot agent mode,
or any other client. JSON-RPC 2.0, newline-delimited, over stdio.

Two rules hold this together:

* **stdout carries protocol messages only.** Diagnostics go to stderr. Nothing
  reachable from here may import rich or typer, or print.
* **The tool surface is read-only.** Commenting, logging work and transitioning
  stay in ``jira-tool checkin``, where a human presses the key. An agent posting
  to a team's Jira is visible to everyone and awkward to undo.

Run: python -m jira_tool mcp
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, Optional

from . import __version__, read, render
from .cache import Cache
from .config import Config, ConfigError, load_config
from .jira_client import JiraClient, JiraError

_FALLBACK_PROTOCOL = "2024-11-05"

TOOLS = [
    {
        "name": "jira_issue",
        "description": "Read a Jira issue in full: summary, status, priority,"
                       " assignee, components, environment, description,"
                       " custom fields, linked issues and subtasks, comments,"
                       " change history, attachments and remote links (Gerrit"
                       " / GitHub). Use this first when investigating a bug"
                       " ticket - the comments carry the root-cause"
                       " discussion and the history shows reopens.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string",
                        "description": "issue key, e.g. 'PROJ-1234'"},
                "comments": {"type": "boolean",
                             "description": "include comments (default true)"},
                "history": {"type": "boolean",
                            "description": "include the change history"
                                           " (default true)"},
                "max_comments": {"type": "integer",
                                 "description": "keep only the newest N"
                                                " comments (default 30; 0 ="
                                                " all)"},
                "refresh": {"type": "boolean",
                            "description": "bypass the cache and refetch"},
            },
            "required": ["key"],
        },
    },
    {
        "name": "jira_search",
        "description": "Run a JQL query and list the matching issues with"
                       " status, type, priority, assignee and last update."
                       " Use to find prior art before investigating: e.g."
                       " 'text ~ \"C1234F\"' for everyone who hit a code, or"
                       " 'project = PROJ AND component = X AND status ="
                       " Open'.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "jql": {"type": "string"},
                "limit": {"type": "integer",
                          "description": "max issues to return (default 25)"},
            },
            "required": ["jql"],
        },
    },
    {
        "name": "jira_comments",
        "description": "Every comment on an issue, chronologically. Use when"
                       " jira_issue truncated a long thread.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "limit": {"type": "integer",
                          "description": "newest N only; omit for all"},
            },
            "required": ["key"],
        },
    },
    {
        "name": "jira_history",
        "description": "The change history of an issue: every field"
                       " transition with who made it and when. Use to see"
                       " whether a bug was fixed and reopened, when it"
                       " changed assignee, or when the fix version moved.",
        "inputSchema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    },
    {
        "name": "jira_attachment",
        "description": "Download one attachment of an issue to local disk and"
                       " return its path. Text attachments (logs, traces,"
                       " CSV) are returned inline; binary ones (screenshots)"
                       " are saved so they can be opened from the path.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "filename": {"type": "string",
                             "description": "exact name, or a unique"
                                            " substring of one"},
            },
            "required": ["key", "filename"],
        },
    },
]


def _text(body: str) -> dict:
    return {"content": [{"type": "text", "text": body}]}


def _error(body: str) -> dict:
    return {"content": [{"type": "text", "text": body}], "isError": True}


class Server:
    def __init__(self, config: Optional[Config] = None):
        self._config = config
        self._field_map: Optional[Dict[str, str]] = None

    # -- context -------------------------------------------------------------

    def _client(self) -> "tuple[JiraClient, Config]":
        config = self._config if self._config is not None else load_config()
        return JiraClient(config), config

    def _issue(self, key: str, refresh: bool = False) -> Dict[str, Any]:
        client, config = self._client()
        if self._field_map is None:
            self._field_map = read.load_field_map(client)
        return read.fetch_issue(
            client,
            config,
            key,
            refresh=refresh,
            cache=Cache(config.cache_ttl_minutes * 60),
            field_map=self._field_map,
        )

    # -- tool handlers -------------------------------------------------------

    def t_issue(self, args):
        max_comments = args.get("max_comments")
        max_comments = 30 if max_comments is None else int(max_comments)
        issue = self._issue(args["key"], bool(args.get("refresh")))
        return _text(
            render.render_issue(
                issue,
                comments=args.get("comments", True),
                history=args.get("history", True),
                max_comments=max_comments or None,
            )
        )

    def t_search(self, args):
        client, _ = self._client()
        limit = int(args.get("limit") or 25)
        rows = read.search(client, args["jql"], limit=limit)
        return _text(render.render_search(rows, args["jql"]))

    def t_comments(self, args):
        issue = self._issue(args["key"])
        limit = args.get("limit")
        lines = render.render_comments(
            issue, limit=int(limit) if limit else None, heading=True
        )
        return _text(f"# {issue['key']} comments\n\n" + "\n".join(lines))

    def t_history(self, args):
        issue = self._issue(args["key"])
        lines = render.render_history(issue, heading=True)
        return _text(f"# {issue['key']} history\n\n" + "\n".join(lines))

    def t_attachment(self, args):
        client, _ = self._client()
        issue = self._issue(args["key"])
        result = read.fetch_attachment(client, issue, args["filename"])
        return _text(render.render_attachment(result))

    # -- dispatch ------------------------------------------------------------

    _HANDLERS = {
        "jira_issue": t_issue,
        "jira_search": t_search,
        "jira_comments": t_comments,
        "jira_history": t_history,
        "jira_attachment": t_attachment,
    }

    def call(self, name: str, args: dict) -> dict:
        handler = self._HANDLERS.get(name)
        if handler is None:
            return _error(f"unknown tool: {name}")
        try:
            return handler(self, args)
        except ConfigError as exc:
            return _error(f"{exc} (run 'jira-tool setup')")
        except JiraError as exc:
            return _error(str(exc))
        except KeyError as exc:
            return _error(f"missing required argument: {exc}")
        except Exception as exc:  # tool errors are results, not crashes
            return _error(f"{type(exc).__name__}: {exc}")


def _write(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def serve(config: Optional[Config] = None) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    except AttributeError:
        pass
    if config is None:
        # Start anyway on a bad config: a live server that returns a clear
        # error per call is easier to diagnose than one that exits at startup.
        try:
            config = load_config()
        except ConfigError as exc:
            print(f"jira-tool MCP: {exc}", file=sys.stderr)
    server = Server(config)
    target = config.base_url if config else "no config"
    print(f"jira-tool MCP server on stdio ({target})", file=sys.stderr)

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        mid = msg.get("id")
        method = msg.get("method")
        try:
            if method == "initialize":
                params = msg.get("params") or {}
                result = {
                    "protocolVersion": params.get("protocolVersion")
                                       or _FALLBACK_PROTOCOL,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "jira-tool",
                                   "version": __version__},
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                params = msg.get("params") or {}
                result = server.call(params.get("name", ""),
                                     params.get("arguments") or {})
            elif method and method.startswith("notifications/"):
                continue
            else:
                if mid is not None:
                    _write({"jsonrpc": "2.0", "id": mid,
                            "error": {"code": -32601,
                                      "message": f"method not found:"
                                                 f" {method}"}})
                continue
            if mid is not None:
                _write({"jsonrpc": "2.0", "id": mid, "result": result})
        except Exception as exc:
            if mid is not None:
                _write({"jsonrpc": "2.0", "id": mid,
                        "error": {"code": -32603, "message": str(exc)}})
