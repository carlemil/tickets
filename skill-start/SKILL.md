---
name: tickets-start
description: Work this folder's Tickets board (http://127.0.0.1:8123) — set the folder up as a Tickets project where anything is missing (backend, MCP server, CLAUDE.md rules, the project row), then take every ready card in plan, develop and test through to verify, unattended, every N minutes (default 15m) for as long as this session stays open. Run by hand as /tickets-start in a session opened in the repo's folder; argument an interval (10m, 1h), `once` for a single run, or a card id (#12 or 12) to work only that card once. /tickets-stop stops the loop.
disable-model-invocation: true
---

# Tickets start: get this folder ready, then work the board

Two parts: steps 1–5 set the folder up, fixing only what is missing (a step already in
order is one line in the summary, so a second `/tickets-start` is quick), and step 6
works the board — on a loop, or once. The work itself is `run.md` next to this file.
This setup part is a person's command, so it may configure the project — over the
board's HTTP API (`/api/projects`), since the MCP tools deliberately cannot; a run never
does.

The Tickets repo is `git -C ${CLAUDE_SKILL_DIR} rev-parse --show-toplevel` — not
`${CLAUDE_SKILL_DIR}/..`: the skill folder is a link, and `..` would land in
`~/.claude/skills`. Resolve it once and use that. The **folder** is the session's
working directory; the **repo** is its git toplevel.

## 1. Backend

`GET http://127.0.0.1:8123/api/projects` (PowerShell `Invoke-RestMethod`, or `curl -s`).
Answers → fine. Otherwise start it detached — a uvicorn started straight from a tool call
dies with the call:
`powershell -NoProfile -File <tickets repo>\restart-backend.ps1 -Delay 1`, wait ~12 s and
ask again. Still down → stop: show the tail of `<tickets repo>\restart-backend.log` and
say `uv sync` in the Tickets repo may be needed.

**Start it at logon.** Claude Code connects its MCP servers once, when a session starts,
and does not retry: a session opened while the board was down has no board tools until
`/mcp` reconnects them (a run falls back to HTTP, but the tools are
better). So the board should already be up when any session starts. Windows: look for
`tickets-backend.cmd` in the Startup folder (`[Environment]::GetFolderPath('Startup')`).
Missing → ask once (AskUserQuestion: "Start the Tickets board when you log in?",
yes recommended); yes → write that file with the single line
`start "" /min powershell -NoProfile -WindowStyle Hidden -File "<tickets repo>\restart-backend.ps1"`.
Removing the file undoes it. Elsewhere, suggest the platform's equivalent (a launchd
agent, a systemd user unit) and do not install it.

## 2. The board's tools

- `claude mcp get tickets`. Missing → `claude mcp add --scope user --transport http
  tickets http://127.0.0.1:8123/mcp`. A server added now reaches only new sessions: say
  so in the summary (until a new session or `/mcp`, runs work over HTTP).
- Registered but its tools are missing in this session (no `mcp__tickets__*` tools, or
  `claude mcp get tickets` not connected while the board answers) → the session started
  before the board did. Say so in the summary: `/mcp` → tickets → Reconnect, or a new
  session; the logon start above keeps it from happening again.

## 3. The repo

- `git -C <folder> rev-parse --show-toplevel`. Not a repo → cards can be planned but
  never built: ask whether to `git init` here; no → go on and say so in the summary.
- `git -C <repo> remote get-url origin`. None → say the test stage pushes `card/<id>`
  there, so add one before the first card reaches test. Do not create a remote yourself.

## 4. CLAUDE.md

The repo's `CLAUDE.md` holds the project's rules; every session and subagent reads it,
and the board opens a setup card if it says nothing about testing or deploying. Read it
(it may not exist). If it already says how to test and how to deploy, leave it.

Otherwise work out what you can from the repo (package.json scripts, pyproject, Makefile,
gradle, CI workflow files, README) and use AskUserQuestion — one call, at most 3
questions — for what you cannot tell: the test command (your guess first, marked
recommended), and how a card is deployed (nothing / restart a local server / install on
a connected phone / other; production only if they say so). Then add a short `Tickets`
section (or extend the file) with just those rules; show what you wrote. Do not commit
it — it is the person's repo.

## 5. The project

`GET /api/projects` and look for one whose `path` is the folder (case-insensitive, `\`
and `/` alike, no trailing slash).

- Found → show its name, land and instructions; change nothing unless asked.
- Not found, and one with the folder's name but another path exists → ask whether to
  point that one here (`PATCH /api/projects/<name>` with `{"path": …}`) or make a new one.
- Not found → ask in one AskUserQuestion call: the name (folder name recommended) and how
  a passed card lands — `merge` into the base branch (recommended) or `pr` (needs `gh`;
  check `gh auth status` and warn if it fails). Then `POST /api/projects` with `{"name",
  "path": <folder>, "land", "instructions"}`; `instructions` holds only board-specific
  extras such as the deploy target from step 4, or "" — the rules live in CLAUDE.md.

Creating a project opens a `todo` card for anything its setup still lacks;
`GET /api/cards?project=<name>` and list any authored by `board`.

## 6. Work the board

No project row after step 5 (the person declined) → stop here: a run would find no
project. Say so in the summary. Otherwise pick by `$ARGUMENTS`:

- a card id (`12`, `#12`) → **one run, that card only**, no loop;
- `once` → **one run**, no loop;
- an interval (`10m`, `30m`, `1h`) or nothing → **the loop**, at that interval or `15m`.
  Shorter than `5m` → use `5m` and say so: a run takes minutes.

**One run:** read `${CLAUDE_SKILL_DIR}/run.md` and follow it exactly in this folder, with
session id `${CLAUDE_SESSION_ID}`, the Tickets repo from above, and the card id if one
was given. Write the summary below first, briefly, then the run's own report at its end.

**The loop:** `/loop` runs a prompt, not this skill (it refuses model invocation, so
starting work stays a person's act); each tick reads `run.md` and follows it — same run,
same rules. It lives in this session only: closing it stops the loop.

- Already running? CronList (load it with ToolSearch `select:CronList` if it is
  deferred): a job whose prompt starts with `Tickets loop tick.` → do not start a second;
  say it is already running, at what cadence.
- Invoke the Skill tool with skill `loop` and args `<interval> <tick prompt>`, the tick
  prompt being exactly this, with `<tickets repo>` replaced by the path resolved above:

> Tickets loop tick. Read ${CLAUDE_SKILL_DIR}/run.md and do one run of it in this
> folder, following it exactly, with session id ${CLAUDE_SESSION_ID} and Tickets repo
> <tickets repo>. If cards from an earlier tick are still in flight in this session
> (background subagents not yet reported), do not start a new run: answer only
> "tickets: previous run still working" and stop. If the queue is empty, do not write
> ideas or a summary: answer only "tickets: nothing to do" and stop.

`/loop` runs the first tick right away; write the summary below before that run's own
output gets long, or after it if the run is already under way.

## 7. Summary

One line per step: ok / fixed (what) / needs you (what). Then the board as a clickable
markdown link on a line of its own — `[Open the board](http://127.0.0.1:8123/)`. Loop:
two lines — the board is worked every <interval> while this session is open, and
`/tickets-stop` (or closing the session) stops it. If step 2 added the MCP server, say
runs work over HTTP until a new session picks the tools up. The next move for the
person: add a card in `todo` and drag it to `plan`; the next tick takes it (after a
single run: `/tickets-start` again). `/tickets-open-board` opens the board in the
browser.
