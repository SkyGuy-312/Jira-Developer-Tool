from unittest.mock import Mock

import pytest

from jira_tool import read
from jira_tool.cache import Cache
from jira_tool.config import Config
from jira_tool.jira_client import JiraError


def make_config(**overrides):
    defaults = dict(base_url="https://jira.example.com", auth_method="pat", token="tok")
    defaults.update(overrides)
    return Config(**defaults)


FIELD_MAP = {
    "customfield_10001": "Severity",
    "customfield_10002": "Root Cause",
    "customfield_10003": "Sprint",
}


def raw_issue(**field_overrides):
    fields = {
        "summary": "Fault flag stuck at 0x50",
        "issuetype": {"name": "Bug"},
        "status": {"name": "In Progress", "statusCategory": {"name": "In Progress"}},
        "resolution": None,
        "priority": {"name": "Major"},
        "assignee": {"displayName": "Sherif Ahmed", "name": "sahmed"},
        "reporter": {"displayName": "Dana Lee"},
        "created": "2026-05-02T10:11:12.000+0300",
        "updated": "2026-08-14T09:30:00.000+0300",
        "resolutiondate": None,
        "project": {"name": "Integration"},
        "components": [{"name": "FaultManager"}],
        "labels": ["sit", "firmware"],
        "versions": [{"name": "25.20"}],
        "fixVersions": [{"name": "25.30"}],
        "environment": "Rig 4, firmware 25.30.1",
        "description": "Seen on PROJ-1200 too.\nh2. Trace\n{code:c}status = 0x50;{code}",
        "customfield_10001": {"value": "S2"},
        "customfield_10002": None,
        "customfield_10003": ["com.atlassian.greenhopper.Sprint@1[id=9,name=Sprint 4,x=1]"],
        "comment": {"total": 1, "comments": [
            {
                "author": {"displayName": "Dana Lee"},
                "created": "2026-05-03T08:14:00.000+0300",
                "updated": "2026-05-03T08:14:00.000+0300",
                "body": "Reproduced on rig 2, see OTHER-77.",
            }
        ]},
        "attachment": [
            {
                "filename": "trace.asc",
                "size": 2400,
                "mimeType": "text/plain",
                "created": "2026-05-02T11:00:00.000+0300",
                "author": {"displayName": "Dana Lee"},
                "content": "https://jira.example.com/secure/attachment/1/trace.asc",
            }
        ],
        "issuelinks": [
            {
                "type": {"name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
                "outwardIssue": {
                    "key": "PROJ-2801",
                    "fields": {"summary": "SIT handoff", "status": {"name": "Open"}},
                },
            },
            {
                "type": {"name": "Duplicate", "inward": "is duplicated by",
                         "outward": "duplicates"},
                "inwardIssue": {
                    "key": "PROJ-2400",
                    "fields": {"summary": "Old report", "status": {"name": "Closed"},
                               "resolution": {"name": "Done"}},
                },
            },
        ],
        "subtasks": [
            {"key": "PROJ-2716", "fields": {"summary": "Add UT", "status": {"name": "Done"}}}
        ],
        "parent": {"key": "PROJ-2000", "fields": {"summary": "Epic",
                                                  "status": {"name": "Open"}}},
    }
    fields.update(field_overrides)
    return {"key": "PROJ-2715", "fields": fields}


def bundle(**field_overrides):
    issue = raw_issue(**field_overrides)
    return {
        "issue": issue,
        "comments": issue["fields"]["comment"]["comments"],
        "changelog": [
            {
                "author": {"displayName": "Bob"},
                "created": "2026-06-14T11:02:00.000+0300",
                "items": [{"field": "status", "fromString": "Fixed",
                           "toString": "Reopened"}],
            },
            {
                "author": {"displayName": "Dana Lee"},
                "created": "2026-05-03T08:20:00.000+0300",
                "items": [{"field": "status", "fromString": "In Progress",
                           "toString": "Fixed"},
                          {"field": "description", "fromString": "old body",
                           "toString": "new body"}],
            },
        ],
        "remote_links": [
            {"object": {"title": "Gerrit 123456", "url": "https://gerrit/123456"}}
        ],
    }


# -- normalisation ----------------------------------------------------------


def test_normalize_core_fields():
    issue = read.normalize(bundle(), make_config(), FIELD_MAP)

    assert issue["key"] == "PROJ-2715"
    assert issue["type"] == "Bug"
    assert issue["status"] == "In Progress"
    assert issue["resolution"] is None
    assert issue["assignee"] == "Sherif Ahmed"
    assert issue["reporter"] == "Dana Lee"
    assert issue["components"] == ["FaultManager"]
    assert issue["labels"] == ["sit", "firmware"]
    assert issue["affects_versions"] == ["25.20"]
    assert issue["fix_versions"] == ["25.30"]
    assert issue["url"] == "https://jira.example.com/browse/PROJ-2715"


def test_wiki_code_block_becomes_fenced():
    issue = read.normalize(bundle(), make_config(), FIELD_MAP)
    assert "```c\nstatus = 0x50;\n```" in issue["description"]


def test_wiki_lists_become_markdown_lists_not_headings():
    # '#' starts an ordered list item in wiki markup. Passed through, a
    # four-item list would render as four H1 headings.
    issue = read.normalize(
        bundle(description="# Implement it.\n# Perform UT.\n** nested note\n* flat"),
        make_config(),
        FIELD_MAP,
    )
    assert "1. Implement it." in issue["description"]
    assert "1. Perform UT." in issue["description"]
    assert "  - nested note" in issue["description"]
    assert "- flat" in issue["description"]
    assert "# Implement" not in issue["description"]


def test_indented_list_items_still_convert():
    # Real tickets indent continuation items; the marker is not at column 0.
    issue = read.normalize(
        bundle(description="# First\n # Second\n  # Third"), make_config(), FIELD_MAP
    )
    assert issue["description"].splitlines() == ["1. First", "1. Second", "1. Third"]


def test_wiki_table_header_gets_a_markdown_separator():
    issue = read.normalize(
        bundle(description="||ID||Title||\n|DOC-1|Truth table|"),
        make_config(),
        FIELD_MAP,
    )
    assert "| ID | Title |" in issue["description"]
    assert "|---|---|" in issue["description"]


def test_inline_bold_is_not_mistaken_for_a_list():
    issue = read.normalize(
        bundle(description="*emphasis* leads the line"), make_config(), FIELD_MAP
    )
    assert issue["description"].startswith("*emphasis*")


def test_wiki_links_and_mentions_are_converted():
    issue = read.normalize(
        bundle(description="See [change 400123|https://gerrit/400123], ask [~jdoe].\n"
                           "Raw [https://polarion/wi/DOC-1]"),
        make_config(),
        FIELD_MAP,
    )
    assert "[change 400123](https://gerrit/400123)" in issue["description"]
    assert "@jdoe" in issue["description"]
    assert "<https://polarion/wi/DOC-1>" in issue["description"]


def test_code_blocks_are_immune_to_the_other_wiki_rules():
    issue = read.normalize(
        bundle(description="{code:c}\n*ptr = 1;\n# not a list\n{code}"),
        make_config(),
        FIELD_MAP,
    )
    assert "*ptr = 1;" in issue["description"]
    assert "# not a list" in issue["description"]


def test_java_bean_dumps_are_dropped_from_custom_fields():
    dump = ("{summaryBean=com.atlassian.jira.plugin.devstatus.rest.SummaryBean"
            "@2709308f[summary={pullrequest=...}]}")
    issue = read.normalize(
        bundle(customfield_10002=dump), make_config(), FIELD_MAP
    )
    assert "Root Cause" not in [item["name"] for item in issue["custom"]]


def test_integral_floats_lose_their_decimal():
    assert read.scalar(8.0) == "8"
    assert read.scalar(2.5) == "2.5"


def test_configured_pattern_claims_keys_from_the_generic_bucket():
    config = make_config(ref_patterns={"Polarion": r"\bDOC-\d+\b"})
    issue = read.normalize(
        bundle(description="Per DOC-100200 and PROJ-9."), config, FIELD_MAP
    )
    assert issue["refs"]["Polarion"] == ["DOC-100200"]
    assert "DOC-100200" not in issue["refs"]["Issues"]
    assert "PROJ-9" in issue["refs"]["Issues"]


def test_headings_in_ticket_text_cannot_forge_section_structure():
    issue = read.normalize(
        bundle(description="h1. Top\nh2. Trace\nbody"), make_config(), FIELD_MAP
    )
    # Demoted so they nest under the render's own '## Description'.
    assert "#### Top" in issue["description"]
    assert "##### Trace" in issue["description"]
    assert "\n## " not in issue["description"]


def test_custom_fields_named_and_empties_dropped():
    issue = read.normalize(bundle(), make_config(), FIELD_MAP)
    names = {item["name"]: item["value"] for item in issue["custom"]}

    assert names["Severity"] == "S2"
    assert names["Sprint"] == "Sprint 4"  # unwrapped from the greenhopper blob
    assert "Root Cause" not in names  # None-valued fields are not rendered


def test_custom_field_allowlist_and_denylist():
    allowed = read.normalize(
        bundle(), make_config(custom_fields=["Severity"]), FIELD_MAP
    )
    assert [item["name"] for item in allowed["custom"]] == ["Severity"]

    hidden = read.normalize(bundle(), make_config(hide_fields=["Sprint"]), FIELD_MAP)
    assert "Sprint" not in [item["name"] for item in hidden["custom"]]


def test_unmapped_custom_field_falls_back_to_its_id():
    issue = read.normalize(bundle(), make_config(), {})
    assert {"name": "customfield_10001", "value": "S2"} in issue["custom"]


def test_links_use_directional_phrases():
    issue = read.normalize(bundle(), make_config(), FIELD_MAP)
    relations = {item["key"]: item["relation"] for item in issue["links"]}

    assert relations["PROJ-2801"] == "blocks"
    assert relations["PROJ-2400"] == "is duplicated by"


def test_refs_exclude_self_and_already_linked_issues():
    issue = read.normalize(bundle(), make_config(), FIELD_MAP)
    refs = issue["refs"]["Issues"]

    assert "PROJ-1200" in refs  # only named in the description
    assert "OTHER-77" in refs  # only named in a comment
    assert "PROJ-2715" not in refs  # itself
    assert "PROJ-2400" not in refs  # already rendered under Links


def test_standards_are_not_mistaken_for_issue_keys():
    issue = read.normalize(
        bundle(description="Encode as UTF-8 per ISO-26262 and SAE-J1939; see PROJ-9."),
        make_config(),
        FIELD_MAP,
    )
    # OTHER-77 comes from the fixture's comment and is a real key.
    assert issue["refs"]["Issues"] == ["OTHER-77", "PROJ-9"]


def test_ref_patterns_pick_up_domain_codes():
    config = make_config(ref_patterns={"DTCs": r"\b[BCPU][0-9A-F]{5}\b"})
    issue = read.normalize(
        bundle(description="Fault C1234F fires with U5678B on wake."), config, FIELD_MAP
    )
    assert issue["refs"]["DTCs"] == ["C1234F", "U5678B"]


def test_history_is_chronological_and_collapses_description_edits():
    issue = read.normalize(bundle(), make_config(), FIELD_MAP)

    assert [entry["author"] for entry in issue["history"]] == ["Dana Lee", "Bob"]
    first = issue["history"][0]["changes"]
    assert {"field": "status", "from": "In Progress", "to": "Fixed"} in first
    assert {"field": "description", "from": None, "to": None} in first


def test_bad_ref_pattern_is_rejected_by_config():
    from jira_tool.config import ConfigError

    with pytest.raises(ConfigError):
        make_config(ref_patterns={"broken": "[unclosed"})


# -- markup coercion --------------------------------------------------------


def test_adf_body_is_flattened():
    adf = {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Root cause:"}]},
            {"type": "heading", "attrs": {"level": 1},
             "content": [{"type": "text", "text": "Trace"}]},
            {"type": "codeBlock", "attrs": {"language": "c"},
             "content": [{"type": "text", "text": "x = 1;"}]},
        ],
    }
    text = read.body_text(adf)
    assert "Root cause:" in text
    assert "#### Trace" in text  # demoted, like the wiki-markup path
    assert "```c\nx = 1;\n```" in text


