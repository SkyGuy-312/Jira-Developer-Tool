"""Fetch an issue and normalise it into a predictable shape.

A raw Jira issue is 100-200 KB of JSON, most of it avatar URLs, ``self``
links and dozens of empty ``customfield_1xxxx`` entries. Everything here
turns that into a small dict with stable keys, which :mod:`jira_tool.render`
then prints. The split matters: normalisation is where the Jira quirks live
(wiki markup vs. ADF, four shapes of custom-field value, embedded-vs-paginated
comments), and it is pure enough to test against a recorded fixture.

Nothing in this module prints or imports rich/typer - the MCP server shares it,
and there stdout carries protocol messages only.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .cache import Cache, attachment_dir, cache_dir
from .config import Config
from .jira_client import JiraClient, JiraError

# Fields rendered in their own section; everything else non-empty is "custom".
_STANDARD_FIELDS = frozenset(
    """
    summary status issuetype priority resolution resolutiondate assignee
    reporter creator created updated duedate components labels versions
    fixVersions environment description comment attachment issuelinks
    subtasks parent project worklog timetracking watches votes progress
    aggregateprogress workratio security thumbnail lastViewed statuscategory
    statusCategory statuscategorychangedate timespent timeestimate
    timeoriginalestimate aggregatetimespent aggregatetimeestimate
    aggregatetimeoriginalestimate
    """.split()
)

_ISSUE_KEY_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b")
# The issue-key shape also matches standards and encodings, which turn up
# constantly in engineering tickets. Anything here is never a ticket.
_NOT_ISSUE_PREFIXES = frozenset(
    """
    UTF ISO IEC RFC SAE ANSI IEEE ASCII MISRA AUTOSAR ASPICE EN DIN JIS
    USB PCI SHA MD RGB CVE ECE UN
    """.split()
)

# Jira wiki markup that is worth converting; the rest reads fine as-is.
_WIKI_CODE_RE = re.compile(r"\{(code|noformat)(?::([^}]*))?\}(.*?)\{\1\}", re.DOTALL)
_WIKI_HEADING_RE = re.compile(r"^h([1-6])\.\s*", re.MULTILINE)
_WIKI_WRAPPER_RE = re.compile(r"\{(color|panel|quote)(?::[^}]*)?\}")
# In wiki markup '#' starts an ordered list item and '*' an unordered one,
# nested by repeating the marker. Left alone, a four-item numbered list renders
# as four markdown H1 headings.
# Leading whitespace is allowed before the marker, and is common - authors
# indent continuation items. Nesting comes from repeating the marker, not from
# the indent, so the leading space is dropped.
_WIKI_LIST_RE = re.compile(r"^[ \t]*([*#]{1,5})[ \t]+", re.MULTILINE)
# '||a||b||' is a header row; markdown needs the separator underneath it.
_WIKI_TABLE_HEAD_RE = re.compile(r"^[ \t]*\|\|(.+?)\|\|[ \t]*$", re.MULTILINE)
_WIKI_MENTION_RE = re.compile(r"\[~([A-Za-z0-9._-]+)\]")
_WIKI_LINK_RE = re.compile(r"\[([^\]|\n]+)\|([^\]|\n]+)(?:\|[^\]\n]*)?\]")
_WIKI_BARE_LINK_RE = re.compile(r"\[(\w+://[^\]|\n]+)\]")
# Some fields hand back a Java toString dump of an internal bean rather than a
# value - kilobytes of noise that says nothing. Detected by shape, not by name,
# so it holds on any instance.
_JAVA_DUMP_RE = re.compile(r"com\.atlassian\.[\w.]+@[0-9a-f]{4,}")
# Greenhopper stuffs sprints into a string like "...[id=12,name=Sprint 4,...]".
_SPRINT_NAME_RE = re.compile(r"name=([^,\]]+)")

_FIELD_MAP_TTL_SECONDS = 7 * 24 * 3600

# Bodies are embedded under a '## Description' / '### <comment>' heading, so
# headings written inside a ticket are pushed down to nest beneath it rather
# than competing with the render's own structure.
_HEADING_OFFSET = 3


def _demote(level: Any) -> str:
    try:
        depth = int(level)
    except (TypeError, ValueError):
        depth = 1
    return "#" * min(6, depth + _HEADING_OFFSET)


# -- value coercion ---------------------------------------------------------


def adf_to_text(node: Any) -> str:
    """Flatten Atlassian Document Format (Jira Cloud) into plain text."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(adf_to_text(child) for child in node)
    if not isinstance(node, dict):
        return str(node)

    kind = node.get("type")
    content = node.get("content")
    if kind == "text":
        return node.get("text", "")
    if kind == "hardBreak":
        return "\n"
    if kind == "rule":
        return "\n---\n"
    if kind == "mention":
        return "@" + (node.get("attrs", {}).get("text") or "").lstrip("@")
    if kind in ("inlineCard", "blockCard"):
        return node.get("attrs", {}).get("url", "")
    if kind == "emoji":
        return node.get("attrs", {}).get("shortName", "")
    if kind == "codeBlock":
        language = node.get("attrs", {}).get("language") or ""
        return f"\n```{language}\n{adf_to_text(content)}\n```\n"
    if kind == "heading":
        level = node.get("attrs", {}).get("level", 1)
        return f"\n{_demote(level)} {adf_to_text(content)}\n"
    if kind == "paragraph":
        return adf_to_text(content) + "\n"
    if kind == "listItem":
        return "- " + adf_to_text(content).strip() + "\n"
    if kind == "blockquote":
        body = adf_to_text(content).strip().splitlines()
        return "\n".join(f"> {line}" for line in body) + "\n"
    if kind in ("tableCell", "tableHeader"):
        return adf_to_text(content).strip() + " | "
    if kind == "tableRow":
        return adf_to_text(content).rstrip(" |") + "\n"
    return adf_to_text(content)


