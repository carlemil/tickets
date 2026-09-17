# Tickets — lane-based board with an MCP interface

A personal Jira replacement. A lane board a human drives in a browser and an AI agent
drives over MCP, sharing one SQLite file. Cards record who did what.

**No auth.** Users are identities (a name), not accounts — nothing is enforced. Bind to
`127.0.0.1` only. Do not expose this to a network without adding auth first.

## Lanes

`todo → plan → develop → test → verify → done`

A single `LANES` constant in `core.py`. Renaming or reordering is a one-line change.

## Shape

One process, one SQLite file, no build step, two dependencies (`mcp`, `uvicorn`).

```
core.py        schema + every operation (the only file that touches SQL)
app.py         MCPServer: MCP tools + HTTP routes, both thin wrappers over core
board.html     the board, vanilla JS
test_core.py   assert-based self-check
```

Run: `uv run uvicorn app:app --host 127.0.0.1 --port 8123`
Agent hookup: `claude mcp add --transport http tickets http://127.0.0.1:8123/mcp`

Port 8000 is already in use on this machine by something else, so the default would
fail to bind. 8123 is what the end-to-end run used; any free port works, as long as the
`claude mcp add` URL matches.

### Why no FastAPI

`mcp` 2.x's `MCPServer.streamable_http_app()` returns a Starlette app that already
serves `/mcp`, and `@mcp.custom_route(path, methods)` registers plain HTTP routes on
that same app. One app serves both surfaces, its lifespan starts the session manager by
itself, and there is nothing to mount. Verified against mcp 2.2.0 before writing code.

**mcp 2.x note:** `FastMCP` was renamed `MCPServer` (`from mcp.server.mcpserver import
MCPServer`). `mcp.server.fastmcp` raises `ModuleNotFoundError` on import. Any 1.x
example found online needs translating.

## Schema

```sql
users(id, name UNIQUE)
cards(id, project, title, description, lane, assignee, created_by,
      created_at, updated_at, priority, labels, checklist)
events(id, card_id, actor, kind, detail, at)     -- append-only
links(from_id, to_id, kind)                      -- kind: 'parent' | 'blocks'
```

- `labels` and `checklist` are JSON text columns parsed in Python. No child tables.
- **`events` is both the activity log and the comment store.** Every mutation appends a
  row (`created` | `moved` | `assigned` | `edited` | `checked` | `comment` | `linked`)
  carrying the `actor`. That one table answers "who did what".
- `project` is a free-text string on the card, not a table. `list_projects()` is a
  `SELECT DISTINCT` — that gives the board a dropdown without a second table to manage.
  Matching in filters is case-insensitive; the stored casing is preserved for display.
  Default is `DEFAULT_PROJECT = "inbox"` so a card always belongs somewhere.
- `updated_at` is denormalised onto cards so the board sorts without a join.
- WAL mode, one connection per request.

## Operations (`core.py`)

`list_cards(project, lane, assignee, label)` · `get_card(id)` · `create_card(...)` ·
`update_card(id, actor, **fields)` · `comment(id, actor, text)` ·
`link_cards(from_id, to_id, kind, actor)` / `unlink_cards(...)` ·
`list_users()` / `ensure_user(name)` · `list_projects()`

`update_card` is one function covering move-lane, assign, retitle, edit body and tick
checklist. It diffs old against new and writes one event per changed field — that is
what makes the audit trail free instead of something every caller must remember. Every
card write goes through it; no route or tool writes SQL.

Rejects: a lane not in `LANES`, a priority not in `low|med|high`, a link kind not in
`parent|blocks`, a self-link, an unknown field name, an unknown card id.

**Idempotence rule.** "Already in that state" is a silent no-op that writes no event — a
no-op update, a repeat link, an unlink of an absent link. "Not a valid thing" is a
`ValueError`. The activity log therefore holds one event per actual state change, which
is what makes it trustworthy. HTTP maps `NotFound` → 404, `ValueError` → 400.

Actors self-register: `_event()` is the choke point every mutation passes through, so it
does an `INSERT OR IGNORE` into `users`. The board's dropdown self-populates and no
caller has to remember.

## HTTP routes (`app.py`)