def test_scalar_handles_the_shapes_jira_returns():
    assert read.scalar({"value": "S2"}) == "S2"
    assert read.scalar({"displayName": "Dana", "name": "dlee"}) == "Dana"
    assert read.scalar([{"name": "a"}, {"name": "b"}]) == "a, b"
    assert read.scalar(3) == "3"
    assert read.scalar("  ") is None
    assert read.scalar(None) is None


# -- fetching ---------------------------------------------------------------


class FakeClient:
    def __init__(self, issue=None, comment_pages=None):
        self.issue = issue if issue is not None else raw_issue()
        self.comment_pages = comment_pages
        self.calls = []

    def get_issue(self, key, fields=None, expand=None):
        self.calls.append(("get_issue", key))
        return self.issue

    def get_issue_updated(self, key):
        self.calls.append(("probe", key))
        return self.issue["fields"]["updated"]

    def get_comments(self, key):
        self.calls.append(("get_comments", key))
        return self.comment_pages or []

    def get_changelog(self, key):
        self.calls.append(("get_changelog", key))
        return []

    def get_remote_links(self, key):
        return []

    def get_fields(self):
        return [{"id": k, "name": v} for k, v in FIELD_MAP.items()]


def test_fetch_bundle_paginates_when_embedded_comments_are_capped():
    issue = raw_issue()
    issue["fields"]["comment"] = {"total": 40, "maxResults": 20, "comments": [{}] * 20}
    client = FakeClient(issue, comment_pages=[{"body": "x"}] * 40)

    result = read.fetch_bundle(client, "PROJ-2715")

    assert ("get_comments", "PROJ-2715") in client.calls
    assert len(result["comments"]) == 40


