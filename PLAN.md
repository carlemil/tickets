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
Tests add two more, dev-only: `pytest` and `playwright`.

```
core.py        schema + every operation (the only file that touches SQL)
app.py         MCPServer: MCP tools + HTTP routes, both thin wrappers over core
board.html     the board, vanilla JS
docs.html      the user docs: what Tickets is, the board, the agent, the workflow
conftest.py    fixtures: temp DB, TestClient, uvicorn thread, Chrome page
test_core.py   core operations, rejections and no-ops
test_app.py    status codes, the actor rule, both error mappings, ToolError, the MCP wire
test_board.py  the board in a real browser, plus one lock per bug that shipped
test_e2e.py    one card, browser and MCP, one attributed history
skill/SKILL.md the /tickets Claude Code skill: works a project's cards, over MCP
log-config.json uvicorn log format: a timestamp per row
```

Run: `uv run uvicorn app:app --host 127.0.0.1 --port 8123 --log-config log-config.json`
Agent hookup, once, at user scope so every project's session sees the board:
`claude mcp add --scope user --transport http tickets http://127.0.0.1:8123/mcp`

8123 is the port the MCP registration, `restart-backend.ps1` and the docs use; any free
port works, as long as all three match.

**The restart waits for the port.** `restart-backend.ps1` kills what holds 8123 and starts
a new backend, and a deploy schedules one 30 s out (`-Delay 30`) so its own report gets
through first. A delayed restart therefore lands on whatever is serving by then, including
another restart: the start raced the dying process for the port, lost it to "address
already in use", and left nothing serving — the board in the browser just refused to
connect. So the script now waits for the port to go free before starting, waits for it to
answer after, and retries up to three times. Success is "8123 is served", not "my uvicorn
serves it", so two restarts that overlap both finish happy as soon as either backend is
up. A detached restart has nowhere to report, so each run appends what it did to
`restart-backend.log`.

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
      created_at, updated_at, pos, labels, checklist, archived,
      plan, questions, answers, merged, deployed)
      -- plus auto_advance, attention: the retired agent's, created but never read
events(id, card_id, actor, kind, detail, at)     -- append-only
links(from_id, to_id, kind)                      -- kind: 'parent' | 'blocks'
projects(name PRIMARY KEY COLLATE NOCASE, path, instructions, color)
activity(actor PRIMARY KEY, card_id, doing, since)   -- what agents are doing now
```

- `labels` and `checklist` are JSON text columns parsed in Python. No child tables.
- **`events` is both the activity log and the comment store.** Every mutation appends a
  row (`created` | `moved` | `assigned` | `edited` | `checked` | `comment` | `linked`)
  carrying the `actor`. That one table answers "who did what".
- **Projects are configured, and every card names one.** `cards.project` holds the
  project's name (not an id, so MCP callers pass names); `create_card`/`update_card`
  reject a name with no `projects` row and store the project's own spelling. A project
  has an optional `path` (an absolute, existing folder — where the agent works) and free
  `instructions` for agents. Renaming one rewrites `cards.project` for all its cards,
  archived ones too, with no card events: the cards did not change. **There is no
  default project:** a fresh board has none, and a card without a project is refused
  with "a card needs a project: create one first" (400 over HTTP; `project` is a
  required argument of the `create_card` tool). `connect()` backfills a row for every
  name already on a card, so a database from before projects — or from when `inbox`
  was the built-in default — keeps working, its `inbox` now an ordinary, renamable
  project; case variants collapse into one project and cards keep their stored
  spelling. Filters match ignoring case.
- **Adding a project opens a card for whatever its setup is missing.** `POST
  /api/projects` calls `setup_cards(name)`, which puts a `todo` card in the new project —
  authored by `board`, unassigned — for each of: no `path`, and
  instructions that do not mention testing or deploying (a word match). A person fixes
  them and archives them; agents cannot configure a project. The check sits on the route,
  not in `create_project`, so scripted setup and the test fixtures do not spawn cards.
- **Every project has a color** (`#rrggbb`, stored lowercase), and its cards are tagged
  with it. A new project takes the least-used color of `PALETTE`, earliest on a tie, unless
  one is given; `connect()` gives one the same way to every project without a color — a
  database from before colors, or rows just backfilled from cards.
