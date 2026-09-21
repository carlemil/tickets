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
(`claude` on PATH — `agent.py` shells out to it). Chrome too, but only for the browser
tests.

1. Install the dependencies:

       uv sync

2. Start the backend:

       uv run uvicorn app:app --host 127.0.0.1 --port 8123

   On Windows, `powershell -NoProfile -File restart-backend.ps1` starts or restarts it.

3. Open `http://127.0.0.1:8123/` and create a project under "projects…". There is no
   default project and a card needs one. A project carries a `path` (its folder on this
   machine, a git repo) and free-text `instructions` the agent follows — how to test the
   project, how to deploy it. Whatever you leave out shows up as a card in `todo`.

4. Give Claude Code the board's tools:

       claude mcp add --transport http tickets http://127.0.0.1:8123/mcp

5. Start the board agent, in a second terminal:

       uv run python agent.py

   Only ever one: it holds a lock on `127.0.0.1:8124`, and a second one exits.

8123 is not a runtime setting. Any free port works, but `URL` at the top of `agent.py`,
`restart-backend.ps1` and the `claude mcp add` URL must all match.

## Your first card

Create a card in `todo`, then drag it to `plan`. The agent writes its `plan` and, if
anything is unclear, `questions` — the card's dot turns red when it is your turn. Fill in
your answers and the agent replans with them. With no open questions and auto advance on,
the card carries itself through `develop` and `test` and stops in `verify` for you.

## Layout

    core.py        schema + every operation (the only file that touches SQL)
    app.py         MCP tools + HTTP routes, both thin wrappers over core
    board.html     the board, vanilla JS
    docs.html      the user manual, served at /docs
    agent.py       the board agent: plans, develops and tests cards, over MCP
    conftest.py    fixtures: temp DB, TestClient, uvicorn thread, Chrome page
    test_*.py      the suite: core, app, board, agent, end-to-end
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
