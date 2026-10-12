# Tickets — lane-based board with an MCP interface

A personal lane board that sits next to JIRA, not in place of it. A human drives it in a
browser and an AI agent drives it over MCP, sharing one SQLite file. Cards record who did
what.

**No auth.** Users are identities (a name), not accounts — nothing is enforced. It binds
to `127.0.0.1` by default. The board's "host the board on the LAN" setting (#98) binds
`0.0.0.0` instead, still without auth — the owner's call: anyone on that network can then
read and change the board. A password or token may come later.

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
docs_card.py   makes docs-card.png: the real card sheet, every field filled and numbered
docs-card.png  that screenshot, shown near the top of docs.html (served at /docs/card.png)
conftest.py    fixtures: temp DB, TestClient, uvicorn thread, Chrome page
test_core.py   core operations, rejections and no-ops
test_app.py    status codes, the actor rule, both error mappings, ToolError, the MCP wire
test_board.py  the board in a real browser, plus one lock per bug that shipped
test_e2e.py    one card, browser and MCP, one attributed history
test_idle_gate.py  the idle gate: which boards let a loop tick through
skill-start/SKILL.md  /tickets-start: sets a repo up where needed, then works its cards, on a /loop or once
skill-start/run.md    one run over a project's cards, over MCP (read by /tickets-start, not a skill)
skill-start/idle_gate.py  UserPromptSubmit hook: answers an idle loop tick before the model wakes
skill-start/agents/tickets-stage.md  the stage subagent: shell, file, Skill and board tools only (copied to ~/.claude/agents/ by /tickets-start)
skill-open-board/SKILL.md  /tickets-open-board: opens the board in the browser
skill-stop/SKILL.md  /tickets-stop: cancels that loop
CLAUDE.md      this repo's own rules (test gate, docs, deploy), read by every session
log-config.json uvicorn log format: a timestamp per row
STATUS.md      the change log: one row per change, appended keyed by card id (#177)
.gitattributes `STATUS.md merge=union`: parallel cards' rows merge without a conflict (#177)
```

Run: `uv run uvicorn app:app --host 127.0.0.1 --port 8123 --log-config log-config.json`
Agent hookup, once, at user scope so every project's session sees the board:
`claude mcp add --scope user --transport http tickets http://127.0.0.1:8123/mcp`

8123 is the port the MCP registration, `restart-backend.ps1`, the skills,
`skill-start/idle_gate.py` and the docs use; any free port works, as long as they all match.

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

**Hosting on the LAN (#98).** One board setting, `lan` (off by default), in
`settings.json` beside the database (`core.settings_path()`: it follows `DB_PATH`, so each
test gets its own; gitignored). A file rather than a table so `restart-backend.ps1` can read
it with `ConvertFrom-Json` before any Python runs: `"lan": true` (a real JSON bool, as
`core.get_settings` reads it) → `--host 0.0.0.0`, anything else, a missing or unreadable
file included → `127.0.0.1`. `0.0.0.0` still answers on `127.0.0.1:8123`, so the MCP
registration, the skills and the idle gate need no change. `/mcp` stays this-machine-only
anyway: the MCP SDK's DNS-rebinding check accepts only a `127.0.0.1`/`localhost` Host, so a
LAN device reaches the board and its HTTP API, not the tools. uvicorn binds once, so a
change needs a restart: `PATCH /api/settings` runs `restart-backend.ps1 -Delay 2` itself,
hidden and not waited for (its -Delay branch Start-Process'es the real restart, which
outlives the backend it kills), when the PATCH changed the setting, it no
longer matches the running bind and the backend can restart itself (`app.can_restart()`):
it knows its bind (`--host` on uvicorn's command line, `app.bound_host()`), it runs on the
script's port 8123 (`app.bound_port()`; a worktree's trial server on 8124 would otherwise
have the script kill and replace the live board), it is Windows, the script is there, and
its `settings.json` is the one beside the script (a `DB_PATH` elsewhere would restart onto
a file it never wrote). A PATCH that changes nothing never restarts, so two tabs or a retry
cannot stack restarts; to retry a failed one, untick and tick again. Otherwise the sheet
says how to apply the change by hand. If the LAN bind does not come up in two tries, the
script's third try falls back to `127.0.0.1` (logged), so a failed LAN restart still leaves
a board on this computer, with the setting shown as pending. The suite's conftest replaces
`app.restart_backend` for every test: the real one would kill the live board on 8123;
`test_restart_script_binds_the_lan_only_for_a_real_true` runs only the script's bind
choice, cut out into a temp folder.

**No `DETACHED_PROCESS` (#138).** The spawn used to pass it, and a Windows PowerShell
started with no console at all exits 0 at once without running its `-File` script: the
PATCH answered `restarting`, nothing ran, `restart-backend.log` got no line, the setting
stayed pending. It never depended on how the backend was started; the suite could not see
it because the guard replaces `restart_backend`. `app.spawn_restart(script, delay)` is now
the real Popen with `CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP` (a console of its own
nobody sees), `restart_backend()` its one-line caller the guard replaces, and
`test_the_real_spawn_runs_the_scripts_delay_branch_to_the_end` runs the real spawn against
a stand-in made of the script's own param line and -Delay branch plus a harmless tail: it
failed with the old flags. Proven by hand too, on a throwaway copy on port 8197. A restart
that still does not happen shows: the backend remembers when it asked
(`app.ask_restart`), and `restart_failed` is true while the setting is pending and this
same process still answers `RESTART_GRACE` (20 s) after asking, since a restart that worked
has killed it by then; a Popen that cannot start (no powershell) answers
`restarting: false, restart_failed: true` rather than a 500.

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
      plan, questions, answers, merged, deployed, session, pr)
      -- plus auto_advance, attention: the retired agent's, created but never read
events(id, card_id, actor, kind, detail, at)     -- append-only
links(from_id, to_id, kind)                      -- kind: 'parent' | 'blocks'
projects(name PRIMARY KEY COLLATE NOCASE, path, instructions, color, land)
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
  rules that do not mention testing or deploying (a word match over the instructions plus
  the repo's `CLAUDE.md`, #5: every Claude Code session reads that file anyway, so it is
  the preferred home of a project's rules and the instructions keep what is specific to
  the board). A person fixes
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

Every card `list_cards` and `get_card` return carries two computed lists (#35), sorted
ids, empty when none: `blocked_by`, the cards with a `blocks` link to it, and `blocking`,
the cards it has a `blocks` link to. Only live links count: the blocker not finished and
the blocked card not `done`. Finished (#62, #87) is `done`, or `verify` with `merged`
set, or `verify` with `pr` empty when the blocker's project lands by `merge`: a merge
project's verify is already merged and deployed, a PR project's unmerged code is not on
the base yet. In a `pr` project an empty `pr` is not enough (#87): rework clears it
(#72), so a card dragged to develop and straight back to verify by hand, without a ship,
would otherwise unblock early. A person's "Blocked by a person" todo card dragged to
verify (no PR, `merged` false) still unblocks in a merge project; in a `pr` project
nothing tells it from that rework card, so it unblocks at `done`. The blocker's own
project decides, not the blocked card's; a card whose project row is missing counts as
`merge` (the column default); `_blocks` joins `projects` for it, so changing a project's
`land` flips its verify blockers at once. It is computed, links are never
deleted, so a blocker moved back to work blocks again. An archived card that is not
finished still counts. The run and the idle gate read `blocked_by` too, so all three agree. The
other end may be in any project, so the server works them out (`_blocks`, one query)
rather than the board, which loads one project. The list stays without `links` and
`events`. #37 draws its arrows from these.

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

`GET /` → board.html · `GET /docs` → docs.html · `GET /docs/card.png` → docs-card.png · `GET /api/cards` · `GET|PATCH /api/cards/{id}` ·
`POST /api/cards` · `POST /api/cards/{id}/comment` · `POST|DELETE /api/links` ·
`GET|POST /api/users` · `GET|POST /api/activity` · `GET /api/version` · `GET|POST /api/projects` · `PATCH|DELETE /api/projects/{name}` ·
`GET|PATCH /api/settings`. Every
write body carries `actor` — except `POST /api/users` (`{"name"}` → 201), the one write
that predates having an actor, and the project and settings writes, which are
configuration rather than card activity and log no event (`PATCH /api/settings` ignores
an `actor` the board sends). `/api/settings` answers the settings plus the running
backend's `bound` host (`null` if unknown), `port`, `listening_lan`, `pending` (setting
and bind disagree), `can_restart`, `restart_failed` (#138: a restart this backend asked
for 20 s ago or more has not happened), and `urls`: `http://<ipv4>:<port>/` per LAN address,
the default route's first (virtual WSL/Hyper-V adapters add ones no other device reaches);
the PATCH adds `restarting`. `settings_view` resolves the host name, so both routes run it
in a thread, off the event loop. `POST /api/projects` also opens a setup card per
missing piece of the new project's configuration.

## MCP tools (`app.py`)

`list_cards` · `get_card` · `create_card` · `update_card` · `comment` · `link_cards` ·
`unlink_cards` · `create_user` · `list_projects` (read-only: agents see each project's
path and instructions, but configuring projects is left to the board) · `set_activity`
(the status bar)

`update_card` takes only the fields you are changing, so `None` means "not passed".
That leaves no way to spell "clear it", which matters for exactly one field: pass
`assignee=""` to unassign.

**Slim replies (#203).** `update_card` and `comment` answer with the whole card, `events`
log and `links` included, so a plan post or a checklist tick echoed the plan several times
over (the field, the checklist, the events that set them) into every stage's context; the
dispatcher's own board calls were already trimmed over HTTP (#186). Both tools take an
opt-in `slim: bool = False`: `slim=True` answers only `{"id", "lane", "assignee"}` of the
card as written (the three fields the dispatcher's trimmed writes print), one helper
(`_reply`) for both. The write itself, the `deployed` path of a claude-agent deploy comment
and the `ToolError` mapping are the same either way; the default stays the whole card, so
other MCP clients, the HTTP routes (board.html) and existing callers are unchanged.
`get_card` and the other write tools (`create_card`, `link_cards`, `unlink_cards`: only the
dispatcher uses them) take no `slim`; a stage still reads one whole card before a post or a
tick. An older backend drops the unknown argument (the SDK's argument model ignores extra
keys, the schemas set no `additionalProperties: false`), so `slim=True` sent before the
restart just gets the whole card back, and a session sees the argument in the schema after
`/mcp` reconnects or in a new session.

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

## The run (`skill-start/run.md`)

**Nothing works a card until a person asks.** Work is started by hand: `/tickets-start` in a
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

One run is the file `skill-start/run.md`, which `/tickets-start` reads and follows,
once or per loop tick (#60: it was the `/tickets` skill until then). Read raw, a file gets
no `${...}` substitution, so the caller hands it the session id, the Tickets repo and an
optional card id in words. It uses only the existing MCP tools, as `claude-agent`:

- **Project:** the one whose `path` is the session's folder. Its `instructions` rank
  below the card and above the repo's `CLAUDE.md`, which ranks above the skill.
- **Queue:** the project's cards in `plan`, then `develop`, then `test`, board order
  within a lane. Skipped: questions without answers, a non-empty `blocked_by` (a
  blocker not yet finished, #62 and #87), assigned to a person. `todo` is the backlog: moving a card to
  `plan` queues it. One run drains the queue unattended, working each card once.
- **Several at once (#1).** The session dispatches: each card's stage is a background
  subagent, up to 2 cards in flight by default (a project sets `Cards in flight: N` in its instructions, #193), plan and develop side by side in their
  own worktrees (#84, #155: a card keeps its slot and worktree from plan into develop), one card at a time from test through land/deploy (shared ports, devices, the base checkout). `set_activity` is
  one row per actor, so it points at the card started last with the count in its text.
- **Stages,** each by a new subagent so the session's context stays small and each
  stage starts clean (#61: never a continued one; the brief pastes the whole card
  verbatim, comments without their CLI output, plus project, repo and worktree); the session
  writes the board and does the git: plan (#84, split by #155: a subagent for a card in
  `plan`, in the card's worktree `<repo>.worktrees/card-<id>`, branch `card/<id>`, base
  merged in first; it plans read-only (a small card builds after the post, below), posts plan and checklist itself after a
  `get_card` — a person's move wins — and with no open questions moves the card to
  `develop`, ending `STAGE: PLANNED` and building nothing for a large card (#190: a small card, label `small` or judged at most 5 steps and ~5 files with no new module, schema, route or tool, is built by the same subagent in the same context, ending `STAGE: BUILT` handled like a develop BUILT; label `large` or both labels or unsure forces the split; the plan's first line is `Size: …`; `planning and building` activity); open questions stop it in `plan`
  and the session removes the unused worktree; verdicts PLANNED, BUILT (small), QUESTIONS, BLOCKED,
  MOVED, FAILED), develop (a fresh subagent that builds from the posted plan: started at
  once on PLANNED, or for a card a person put in `develop`; its reply ends with a `Files changed:` list, and before the
  card moves to test the dispatcher checks it against `diff --name-only --no-renames <base>...card/<id>` and `status --porcelain`, #176:
  an empty build fails, any other gap is `build check: MISMATCH` on the BUILT comment and in the test brief, which then
  verifies each claim of the develop reply; #192 it runs only the tests that
  cover its change: none when the docs-only check holds, else the files of the repo `CLAUDE.md`
  test map or, with no map, the changed tests, `test_<stem>` files and importers/quoters; the
  full suite for an unmapped file, a changed fixture/config/lockfile, an un-narrowable command
  or most of the suite; reply line `tests: targeted|full suite|skipped, docs-only` before its
  `Files changed:` list; the test stage's full run stays mandatory), test (always fresh: it reviews work it did not
  write; `/code-review <level> <base>...card/<id>` with the level sized to the diff (low text-only, medium small code, high other, #191) plus a check against the card, #7; one full suite run after the last commit, again only after a failure or fix, rerun only for a known flake; `RESULT: PASS` or the card stays), then
  ship (commit, push `card/<id>`), land (merge into the base when the repo is on it and
  clean; before it the dispatcher checks `git merge-base --is-ancestor <base> card/<id>`
  and that ship committed nothing and the land was not skipped, #156; the test stage's
  last test run, or its docs-only check, follows its last commit; a docs-only card, #163,
  skips the suite: every file in `diff --name-only --no-renames` is `*.md` (a served page
  like `docs.html` never counts) and no test quotes a changed file's name, grep
  `["']([^"']*[/\\])?<name>["']` (exit 1 is no hit; a match or an error runs the suite);
  the reply says `tests: skipped, docs-only (<files>)`; the dispatcher reruns that check
  before land and hands the deploy a third verdict, docs-only, so it runs no tests and its
  line is `tests: skipped, docs-only` (kept on `DEPLOY: SKIPPED` too); if the recheck fails
  the verdict is not tested), deploy (done by the dispatcher itself when the project's deploy instructions are
  mechanical, by a `haiku` subagent when they need judgement, #189; local unless the instructions say
  production; the comment head lights the purple dot; it skips the full test rerun when
  the landed tree is the tested one, else runs the tests, and its `tests:` line says
  which; a failing deploy test run leaves the merge unpushed, #156), `merged=True` from git, remove the worktree, move to `verify`.
  Each stage sets its model (#83): plan `opus` (pinned, no longer the session's:
  planning is judgement; a card labelled `small` (in a git repo) is planned and built on `sonnet`, #187 —
  the labels alone pick it, since the model is set before the judged size is known),
  develop `sonnet` (it writes code from a written plan), test
  `opus` (never weaker than the `sonnet` that builds) and deploy none when mechanical
  (the dispatcher runs it, #189) or `haiku` for a judgement deploy's subagent (it reruns the tests
  only when the base moved since the test stage, #156) —
  the owner's calls, #154 and #155: the strong model thinks and checks, the cheaper one
  writes; the split trades one extra read of the code for a cheaper writer.
- **Landing by pull request (#6).** A project's `land` is `merge` (the default, above) or
  `pr`: then ship also opens a PR with `gh` and stores its URL in the card's `pr`, the
  local merge is skipped (the deploy never merges either), and the merged dot waits for
  the PR — each run first asks `gh` about the project's unmerged `verify` cards with a PR.
  Rework clears `pr` (#72), and ship reuses an existing PR for the branch only while it
  is open, so a reworked card ships as a new PR and never inherits the old merged one.
- **The checklist is the live task list (#3).** The plan's `## Steps` become checklist
  items (new texts only, existing items kept); the subagent that builds ticks each as it
  finishes — its one board write besides the plan stage's plan post — so the board card's `☑ done/total` chip moves while it
  works, and the test review names any step left unticked.
- **Stage board writes are slim (#203).** Every brief carries the line that the stage's
  board writes (`update_card`, `comment`: the plan post and its MOVED comment, the
  checklist ticks) pass `slim=True`, so each answers with only the card's id, lane and
  assignee instead of the whole card with its event log; `tickets-stage.md` says the same
  in its body, and the dispatcher's own writes through MCP (a session without PowerShell)
  pass it too. The dispatcher's HTTP writes stay the trimmed `Select-Object` (#186).
- **What only a person can do** (`## Blocked by`) becomes `todo` cards linked `blocks`.
  A failure is an `agent failed: …` comment, lane unchanged.
- **Wrap-up:** follow-up ideas as `todo` cards, and a summary in the chat.
- **Never wait to move cards (#147).** The owner's rule: the flow of cards is never held
  for the user. A transient board-write failure (tools gone, HTTP error, timeout) is
  retried, over HTTP if the tools failed. A write refused by Claude Code's permission
  check is never retried in another form or through another tool (that would route
  around the user's permission settings; the card's first scope asked for it and was
  dropped): the run comments the refused write on the card, carries on with the queue,
  and retries the same write first on the next tick (the comment is the record across
  ticks); the summary names the permission rule that would let it through, once. A tick
  never ends "waiting on you" to hold the queue — on 2026-10-11 a loop did that for 5 h
  over one refused write while two built cards sat in `develop`. Briefs carry the rule
  to subagents, which report a refusal instead of working around it. Every brief also
  carries the no-chips line (#159): no background-task chips (`spawn_task`), nothing
  scheduled, never ask the user in chat; a chip made by mistake is dismissed, named in the
  reply, and noted on the card and in the summary.
- **The base checkout stays clean (#204).** Plan, develop and test stages never write in
  the base checkout (`<repo>`, e.g. `D:/source/Tickets` on `master`), and every stage keeps
  its scratch files in its own folder, `<session scratchpad>/card-<id>-<stage>`, so two
  stages' scripts never overwrite each other; its reply reports `git status --porcelain`
  of the base checkout before its `tests:` line, `Files changed:` and verdict, which stay
  last. Briefs said so before run.md did, and in the #193 run two stages
  still left copies of their card's edits there and the land (it needs a clean checkout)
  was skipped for hours. So the dispatcher runs `git -C <repo> status --porcelain
  --untracked-files=no` after every stage reply, before anything else for that card, and
  the land runs the check once more when it finds the checkout dirty. A
  listed path is a stray copy, reverted at once with `git restore --source=HEAD --staged
  --worktree` and commented on the card, only when a card in flight changed it on its branch
  (`diff --name-only --no-renames <base>...card/<id>`), the checkout's file is exactly the
  branch's (`diff --quiet card/<id> -- <path>`) and its index entry is HEAD's or the
  branch's: nothing is lost, the branch holds the content (the owner's standing call). Any
  other path (a person's edit, a half-made copy) is never touched, only noted on the
  replying card once per run and in the summary. Untracked files are not checked, as by
  the land's clean test; an untracked copy of a file the card adds still makes `git merge`
  refuse, and the deploy comment says so.

**Why run.md says what it says (#216).** The dispatcher reads `run.md` on every working tick
and pastes much of it into every stage brief, so its size is paid many times per run. It
holds the rules only (734 lines / 54,119 chars before #216, 532 / 38,818 after; a guard keeps
it under 40,000 chars and free of dated history); the reasons and history behind them live
here:
- *Lean board calls (#186).* The owner's call on 2026-10-11, after a 5-card run took the
  5-hour limit to 86%: the MCP tools answer with the whole card, `get_card` and every write
  echo it with its full `events` log and `list_cards` lists every card's text, so a card's
  plan landed in the dispatcher's context several times (the `plan` field, the checklist, the
  `detail` of the events that set them; ~12k tokens for one comment on #159) and every later
  wake re-read it. PowerShell prints only what the command selects, hence the trimmed
  `Invoke-RestMethod` forms; the URL comes first and quoted because the usual allow rule
  matches only that shape, and `ForEach-Object { $_ }` unrolls the array because parentheses
  would no longer match it (`Invoke-RestMethod | Where-Object` otherwise sees the array as one
  object). The bare object's default view cuts `events`, `checklist` and `links` to 4 items,
  hence `ConvertTo-Json -Depth 8` for a brief. Measured in verify: the dispatcher session's
  context on the next comparable run against the 2026-10-11 run (STATUS.md, #186).
- *Board writes that fail (#147).* The owner's rule: never wait for the user to move cards;
  on 2026-10-11 a loop sat 5 h "waiting on you" over one refused write while two built cards
  sat in `develop` (more under "Never wait to move cards" above).
- *Stage agent (#188).* A `general-purpose` agent loads every tool and MCP server of the
  session, and most of a stage's cost is that fixed start (a haiku deploy ended at ~63k
  tokens after 5-7 commands); `tickets-stage` carries only the tools a stage needs.
- *Models (#83, #154, #155, #187).* The strong model thinks (plan) and checks (test: never
  weaker than the `sonnet` that wrote the code); develop writes from a written plan on the
  cheaper `sonnet`, which trades one extra read of the code for a cheaper writer. #187, the
  owner's call after the same 86% run: a card labelled `small` plans and builds on `sonnet`;
  the model is picked before the plan subagent starts, so only labels can decide it, and a
  card judged small stays on `opus`. A judgement deploy runs on `haiku` (push and restart).
- *Card size (#190).* For a small card the second read of the code that the plan/develop
  split costs is more than the cheaper model saves, so one agent plans and builds it; one
  that turns out bigger still finishes, since its code is already read and a handoff would
  pay the second read anyway.
- *Slots (#193).* Each finished stage wakes a dispatcher whose context grows with every card
  in flight, and cards branched from the same base edit the same files, so each land
  conflict costs a rework round in test. A card waiting for the test slot keeps its slot, so
  at 2 the run is near serial while a land is slow; 1 is serial (cheapest, slowest); 3 suits
  many small, unrelated cards when time matters more than tokens. One card in `test` at a
  time because tests, the deploy and restarts share ports, devices and the base checkout.
- *Fresh subagents (#61).* Never continuing a finished subagent keeps what an earlier stage
  assumed or got wrong out of the next, and the test stage reviews work it did not write;
  the brief is its whole world because it starts as empty as a session after `/clear`.
- *No-chips line (#159).* Two haiku agents, #154 and #155, each made a background-task chip
  by accident; #156, with the line in its brief, did not.
- *Base checkout check (#204).* In the #193 run two stages left copies of their card's edits
  in the base checkout and the land (it needs a clean checkout) was skipped for hours;
  reverting an exact copy is the owner's standing call, since the branch holds the content.
  Untracked files are skipped as the land's clean test skips them.
- *Docs-only (#163).* A page the app serves (`docs.html`) never counts as docs-only because
  tests fetch it without naming it; a doubtful grep hit only runs the suite, which is safe.
  A `DEPLOY: SKIPPED` deploy keeps its `tests:` line because a deploy may merge, test and
  push yet skip the rest, as the Tickets repo's does for a docs-only change (#160).
- *Test stage (#191).* `code-review` gets a ref range so it reviews committed work whatever
  the cwd, and does not know the card, hence the separate check against the request.

**No tools, no stop.** Claude Code connects MCP servers once, at session start, and never
retries, so a session opened while the board was down has no `tickets` tools — and a
session cannot reconnect a user's MCP server itself (that reconnect tool covers claude.ai
connectors only). A run therefore starts the backend if it is down and, without the
tools, runs the whole drain over the HTTP API, one route per tool; `POST /api/activity`
exists for that. Over HTTP an unassign is `null`; core stores `""` as NULL too, so both
surfaces agree (an `""` used to be stored as is, and a later null logged a second
unassign).

**`/tickets-start [interval]` (`skill-start/SKILL.md`).** A second global skill, linked
the same way into `~/.claude/skills/tickets-start`, run by hand in the repo's folder. It
merges what were `/tickets-setup` and `/tickets-run-loop` (#59): first the setup,
idempotent and fixing only what is missing — the backend (started with
`restart-backend.ps1 -Delay 1`, because a uvicorn started straight from a tool call dies
with it), the user-scope MCP server, the repo and its `origin`,
the repo's `CLAUDE.md` test/deploy rules (asked for, never committed), and the project row.
Being a person's command, it may configure the project, which the MCP tools cannot: it
uses `/api/projects` and creates the project last, so its setup cards see the new
`CLAUDE.md`, and it offers to start the board at logon (`tickets-backend.cmd` in the
Startup folder), so it is up before any session connects. It finds the Tickets repo as
`git -C ${CLAUDE_SKILL_DIR} rev-parse --show-toplevel`: the skill folder is a junction,
so `${CLAUDE_SKILL_DIR}/..` is `~/.claude/skills`.

Then the work, picked by its argument: a card id or `once` → one run in this session;
an interval or nothing → `/loop` (default 15m, at least 5m) on a tick prompt that reads
`run.md` and does one run, passing the session id and the Tickets repo. `/loop` cannot
call `/tickets-start` itself — it refuses model invocation, so starting work stays a
person's act. A tick with cards still in flight or an empty queue
answers one line, stamped with the local date and time from the clock (#64), so an
idle loop shows how long ago it last looked. It starts no second loop if one already runs, and none when there is
no project row. Session-only: it ends with the session (or after the 7 days a session
loop lives). Its summary ends with a clickable link to the board.

**`/tickets-open-board` (`skill-open-board/SKILL.md`)**, linked the same way: starts the
backend if it is down, opens the board in the system browser and prints the link
(clickable and as a bare URL). The only one of them Claude may invoke by itself.

**`/tickets-stop` (`skill-stop/SKILL.md`)** deletes the session's cron jobs whose prompt
starts `Tickets loop tick.` and lets cards in flight finish; Claude may invoke it by
itself, since stopping starts no work.

**Docs.** `docs.html` is the user's manual, written by hand from this file and
`skill-start/run.md`: a change to how the board or the agent behaves updates it too.
`test_docs_page_is_served_and_covers_the_essentials` checks the lanes and key terms.

**The annotated card (#97).** Right after the contents, "A card at a glance" shows
`docs-card.png` with a numbered legend. The image is a screenshot of the real sheet, never
a drawing: `uv run python docs_card.py` fills a card with every field (verify lane, all
three dots, session, pull request, plan, questions with options, answers, checklist,
labels, parent and blocks links, comments with claude's output) on a throwaway database,
serves the board from its own process on a free port (never 8123), opens the card in
Chrome, draws a red badge per entry of `docs_card.MARKERS` and saves the shot. The legend's
`data-field`s follow `MARKERS` in order, which is the sheet's top-to-bottom order
(activity and say something right under the description, #95). A screenshot cannot update
itself, so two tests keep it honest: the browser one opens the same card and fails when the
sheet's field headings differ from those `MARKERS` names, in order, or a badge no longer
finds its target; the HTTP one checks the PNG is served and the legend matches `MARKERS`.
Any other change to how the sheet looks needs the script run again by hand. The card it
builds has every field filled, so #96's hiding of empty fields never drops one from the
figure.

## Board (`board.html`)

Six columns, native HTML5 drag & drop (`dragstart` / `dragover` + `preventDefault` /
`drop` → `PATCH /api/cards/{id}`). Click a card for a detail panel: project,
description, then right under it the activity log and comment box (#95; a saved card
only, a draft has neither, #94), then the plan
round, assignee and lane, labels, checklist, links. The board writes as `User`, a fixed
name, and agents write under their own names. Project tabs (#63:
"All" first, then one per project with its color dot; the pick is persisted) filter the
board — an agent and a human both scope to one project. Crowded, the strip scrolls
sideways inside itself; a new pick is scrolled into view, a reload leaves the strip where
the person scrolled it (the board reloads itself on others' writes).
Dragging inside a column reorders it: `dropBefore` finds the card the pointer is above
(shown with a `.drop-at` insertion line) and `slots` gives the dropped cards their `pos`
values between its neighbours.

A blocked card (#35, `blocked_by` not empty) gets the `blocked` class: a red 4 px stripe
down its left edge (`::before`, as box-shadow is `.drop-at`'s and outline is
`.selected`'s) and a red "blocked by #12 #14" pill. A blocking one gets `blocking`: amber
stripe, amber "blocks #37" pill. Both: the stripe is half red, half amber. Done cards
are never marked, not even while an optimistic drop into done waits for its PATCH. A
link, unlink or move elsewhere bumps the version, so the status poll's reload updates the
marks. The board's own writes reload at once instead: `replace` reloads when the written
card's marks changed (the other end's changed too), and the panel's `blocks` link and
unlink reload. #37 builds on the classes and fields.

Arrows (#37) go from each blocking card to each card it blocks: one inline
`<svg id="links">`, the last child of `#board` (now `position: relative`), sized to the
board's scroll size, `pointer-events: none` so clicks, selection and drags go through;
cards sit over it (`z-index: 1`), so an arrow crossing a card passes under its text.
`drawLinks()` places it in the board's own scroll coordinates, so scrolling needs no
redraw; it runs at the end of `renderBoard()` (load, poll reload, move, select, drop,
link and unlink all end there) and from one `ResizeObserver` on `#board` (window resize,
wrap, fonts). It removes the old svg before measuring, so a stale one never holds the
board wide. An arrow is drawn only when both ends are on screen and not done (the
`blocking` lists, plus the local lane for an optimistic drop): an end in another tab,
hidden or archived has only its pill. Between neighbouring lanes it is a Bézier from the
facing side edges' middles; within a lane a C out of the right edge and back in, into the
lane gap. An arrow that skips lanes never passes under a card there (it would read as
starting from that card): it jogs up or down in the lane gaps and crosses the lanes in
between at the free height (between cards, above or below them) nearest both ends. A
redraw keeps a hovered card's arrows lit, and the status poll redraws when a work dot
lights or goes out (it can wrap a card's meta row).
Amber `#ff991f` line and head, one arrow (red already means "blocked" on the card it
points at). The head (#64) is a slim notched one, tip on the card edge, and `orient="auto"`
turns it with the path's end, so every path ends in a 10 px straight run into the edge
(`R`): the curve stops short and an `L` finishes it, and the head points along the line
the eye sees, not along a curve that levels off in its last pixel. Against clutter they are
faint (opacity .3); hovering a card lights its arrows, both ways (`.hot`). No on/off
toggle yet: add one if real boards get crowded.
No dark-theme colours: the board has no dark theme yet.

The sheet hides an empty field that only an agent fills at that point and keeps every
field a person fills in by hand at that point (#96). Plan and open questions each get their own box
under the comment box (under the description on a draft) once they hold text; "your answers" shows whenever there are
questions (or answers) and is hidden otherwise. The checklist shows once it has items;
empty, it shows only on a new card or a card in todo, since from plan on the agent writes
it from the plan's steps. The pull request link and session button were already shown
only when set. "plan" is a fixed 10 lines and scrolls, "open questions" and "your
answers" grow like the description. A question may list its choices as indented `- `
bullets under it, one suffixed ` (recommended)` — the AskUserQuestion shape, still plain
text (#4). Under the questions box each such question gets a row of radios (the
recommended one badged, none preselected); a pick replaces or adds that question's
top-level `N. <option>` line in the answers and keeps every other line. The description box grows with its text, from 10 lines
up to 50, then scrolls
(CSS `field-sizing: content`). Lanes run to the bottom of the window even when empty,
and all grow together with the tallest. 

**The work dot.** A board card leads with a green dot that shows and pulses while an
agent's status entry (`/api/activity`) points at the card, and is hidden otherwise (#65:
the element stays, so `markWorking` lights it without a re-render). The same poll pulses
the project tabs (#82): a tab's color dot pulses while an agent works any card in that
project (matched case-insensitively), and "All" — plus a tab with no color, such as a
remembered project that is gone — has a grey dot that shows and pulses only while an agent
works on anything (`markTab`, run by `loadStatus` and on every `renderProj`). The project pill
shows only under the "All" tab, and a `done` card has no archive button of its own: the
lane's "archive all" and the sheet's "archive" cover it.

**Follows other writers (#58).** The 5 s status poll also reads `GET /api/version`, the
newest `events.id`: when it moved, the board reloads once — not under a drag or a held
pointer, and then the value is not recorded, so the next poll retries. The open sheet
re-renders only when the fetched card is newer (the `stale()` rule) and never while one of
its text fields has focus; that waits for a later poll. Writes that log no event (a
pos-only reorder, project renames) do not trigger it. Board loads are numbered (#88): an
answer that arrives after a newer load started is dropped and its caller waits for the
newest load, so a slow reload (a poll's, say) can no longer put an older board back over a
newer one. A poll starts no load while one is pending (#202): that would drop the pending
load's answer, and on a backend slower than the poll no load would ever draw. The poll
marks its reload as due instead (`reloadDue`), because the pending load may have been asked
before the change the poll saw. The due reload starts as soon as the pending load has drawn,
so any number of held polls cost one load; while a pointer is held it waits for the next
poll, as a change does. A pending load that fails leaves the reload to the next poll's
retry (#165), which waits for a released pointer too. Any load started meanwhile (a
write's, a tab pick) clears it, since that load asks after the change. A fetch has no
timeout, so the sixth poll that wants a reload while one load is still pending
(`LOST_AFTER`, half a minute) takes that load for lost and starts a new one; the lost
answer, should it still come, is dropped (#88). A load that started before a write and is
answered after the write's response
is still the newest, so it merges per card (#105): a card whose copy on screen has a newer
`updated_at` keeps that copy — unless that copy has left the view the load asked for
(archived flag or project tab), when it is not drawn at all. A card on screen but missing
from the load that renders needs no such rule (#148): `cards` only gains a card from a
load, a create from the sheet reloads the board itself (so a load held across the POST is
overtaken and dropped), and rendered loads go in start order, each asked after the
previous one was answered. The ordering is causal, not by clock, so client/server clock
skew cannot matter, and a card archived or moved away elsewhere still goes on the next load.

**On master, deployed (#41).** After the auto dot come two more, on the board card and in
the sheet header: blue when `merged` (the card's branch is on the base branch, locally or
on origin), purple when `deployed` (on the local test backend or a device), absent when not (#65).
Not clickable. The skill's land step merges the card's branch into the base branch
(#106) and the run then sets `merged` from git; with `land` `pr` the run's pull request
catch-up sets it once the PR is merged.
`deployed` the board sets itself, from the head of the deploy comment: `core.comment`
lights it on `deployed: ` from `claude-agent` and darkens it on `deploy skipped: ` or
`deploy failed: ` (a skip — nothing to deploy, or no phone — is not a failure). The head
comes from the verdict line (`DEPLOY: OK`, `DEPLOY: SKIPPED`, anything else), so the dot
says what the comment says, whoever wrote it (#68). A move from
todo/verify/done back into plan, develop or test is rework and clears both, and the
card's `pr` too (#72: an old merged PR would vouch for code not on the base), unless the
same write sets them; the history's `edited` event keeps the old PR URL. A card deployed or merged by hand stays dark unless someone sets the dot. MCP `update_card` takes both.

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
scrolls: the card number, the title (edited in place), "archive", "move to done" (only
while the card is in `verify`) and "close". A new
card's row is "new", the title, "cancel", "move to plan", "close" (#153).
The card number carries the auto dot, which pulses while an agent works the card.
The line under it says "created <when> by <author>" (the full date on hover; project
and lane are fields on the sheet, #65), then, once an agent has taken the
card, "session <first 8>" (#2): the `session` field holds the Claude Code session id the
skill writes when it takes a card, and the button copies `claude --resume <id>`, so the
transcript of the run that worked the card is one paste away.
Closing blurs the focused field, so the edit still in progress saves too, and text left
in the comment box is posted rather than dropped. "move to done" is the one click that ends a
verified card: it writes the lane like the lane select does, and the sheet stays open. A comment being typed
survives a re-render: the box is refilled with whatever was in it. The error bar is fixed above the sheet
so its messages stay visible. The sheet keeps "archive".
The activity log leaves out bookkeeping (#65, `bookkeeping()`): edits of `merged`,
`deployed`, `session` and `pr` (the dots, the session button and the PR link show them)
and anyone assigning or unassigning themselves; "show N earlier" counts what it shows.

"New Card" opens the same panel on an unsaved draft (title focused, project from the
filter). It shows the fields a saved card does, links included, but no activity log and
no comment box: a card that does not exist yet has no comments (#94). Nothing
is written until the draft is closed with "close" (or Enter in the title), which
`POST`s the fields all at once (one `created` event), then the links queued on the
draft (checked to exist as they are added), and hides the sheet. A link whose check is
still in flight when the draft is closed is waited for, not dropped: the draft keeps its
pending checks (`checks`) and the `POST` goes out once they have answered, with the link
if it checked out (#104). A draft with nothing typed just closes; a link being checked
counts as typed. A draft with
content but no title stays open and says "a card needs a title". If the `POST` fails
the sheet stays with everything in it and the next "close" retries; a double click makes one
card. "cancel" discards the draft. "move to plan" closes the draft like "close" but
creates the card in `plan`, whatever the lane select holds: `closePanel("plan")` passes the
lane to `createDraft` for the `POST` body only, so it is one `created` event in plan, an empty
draft still just closes, and an untitled one that has to stay keeps its lane (#153). An
existing card's sheet has no such button. The draft re-renders only for checklist edits and
queued links.

**Deleting a project.** Each project in the settings has "delete project…", which opens a
native `<dialog>`: it counts the cards (archived too) that will move, says they go to
"No Project" where agents will not work on them, and that it cannot be undone; only
"Delete project" acts, Cancel or Esc do nothing. `DELETE /api/projects/{name}` →
`core.delete_project` moves the cards to `NO_PROJECT` (created on first use, grey; no
card events, like a rename) and removes the row. "No Project" itself has no delete
button and the server refuses to delete it. The skill never works a card in it. Renaming "No Project" to something else makes its cards workable again.

"projects…" in the header opens the panel on project settings: name, path, agent
instructions, a color picker and the land select ("merge into the base branch" / "open a
pull request") per project, each saving on change, plus a "new project"
form (its picker means "pick one for me" until touched). Each board card carries a tag
with its project's name in its project's color, with dark text on a light color. The card
panel and the draft each have a project `<select>`, right under the title; a draft
starts in the selected tab's project; under All (or a tab whose project is gone) in the
first project in tab order. With no projects at all, "New Card"
says "create a project first" and opens the project settings with the name field
focused. Creating a card in a project the filter hides switches the filter to it, so a
new card is always on screen. Renaming the filtered project carries the filter with it.

"settings…" in the header opens the panel on the board settings (#98), a sheet of its own
like the project settings: a "host the board on the LAN" checkbox that saves on change, a
warning that there is no password, and what the backend is doing — listening on
`127.0.0.1` only, or on every address with the LAN URLs as links, or that the change waits
for a restart. When the PATCH says `restarting`, the checkbox is disabled and the sheet
polls `/api/settings` every second (`RESTART_POLL_MS` in board.html, 1000; tests
shorten it) until an answer is no longer `pending` (the old backend answers for ~2 s,
then nothing, then the new one) or says `restart_failed`, and
gives up with an error after a minute; it keeps the last answer, so a failed restart then
shows on the sheet ("tried to restart itself … and did not", with the log, the command and
"untick and tick again"), as it does when the sheet is opened later (#138). A board opened over the LAN that turns the setting off does not poll (it
cannot reach the backend once it is back on `127.0.0.1`): the sheet says this device loses
the board and gives the `127.0.0.1` URL to open on the host. With the LAN URLs it points at
the docs for the Windows firewall in case another device gets no answer.

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

The change log is `STATUS.md` (#177): one row per change, the numbered rows 0-108 frozen at
the top, then a `By card` table keyed `| #<id> | <change> | <state> |` with no running
number. A card appends its own row as the file's last line and edits nothing else there;
`.gitattributes` merges that file with `merge=union`, so parallel cards' rows both stay and
never conflict. PLAN.md itself keeps a normal merge, so real design conflicts still show.
The rules are in STATUS.md's header.

Gate for every task (except a docs-only test stage, #163; the develop stage runs the targeted files, #192, the test stage the full suite): `uv run pytest -q` — the full suite (its count is pytest's own last line) across core, HTTP, the MCP tools
and wire, the board in Chrome, and the two-surface end-to-end. Every test gets its own
temp database, so `tickets.db` is never touched. The browser tests drive the real
`board.html` through system Chrome (`channel="chrome"`, no browser download) and skip
themselves if Playwright or Chrome is missing, so the gate still passes on a bare
checkout.

The suite shares one process on purpose: `core.DB_PATH` is re-read on every connect, so
the temp database reaches the in-thread uvicorn server the browser talks to. That is what
lets `test_e2e.py` drag a card in Chrome and then call an MCP tool on the same card — and
it is why this suite must not be run under `pytest-xdist`.

A browser test waits on a condition, never a sleep (#88). The board draws first and writes
after (a drop moves the card, then PATCHes; a link on a new card is checked, then shown),
so a test that reads the database or types on after a board action first waits for the
board's own requests: `window.__inflight === 0` (the page fixture counts `api()` calls),
or the state the action ends in. A full run is slower than a lone file, which is when
such a missing wait shows.

A DOM read after an action that can re-render (a load, `loadStatus()`, a drop, a patch) is
one `page.evaluate` (`each()` / `shown()` in `test_board.py`): `locator.evaluate_all` and
`all_text_contents` resolve the nodes in one protocol step and read them in the next, and a
re-render between them hands over detached old nodes (#165). The board's 5 s status-poll
timer is off in the browser tests, which call `loadStatus()` themselves; `--poll-ms N`
turns it on at that interval for stress runs, and a test whose point is the timer takes
`@pytest.mark.real_poll`. The page fixture's teardown waits for `__inflight === 0`
(at most 5 s) so no request lands in the next test's database.

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

Auth · live refresh over WebSocket (the board polls `/api/version` instead) · attachments · search.
The LAN setting (#98) binds off loopback without auth, by the owner's decision; auth may
follow.

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
