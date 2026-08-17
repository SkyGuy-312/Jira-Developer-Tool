from jira_tool import read, render

from test_read import FIELD_MAP, bundle, make_config


def normalized(**overrides):
    return read.normalize(bundle(**overrides), make_config(), FIELD_MAP)


def test_render_issue_has_the_sections_that_matter():
    text = render.render_issue(normalized())

    assert text.startswith("# PROJ-2715 — Fault flag stuck at 0x50")
    for heading in ("## Environment", "## Description", "## Fields", "## Parent",
                    "## Links", "## Subtasks (1)", "## Comments (1)",
                    "## History (2)", "## Attachments (1)", "## Remote links",
                    "## Referenced in text"):
        assert heading in text, heading


def test_render_issue_shows_status_priority_and_people():
    text = render.render_issue(normalized())

    assert "**Status:** In Progress" in text
    assert "**Resolution:** —" in text
    assert "**Assignee:** Sherif Ahmed" in text
    assert "**Reporter:** Dana Lee" in text
    assert "**Fix Version:** 25.30" in text


def test_render_links_read_as_sentences():
    text = render.render_issue(normalized())

    assert "- blocks PROJ-2801 [Open] — SIT handoff" in text
    assert "- is duplicated by PROJ-2400 [Closed/Done] — Old report" in text


def test_render_history_shows_transitions_and_collapsed_edits():
    text = render.render_issue(normalized())

    assert "status: In Progress → Fixed" in text
    assert "status: Fixed → Reopened" in text
    assert "description edited" in text


def test_sections_can_be_switched_off():
    text = render.render_issue(normalized(), comments=False, history=False)

    assert "## Comments" not in text
    assert "## History" not in text
    assert "## Description" in text


def test_comment_limit_keeps_the_newest_and_says_so():
    data = bundle()
    data["comments"] = [
        {
            "author": {"displayName": f"User {i}"},
            "created": "2026-05-03T08:14:00.000+0300",
            "updated": "2026-05-03T08:14:00.000+0300",
            "body": f"comment {i}",
        }
        for i in range(5)
    ]
    issue = read.normalize(data, make_config(), FIELD_MAP)

    text = render.render_issue(issue, max_comments=2)

    assert "comment 4" in text
    assert "comment 0" not in text
    assert "3 earliest comment(s) are omitted" in text
    assert "### 4. User 3" in text  # numbering stays absolute


def test_description_truncation_reports_what_was_cut():
    issue = normalized(description="x" * 500)
    text = render.render_issue(issue, max_body=100)
    assert "[... 400 more characters]" in text


def test_empty_description_is_explicit():
    text = render.render_issue(normalized(description=None))
    assert "## Description\n\n_(empty)_" in text


def test_cache_note_is_surfaced_at_the_top():
    issue = normalized()
    issue["note"] = "Served from cache, Jira is not reachable."
    text = render.render_issue(issue)
    assert text.splitlines()[2].startswith("> Served from cache")


def test_render_search_lists_key_status_and_summary():
    rows = [
        {
            "key": "PROJ-1",
            "summary": "A thing",
            "type": "Bug",
            "status": "Open",
            "resolution": None,
            "priority": "Major",
            "assignee": None,
            "updated": "2026-08-14T09:30:00.000+0300",
        }
    ]
    text = render.render_search(rows, "project = PROJ")

    assert "1 issue(s) for: project = PROJ" in text
    assert "- PROJ-1 [Open] — A thing" in text
    assert "unassigned" in text


def test_render_search_handles_no_matches():
    assert "No issues match" in render.render_search([], "project = NOPE")


def test_render_attachment_inlines_text(tmp_path):
    result = {
        "attachment": {"filename": "trace.asc", "size": 2400, "mime": "text/plain",
                       "created": "2026-05-02T11:00:00.000+0300", "author": "Dana"},
        "path": tmp_path / "trace.asc",
        "text": "0x50 stuck",
    }
    text = render.render_attachment(result)

    assert "# trace.asc" in text
    assert "**Size:** 2.3 KB" in text
    assert "0x50 stuck" in text


def test_render_attachment_points_at_the_path_for_binaries(tmp_path):
    result = {
        "attachment": {"filename": "shot.png", "size": 90000, "mime": "image/png",
                       "created": None, "author": "Dana"},
        "path": tmp_path / "shot.png",
        "text": None,
    }
    text = render.render_attachment(result)

    assert "Binary or too large" in text
    assert "shot.png" in text