def wiki_to_markdown(text: str) -> str:
    """Convert the wiki markup worth converting; leave the rest alone.

    Code blocks are lifted out first and put back at the end, so none of the
    other rules can rewrite something inside a log dump or a stack trace.
    """
    blocks: List[str] = []

    def stash(match: "re.Match[str]") -> str:
        language = (match.group(2) or "").split("|")[0].strip()
        body = match.group(3).strip("\n")
        blocks.append(f"\n```{language}\n{body}\n```\n")
        return f"\x00{len(blocks) - 1}\x00"

    def bullet(match: "re.Match[str]") -> str:
        marker = match.group(1)
        return "  " * (len(marker) - 1) + ("1. " if marker[-1] == "#" else "- ")

    def table_head(match: "re.Match[str]") -> str:
        cells = [cell.strip() for cell in match.group(1).split("||")]
        return (
            "| " + " | ".join(cells) + " |\n|" + "|".join(["---"] * len(cells)) + "|"
        )

    text = _WIKI_CODE_RE.sub(stash, text)
    text = _WIKI_TABLE_HEAD_RE.sub(table_head, text)
    # Lists before headings: heading conversion emits '####', which the list
    # rule would otherwise read as a fourth-level ordered item.
    text = _WIKI_LIST_RE.sub(bullet, text)
    text = _WIKI_HEADING_RE.sub(lambda m: _demote(m.group(1)) + " ", text)
    text = _WIKI_MENTION_RE.sub(r"@\1", text)
    text = _WIKI_BARE_LINK_RE.sub(r"<\1>", text)
    text = _WIKI_LINK_RE.sub(r"[\1](\2)", text)
    text = _WIKI_WRAPPER_RE.sub("", text)
    for index, block in enumerate(blocks):
        text = text.replace(f"\x00{index}\x00", block)
    return text


def body_text(value: Any) -> Optional[str]:
    """A description/comment body, whichever markup the instance uses."""
    if isinstance(value, dict):
        text = adf_to_text(value).strip()
    elif isinstance(value, str):
        text = wiki_to_markdown(value).strip()
    else:
        return None
    return text or None