`GET /` → board.html · `GET /api/cards` · `GET|PATCH /api/cards/{id}` ·
`POST /api/cards` · `POST /api/cards/{id}/comment` · `POST|DELETE /api/links` ·
`GET /api/users` · `GET /api/projects`. Every write body carries `actor`.

## MCP tools (`app.py`)

`list_cards` · `get_card` · `create_card` · `update_card` · `comment` · `link_cards` ·
`unlink_cards`

Docstrings state the lane order and that `actor` identifies the caller — they are the
agent's only instruction manual, so they carry more weight than the code around them.
**An f-string is not a docstring**: written that way, `__doc__` is `None` and the tools
ship to agents with no description while everything still appears to work. The lane
order is therefore spelled out literally, and a module-level `assert` at the bottom of
`app.py` fails the import if it drifts from `core.LANES` — that rename has already
happened once here.

Two decorators carry the error contract, so no handler or tool has its own try/except:
`route()` maps `NotFound` → 404 and `ValueError`/`TypeError` → 400; `tool()` converts the
same core exceptions to `ToolError`, which is what puts the reason in front of the agent.
Without it the SDK masks a `ValueError` as a bare "Error executing tool X" — useless to
an agent expected to correct itself.

## Board (`board.html`)

Six columns, native HTML5 drag & drop (`dragstart` / `dragover` + `preventDefault` /
`drop` → `PATCH /api/cards/{id}`). Click a card for a detail panel: description,
priority, labels, checklist, links, activity log, comment box. A "you are:" `<select>`
persisted in `localStorage` supplies `actor` on every write, and a project `<select>`
(also persisted) filters the board — an agent and a human both scope to one project.

## Status

| # | Task | State |
|---|------|-------|
| 0 | scaffold: git, gitignore, pyproject, PLAN.md | done |
| 1 | `core.py` + `test_core.py` | done — 17 checks |
| 2 | `app.py` — MCP tools + HTTP routes | done |
| 3 | `board.html` — drag & drop board | done |
| 4 | end-to-end verification | done |

Gate for every task: `uv run python test_core.py`

## End-to-end result (task 4)

A human dragged a card with the mouse, then an agent moved it further over MCP,
assigned it and commented — one card, one attributed history:

```
ce           created   {'title': 'Verify the real mouse drag', 'lane': 'develop'}
ce           moved     develop -> test          <- real mouse drag
claude-agent moved     test -> verify           <- over MCP
claude-agent assigned  None -> ce
claude-agent comment   'Dragged by hand, verified by agent. Shared state works.'
```

Both actors self-registered. That shared, attributed state is the whole point of the
system, and it works across both surfaces.

## Deliberately skipped

Auth · live refresh over WebSocket (reload the page) · attachments · search.
Auth comes first, and before anything binds off loopback.

## Event detail shapes

`events.detail` is always a JSON object, parsed to a dict by `get_card`. The renderer
needs five branches:

| kind | detail |
|---|---|
| `created` | `{"title", "lane"}` |
| `moved` / `assigned` / `edited` | `{"field", "from", "to"}` |
| `checked` | `{"text", "done"}` |
| `comment` | `{"text"}` |
| `linked` / `unlinked` | `{"to", "kind"}` |

Field → kind: `lane` → `moved`, `assignee` → `assigned`, everything else → `edited`.
`from`/`to` carry real values, so for `labels` they are lists, not strings.

## Follow-ups

- No migrations by design: `CREATE TABLE IF NOT EXISTS` will not add a column to an
  existing table. A `tickets.db` predating a schema change must be deleted, not migrated.
- `NOCASE` folds ASCII only, so `Ärende` and `ärende` list as two projects. Upgrade is a
  normalised `project_key` column, if it ever matters.
- The MCP `update_card` tool cannot unassign a card: it filters `None` to mean "not
  passed", so there is no way to say "set to nobody". `PATCH {"assignee": null}` over
  HTTP can. Needs a sentinel if an agent should be able to unassign.
- No `DELETE /api/cards/{id}` and no delete in `core` — cards are forever. Decide in use
  whether a board with no way to remove a mistake is actually tolerable.
- `GET /api/cards` returns every match, unpaginated, by design.
- ~~Native drag unverified~~ — **resolved in task 4.** A real `left_click_drag` in
  Chrome moved a card between lanes and the server recorded the `moved` event. The
  native gesture works.
- Panel is a fixed 460px single column: fine on a laptop, cramped on a phone.
