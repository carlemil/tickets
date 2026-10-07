---
name: tickets-start
description: Start working this folder's Tickets board — set the folder up as a Tickets project where anything is missing (backend at http://127.0.0.1:8123, MCP server, the /tickets skill, CLAUDE.md rules, the project row), then run /tickets every N minutes (default 15m) for as long as this session stays open. Run by hand as /tickets-start [interval, e.g. 10m or 1h] in a session opened in the repo's folder; /tickets-stop stops it.
disable-model-invocation: true
---

# Tickets start: get this folder ready, then work the board on a timer

Two parts: steps 1–5 set the folder up, fixing only what is missing (a step already in
order is one line in the summary, so a second `/tickets-start` is quick), and step 6
starts the loop. This is a person's command, so unlike `/tickets` it may configure the
project — over the board's HTTP API (`/api/projects`), since the MCP tools deliberately
cannot.

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
`/mcp` reconnects them (the `/tickets` skill falls back to HTTP, but the tools are
better). So the board should already be up when any session starts. Windows: look for
`tickets-backend.cmd` in the Startup folder (`[Environment]::GetFolderPath('Startup')`).
Missing → ask once (AskUserQuestion: "Start the Tickets board when you log in?",
yes recommended); yes → write that file with the single line
`start "" /min powershell -NoProfile -WindowStyle Hidden -File "<tickets repo>\restart-backend.ps1"`.
Removing the file undoes it. Elsewhere, suggest the platform's equivalent (a launchd
agent, a systemd user unit) and do not install it.

## 2. The board's tools and the /tickets skill

- `claude mcp get tickets`. Missing → `claude mcp add --scope user --transport http
  tickets http://127.0.0.1:8123/mcp`. A server added now reaches only new sessions: say
  so in the summary (restart the session, or `/mcp`, before `/tickets`).
- Registered but its tools are missing in this session (no `mcp__tickets__*` tools, or
  `claude mcp get tickets` not connected while the board answers) → the session started
  before the board did. Say so in the summary: `/mcp` → tickets → Reconnect, or a new
  session; the logon start above keeps it from happening again.
- `~/.claude/skills/tickets` exists. Missing → link it (Windows:
  `New-Item -ItemType Junction "$HOME\.claude\skills\tickets" -Target <tickets repo>\skill`;
  elsewhere `ln -s <tickets repo>/skill ~/.claude/skills/tickets`).

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

## 6. The loop

`/tickets` refuses to be started by the model (`disable-model-invocation`), so `/loop`
cannot call it by name. Instead each tick reads the skill's file and follows it — same
run, same rules. Read that way its placeholders are not filled in, so the tick prompt
fills them (the session id and the skill folder); the prompt below names them in words
because anything written as a placeholder here is filled with this skill's values. The
loop lives in this session only: closing it stops the loop.

- Already running? CronList (load it with ToolSearch `select:CronList` if it is
  deferred): a job whose prompt starts with `Tickets loop tick.` → do not start a second;
  say it is already running, at what cadence.
- Interval: `$ARGUMENTS` if it is one (like `10m`, `30m`, `1h`), else `15m`. Anything
  shorter than `5m` → use `5m` and say so: a run takes minutes.
- No project row after step 5 (the person declined) → do not start the loop: `/tickets`
  would find no project. Say so in the summary.

Invoke the Skill tool with skill `loop` and args `<interval> <tick prompt>`, the tick
prompt being exactly:

> Tickets loop tick. Read ~/.claude/skills/tickets/SKILL.md and do one run of it in this
> folder, following it exactly, with these differences: where it puts the session-id
> placeholder into `session=`, use ${CLAUDE_SESSION_ID}; where it names the skill-folder
> placeholder, use ~/.claude/skills/tickets. If cards from an earlier tick are still in
> flight in this session (background subagents not yet reported), do not start a new
> run: answer only "tickets: previous run still working" and stop. If the queue is empty,
> do not write ideas or a summary: answer only "tickets: nothing to do" and stop.

`/loop` runs the first tick right away; write the summary below before that run's own
output gets long, or after it if the run is already under way.

## 7. Summary

One line per step: ok / fixed (what) / needs you (what). Then the board as a clickable
markdown link on a line of its own — `[Open the board](http://127.0.0.1:8123/)` — and two
lines on the loop: the board is worked every <interval> while this session is open, and
`/tickets-stop` (or closing the session) stops it. If step 2 added the MCP server, say the
loop works over HTTP until a new session picks the tools up. The next move for the
person: add a card in `todo` and drag it to `plan`; the next tick takes it.
`/tickets-open-board` opens the board in the browser.