- `updated_at` is denormalised onto cards so the board sorts without a join.
- WAL mode, one connection per request.

## Operations (`core.py`)

`list_cards(project, lane, assignee, label)` · `get_card(id)` · `create_card(...)` ·
`update_card(id, actor, **fields)` · `comment(id, actor, text)` ·
`link_cards(from_id, to_id, kind, actor)` / `unlink_cards(...)` ·
`list_users()` / `ensure_user(name)` · `list_projects()` / `get_project(name)` /
`create_project(name, path, instructions)` / `update_project(name, /, **fields)` /
`setup_cards(name)`

`update_card` is one function covering move-lane, assign, retitle, edit body and tick
checklist. It diffs old against new and writes one event per changed field — that is
what makes the audit trail free instead of something every caller must remember. Every
card write goes through it; no route or tool writes SQL.

Rejects: a lane not in `LANES`, a `pos` that is not a number, a link kind not in
`parent|blocks`, a self-link, an unknown field name, an unknown card id.

`pos` is the card's place in its column (`REAL`, listed `ORDER BY pos, id`). Anything
arriving in a lane — a new card, a card moved without an explicit `pos` — lands at the
bottom of it, so the agent takes the top card and a card it moves queues behind the ones
already waiting. A drop between two cards takes the midpoint of their `pos` values, so a
reorder writes one row. A `pos`-only write is not history: it logs no event.

**Idempotence rule.** "Already in that state" is a silent no-op that writes no event — a
no-op update, a repeat link, an unlink of an absent link. "Not a valid thing" is a
`ValueError`. The activity log therefore holds one event per actual state change, which
is what makes it trustworthy. HTTP maps `NotFound` → 404, `ValueError` → 400.

Actors self-register: `_event()` is the choke point every mutation passes through, so it
does an `INSERT OR IGNORE` into `users`. The board's dropdown self-populates and no
caller has to remember. `ensure_user(name)` is the explicit door for a name that has not
written anything yet — an agent announcing itself, or a person you want to assign work to
before they have touched the board. Same `INSERT OR IGNORE`, so it is idempotent; a blank
name is a `ValueError`.

## HTTP routes (`app.py`)

`GET /` → board.html · `GET /docs` → docs.html · `GET /api/cards` · `GET|PATCH /api/cards/{id}` ·
`POST /api/cards` · `POST /api/cards/{id}/comment` · `POST|DELETE /api/links` ·
`GET|POST /api/users` · `GET /api/activity` · `GET|POST /api/projects` · `PATCH|DELETE /api/projects/{name}`. Every
write body carries `actor` — except `POST /api/users` (`{"name"}` → 201), the one write
that predates having an actor, and the project writes, which are configuration rather
than card activity and log no event. `POST /api/projects` also opens a setup card per
missing piece of the new project's configuration.

## MCP tools (`app.py`)

`list_cards` · `get_card` · `create_card` · `update_card` · `comment` · `link_cards` ·
`unlink_cards` · `create_user` · `list_projects` (read-only: agents see each project's
path and instructions, but configuring projects is left to the board) · `set_activity`
(the status bar)

`update_card` takes only the fields you are changing, so `None` means "not passed".
That leaves no way to spell "clear it", which matters for exactly one field: pass
`assignee=""` to unassign.

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

## The skill (`skill/SKILL.md`)

**Nothing works a card until a person asks.** Work is started by hand: `/tickets` in a
Claude Code session opened in a project's folder. The board is the picture and the
record — what is queued, what is being worked, what was done, and ideas for what next —
not a dispatcher. There is no polling agent, no lock and no quota handling: the session
is the agent, and its own permission mode is the user's choice (auto mode suits an
unattended run).

