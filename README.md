# Tickets

A personal lane board. You drive it in a browser, Claude drives it over MCP, and both
share one SQLite file (`tickets.db`). Every card records who did what.

It sits *next to* JIRA and similar trackers, not in place of them. Team work and anything
shared stays there. Tickets is for the local loop: you hand coding tasks to an agent on
your machine and review what comes back.

**Local and single-person only.** One process, bound to `127.0.0.1`, with no auth and no
accounts — users are just names, nothing is enforced. Never put it on a network.

## Lanes

`todo → plan → develop → test → verify → done`

- `todo` — you write the card: a title and what you want, and why.
- `plan` — the agent writes a plan, and any open questions for you.
- `develop` — the agent implements it on a `card/<id>` branch in its own worktree.
- `test` — the agent runs the project's tests and fixes what fails.
- `verify` — yours: read the comments, look at the branch, merge it.
- `done` — finished.

## Setup

You need Python 3.12+, [uv](https://docs.astral.sh/uv/), `git`, and the Claude Code CLI
(`claude` on PATH). Chrome too, but only for the browser
tests.

1. Install the dependencies:

       uv sync

2. Start the backend:

       uv run uvicorn app:app --host 127.0.0.1 --port 8123

   On Windows, `powershell -NoProfile -File restart-backend.ps1` starts or restarts it.

3. Link the Claude Code skills into your user skills folder, once (Windows shown; on
   macOS/Linux `ln -s <this repo>/<folder> ~/.claude/skills/<name>`):

       foreach ($s in @{tickets='skill'; 'tickets-start'='skill-start';
                        'tickets-open-board'='skill-open-board';
                        'tickets-stop'='skill-stop'}.GetEnumerator()) {
           New-Item -ItemType Junction "$HOME\.claude\skills\$($s.Key)" -Target "$PWD\$($s.Value)"
       }

   They are links, so editing a skill here takes effect in every session at once.

4. In a Claude Code session opened in the repo you want worked, run `/tickets-start`. It
   does the rest (MCP server, `CLAUDE.md` rules, the project on the board) and starts
   working the board.

8123 is not a runtime setting. Any free port works, but `restart-backend.ps1`, the
`claude mcp add` URL and the skills must match.

## Skills

Claude Code works the board through four skills, all run from a session opened in the
project's folder.

| Skill | What it does |
|---|---|
| `/tickets-start [interval]` | Gets a repo ready and starts working its board. Setup comes first and only fixes what is missing, so a second run is quick. It starts the board if it is down and offers to start it at every logon. It registers the `tickets` MCP server, checks the repo has `git` and an `origin`, helps write the repo's `CLAUDE.md` test and deploy rules, and creates the project on the board. Then it runs `/tickets` every 15 minutes (or `10m`, `1h`, at least `5m`) for as long as this session stays open. It ends with a link to the board. |
| `/tickets-stop` | Stops that loop. Cards already in progress finish. |
| `/tickets [id]` | Works the board, once. It takes every ready card of this folder's project in `plan`, `develop` and `test` through to `verify`. It plans first: open questions stay on the card for you, with clickable options. It builds in a worktree on `card/<id>` and ticks the checklist as it goes. It tests with `/code-review` plus the project's tests, then ships, merges (or opens a PR) and deploys. It works up to 3 cards at once, with one in test at a time. With an id it works only that card. If the board is down it starts it, and without MCP tools it uses the HTTP API. |
| `/tickets-open-board` | Opens the board in your browser, starting it first if it is down, and prints the link. |

Nothing works the board on its own: `/tickets` and `/tickets-start` start only when you
type them. Claude may run `/tickets-open-board` and `/tickets-stop` when you ask in plain
words.

## Your first card

Create a card in `todo`, then drag it to `plan`. Run `/tickets` in the project's folder:
it writes its plan, comments and moves onto the board as `claude-agent`, and leaves the
card in `verify` for you. Open questions stop the card in `plan`: answer them on the card
and run `/tickets` again. When the card in `verify` looks right, click "move to done".

## Layout

    core.py        schema + every operation (the only file that touches SQL)
    app.py         MCP tools + HTTP routes, both thin wrappers over core
    board.html     the board, vanilla JS
    docs.html      the user manual, served at /docs
    conftest.py    fixtures: temp DB, TestClient, uvicorn thread, Chrome page
    test_*.py      the suite: core, app, board, end-to-end
    skill*/        the Claude Code skills, one folder each (see Skills)
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