def scalar(value: Any) -> Optional[str]:
    """Flatten a field value to one line: str, number, option, user, or list."""
    if value is None or isinstance(value, bool):
        return str(value) if isinstance(value, bool) else None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))  # story points come back as 8.0, not 8
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if "greenhopper" in text and "name=" in text:
            names = _SPRINT_NAME_RE.findall(text)
            return ", ".join(names) if names else text
        return text
    if isinstance(value, list):
        parts = [scalar(item) for item in value]
        joined = ", ".join(part for part in parts if part)
        return joined or None
    if isinstance(value, dict):
        if value.get("type") == "doc":  # an ADF-bodied custom field
            return body_text(value)
        for key in ("displayName", "name", "value", "label", "text"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return None
    return str(value)


def _user(value: Any) -> Optional[str]:
    if not isinstance(value, dict):
        return None
    return value.get("displayName") or value.get("name") or value.get("emailAddress")


def _names(values: Any) -> List[str]:
    if not isinstance(values, list):
        return []
    out = []
    for item in values:
        name = item.get("name") if isinstance(item, dict) else scalar(item)
        if name:
            out.append(name)
    return out


def human_size(num_bytes: Any) -> str:
    try:
        size = float(num_bytes)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


# -- cross references -------------------------------------------------------


def extract_refs(
    texts: Iterable[str],
    patterns: Optional[Dict[str, str]] = None,
    exclude: Sequence[str] = (),
) -> Dict[str, List[str]]:
    """Pull issue keys - and any configured domain codes - out of issue text.

    This is what makes a ticket a join key: the issue keys it mentions lead to
    related tickets, and configured patterns (DTCs, DIDs, part numbers) lead
    wherever the rest of the toolchain can take them.
    """
    blob = "\n".join(text for text in texts if text)
    skip = {item.upper() for item in exclude}
    refs: Dict[str, List[str]] = {}

    claimed = set()
    for label, pattern in (patterns or {}).items():
        try:
            found = re.findall(pattern, blob)
        except re.error:
            continue
        flat = sorted({m if isinstance(m, str) else m[0] for m in found if m})
        if flat:
            refs[label] = flat
            claimed.update(item.upper() for item in flat)

    # A configured pattern wins: if DOC-100200 is labelled "Polarion", it has
    # no business also appearing in the generic issue-key bucket.
    keys = sorted(
        {
            match
            for match in _ISSUE_KEY_RE.findall(blob)
            if match.upper() not in skip
            and match.upper() not in claimed
            and match.split("-", 1)[0].upper() not in _NOT_ISSUE_PREFIXES
        }
    )
    if keys:
        refs = {"Issues": keys, **refs}
    return refs


# -- fetching ---------------------------------------------------------------


def load_field_map(
    client: JiraClient, directory: Optional[Path] = None, refresh: bool = False
) -> Dict[str, str]:
    """``customfield_10234`` -> ``Severity``. Cached for a week; rarely moves."""
    cache = Cache(_FIELD_MAP_TTL_SECONDS, directory or (cache_dir() / "meta"))
    cached = cache.load("fields")
    if cached and cached["fresh"] and not refresh:
        return cached["payload"]
    try:
        fields = client.get_fields()
    except JiraError:
        # Fall back to whatever is on disk even on an explicit refresh: field
        # names a week old still beat rendering every custom field as an id.
        return cached["payload"] if cached else {}
    mapping = {
        item["id"]: item.get("name") or item["id"]
        for item in fields
        if isinstance(item, dict) and item.get("id")
    }
    cache.store("fields", mapping)
    return mapping


def fetch_bundle(client: JiraClient, issue_key: str) -> Dict[str, Any]:
    """Everything needed to render one issue, in 1-4 requests."""
    raw = client.get_issue(issue_key)
    fields = raw.get("fields") or {}

    comment_block = fields.get("comment") or {}
    comments = comment_block.get("comments") or []
    if comment_block.get("total", len(comments)) > len(comments):
        comments = client.get_comments(issue_key)  # embedded copy was capped

    changelog = raw.get("changelog") or {}
    histories = changelog.get("histories") or []
    if changelog.get("total", len(histories)) > len(histories):
        try:
            histories = client.get_changelog(issue_key)
        except JiraError:
            pass  # /changelog is Jira 8.14+; the capped copy still beats nothing

    try:
        remote_links = client.get_remote_links(issue_key)
    except JiraError:
        remote_links = []  # some instances disable the remote-link endpoint

    return {
        "issue": raw,
        "comments": comments,
        "changelog": histories,
        "remote_links": remote_links,
    }


def fetch_issue(
    client: JiraClient,
    config: Config,
    issue_key: str,
    *,
    refresh: bool = False,
    cache: Optional[Cache] = None,
    field_map: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Fetch (or reuse) an issue bundle and normalise it.

    Within the TTL a cached bundle is served blind. Past it, one small request
    for ``updated`` decides whether the full refetch is warranted - and if that
    request fails, a stale bundle is served with a note instead of an error.
    """
    cache = cache if cache is not None else Cache(config.cache_ttl_minutes * 60)
    entry = None if refresh else cache.load(issue_key)
    note = ""

    if entry and entry["fresh"]:
        bundle = entry["payload"]
    elif entry:
        try:
            updated = client.get_issue_updated(issue_key)
        except JiraError as exc:
            bundle = entry["payload"]
            age = entry["age_seconds"] / 60
            note = (
                f"Served from cache, fetched {age:.0f} min ago - Jira is not "
                f"reachable ({exc}). Details may be out of date."
            )
        else:
            if updated and updated == entry.get("updated"):
                cache.touch(issue_key)
                bundle = entry["payload"]
            else:
                bundle = fetch_bundle(client, issue_key)
                cache.store(issue_key, bundle, updated)
    else:
        bundle = fetch_bundle(client, issue_key)
        cache.store(
            issue_key,
            bundle,
            ((bundle["issue"].get("fields") or {}).get("updated")),
        )

    if field_map is None:
        field_map = load_field_map(client)
    issue = normalize(bundle, config, field_map)
    if note:
        issue["note"] = note
    return issue


def search(
    client: JiraClient, jql: str, limit: int = 50
) -> List[Dict[str, Any]]:
    """JQL search, normalised to the columns worth showing."""
    raw = client.search_issues(
        jql,
        fields=("summary", "status", "updated", "issuetype", "priority", "assignee",
                "resolution"),
        limit=limit,
    )
    rows = []
    for issue in raw:
        fields = issue.get("fields") or {}
        rows.append(
            {
                "key": issue.get("key", "?"),
                "summary": fields.get("summary") or "",
                "type": scalar(fields.get("issuetype")),
                "status": scalar(fields.get("status")),
                "resolution": scalar(fields.get("resolution")),
                "priority": scalar(fields.get("priority")),
                "assignee": _user(fields.get("assignee")),
                "updated": fields.get("updated"),
            }
        )
    return rows


def fetch_attachment(
    client: JiraClient,
    issue: Dict[str, Any],
    filename: str,
    out_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Download one of an issue's attachments and report where it landed."""
    matches = [
        att
        for att in issue.get("attachments", [])
        if att["filename"].lower() == filename.lower()
    ]
    if not matches:
        partial = [
            att
            for att in issue.get("attachments", [])
            if filename.lower() in att["filename"].lower()
        ]
        if len(partial) != 1:
            names = ", ".join(att["filename"] for att in issue.get("attachments", []))
            raise JiraError(
                f"No attachment matching {filename!r} on {issue['key']}."
                + (f" Available: {names}" if names else " It has none.")
            )
        matches = partial

    attachment = matches[0]
    directory = out_dir or attachment_dir(issue["key"])
    dest = directory / attachment["filename"]
    client.download(attachment["url"], dest)

    text = None
    if dest.stat().st_size <= 512 * 1024:
        try:
            text = dest.read_text(encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, OSError):
            text = None  # binary: the caller reads it from disk instead
    return {"attachment": attachment, "path": dest, "text": text}


# -- normalisation ----------------------------------------------------------


def normalize(
    bundle: Dict[str, Any], config: Config, field_map: Dict[str, str]
) -> Dict[str, Any]:
    raw = bundle["issue"]
    fields = raw.get("fields") or {}
    key = raw.get("key", "?")

    description = body_text(fields.get("description"))
    comments = [
        {
            "author": _user(comment.get("author")),
            "created": comment.get("created"),
            "updated": comment.get("updated"),
            "edited": bool(
                comment.get("updated")
                and comment.get("updated") != comment.get("created")
            ),
            "body": body_text(comment.get("body")) or "",
        }
        for comment in bundle.get("comments", [])
    ]

    issue: Dict[str, Any] = {
        "key": key,
        "url": config.browse_url(key),
        "summary": fields.get("summary") or "",
        "type": scalar(fields.get("issuetype")),
        "status": scalar(fields.get("status")),
        "status_category": (
            (fields.get("status") or {}).get("statusCategory", {}).get("name")
            if isinstance(fields.get("status"), dict)
            else None
        ),
        "resolution": scalar(fields.get("resolution")),
        "priority": scalar(fields.get("priority")),
        "assignee": _user(fields.get("assignee")),
        "reporter": _user(fields.get("reporter")),
        "created": fields.get("created"),
        "updated": fields.get("updated"),
        "resolved": fields.get("resolutiondate"),
        "due": fields.get("duedate"),
        "project": scalar(fields.get("project")),
        "components": _names(fields.get("components")),
        "labels": [l for l in (fields.get("labels") or []) if l],
        "affects_versions": _names(fields.get("versions")),
        "fix_versions": _names(fields.get("fixVersions")),
        "environment": body_text(fields.get("environment")),
        "description": description,
        "parent": _linked_issue(fields.get("parent")),
        "subtasks": [
            _linked_issue(sub) for sub in (fields.get("subtasks") or []) if sub
        ],
        "links": _issue_links(fields.get("issuelinks")),
        "custom": _custom_fields(fields, config, field_map),
        "comments": comments,
        "history": _history(bundle.get("changelog", [])),
        "attachments": [
            {
                "filename": att.get("filename", "?"),
                "size": att.get("size"),
                "mime": att.get("mimeType"),
                "created": att.get("created"),
                "author": _user(att.get("author")),
                "url": att.get("content"),
            }
            for att in (fields.get("attachment") or [])
        ],
        "remote_links": [
            {
                "title": (link.get("object") or {}).get("title") or link.get("id"),
                "url": (link.get("object") or {}).get("url"),
                "relationship": link.get("relationship"),
            }
            for link in bundle.get("remote_links", [])
        ],
    }

    linked_keys = [issue["key"]]
    linked_keys += [item["key"] for item in issue["links"]]
    linked_keys += [item["key"] for item in issue["subtasks"]]
    if issue["parent"]:
        linked_keys.append(issue["parent"]["key"])
    issue["refs"] = extract_refs(
        [description or "", issue["environment"] or ""]
        + [comment["body"] for comment in comments],
        config.ref_patterns,
        exclude=linked_keys,
    )
    return issue


def _linked_issue(value: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(value, dict) or not value.get("key"):
        return None
    fields = value.get("fields") or {}
    return {
        "key": value["key"],
        "summary": fields.get("summary") or "",
        "status": scalar(fields.get("status")),
        "resolution": scalar(fields.get("resolution")),
        "type": scalar(fields.get("issuetype")),
    }


def _issue_links(values: Any) -> List[Dict[str, Any]]:
    links = []
    for link in values or []:
        if not isinstance(link, dict):
            continue
        link_type = link.get("type") or {}
        for side, phrase_key in (("outwardIssue", "outward"), ("inwardIssue", "inward")):
            target = _linked_issue(link.get(side))
            if target:
                target["relation"] = link_type.get(phrase_key) or link_type.get("name")
                links.append(target)
    return links


def _custom_fields(
    fields: Dict[str, Any], config: Config, field_map: Dict[str, str]
) -> List[Dict[str, str]]:
    allow = {name.lower() for name in config.custom_fields}
    hide = {name.lower() for name in config.hide_fields}
    out = []
    for field_id, value in sorted(fields.items()):
        if field_id in _STANDARD_FIELDS:
            continue
        name = field_map.get(field_id, field_id)
        if name.lower() in hide:
            continue
        if allow and name.lower() not in allow:
            continue
        text = scalar(value)
        if text and not _JAVA_DUMP_RE.search(text):
            out.append({"name": name, "value": text})
    out.sort(key=lambda item: item["name"].lower())  # by name, not by field id
    return out


def _history(histories: Any) -> List[Dict[str, Any]]:
    entries = []
    for entry in histories or []:
        changes = []
        for item in entry.get("items") or []:
            field_name = item.get("field") or item.get("fieldId") or "?"
            before = item.get("fromString") or item.get("from")
            after = item.get("toString") or item.get("to")
            if field_name.lower() == "description" or (
                before is None and after is None
            ):
                # Description edits dump both full bodies into the changelog.
                changes.append({"field": field_name, "from": None, "to": None})
                continue
            changes.append({"field": field_name, "from": before, "to": after})
        if changes:
            entries.append(
                {
                    "author": _user(entry.get("author")),
                    "created": entry.get("created"),
                    "changes": changes,
                }
            )
    entries.sort(key=lambda e: e.get("created") or "")
    return entries
