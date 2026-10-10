# Tickets

A personal lane board. You drive it in a browser, Claude drives it over MCP, and both
share one SQLite file (`tickets.db`). Every card records who did what.

It sits *next to* JIRA and similar trackers, not in place of them. Team work and anything
shared stays there. Tickets is for the local loop: you hand coding tasks to an agent on
your machine and review what comes back.

**Local and single-person only.** One process, bound to `127.0.0.1`, with no auth and no
accounts — users are just names, nothing is enforced. "settings…" on the board can host it
on your LAN (`0.0.0.0`, restarted through `restart-backend.ps1`); there is still no
password then, so only on a network you trust. Never host it online.

## Lanes

`todo → plan → develop → test → verify → done`

- `todo` — you write the card: a title and what you want, and why.
- `plan` — the agent writes a plan, and any open questions for you.
- `develop` — the agent implements it on a `card/<id>` branch in its own worktree.
- `test` — the agent runs the project's tests and fixes what fails.
- `verify` — yours: read the comments, look at the branch, try the deployed build. With
  `land: merge` the agent already merged `card/<id>` into the base and pushed; merge it
  yourself only if the deploy comment says the merge was skipped or conflicted. With
  `land: pr`, merge the pull request.
- `done` — finished.

## Setup

You need Python 3.12+, [uv](https://docs.astral.sh/uv/), `git`, and the Claude Code CLI
(`claude` on PATH). Chrome too, but only for the browser
tests.

1. Install the dependencies:

       uv sync

2. Start the backend:

       uv run uvicorn app:app --host 127.0.0.1 --port 8123 --log-config log-config.json

   On Windows, `powershell -NoProfile -File restart-backend.ps1` starts or restarts it.

3. Link the Claude Code skills into your user skills folder, once (Windows shown; on
   macOS/Linux `ln -s <this repo>/<folder> ~/.claude/skills/<name>`):

       foreach ($s in @{'tickets-start'='skill-start';
                        'tickets-open-board'='skill-open-board';
                        'tickets-stop'='skill-stop'}.GetEnumerator()) {
           New-Item -ItemType Junction "$HOME\.claude\skills\$($s.Key)" -Target "$PWD\$($s.Value)"
       }

   They are links, so editing a skill here takes effect in every session at once.

4. In a Claude Code session opened in the repo you want worked, run `/tickets-start`. It
   does the rest (the logon start, the idle gate hook, the MCP server, `CLAUDE.md` rules,
   the project on the board) and starts working the board.

8123 is not a runtime setting. Any free port works, but `restart-backend.ps1`, the
`claude mcp add` URL, the skills and `skill-start/idle_gate.py` must match.

## Skills

Claude Code works the board through three skills, all run from a session opened in the
project's folder.

| Skill | What it does |
|---|---|
| `/tickets-start` | Sets the repo up where needed, then works the board every 15 minutes for as long as this session stays open. |
| `/tickets-start 10m` | The same, at another interval (`30m`, `1h`; at least `5m`). |
| `/tickets-start once` | Sets the repo up where needed, then works the board once. |
| `/tickets-start 12` | Sets the repo up where needed, then works only card 12, once. |
| `/tickets-stop` | Stops the loop. Cards already in progress finish. |
| `/tickets-open-board` | Opens the board in your browser, starting it first if it is down, and prints the link. |

**Setup** only fixes what is missing, so running it again is quick. It starts the board
if it is down and offers to start it at every logon. It registers the `tickets` MCP
server, checks the repo has `git` and an `origin`, helps write the repo's `CLAUDE.md`
test and deploy rules, and creates the project on the board. It also installs the idle
gate (below).

**The idle gate** keeps the loop from spending tokens when it has nothing to do.
`skill-start/idle_gate.py` runs as a `UserPromptSubmit` hook in
`~/.claude/settings.json` and checks the board before each loop tick reaches the model.
If this folder's project has no card a run would take, it blocks the tick and prints
`tickets: nothing to do (<time>)`, so the model never wakes. Otherwise the tick goes
through as usual. A card that is ready, in progress or has an open PR is one a run would
take. Cards waiting for your answers, assigned to someone else or blocked by an unfinished
card are not; a blocker is finished once it is in done, or in verify with a merged PR,
or with no PR in a project that lands by merge. When the board is down, or the folder has no project, the tick also goes
through, so the run can deal with it. Without the gate, an idle tick in a long session
cost about 400k cached tokens. The gate needs `python` on PATH and uses only the standard
library.

**A run** takes every ready card of this folder's project in `plan`, `develop` and
`test` through to `verify`, up to 3 at once and one in test at a time. It plans first:
open questions stay on the card for you, with clickable options. With none, the same
agent goes on to build in a worktree on `card/<id>`, ticking the checklist as it goes.
A separate, fresh agent tests with `/code-review` plus the project's tests, then merges (or opens a PR) and deploys. Without the MCP tools it uses
the board's HTTP API.

Nothing works the board on its own: `/tickets-start` starts only when you type it.
Claude may run `/tickets-open-board` and `/tickets-stop` when you ask in plain words.

## Your first card

Create a card in `todo`, then drag it to `plan`. With `/tickets-start` running in the
project's folder, the next run takes it: it writes its plan, comments and moves onto the
board as `claude-agent`, and leaves the card in `verify` for you. Open questions stop the
card in `plan`: answer them on the card and the next run replans with them. When the card
in `verify` looks right, click "move to done".

## Layout

    core.py        schema + every operation (the only file that touches SQL)
    app.py         MCP tools + HTTP routes, both thin wrappers over core
    board.html     the board, vanilla JS
    docs.html      the user manual, served at /docs
    conftest.py    fixtures: temp DB, TestClient, uvicorn thread, Chrome page
    test_*.py      the suite: core, app, board, end-to-end, idle gate (test_idle_gate.py)
    skill*/        the Claude Code skills, one folder each (see Skills);
                   skill-start/idle_gate.py is the idle gate hook
    CLAUDE.md      this repo's own rules for Claude Code sessions
    PLAN.md        the design document

## Tests

    uv run pytest -q

This includes board tests that drive a real Chrome. Never run the suite under
pytest-xdist: it shares one process on purpose and redirects `core.DB_PATH` (see the
docstring at the top of `conftest.py`).

## More

- `http://127.0.0.1:8123/docs` — the full manual: the board, the agent, the workflow,
  using it from other MCP clients, and its limits. Needs the server running.
- `PLAN.md` — the design: schema, operations, decisions and change log.
