# Tickets — lane-based board with an MCP interface

A personal Jira replacement. A lane board a human drives in a browser and an AI agent
drives over MCP, sharing one SQLite file. Cards record who did what.

**No auth.** Users are identities (a name), not accounts — nothing is enforced. Bind to
`127.0.0.1` only. Do not expose this to a network without adding auth first.

## Lanes

`todo → plan → develop → test → review → done`

A single `LANES` constant in `core.py`. Renaming or reordering is a one-line change.

## Shape

One process, one SQLite file, no build step, two dependencies (`mcp`, `uvicorn`).

```
core.py        schema + every operation (the only file that touches SQL)
app.py         MCPServer: MCP tools + HTTP routes, both thin wrappers over core
board.html     the board, vanilla JS
test_core.py   assert-based self-check
```

Run: `uv run uvicorn app:app --host 127.0.0.1`
Agent hookup: `claude mcp add --transport http tickets http://127.0.0.1:8000/mcp`

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
cards(id, title, description, lane, assignee, created_by,
      created_at, updated_at, priority, labels, checklist)
events(id, card_id, actor, kind, detail, at)     -- append-only
links(from_id, to_id, kind)                      -- kind: 'parent' | 'blocks'
```

- `labels` and `checklist` are JSON text columns parsed in Python. No child tables.
- **`events` is both the activity log and the comment store.** Every mutation appends a
  row (`created` | `moved` | `assigned` | `edited` | `checked` | `comment` | `linked`)
  carrying the `actor`. That one table answers "who did what".
- `updated_at` is denormalised onto cards so the board sorts without a join.
- WAL mode, one connection per request.

## Operations (`core.py`)

`list_cards(lane, assignee, label)` · `get_card(id)` · `create_card(...)` ·
`update_card(id, actor, **fields)` · `comment(id, actor, text)` ·
`link_cards(from_id, to_id, kind, actor)` · `list_users()` / `ensure_user(name)`

`update_card` is one function covering move-lane, assign, retitle, edit body and tick
checklist. It diffs old against new and writes one event per changed field — that is
what makes the audit trail free instead of something every caller must remember. Every
card write goes through it; no route or tool writes SQL.

Rejects: a lane not in `LANES`, a priority not in `low|med|high`, a self-link, an
unknown card id.

## HTTP routes (`app.py`)

`GET /` → board.html · `GET /api/cards` · `GET|PATCH /api/cards/{id}` ·
`POST /api/cards` · `POST /api/cards/{id}/comment` · `POST|DELETE /api/links` ·
`GET /api/users`. Every write body carries `actor`.

## MCP tools (`app.py`)

`list_cards` · `get_card` · `create_card` · `update_card` · `comment` · `link_cards`

Docstrings state the lane order and that `actor` identifies the caller — they are the
agent's only instruction manual.

## Board (`board.html`)

Six columns, native HTML5 drag & drop (`dragstart` / `dragover` + `preventDefault` /
`drop` → `PATCH /api/cards/{id}`). Click a card for a detail panel: description,
priority, labels, checklist, links, activity log, comment box. A "you are:" `<select>`
persisted in `localStorage` supplies `actor` on every write.

## Status

| # | Task | State |
|---|------|-------|
| 0 | scaffold: git, gitignore, pyproject, PLAN.md | done |
| 1 | `core.py` + `test_core.py` | next |
| 2 | `app.py` — MCP tools + HTTP routes | queued |
| 3 | `board.html` — drag & drop board | queued |
| 4 | end-to-end verification | queued |

Gate for every task: `uv run python test_core.py`

## Deliberately skipped

Auth · live refresh over WebSocket (reload the page) · attachments · search.
Auth comes first, and before anything binds off loopback.

## Follow-ups

_(none yet)_
