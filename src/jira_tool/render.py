"""Turn a normalised issue into markdown.

Pure string building: no printing, no rich, no network. The CLI and the MCP
server both render through here so a ticket reads the same either way.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .read import human_size
from .utils import days_since, format_timestamp

DASH = "—"


def _age(value: Optional[str]) -> str:
    if not value:
        return ""
    try:
        days = days_since(value)
    except (ValueError, TypeError):
        return ""
    if days < 1:
        return f" ({days * 24:.0f}h ago)"
    return f" ({days:.1f}d ago)"


def _pairs(*items: Any) -> str:
    """One bullet of '**Label:** value' pairs, skipping the empty ones."""
    parts = [f"**{label}:** {value}" for label, value in items if value]
    return "- " + "   ".join(parts) if parts else ""


def _issue_line(item: Dict[str, Any], prefix: str = "") -> str:
    state = item.get("status") or "?"
    if item.get("resolution"):
        state += f"/{item['resolution']}"
    label = f"{prefix}{item['key']}" if prefix else item["key"]
    summary = item.get("summary") or ""
    return f"- {label} [{state}] {DASH} {summary}".rstrip(" —")


def render_issue(
    issue: Dict[str, Any],
    *,
    comments: bool = True,
    history: bool = True,
    max_comments: Optional[int] = None,
    max_body: Optional[int] = None,
) -> str:
    lines: List[str] = [f"# {issue['key']} {DASH} {issue['summary']}", ""]
    if issue.get("note"):
        lines += [f"> {issue['note']}", ""]

    status = issue.get("status") or "?"
    if issue.get("status_category") and issue["status_category"] != status:
        status = f"{status} ({issue['status_category']})"
    # Built separately so dropping the bullets that came out empty cannot eat
    # the blank lines that separate the header, the note and the body.
    meta: List[str] = []
    meta.append(
        _pairs(
            ("Type", issue.get("type")),
            ("Status", status),
            ("Resolution", issue.get("resolution") or DASH),
            ("Priority", issue.get("priority")),
        )
    )
    meta.append(
        _pairs(
            ("Assignee", issue.get("assignee") or "unassigned"),
            ("Reporter", issue.get("reporter")),
            ("Project", issue.get("project")),
        )
    )
    meta.append(
        _pairs(
            ("Created", format_timestamp(issue.get("created"))),
            ("Updated", format_timestamp(issue.get("updated")) + _age(issue.get("updated"))),
            ("Resolved", format_timestamp(issue["resolved"]) if issue.get("resolved") else None),
            ("Due", issue.get("due")),
        )
    )
    meta.append(
        _pairs(
            ("Components", ", ".join(issue.get("components") or [])),
            ("Labels", ", ".join(issue.get("labels") or [])),
        )
    )
    meta.append(
        _pairs(
            ("Affects", ", ".join(issue.get("affects_versions") or [])),
            ("Fix Version", ", ".join(issue.get("fix_versions") or [])),
        )
    )
    meta.append(_pairs(("URL", issue.get("url"))))
    lines += [line for line in meta if line] + [""]

    if issue.get("environment"):
        lines += ["## Environment", "", issue["environment"], ""]

    if issue.get("description"):
        body = issue["description"]
        if max_body and len(body) > max_body:
            body = body[:max_body] + f"\n\n[... {len(body) - max_body} more characters]"
        lines += ["## Description", "", body, ""]
    else:
        lines += ["## Description", "", "_(empty)_", ""]

    if issue.get("custom"):
        lines += ["## Fields", ""]
        lines += [f"- **{item['name']}:** {item['value']}" for item in issue["custom"]]
        lines.append("")

    lines += _render_relations(issue)

    if comments:
        lines += render_comments(issue, limit=max_comments, heading=True)
    if history:
        lines += render_history(issue, heading=True)

    if issue.get("attachments"):
        lines += [f"## Attachments ({len(issue['attachments'])})", ""]
        for att in issue["attachments"]:
            meta = ", ".join(
                part
                for part in (human_size(att.get("size")), att.get("mime"))
                if part
            )
            lines.append(f"- {att['filename']} ({meta})")
        lines += ["", "_Fetch one with `jira_attachment` / `jira-tool attach`._", ""]

    if issue.get("remote_links"):
        lines += ["## Remote links", ""]
        for link in issue["remote_links"]:
            title = link.get("title") or link.get("url") or "?"
            lines.append(f"- {title}: {link.get('url') or DASH}")
        lines.append("")

    refs = issue.get("refs") or {}
    if refs:
        lines += ["## Referenced in text", ""]
        for label, values in refs.items():
            lines.append(f"- **{label}:** {', '.join(values)}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _render_relations(issue: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    if issue.get("parent"):
        lines += ["## Parent", "", _issue_line(issue["parent"]), ""]
    if issue.get("links"):
        lines += ["## Links", ""]
        for item in issue["links"]:
            relation = item.get("relation") or "relates to"
            lines.append(_issue_line(item, prefix=f"{relation} "))
        lines.append("")
    if issue.get("subtasks"):
        lines += [f"## Subtasks ({len(issue['subtasks'])})", ""]
        lines += [_issue_line(item) for item in issue["subtasks"]]
        lines.append("")
    return lines


def render_comments(
    issue: Dict[str, Any], limit: Optional[int] = None, heading: bool = False
) -> List[str]:
    comments = issue.get("comments") or []
    lines: List[str] = []
    if heading:
        lines += [f"## Comments ({len(comments)})", ""]
    if not comments:
        return (lines + ["_(none)_", ""]) if heading else lines

    shown = comments
    omitted = 0
    if limit is not None and len(comments) > limit:
        # Keep the newest: on a bug ticket the recent exchange is the live
        # thread, and the earlier ones are one jira_comments call away.
        shown = comments[-limit:]
        omitted = len(comments) - limit
        lines += [
            f"_The {omitted} earliest comment(s) are omitted; use "
            f"`jira_comments` / `jira-tool show --max-comments 0` for all._",
            "",
        ]

    for index, comment in enumerate(shown, start=omitted + 1):
        stamp = format_timestamp(comment.get("created"))
        edited = " (edited)" if comment.get("edited") else ""
        lines += [
            f"### {index}. {comment.get('author') or 'unknown'} {DASH} {stamp}{edited}",
            "",
            comment.get("body") or "_(empty)_",
            "",
        ]
    return lines


def render_history(issue: Dict[str, Any], heading: bool = False) -> List[str]:
    entries = issue.get("history") or []
    lines: List[str] = []
    if heading:
        lines += [f"## History ({len(entries)})", ""]
    if not entries:
        return (lines + ["_(none)_", ""]) if heading else lines

    for entry in entries:
        changes = []
        for change in entry["changes"]:
            if change["from"] is None and change["to"] is None:
                changes.append(f"{change['field']} edited")
            else:
                changes.append(
                    f"{change['field']}: {change['from'] or DASH} → {change['to'] or DASH}"
                )
        lines.append(
            f"- {format_timestamp(entry.get('created'))} "
            f"{entry.get('author') or 'unknown'}: " + "; ".join(changes)
        )
    lines.append("")
    return lines


def render_search(rows: List[Dict[str, Any]], jql: str) -> str:
    if not rows:
        return f"No issues match:\n    {jql}\n"
    lines = [f"{len(rows)} issue(s) for: {jql}", ""]
    for row in rows:
        state = row.get("status") or "?"
        if row.get("resolution"):
            state += f"/{row['resolution']}"
        meta = ", ".join(
            part
            for part in (
                row.get("type"),
                row.get("priority"),
                row.get("assignee") or "unassigned",
            )
            if part
        )
        lines.append(f"- {row['key']} [{state}] {DASH} {row.get('summary') or ''}")
        lines.append(
            f"    {meta}; updated {format_timestamp(row.get('updated'))}"
            f"{_age(row.get('updated'))}"
        )
    return "\n".join(lines) + "\n"


def render_attachment(result: Dict[str, Any]) -> str:
    att = result["attachment"]
    lines = [
        f"# {att['filename']}",
        "",
        _pairs(
            ("Size", human_size(att.get("size"))),
            ("Type", att.get("mime")),
            ("Uploaded", format_timestamp(att.get("created"))),
            ("By", att.get("author")),
        ),
        _pairs(("Saved to", result["path"])),
        "",
    ]
    if result.get("text") is not None:
        lines += ["## Contents", "", "```", result["text"].rstrip(), "```", ""]
    else:
        lines += [
            "_Binary or too large to inline - read it from the path above._",
            "",
        ]
    return "\n".join(lines)
