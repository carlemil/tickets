---
name: tickets-open-board
description: Open the Tickets board (http://127.0.0.1:8123) in the system's default web browser, starting the backend first if it is down. Use when the user asks to open, show or bring up the Tickets board.
---

# Open the Tickets board

1. `GET http://127.0.0.1:8123/api/projects` (PowerShell `Invoke-RestMethod`, or
   `curl -s`). No answer → start it detached — a uvicorn started straight from a tool
   call dies with the call: `powershell -NoProfile -File <tickets repo>/restart-backend.ps1
   -Delay 1`, wait ~12 s, ask again. The Tickets repo is
   `git -C ${CLAUDE_SKILL_DIR} rev-parse --show-toplevel` (not `${CLAUDE_SKILL_DIR}/..`:
   the skill folder is a link, and `..` would land in `~/.claude/skills`). Still down →
   stop: show the tail of `<tickets repo>/restart-backend.log` and suggest `/tickets-start`.
2. Open it in the default browser, not the in-app one: Windows
   `Start-Process "http://127.0.0.1:8123/"`, macOS `open http://127.0.0.1:8123/`, Linux
   `xdg-open http://127.0.0.1:8123/`.
3. Always print the link, even when the browser opened (or failed to — then say so), so
   the user can click or copy it: a clickable `[Tickets board](http://127.0.0.1:8123/)`
   and the bare URL `http://127.0.0.1:8123/` in a code span. Say if you had to start the
   backend.