def test_fetch_bundle_keeps_embedded_comments_when_complete():
    client = FakeClient()
    read.fetch_bundle(client, "PROJ-2715")
    assert ("get_comments", "PROJ-2715") not in client.calls


def test_fetch_issue_serves_fresh_cache_without_touching_jira(tmp_path):
    client = FakeClient()
    cache = Cache(3600, tmp_path)
    config = make_config()

    read.fetch_issue(client, config, "PROJ-2715", cache=cache, field_map=FIELD_MAP)
    client.calls.clear()
    issue = read.fetch_issue(client, config, "PROJ-2715", cache=cache,
                             field_map=FIELD_MAP)

    assert client.calls == []
    assert issue["key"] == "PROJ-2715"


def test_fetch_issue_probes_updated_when_ttl_expired(tmp_path):
    client = FakeClient()
    config = make_config()
    read.fetch_issue(client, config, "PROJ-2715", cache=Cache(3600, tmp_path),
                     field_map=FIELD_MAP)
    client.calls.clear()

    # TTL of 0 forces the freshness probe; 'updated' is unchanged, so the
    # cached bundle is reused and no full fetch happens.
    read.fetch_issue(client, config, "PROJ-2715", cache=Cache(0, tmp_path),
                     field_map=FIELD_MAP)

    assert client.calls == [("probe", "PROJ-2715")]


