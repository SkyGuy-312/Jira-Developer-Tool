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

## Scheduling the reminder

Cron (Linux/macOS), weekdays at 16:30:

```cron
30 16 * * 1-5 DISPLAY=:0 /path/to/jira-tool remind --notify
```

Windows Task Scheduler: create a daily task running
`jira-tool.exe remind --notify`.

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
`cli.py` (Typer entry points), `config.py`, `display.py`, `utils.py`.

## Roadmap

- **Git-aware drafting** — read your recent branches/commits (branch names
  usually carry the ticket key), draft the day's comment and a time estimate
  from them, so a check-in becomes "accept / edit / skip" per ticket.
- Detect merged PRs and suggest closing the matching ticket.
- Tempo worklog support, if native worklogs aren't what your team uses.