It replaced `agent.py` (a process polling the board every 15 s and running headless
`claude -p` on any card assigned to it or set to auto advance), and with it the board's
handshake machinery: auto advance, the attention dot, a reply restarting a run, a
comment on a verify card sending it back to plan (#60), the 8124 one-agent lock,
self-restart on a changed `agent.py`, and the periodic `light_merged` git check. Their
`cards` columns `auto_advance` and `attention` are still created (and added to an older
database) so it opens, but `core.LEGACY_COLUMNS` are never read, written or returned.
The history of why each rule existed is in git, before this change.

One global skill, versioned here and linked into `~/.claude/skills/tickets` (a junction:
`New-Item -ItemType Junction "$HOME\.claude\skills\tickets" -Target
D:\source\Tickets\skill`). It uses only the existing MCP tools, as `claude-agent`:

- **Project:** the one whose `path` is the session's folder. Its `instructions` rank
  below the card and above the skill.
- **Queue:** the project's cards in `plan`, then `develop`, then `test`, board order
  within a lane. Skipped: questions without answers, an incoming `blocks` link from a
  card not in `done`, assigned to a person. `todo` is the backlog: moving a card to
  `plan` queues it. One run drains the queue unattended, working each card once.
- **Stages,** each by a subagent so the session's context stays small; the session
  writes the board and does the git: plan (read-only; open questions stop the card in
  `plan`), develop in the card's worktree (`<repo>.worktrees/card-<id>`, branch
  `card/<id>`, base merged in first), test (`RESULT: PASS` or the card stays), then
  ship (commit, push `card/<id>`), land (merge into the base when the repo is on it and
  clean), deploy (local unless the instructions say production; the comment head lights
  the purple dot), `merged=True` from git, remove the worktree, move to `verify`.
- **What only a person can do** (`## Blocked by`) becomes `todo` cards linked `blocks`.
  A failure is an `agent failed: …` comment, lane unchanged.
- **Wrap-up:** follow-up ideas as `todo` cards, and a summary in the chat.

**Docs.** `docs.html` is the user's manual, written by hand from this file and
`skill/SKILL.md`: a change to how the board or the agent behaves updates it too.
`test_docs_page_is_served_and_covers_the_essentials` checks the lanes and key terms.

## Board (`board.html`)

Six columns, native HTML5 drag & drop (`dragstart` / `dragover` + `preventDefault` /
`drop` → `PATCH /api/cards/{id}`). Click a card for a detail panel: description,
labels, checklist, links, activity log, comment box. The board writes as `User`, a fixed
name, and agents write under their own names. A project `<select>`
(also persisted) filters the board — an agent and a human both scope to one project.
Dragging inside a column reorders it: `dropBefore` finds the card the pointer is above
(shown with a `.drop-at` insertion line) and `slots` gives the dropped cards their `pos`
values between its neighbours.

Once a card has a plan, questions or answers, the sheet shows each in its own box under
the description: "plan" is a fixed 10 lines and scrolls, "open questions" and "your
answers" grow like the description. The description box grows with its text, from 10 lines
up to 50, then scrolls
(CSS `field-sizing: content`). Lanes run to the bottom of the window even when empty,
and all grow together with the tallest. 

**The work dot.** Every board card leads with a small ring that fills green and pulses
while an agent's status entry (`/api/activity`) points at the card.