def test_fetch_issue_refetches_when_the_issue_moved(tmp_path):
    client = FakeClient()
    config = make_config()
    read.fetch_issue(client, config, "PROJ-2715", cache=Cache(3600, tmp_path),
                     field_map=FIELD_MAP)
    client.calls.clear()
    client.issue = raw_issue(updated="2026-08-16T12:00:00.000+0300",
                             summary="Now with a new title")

    issue = read.fetch_issue(client, config, "PROJ-2715", cache=Cache(0, tmp_path),
                             field_map=FIELD_MAP)

    assert ("get_issue", "PROJ-2715") in client.calls
    assert issue["summary"] == "Now with a new title"


def test_fetch_issue_serves_stale_cache_when_jira_is_unreachable(tmp_path):
    client = FakeClient()
    config = make_config()
    read.fetch_issue(client, config, "PROJ-2715", cache=Cache(3600, tmp_path),
                     field_map=FIELD_MAP)
    client.get_issue_updated = Mock(side_effect=JiraError("Could not reach Jira"))

    issue = read.fetch_issue(client, config, "PROJ-2715", cache=Cache(0, tmp_path),
                             field_map=FIELD_MAP)

    assert issue["key"] == "PROJ-2715"
    assert "not reachable" in issue["note"]


def test_fetch_issue_raises_when_uncached_and_jira_is_down(tmp_path):
    client = FakeClient()
    client.get_issue = Mock(side_effect=JiraError("Could not reach Jira"))

    with pytest.raises(JiraError):
        read.fetch_issue(client, make_config(), "PROJ-2715",
                         cache=Cache(3600, tmp_path), field_map=FIELD_MAP)


def test_field_map_falls_back_to_stale_copy_when_jira_errors(tmp_path):
    client = FakeClient()
    first = read.load_field_map(client, tmp_path)
    assert first["customfield_10001"] == "Severity"

    client.get_fields = Mock(side_effect=JiraError("down"))
    assert read.load_field_map(client, tmp_path, refresh=True) == first


def test_fetch_attachment_reports_the_available_names(tmp_path):
    issue = read.normalize(bundle(), make_config(), FIELD_MAP)
    with pytest.raises(JiraError) as excinfo:
        read.fetch_attachment(FakeClient(), issue, "nope.log", out_dir=tmp_path)
    assert "trace.asc" in str(excinfo.value)


def test_fetch_attachment_downloads_and_inlines_text(tmp_path):
    issue = read.normalize(bundle(), make_config(), FIELD_MAP)
    client = FakeClient()

    def fake_download(url, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("0x50 stuck\n", encoding="utf-8")
        return dest

    client.download = fake_download
    result = read.fetch_attachment(client, issue, "trace", out_dir=tmp_path)

    assert result["path"].name == "trace.asc"
    assert result["text"] == "0x50 stuck\n"
