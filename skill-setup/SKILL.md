---
name: tickets-setup
description: Start the Tickets board (http://127.0.0.1:8123) and set the current folder up as a Tickets project — backend, MCP server, the /tickets skill, the project row, and the repo's CLAUDE.md rules — fixing only what is missing. Run by hand as /tickets-setup in a session opened in the repo's folder, before the first /tickets.
disable-model-invocation: true
---

# Tickets setup: get this folder ready for /tickets

Check each step and fix only what is missing; a step already in order is one line in the
summary. Safe to run again any time. This is a person's command, so unlike `/tickets` it
may configure the project — over the board's HTTP API (`/api/projects`), since the MCP
tools deliberately cannot.

The Tickets repo is the parent of this skill's folder: `${CLAUDE_SKILL_DIR}/..`
(resolve it to an absolute path once and use that). The **folder** is the session's
working directory; the **repo** is its git toplevel.

## 1. Backend

`GET http://127.0.0.1:8123/api/projects` (PowerShell `Invoke-RestMethod`, or `curl -s`).
Answers → fine. Otherwise start it detached — a uvicorn started straight from a tool call
dies with the call:
`powershell -NoProfile -File <tickets repo>\restart-backend.ps1 -Delay 1`, wait ~12 s and
ask again. Still down → stop: show the tail of `<tickets repo>\restart-backend.log` and
say `uv sync` in the Tickets repo may be needed.

## 2. The board's tools and the /tickets skill

- `claude mcp get tickets`. Missing → `claude mcp add --scope user --transport http
  tickets http://127.0.0.1:8123/mcp`. A server added now reaches only new sessions: say
  so in the summary (restart the session, or `/mcp`, before `/tickets`). Present but not
  connected → it was the backend; `/mcp` reconnects.
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

## 6. Summary

One line per step: ok / fixed (what) / needs you (what). Then the next move: open the
board, add a card in `todo`, drag it to `plan`, run `/tickets` here (after restarting the
session if step 2 added the MCP server).
