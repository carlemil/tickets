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
agent.py       board agent: plans, develops and tests cards, over MCP
test_agent.py  the agent against the real tools in-process, claude faked
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
      created_at, updated_at, pos, labels, checklist, archived, auto_advance,
      attention, plan, questions, answers, merged, deployed)
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
`create_project(name, path, instructions)` / `update_project(name, /, **fields)`

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
reorder writes one row. A `pos`-only write is not history: it logs no event and does not
count as the reply that clears the attention dot.

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
than card activity and log no event.

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

## Agent (`agent.py`)

Run: `uv run python agent.py` (needs the server on 8123). Polls `list_cards` over MCP every
15s — there is no push channel. **Assignment is the go signal:** a card assigned to
`claude-agent` in `plan`, `develop` or `test` gets headless `claude -p` run on it in its
project's configured `path`, with the project's `instructions` placed before the card in
the prompt; a project with no path fails the card with "has no path configured". The
output becomes a comment, and the card moves on
(`plan → develop`, `develop → test`, `test → verify`) and is unassigned. Unassigning is both the human gate
(read the plan, reassign to have it built) and the loop guard (no re-trigger on its own
write, no state file). On any failure it comments `agent failed: …` and unassigns, lane
unchanged. Running out of quota is not a failure: it comments `out of quota: resuming
at HH:MM`, pauses all runs until the limit resets, and leaves the card as it was, so the
next poll after the pause takes it again. Development is a normal agent in auto mode (`--permission-mode auto`, never
plan mode): it edits and runs the tests on its own, with Claude Code's auto-mode checks
still on, and commits its work on the card's own branch. The test stage runs with
`--dangerously-skip-permissions`, reviews the card's changes against the card
and plan, runs the tests, and must end with a last line of `RESULT: PASS` (markdown
`*`/`` ` `` around it tolerated) to move on; anything else is a failure.

**One run per project lane at a time.** Each `(project, lane)` pair has at most one run
going, so two cards never run a lane's tests, ports or worktree at once; a card waits only
while another card in the same lane of its project is worked, and is started by the first
poll after that run ends (failed or not). A project's plan, develop and test lanes run
side by side, and so do different projects: each poll starts a run for every waiting card
whose project lane is free, and each run has its own MCP client, as it outlives the poll.
The status bar keeps one entry per actor, so each run shows as
`claude-agent (<project> <lane>)`. The limit is kept in the agent process (`running`),
so it needs one agent process: `agent.py` holds 127.0.0.1:8124 while it runs (exclusive
on Windows) and a second one exits at once with "already running", before it touches the
board. Two agents did run side by side once (two sessions each started one): both
worked card #32 at the same moment — double test runs, a worktree edited under a run.

**It restarts itself when `agent.py` changes.** Python does not reload code while it
runs, so a deploy that changed the agent used to need a person to restart it — the #41
merged and deployed dots stayed dark for a day because the agent running was older than
the code that sets them. After each poll the agent compares `agent.py`'s mtime with the
one it started from; if it differs, and no run is in flight and no quota pause is on
(both live in this process and would be lost), it closes the lock port — or the new
process would exit as "already running" — starts `python -u agent.py` and exits. A run in
progress simply defers the restart to a later poll.

**A git worktree per card.** Develop and test run in a worktree of the card's own, next
to the project's repo: `<repo>.worktrees/card-<id>` on branch `card/<id>`, made from the
repo's current branch on the card's first develop run and reused after that (rework, the
test stage). No two cards, and no person working in the repo, share a folder, so changes
never overwrite or mix. The prompt is told where it is: develop commits on its branch
(never pushes, merges or switches), test reviews `git diff <base>...HEAD` plus anything
uncommitted. The card's comment ends with the worktree's path and branch. Planning only
reads and runs in the repo. Develop and test never run anywhere but the card's worktree:
a project folder that is not a git repository fails the card (it can still be planned),
and so does a card in test with no worktree (built before worktrees, or the worktree was
removed): move it back to develop to build it in one. A worktree that cannot be made
fails the card.

**Commit and push before verify.** A test that ends in `RESULT: PASS` is not enough to
move on: the agent first stages everything left in the worktree (`git add -A`, untracked
files too), commits it if there is anything (message `#<id> <title>`), and pushes
`card/<id>` to `origin` (`-u`, no prompt: `GIT_TERMINAL_PROMPT=0`). A comment names the
commit pushed. If any git step fails — no `origin`, rejected, needs credentials — the card
fails in test, with the git error, and is not moved. A failed test commits nothing.

**Deploy once in verify.** Right after the agent moves a card to verify it runs one more
`claude -p` in the worktree (`--dangerously-skip-permissions`, it runs deploy tools and
`adb`): deploy the backend if the card changed it; install the mobile app on the phone if
the card changed it and a phone is connected, and skip the phone otherwise; deploy
nothing if neither changed. How to deploy comes from the project's instructions. The reply
must end in `DEPLOY: OK`; it becomes a comment `deployed: …`, anything else (or a crash)
`deploy failed: …`. Either way the card stays in verify, handed back with the dot: a
deploy never moves a card. Only the agent's own move to verify deploys; a card moved off
test during its run, or a failed test, is not deployed. The deploy prompt lets the
project's instructions merge, push or restart services (develop's "never merge" rule is
not the deploy's), and forbids anything beyond them. Unless a project's deploy merges,
merging `card/<id>` and removing
the worktree (`git worktree remove`) is a person's job, after verify; a fresh worktree has
no build output or installed dependencies, so the project's instructions should say how to
get them if the tests need them.

**The agent is assigned while it works.** Before a run it assigns the card to itself
(the board shows `claude-agent` on it, next to the status bar's entry), and every way out
— next stage, hand-back, open questions, failure — gives it back to whoever had it: a
person keeps their card, and a card that was unassigned or assigned to the agent ends up
unassigned. So an auto card is unassigned between stages and taken again by the next
poll. If a person reassigned the card during the run, it stays theirs. Assigning a card
to a name registers that name as a user (`create_card`/`update_card`), so any agent can
put itself on a card without `create_user` first; the `update_card` tool's docs ask every
agent to follow the same take-it, give-it-back convention.

**A person's move wins.** A run takes minutes, and the card can be moved meanwhile — card
#3 was: its auto advance was switched back on in `test`, the agent started a test run,
the card was then moved to `develop`, and the finished run failed it and switched auto
advance off, as if it were still in `test`. So after a run the agent re-reads the lane;
if it changed, the output is kept as a comment noting the move, and the card is not moved
on, unassigned or switched off.

**The plan stage** runs `claude -p --permission-mode plan` (what `/plan` switches on, so it is
read-only). A card keeps its planning round in three text fields of its own, so each reads
on its own: `description` is the person's request and the agent never writes it; `plan` is
the plan; `questions` the plan's open questions; `answers` the person's reply. Nobody can
answer questions mid-run, so the prompt has Claude put them in a last `## Open questions`
section, as a list numbered from 1, and end with a last line of `QUESTIONS: NONE` or `QUESTIONS: OPEN`.
`agent.split_plan` drops the verdict line and cuts that section off into `questions`,
renumbering its top-level bullets or numbers `1.`, `2.`, … in order (`agent.number`;
indented lines and other text kept), so the questions are numbered whatever Claude used.
`NONE` → only `plan` is written (the last round's questions and answers stay, as the
record of what was decided), the card moves to `develop` and, unless auto advance is on,
halts there unassigned. Anything else, including no verdict line, → `plan` and `questions`
are written, the card stays in `plan` unassigned, auto advance is switched off so it is not
replanned every poll, and a comment asks for the answers. Filling them in is a reply (see
attention), which switches auto advance back on: the agent replans with the answers in the
prompt, told not to ask them again. An empty plan is a failure, so a plan is never blanked.
The develop and test prompts name the fields too, and the MCP tools' docs say what goes in
each, so any agent writes the plan to `plan`, not over the request.

**Auto advance.** `cards.auto_advance` is a switch on the card. With it on, the agent
works the card in `plan`, `develop` and `test` whoever it is assigned to (or nobody) and
keeps it moving, one stage per poll, until it lands in `verify` — the person's stage —
where it is unassigned. `todo` is the backlog, so moving the card to `plan` is the go
signal. Any failure — a stage erroring, a project with no path, a test stage without a
pass — comments why, unassigns and **turns the switch off**, leaving the card in its
lane: that is the loop guard for auto cards, which have no unassign-to-stop of their own.

**Status bar.** Around each card it works, the agent calls the `set_activity` tool
(`planning` / `developing` / `testing`) and clears it in a `finally`; it also clears its
own entry on startup, in case a crashed run left one. `activity` is one row per actor in
SQLite, so it survives a backend restart, and it is live state, not history: it writes no
event and does not bump `updated_at`. The board polls `GET /api/activity` every 5s and
shows each agent, what it is doing, the card (click to open) and how long ago it started;
when the set of busy cards changes it reloads the board (skipped mid-drag), so a card the
agent just moved shows up in its new lane. A killed agent's entry lingers until it
restarts — the "started" age is what makes that visible.

**Attention: "waiting for you".** `cards.attention` turns the card's auto dot red on the
board. Only an explicit `attention=True` sets it, and the agent passes it whenever it
hands a card back to a person: open questions, a stage done on an assigned card, an auto
card reaching `verify`, any failure. An auto card still moving on does not ask. Any
other change to the card — an edit, a move, a comment, by anyone — clears it; opening
the card does not. It is a notification, not history, so setting and clearing it write
no event. **The reply restarts the agent:** a change or comment that clears the dot on a
card in `plan`, `develop` or `test` also switches auto advance on (logged as an ordinary
`edited` event, under the person), so the agent takes the card up again with the new
input — answers, a comment on a failure, a fixed description. A write that sets
`auto_advance` itself wins, and a reply that moves the card to `todo`, `verify` or `done`
leaves it off. Edits to a card with no dot never start the agent. Each field saves on
`change`, so an answers box is one reply however many questions it answers.

**A new request on a verified card (#60).** A card in `verify` is finished work waiting
for a person, and the agent never polls that lane, so an update there would otherwise sit
unread. Instead, a person's write to `description`, `answers` or `checklist` — or a
comment — on a `verify` card sends it back to `plan` in `core.update_card`, assigned to
`claude-agent`, which turns auto advance on and clears `merged`/`deployed` as rework: the
next poll replans it and it runs the whole loop again against the new request. The agent's
own writes are exempt, or its deploy comment would bounce every card it just deployed.
Passing `lane` in the same write keeps the card where it is.

**Docs.** `docs.html` is the user's manual, written by hand from this file and
`agent.py`: a change to how the board or the agent behaves updates it too.
`test_docs_page_is_served_and_covers_the_essentials` checks the lanes and key terms.

## Board (`board.html`)

Six columns, native HTML5 drag & drop (`dragstart` / `dragover` + `preventDefault` /
`drop` → `PATCH /api/cards/{id}`). Click a card for a detail panel: description,
labels, checklist, an "auto advance" checkbox (an `auto` pill on the
board card), links, activity log, comment box. The board writes as `User`, a fixed
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
and all grow together with the tallest. Moving a card forward — drag, sheet or MCP — or
back into `plan` to replan it turns its auto advance on in `core.update_card`, unless the
same write says `auto_advance=False`. Into `done` it does not: nothing more is to be done.
Other moves back leave it alone, and so does a write to a card already in its lane, so
one the agent stopped stays stopped. The agent's own forward moves pass the card's
current `auto_advance`, so a card it hands back still halts for a person.

**Auto advance at a glance.** Every board card leads with a small dot: lit green when
auto advance is on, a faint ring when it is off (it replaced the "auto" pill, which only
showed the on state). Attention shares the dot (#46): red if `attention`, else green if
`auto_advance`, else the ring; the pulse means an agent is working it. No state is lost:
in the agent lanes a hand-back with attention always has auto off (failures and open
questions switch it off, a stage is handed back only when it is already off). Only in
`verify` can both be on, where the agent never picks the card up, so red wins and the
tooltip still gives the auto state. Clicking a red dot toggles auto advance, and that
write clears attention: on a failed card it means "go again".

**On master, deployed (#41).** After the auto dot come two more, on the board card and in
the sheet header: blue when `merged` (the card's branch is on the base branch on origin),
purple when `deployed` (on the local test backend or a device), rings when not. Not
clickable. Only the agent's deploy step sets them, in the same write as `attention=True`:
`merged` from `git merge-base --is-ancestor card/<id> origin/<base>` after the run,
whatever its verdict; `deployed` only from `DEPLOY: OK` (`DEPLOY: SKIPPED`, nothing to
deploy or no phone, comments `deploy skipped:` and is not a failure). A move from
todo/verify/done back into plan, develop or test is rework and clears both, unless the
same write sets them. A card merged or deployed by hand stays dark; MCP `update_card`
takes both.

**Hover help.** Every control has a `title`. Panel fields get theirs from `FIELD_TIPS` in
`field()`, lane headings from `LANE_TIPS`; everything else from one `TIPS` list of
`[selector, text]` that a `MutationObserver` applies to whatever is rendered, never over
a title an element already has (the auto dot, the attention dot and status jobs set
their own, more specific ones). `test_every_control_on_the_board_and_sheet_has_hover_help`
fails on any visible control left without one.

**Dropping a card.** One set of `dragover`/`drop` listeners on the document, not one per
lane: a card drops into the lane it is over, or, anywhere else on the page (below a lane's
end, between lanes, on the status bar), into the column nearest the pointer. Lanes are as
tall as the tallest one, so a full lane had no room left under its last card, and the
fixed status bar covered the bottom of the window: a drop there used to land on nothing.
Only a card's drag counts (`.card.dragging`), not text or files dragged in.

**The sheet covers the whole window; "close" is the way out.** Every field saves on
its own `change`, so there never was a save button. The sheet is full width, so there is
no outside to click: the "close" button top right is the way out (the projects sheet's
is "done").
The top of a card's sheet is one sticky header row that stays put while the sheet
scrolls: the card number, the title (edited in place), "created <when>" (the full date on
hover), "archive", "cancel" and "close". A new card's row is "new", the title, "cancel", "close".
The card number carries the auto dot, which pulses while an agent works the card.
Project, lane and creator sit on the line under it.
Closing blurs the focused field, so the edit still in progress saves too, and text left
in the comment box is posted rather than dropped. "cancel" closes a card without saving the field
being edited or posting the comment box; edits already saved stay. A comment being typed
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
button and the server refuses to delete it. The agent skips any card in it, silently,
auto advance or not. Renaming "No Project" to something else makes its cards workable again.

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

Gate for every task: `uv run pytest -q` — 77 checks across core, HTTP, the MCP tools
and wire, the board in Chrome, and the two-surface end-to-end. Every test gets its own
temp database, so `tickets.db` is never touched. The browser tests drive the real
`board.html` through system Chrome (`channel="chrome"`, no browser download) and skip
themselves if Playwright or Chrome is missing, so the gate still passes on a bare
checkout — `66 passed, 11 skipped`.

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
  `connect()` because the live board already held real cards, and `cards.auto_advance`
  the same way: `BOOL_FIELDS` is the list the guarded `ALTER` walks.
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
- The sheet is full width, so a card's fields stretch across a wide screen.
