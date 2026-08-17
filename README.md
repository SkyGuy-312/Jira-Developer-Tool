# Jira Developer Tool

A daily check-in CLI for developers who forget to update their Jira tickets.

You know the failure mode: the sprint ends, the work is done, but the tickets
say nothing — no comments, no logged time, still sitting in "In Progress".
`jira-tool` fixes that by making the update nearly zero effort:

- **`jira-tool remind`** — run it from cron; it lists your "In Progress" /
  "In Review" tickets, flags the ones that have gone quiet, and can raise a
  desktop notification.
- **`jira-tool checkin`** — an interactive end-of-day walk through your active
  tickets. For each one: post a comment, log time worked, close it with a
  resolution, or skip — a few keystrokes per ticket instead of a Jira tab safari.

Built for **Jira Server / Data Center** (REST API v2).

## Install

Requires Python 3.9+.

```bash
pip install .          # or: pipx install .
```

## Setup

```bash
jira-tool setup
```

You'll be asked for:

- your Jira base URL (e.g. `https://jira.mycompany.com`)
- an auth method — a **personal access token** (Jira 8.14+: your avatar →
  Profile → Personal Access Tokens) or username/password basic auth
- the statuses to track (default: `In Progress, In Review`)
- how many days without an update makes a ticket "stale" (default: 2)

Setup tests the connection and writes the config to
`~/.config/jira-tool/config.json` (owner-readable only). If you'd rather not
store the token on disk, leave it empty during setup and export
`JIRA_TOOL_TOKEN` instead.

If your Jira uses an internal certificate authority, set `"verify_ssl"` in the
config file to the path of your CA bundle.

## Daily use

```bash
jira-tool list      # table of your active tickets, staleness color-coded
jira-tool checkin   # interactive walk through each ticket
jira-tool remind    # the cron-friendly report; --notify for a desktop popup
```

During a check-in, each ticket offers:

| Key | Action |
| --- | ------ |
| `c` | write a comment (multi-line, finish with an empty line) |
| `l` | log work (`45m`, `2h`, `1h 30m`, …) with an optional description |
| `d` | transition/close the ticket, picking a resolution (e.g. Done, Won't Fix) and an optional closing comment |
| `o` | open the ticket in your browser |
| `n` | skip to the next ticket |
| `q` | quit (a summary of everything you did is printed at the end) |

Both `checkin` and `list` accept `--jql` to override the default query
(`assignee = currentUser() AND status in (...) ORDER BY updated ASC`).

## Reading tickets

The check-in commands write to Jira; these read from it. A raw Jira issue is
100–200 KB of JSON, most of it avatar URLs and empty custom fields — `show`
renders the few KB that a person (or an agent) actually needs:

```bash
jira-tool show PROJ-1234              # the whole ticket as markdown
jira-tool show PROJ-1234 --no-history --max-comments 5
jira-tool show PROJ-1234 --json       # the normalised issue, for scripting
jira-tool search "text ~ 'C1234F' ORDER BY updated DESC"
jira-tool attach PROJ-1234            # list attachments
jira-tool attach PROJ-1234 trace.asc  # download one (text is printed inline)
```

`show` prints the fields, environment, description, custom fields, linked
issues and subtasks, every comment, the change history (reopens included),
attachments and remote links — and ends with the issue keys and domain codes
it found in the text, so a ticket can be followed to whatever it references.

Reads are cached. Within `cache_ttl_minutes` (default 15) a ticket is served
straight from disk; after that, one small request asks Jira whether the issue
moved and only refetches if it did. If Jira can't be reached at all, the cached
copy is served with a note rather than an error — useful off the VPN. Use
`--refresh` to force a fetch, or `jira-tool cache-clear [KEY]` to drop it.

## Using it from an AI agent (MCP)

`jira-tool mcp` serves the read layer over the [Model Context
Protocol](https://modelcontextprotocol.io) on stdio, so an agent can pull a
ticket into a conversation instead of being told about it second-hand:
`jira_issue`, `jira_search`, `jira_comments`, `jira_history`, `jira_attachment`.

**The MCP surface is read-only by design.** Commenting, logging work and
transitioning stay in `jira-tool checkin`, where a human presses the key — an
agent posting to a team's Jira is visible to everyone and awkward to undo.

Register it with Claude Code:

```bash
claude mcp add --scope user jira -- jira-tool mcp
```

or add it to an MCP client's config directly:

```json
{
  "mcpServers": {
    "jira": { "type": "stdio", "command": "jira-tool", "args": ["mcp"] }
  }
}
```

If `jira-tool` isn't on the launching process's PATH, use the interpreter
instead: `"command": "python", "args": ["-m", "jira_tool", "mcp"]`.

## Tuning what a ticket renders

Optional keys in `config.json`, all of which affect `show` / `search` / MCP:

| Key | What it does |
| --- | --- |
| `custom_fields` | Custom fields to render, by display name. Empty (default) = every non-empty one. |
| `hide_fields` | Field display names to drop. |
| `ref_patterns` | `{label: regex}` of domain codes to extract from issue text, alongside the issue keys that are always extracted. |
| `cache_ttl_minutes` | How long a cached issue is served without asking Jira (default 15). |

For example, to pull diagnostic trouble codes out of ticket text and keep the
field list to what matters on a bug:

```json
{
  "custom_fields": ["Severity", "Root Cause", "Found In Build"],
  "ref_patterns": { "DTCs": "\\b[BCPU][0-9A-F]{5}\\b" }
}
```

Custom-field ids are resolved to their display names automatically (the map is
fetched once and cached for a week).

## Scheduling the reminder

Let the tool set up the schedule for you — no schtasks or crontab wrangling:

```bash
jira-tool schedule add --at 12:30 --at 16:30   # two reminders, weekdays
jira-tool schedule add --at 09:00 --daily      # every day, incl. weekends
jira-tool schedule list                        # what's scheduled
jira-tool schedule remove --at 12:30           # drop one
jira-tool schedule remove                      # drop all jira-tool reminders
```

Pass `--at` as many times as you like (or comma-separate: `--at 12:30,16:30`)
to get several reminders a day. Add `--no-notify` if you'd rather it just log
to the console without a popup.

**Choosing the days** with `--days` (default: weekdays) — useful when your work
week isn't Mon–Fri or shifts around a release:

```bash
jira-tool schedule add --at 16:30 --days sun-thu      # Sun–Thu working week
jira-tool schedule add --at 10:00 --days mon,wed,fri  # specific days
jira-tool schedule add --at 09:00 --days daily        # every day (= --daily)
jira-tool schedule add --at 18:00 --days weekends     # Sat–Sun crunch
```

`--days` accepts a keyword (`weekdays`, `weekends`, `daily`), a comma list of
day names (`sun,mon,tue` — full names work too), or a range that may wrap the
week (`sun-thu`, `fri-mon`). Each reminder keeps its own days, so you can mix a
Sun–Thu reminder and a Mon–Fri one, and removing one won't disturb the other.
`jira-tool schedule list` shows each reminder's time and days.

Reminders **run in the background** — no console window flashes when they fire:

- **Windows**: each time becomes a Task Scheduler entry whose action is
  `pythonw.exe -m jira_tool remind --notify` (pythonw is the windowless Python
  interpreter), and the toast's own PowerShell call is suppressed too. Tasks run
  only while you're logged in, so the toast can reach your desktop.
- **Linux/macOS**: a delimited block in your crontab (other crontab entries are
  left untouched).

### The reminder runs but no toast appears

Two things to check, in order:

1. **Is a ticket actually stale?** A reminder only pops a toast when a ticket
   has gone `stale_after_days` without an update (or when Jira can't be reached).
   If everything's fresh, a silent run is correct. Run `jira-tool list` to see.
2. **Does the notification path work at all?** Verify it independently of ticket
   state — run it under the *same interpreter the scheduler uses*:

   ```powershell
   jira-tool notify-test                    # console interpreter
   pythonw -m jira_tool notify-test         # the windowless one the task uses
   ```

   If the first shows a toast but the second doesn't, the issue is the
   windowless path; if neither does, it's your notification settings (Focus
   Assist / Do Not Disturb, or notifications disabled for the app).

### Doing it by hand

If you prefer, schedule it yourself. Cron (Linux/macOS), weekdays at 16:30:

```cron
30 16 * * 1-5 DISPLAY=:0 /path/to/pythonw -m jira_tool remind --notify
```

Windows Task Scheduler: a daily task running
`pythonw.exe -m jira_tool remind --notify` (use `pythonw`, not `jira-tool.exe`,
to avoid a console window).

When `--notify` is set, a toast is also raised if the tool **can't reach Jira
at all** — e.g. you're off the VPN. Instead of a silent scheduled run that you'd
mistake for "nothing stale", you get a "can't connect — is the VPN on?" nudge so
you can reconnect and run `jira-tool checkin`.

`--notify` picks a notification backend per platform:

- **Linux**: `notify-send` — install libnotify if it's missing
  (e.g. `sudo apt install libnotify-bin`)
- **macOS**: the built-in `osascript`
- **Windows**: a toast notification via Windows PowerShell (no setup needed);
  this also works from inside WSL through `powershell.exe`

## Development

```bash
pip install -e ".[dev]"
pytest
```

Layout: `src/jira_tool/` — `jira_client.py` (thin REST v2 wrapper),
`checkin.py` (interactive flow), `remind.py` (staleness report),
`schedule.py` (scheduler backends), `cli.py` (Typer entry points),
`config.py`, `display.py`, `utils.py`.

The read path is a separate stack over the same client: `read.py` (fetch and
normalise — this is where the Jira quirks live: wiki markup vs. ADF, the four
shapes a custom-field value takes, embedded-vs-paginated comments), `render.py`
(normalised issue → markdown), `cache.py`, and `mcp_server.py`.

Two invariants to preserve when changing them:

- `read.py` and `render.py` must not import rich or typer, and must not print.
  The MCP server shares them, and there stdout carries protocol messages only.
- `render.py` is pure: dict in, string out. It makes the output testable
  without a Jira, which is the only reason the renderers have real coverage.

## Roadmap

- **Git-aware drafting** — read your recent branches/commits (branch names
  usually carry the ticket key), draft the day's comment and a time estimate
  from them, so a check-in becomes "accept / edit / skip" per ticket.
- Detect merged PRs and suggest closing the matching ticket.
- Tempo worklog support, if native worklogs aren't what your team uses.
- Resolve a ticket key to its local commits, so `show` can list the code that
  claimed to fix the bug next to the comments that reported it.