**On master, deployed (#41).** After the auto dot come two more, on the board card and in
the sheet header: blue when `merged` (the card's branch is on the base branch, locally or
on origin), purple when `deployed` (on the local test backend or a device), rings when not.
Not clickable. The skill's deploy step merges the card's branch into the base branch
(#106) and sets `merged` from git.
`deployed` the board sets itself, from the head of the deploy comment: `core.comment`
lights it on `deployed: ` from `claude-agent` and darkens it on `deploy skipped: ` or
`deploy failed: ` (a skip — nothing to deploy, or no phone — is not a failure). The head
comes from the verdict line (`DEPLOY: OK`, `DEPLOY: SKIPPED`, anything else), so the dot
says what the comment says, whoever wrote it (#68). A move from
todo/verify/done back into plan, develop or test is rework and clears both, unless the
same write sets them. A card deployed or merged by hand stays dark unless someone sets the dot. MCP `update_card` takes both.

**Hover help.** Every control has a `title`. Panel fields get theirs from `FIELD_TIPS` in
`field()`, lane headings from `LANE_TIPS`; everything else from one `TIPS` list of
`[selector, text]` that a `MutationObserver` applies to whatever is rendered, never over
a title an element already has (the work dot and status jobs set
their own, more specific ones). `test_every_control_on_the_board_and_sheet_has_hover_help`
fails on any visible control left without one.

**Dropping a card.** One set of `dragover`/`drop` listeners on the document, not one per
lane: a card drops into the lane it is over, or, anywhere else on the page (below a lane's
end, between lanes, on the status bar), into the column nearest the pointer. Lanes are as
tall as the tallest one, so a full lane had no room left under its last card, and the
fixed status bar covered the bottom of the window: a drop there used to land on nothing.
Only a card's drag counts (`.card.dragging`), not text or files dragged in.

**The sheet is inset 5% on all sides; "close" or a click outside is the way out.**
Every field saves on its own `change`, so there never was a save button. The board shows
in the margin all round and stays live: click it and the sheet closes, click a card there
and it opens in the sheet's place. The "close" button top right does the same (the
projects sheet's is "done").
The top of a card's sheet is one sticky header row that stays put while the sheet
scrolls: the card number, the title (edited in place), "created <when>" (the full date on
hover), "archive", "move to done" (only while the card is in `verify`) and "close". A new
card's row is "new", the title, "cancel", "close".
The card number carries the auto dot, which pulses while an agent works the card.
Project, lane and creator sit on the line under it.
Closing blurs the focused field, so the edit still in progress saves too, and text left
in the comment box is posted rather than dropped. "move to done" is the one click that ends a
verified card: it writes the lane like the lane select does, and the sheet stays open. A comment being typed
survives a re-render: the box is refilled with whatever was in it. The error bar is fixed above the sheet
so its messages stay visible. The sheet keeps "archive".

"New Card" opens the same panel on an unsaved draft (title focused, project from the
filter). It shows everything a saved card does — links, activity, comment box. Nothing
is written until the draft is closed with "close" (or Enter in the title), which
`POST`s the fields all at once (one `created` event), then the links and comments queued
on the draft (links are checked to exist as they are added), plus any text left in the
comment box, and hides the sheet. A draft with nothing typed just closes. A draft with
content but no title stays open and says "a card needs a title". If the `POST` fails
the sheet stays with everything in it and the next "close" retries; a double click makes one
card. "cancel" discards the draft. The draft re-renders only for checklist edits and
queued links and comments.

**Deleting a project.** Each project in the settings has "delete project…", which opens a
native `<dialog>`: it counts the cards (archived too) that will move, says they go to
"No Project" where agents will not work on them, and that it cannot be undone; only
"Delete project" acts, Cancel or Esc do nothing. `DELETE /api/projects/{name}` →
`core.delete_project` moves the cards to `NO_PROJECT` (created on first use, grey; no
card events, like a rename) and removes the row. "No Project" itself has no delete
button and the server refuses to delete it. The skill never works a card in it. Renaming "No Project" to something else makes its cards workable again.

"projects…" in the header opens the panel on project settings: name, path, agent
instructions and a color picker per project, each saving on change, plus a "new project"
form (its picker means "pick one for me" until touched). Each board card carries a tag
with its project's name in its project's color, with dark text on a light color. The card
panel and the draft each have a project `<select>`, right under the title; a draft
starts in the filtered project, or the first one. With no projects at all, "New Card"
says "create a project first" and opens the project settings with the name field
focused. Creating a card in a project the filter hides switches the filter to it, so a
new card is always on screen. Renaming the filtered project carries the filter with it.

**Lost clicks.** A field saves on `change`, which fires on the mousedown that leaves it;
the save's response re-rendered the panel before mouseup, replacing the button under the
pointer, and the browser dropped the click — typing a title and clicking the header's
closing button left the panel open. `renderPanel()` therefore defers itself while a pointer is held and runs
after the release and its click. A `<select>` press does not count as held: its native
popup can swallow the pointerup and would stall every later render. `page.click()`
presses and releases at once, so it never saw this; `human_click()` in the tests holds
the button for 150 ms.

**Selecting several cards.** Ctrl- (or ⌘-) click toggles a card in the selection and
makes it the anchor; shift-click adds the run from the anchor to the card in the same
lane, or just the card when the anchor is in another lane. Neither opens the card; a
plain click clears the selection and opens it, Esc (with no sheet open) clears it.
Dragging a selected card moves the whole selection, one `move()` PATCH per card, so each
one keeps the single-drag rules and rollback; dragging an unselected card moves it alone
and keeps the selection. The selection survives re-renders and `load()`, but drops cards
no longer on the board.

## Status

| # | Task | State |
|---|------|-------|
| 0 | scaffold: git, gitignore, pyproject, PLAN.md | done |
| 1 | `core.py` + `test_core.py` | done — 17 checks |
| 2 | `app.py` — MCP tools + HTTP routes | done |
| 3 | `board.html` — drag & drop board | done |
| 4 | end-to-end verification | done |
| 5 | explicit user registration: `POST /api/users`, `create_user` tool, UI wiring | done |
| 6 | board fixes: panel above the header, save button, dismissal race, actor placeholder | done |
| 7 | pytest suite across all four surfaces | done — 77 checks |
| 8 | board agent: plan/develop cards assigned to `claude-agent` | done — 143 checks |
| 9 | archiving, new-card panel, configured projects (path + agent instructions), lost-click fix | done — 198 checks |
| 10 | auto advance: agent carries a card plan → verify, with a test stage that must pass | done — 228 checks |
| 11 | no default project: a card needs a configured one; project picked under the title | done — 236 checks |
| 12 | new-card sheet shows links, activity and comments; "create" saves and closes | done — 241 checks |
| 13 | no save/create buttons: click outside closes the sheet and saves, creating a new card | done — 252 checks |
| 14 | project colors: cards tagged with their project's name and color, after the priority/auto chips | done — 283 checks |
| 15 | agent status bar: `set_activity` tool, `GET /api/activity`, bar polls and reloads the board | done — 297 checks |
| 16 | delete a project (confirm dialog; cards move to "No Project", off limits to agents); full-width sheet with a close button | done — 307 checks |
| 17 | description grows to 50 lines; moving into plan turns auto advance on; lanes reach the window bottom | done |
| 18 | attention dot: the agent flags a card it hands back, any other change clears it | done — 353 checks |
| 19 | plan, questions and answers in fields of their own; a reply to a waiting card restarts the agent | done — 380 checks |
| 20 | a forward move (not into done) switches auto advance on; plan-lane cards migrated to the new fields | done |
| 21 | the agent assigns itself while it works and gives the card back; assigning registers a name; drop anywhere snaps to the column | done |
| 22 | auto advance dot on every card; hover help on every control | done — 413 checks |
| 23 | develop and test run in a git worktree per card (`<repo>.worktrees/card-<id>`, branch `card/<id>`) | done — 422 checks |
| 24 | worktree only (no in-place runs); one run per project, projects side by side; commit and push before verify; deploy once in verify; open questions numbered from 1 | done |
| 25 | Markera deploy instructions; description box 10 lines tall (grows to 50); sticky header row on the card sheet | done |
| 26 | docs page at `/docs`, linked from the header | done |
| 27 | one agent at a time (`agent.py` holds 127.0.0.1:8124); Tickets deploy instructions: merge the card into master, test, push, restart the backend 30 s later with `restart-backend.ps1 -Delay 30`; the deploy prompt allows what the instructions say | done |
| 28 | the auto dot pulses on cards an agent is working on, on the board and in the sheet header | done |
| 29 | ctrl/shift-click selects several cards; dragging one moves them all | done |
| 30 | #41 merged and deployed dots, set by the agent's deploy step, cleared on rework | done |
| 31 | #46 one dot: the auto dot turns red for attention; the separate red dot top right is gone | done |
| 32 | #50 the agent restarts itself when `agent.py` changes on disk, so a deploy that changes it takes effect | done |
| 33 | #58 `priority` gone: free ordering per column (`cards.pos`), drag up and down to reorder, anything arriving in a lane lands at its bottom | done |
| 34 | #60 a person's update or comment on a card in `verify` sends it back to `plan`, assigned to the agent, to be planned and built again | done |
| 35 | #63 parallel work per project: the run slot is keyed by `(project, lane)`, so a project's plan, develop and test lanes run side by side, one card each | done |
| 36 | #67 a deploy is local by default: the test backend or a connected phone, production only when a project's instructions say so | done |
| 37 | #68 the verify column's dark dots backfilled from the deploy comments and `git merge-base`; an agent older than #50 cannot restart itself, so it needs one restart by hand | done |
| 38 | the deployed dot follows the deploy comment, written by `core.comment`, not by the agent: a stale agent can no longer leave a deployed card dark | done |
| 39 | #76 one run per card as well as per project lane: a card moved to another lane mid-run no longer starts a second run in the same worktree | done |
| 40 | #89 every develop and test run first merges the base branch into the card's branch; a conflict is left in the worktree for the run to resolve, so the commit, push and deploy merge stay clean | done |
| 41 | #81 every log row is timestamped: `log-config.json` for uvicorn, a `log()` helper in `agent.py` | done |
| 42 | `restart-backend.ps1` waits for the port free and then served, retrying: a deploy's delayed restart landing on another restart no longer leaves the board unreachable | done |
| 43 | #96 the deploy runs at the end of the test stage, before the card moves to verify: a card arriving in verify already has the build on the phone | done |
| 44 | #101 the test stage fixes what it can, and the card's worktree is removed after the deploy: the branch in origin is the record, and a worktree whose folder went missing is re-made from it in either lane | done |
| 45 | the polling agent is gone: work starts by hand with the `/tickets` skill (`skill/SKILL.md`); auto advance, attention and #60 rework removed | done — 349 checks |

Gate for every task: `uv run pytest -q` — 349 checks across core, HTTP, the MCP tools
and wire, the board in Chrome, and the two-surface end-to-end. Every test gets its own
temp database, so `tickets.db` is never touched. The browser tests drive the real
`board.html` through system Chrome (`channel="chrome"`, no browser download) and skip
themselves if Playwright or Chrome is missing, so the gate still passes on a bare
checkout.

The suite shares one process on purpose: `core.DB_PATH` is re-read on every connect, so
the temp database reaches the in-thread uvicorn server the browser talks to. That is what
lets `test_e2e.py` drag a card in Chrome and then call an MCP tool on the same card — and
it is why this suite must not be run under `pytest-xdist`.

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
| `archived` | `{"field": "archived", "from", "to"}` — `to` false means unarchived |

Field → kind: `lane` → `moved`, `assignee` → `assigned`, `archived` → `archived`,
everything else → `edited`.
`from`/`to` carry real values, so for `labels` they are lists, not strings.

## Follow-ups

- No migration framework: `CREATE TABLE IF NOT EXISTS` will not add a column to an
  existing table. The one exception is `cards.archived`, added by a guarded `ALTER` in
  `connect()` because the live board already held real cards, and the other flags
  the same way: the guarded `ALTER` walks `BOOL_FIELDS + LEGACY_COLUMNS`.
- `NOCASE` folds ASCII only, so `Ärende` and `ärende` list as two projects. Upgrade is a
  normalised `project_key` column, if it ever matters.
- ~~The MCP `update_card` tool cannot unassign a card~~ — **resolved in task 7.** The
  tool maps `assignee=""` to `None` before calling core, so `None` keeps meaning "not
  passed" and an agent can still clear the field. Documented in the tool's docstring.
- ~~No way to remove a card~~ — **resolved by archiving.** `archived` is a card field set
  through `update_card` (panel button, `PATCH`, MCP tool); archived cards drop out of
  `list_cards` unless `archived=True` (`?archived=1`, the board's "show archived" box).
  Still no hard delete: cards are forever.
- `GET /api/cards` returns every match, unpaginated, by design.
- ~~Panel responses can land out of order~~ — **resolved.** A slow PATCH response used
  to re-render the panel over a newer link/comment render. The board now drops any card
  older than the one shown: by newest event id in the panel (links and comments do not
  bump `updated_at`, but every write appends an event), by `updated_at` on the board.
- ~~Native drag unverified~~ — **resolved in task 4.** A real `left_click_drag` in
  Chrome moved a card between lanes and the server recorded the `moved` event. The
  native gesture works.
- The sheet is 90% of the window, so a card's fields stretch across a wide screen.
