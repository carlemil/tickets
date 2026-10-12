# Tickets: one run over this project's board

Not a skill of its own: `/tickets-start` reads this file and follows it, once per loop
tick or once when asked. Whoever hands it to you gives three things: the **session id**,
the **Tickets repo** (the folder holding `restart-backend.ps1`), and optionally a **card
id**. The board is the record, not the boss: nothing runs until a person types
`/tickets-start`.
One run takes every ready card of this folder's project, up to the project's in-flight limit (default 2, see Slots) at a time, from
where it stands to `verify`, writing each step onto the board as it goes, and ends
with ideas for what to do next. Work unattended: never stop to ask the user, never wait for an answer —
what needs a person goes on the board (questions, blocker cards) and the run moves on.
The flow of cards is never held for the user over a board write: a tick (one run) never ends
"waiting on you" just to hold the queue, not even when a write is refused (see "Board
writes that fail"). Questions and blocker cards work as below.

Every board write uses `actor="claude-agent"`. Tools are the `tickets` MCP server's
(`list_projects`, `list_cards`, `get_card`, `update_card`, `comment`, `create_card`,
`link_cards`, `set_activity`).

## 0. Board up, tools or not

A missing board never stops a run:
- `GET http://127.0.0.1:8123/api/projects` does not answer → start it detached (a
  uvicorn started straight from a tool call dies with the call):
  `powershell -NoProfile -File <tickets repo>/restart-backend.ps1 -Delay 1`; wait ~12 s and
  ask again. Still down → stop, with the tail of `<tickets repo>/restart-backend.log`.
- The `tickets` tools are missing (the session started while the board was down, or the
  server is not registered) → do the whole run over the board's HTTP API instead; a
  session cannot reconnect a user's MCP server by itself. Same calls, same arguments:

  | tool | HTTP (base `http://127.0.0.1:8123`, JSON bodies) |
  |---|---|
  | `list_projects` | `GET /api/projects` |
  | `list_cards(project=…)` | `GET /api/cards?project=…` |
  | `get_card(id)` | `GET /api/cards/<id>` |
  | `update_card(id, …)` | `PATCH /api/cards/<id>` — unassign with `"assignee": null` |
  | `comment(id, text)` | `POST /api/cards/<id>/comment` `{actor, text}` |
  | `create_card(…)` | `POST /api/cards` |
  | `link_cards(…)` | `POST /api/links` `{from_id, to_id, kind, actor}` |
  | `set_activity(…)` | `POST /api/activity` `{actor, card_id, doing}`; clear: `{actor}` |

  Every write carries `"actor": "claude-agent"`. Send UTF-8 (plans hold non-ASCII): in
  PowerShell `Invoke-RestMethod -Method Patch <url> -ContentType 'application/json;
  charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes(($fields | ConvertTo-Json -Depth
  6)))`. Subagents posting a plan or ticking the checklist get the same instructions. The summary says the
  run used HTTP and that `/mcp` (or a new session) brings the tools back; if the server
  is not registered at all, `/tickets-start` registers it.

### Board writes that fail (#147)

The owner's rule: never wait for the user to move cards; the flow of cards must not be
interrupted. Two kinds of failure, handled differently:
- **Transient** — (only when the error is not a permission denial; a denial says so, e.g.
  "permission denied" or "blocked by the permission check", and is never transient) the
  `tickets` tools vanished mid-run, an HTTP error, a timeout, the
  backend restarting: retry the same write a few times, a few seconds apart, over HTTP
  (as in the table above) if the tool failed. Still failing → treat it like a refused
  write below (note it if you can, carry on, retry next tick).
- **Refused by a permission check** (Claude Code's permission prompt or auto-mode check
  denied the call): do **not** retry it in some other form or through another tool — no
  switching from the tool to HTTP, from `curl` to PowerShell, or splitting it up to get
  past the check. The user's permission settings decide; routing around them is never
  allowed. Instead:
  1. note it on the card if you can: `comment("board write refused by a permission
     check: <the write, e.g. move to test>; retried next tick")` (if the comment is
     refused too, the summary still names it);
  2. carry on with the rest of the queue: the card is done for this run, its slot is
     freed at once for the next ready card, and every other card moves as usual; if the
     note could not be posted, keep the refused write in your own session notes and in
     the summary;
  3. on the next tick, try the **same** write again first: a card still in its old lane
     whose latest `claude-agent` comment is such a note, with no
     human comment or lane change after it, gets that write (e.g. the move to
     `test` of a card whose BUILT summary is already posted), not a new stage;
  4. the run's summary names each refused write **once**, with the permission rule that
     would let it through (e.g. allow `mcp__tickets__update_card`, or the exact `Bash(…)`
     / `PowerShell(…)` command used), so the user can add it in `/permissions`. Each tick's summary
     names the rule once; a retry refused again may use one short line ("still refused: #<id> <write>").
- A tick never ends "still waiting on you" to hold the queue: a refused write is a line in
  the summary, never a reason to stop working the other cards or to idle the loop.

Subagents follow the same rule (say so in every brief): a refused write is reported in
their reply, never retried another way; you note it on the card and retry it next tick.
Every brief also carries the no-chips line (#159; the brief rules are in step 3).

## 1. Find the project

`list_projects`, and take the one whose `path` is the current working directory
(compare case-insensitively, `\` and `/` alike, no trailing slash). None → stop and say:
open the board, "projects…", set this project's path to this folder. Never work
"No Project".

The repo's `CLAUDE.md` holds the project's own rules (tests, conventions, deploy); every
session and subagent loads it anyway. The project's `instructions` are board-specific
additions (e.g. the deploy target). Priority, highest first: the card's latest comments,
the card (`description`, `plan`, `answers`), the project instructions, the repo's
CLAUDE.md, this skill.

Read the run's in-flight limit from the project `instructions` here (see Slots): it is
the one setting outside that priority order, so a card's comments or text never set it.

The **repo** is the git toplevel of the path; the **base** branch is the repo's current
branch (`git -C <repo> rev-parse --abbrev-ref HEAD`). Develop and test need a git repo:
if the path is not one, cards can still be planned (the plan is posted, nothing is
built) but fail in develop/test.

## 2. Build the queue

`list_cards(project=…)` and keep the cards in `plan`, `develop`, `test` (with a
card id: just that card, wherever it is among those lanes). Order: `plan` first (their
questions reach a person sooner), then `develop`, then `test`; within
a lane, the board's order. Skip, and name in the
final summary:

- **waiting for answers:** `questions` filled, `answers` empty;
- **blocked:** `blocked_by` is not empty. The server fills it with the cards that have a
  `blocks` link to this one and are not finished yet; finished is `done`, or `verify`
  with `merged` set, or `verify` with `pr` empty in a project that lands by merge (in a
  `pr` project an empty `pr` is not shipped);
- **assigned to a person:** `assignee` set to anyone but `claude-agent`;
- **already in flight** in this run (see 3).

Re-list whenever a slot frees: people edit the board while you work, and a card you
moved may now be next (a card planned without questions goes straight on to its develop
stage, then to test). Plan then develop of one card is one piece of work, not working
it twice.
Never work the same card twice in one run — once it fails or waits, it is done for this
run.

A card whose latest `claude-agent` comment says a board write was refused (see "Board
writes that fail") is not skipped and not restarted: its first step in the next tick's run is that same
write again (a note followed by a human comment or a lane change is stale; ignore it). Refused again → note nothing new, carry on, name it in the summary.

Before working the queue, catch up on pull requests: for each of the project's `verify` cards with
`pr` set and `merged` false, `gh pr view <pr> --json state -q .state`; `MERGED` →
`update_card(merged=True)`. Any failure here is ignored.

## 3. Work the cards, several at once

You are the dispatcher. Each stage of a card runs in its own **background** subagent
(Agent tool, `general-purpose`, `run_in_background: true`), so several cards move at
once and a long run does not fill this session's context. When one finishes you are
notified: do that card's (you) steps for the stage, then start its next stage in a
**new** subagent, or, when the card is out of your hands, fill the freed slot from a
fresh re-list. A card in `plan` gets a **plan** subagent: it plans, posts the plan and —
with no questions — moves the card to `develop`; a large card it builds nothing (you then
start its **develop** subagent at once), a small one (Card size) it then builds itself
in the same context. A card already in `develop` gets a develop subagent too;
`test` always a fresh one. The one exception is a mechanical deploy, which you run
yourself (ship, step 4, #189). Never
continue a finished subagent (no SendMessage): every stage starts from a clean context
holding only the card, so nothing an earlier stage assumed or got wrong leaks into the
next, and the test stage reviews work it did not write.

**Model per stage (#83).** Each stage's Agent call sets its model on purpose — the
`model` parameter, or none — so the tokens go where they pay:
- plan: `model: "opus"` — the owner's call (#155): planning is judgement, so the strong
  model writes the plan (pinned, no longer the session's model). The one exception (#187,
  the owner's call after a 5-card run took the 5-hour limit to 86%): a card labelled
  `small` (Card size) gets `model: "sonnet"`, which plans and builds it. The model is
  picked before the plan subagent starts, so only the labels decide it (the judged size
  comes later): `small` → `sonnet`; `large`, both labels or no label → `opus`.
- develop: `model: "sonnet"` — the owner's call (#155): it writes code from a written
  plan, for every large card, whether the plan stage just moved it or a person put it in
  `develop`. Splitting plan from develop trades one extra read of the code (develop
  starts fresh) for a cheaper writer; for a small card that read costs more than the
  cheaper model saves, so its plan subagent builds it (Card size, #190): on `sonnet` when
  labelled `small`, on `opus` when judged small.
- test: `model: "opus"` — the owner's call (#154): the review is never weaker than the
  `sonnet` that wrote the code. The strong model thinks (plan) and checks (test).
- deploy: no model for a mechanical deploy (the dispatcher runs it itself, #189); a
  judgement deploy's subagent is `model: "haiku"` — the owner's call (#154): push and
  restart per the deploy instructions; it reruns the tests only when the landed tree is
  not the one the test stage tested (#156).

**Card size (#190):** a small card is planned, posted and built by **one** plan subagent in
one context (the one-agent path, see plan below), which saves the second read of the code
that the plan/develop split costs; a large card keeps the split. One definition of size:
- label `small` → the one-agent path, forced; label `large` → the split path (plan, then
  a fresh develop), forced. Both labels → `large`. Labels match case-insensitively. Not a
  git repo → never the one-agent path (no branch to build on).
- neither → the plan subagent judges after reading the code, before it posts: the card is
  small when the plan has at most 5 steps, no open questions and no `## Blocked by`, and
  the change touches at most ~5 files (PLAN.md, docs and tests included), adds no new
  module, and changes no database schema, HTTP route or MCP tool. Anything else is
  large. When unsure: large.
- the labels are the only signal known before planning, the judged rule only after, so
  the labels alone pick the plan model (#187, Model per stage): `small` → `sonnet`, else
  `opus`. A card judged small was spawned on `opus` and is built there. No guess from the
  description: the label is the per-card override (add `small` for Sonnet, `large` or no
  label for Opus).
- a person forces a path by adding the label `small` or `large` to the card before its
  plan stage starts; you read the labels from the `get_card` right before spawning. No
  label: the agent judges. The posted plan's first line says which path it took:
  `Size: small (label)`, `Size: small (judged: 3 steps, 2 files)` or `Size: large (…)`;
  relabel and move the card back to `plan` to replan.
- a one-agent build that turns out bigger than judged still finishes on the develop rules
  (the code is already read; a handoff would pay the second read anyway) and says so in its
  summary; one that crashes after the post is handled like a FAILED after the post.

**Slots.** At most **2 cards in flight** by default (#193). A project changes it with a line
`Cards in flight: N` in its `instructions` (N a whole number from 1 up; case-insensitive,
the first match wins, no upper cap). A missing, non-numeric or below-1 value falls back to
2 and the summary says so in one line. It is a run-wide setting: only the project
instructions or the default set it, never a card's comments or text. The trade-off:
more slots finish a batch sooner on the clock; fewer slots spend fewer tokens (each
finished stage wakes a dispatcher whose context grows with every card in flight) and cause
fewer merge conflicts at land (cards branched from the same base edit the same files, and
each conflict costs a rework round in test). A card waiting for the test slot keeps its
slot, so at 2 the run is near serial while a land is slow. 1 is serial: cheapest, slowest. 2 is the
default. 3 suits many small, unrelated cards when time matters more than tokens. Plan and
develop stages run side by side within that limit, each in its card's own worktree. A card keeps its slot and its
worktree from plan straight into develop (no re-list in between). At most **one card in `test`**
at a time — from the test stage through ship, land, deploy and clean-up to `verify` —
because tests, the deploy and service
restarts share ports, devices and the base checkout; a card whose next stage is test
waits, with no subagent, until that slot is free. Land merges and pushes of the base are
yours, one card at a time: never interleave two cards' land/deploy sequences. Each card
works in its own worktree (below); do not use the Agent tool's `isolation: "worktree"` —
the `card/<id>` branch and its worktree path are the record.

Starting a card: `update_card(assignee="claude-agent", session="<session id>")`
(the board's link back to this session's transcript). `set_activity` holds one entry
for you, so point it at the card you started last and say how many are in flight:
`set_activity(card_id=…, doing="developing (2 in flight)")` (`planning`,
`planning and building` for a plan stage that may build, `developing`, `testing`); clear it (`set_activity` with no card) when the last card
leaves your hands.
Before every board write after a stage, `get_card` again: **a person's move wins** — if
the lane changed under you (other than by your own move, or the plan stage's move to
`develop`), post your output as a comment saying it was moved during the
run, and leave the card where they put it.

**The brief is the subagent's whole world** — it starts empty, like a session after
`/clear`, so paste everything in; never summarize the person's words and never say "see
the card". `get_card` right before spawning (so earlier stages' writes are in) and
include, each under its own heading:
- the card: `#<id> <title>`, lane, labels; the description, plan, questions and answers
  **verbatim**; the checklist with each item's done state; links (parent, blocks, blocked
  by) with the linked cards' titles and lanes; every comment in order with its author and
  time — but not a comment's `output` (the CLI transcript: long, and the comment says
  what it did);
- the project: name, instructions verbatim, `land`, the repo path and base branch, and
  that the repo's `CLAUDE.md` holds its rules (read it first);
- where to work: the folder (absolute path — tell it to use absolute paths or `git -C`),
  and for plan, develop and test the worktree path and the `card/<id>` branch;
- the stage's rules below, the `## Blocked by` request, and the verdict line it must end
  with;
- that a board write refused by a permission check is never retried in another form or
  through another tool: it reports the refused write in its reply and carries on (you
  note it on the card and retry it next tick);
- that it never creates background-task chips (no `spawn_task`) and never schedules
  anything, and runs unattended and never asks the user in chat (a plan stage's questions
  still go on the card, as its rules say) (#159: two haiku agents, #154 and #155, each
  made a chip by accident; #156 with this line in the brief did not). A chip made by
  mistake is dismissed at once (`dismiss_task`) and named in the reply; you note it on
  the card's comment and in the summary. Every stage brief carries this line, whichever
  stage builds it.

You — not the subagent — write to the board and run the git steps marked (you); the
exceptions are the plan subagent posting its plan (and moving the card to `develop`), and
the develop subagent ticking checklist items.

On a card's way out: `update_card(assignee="")` unless a person had it before you.

### plan  (in the card's worktree)

(you) `git -C <repo> pull --ff-only` first; if it fails, tell the subagent the code may
be behind. Then set up the card's worktree as under develop below, before spawning; note
whether `card/<id>` already existed. Not a git repo → no pull, no worktree: the subagent
plans in the folder. The subagent gets `model: "sonnet"` when the `get_card` right before
spawning shows the label `small` (and not `large`), else `model: "opus"` (Model per stage, #187). Its brief carries the card's
labels and the Card size rule; unless a label is `large` (or it is not a git repo), it also carries the develop
stage's rules (build from the plan, targeted tests, commit on `card/<id>`, never push,
tick checklist items, the `tests:` line and `Files changed:` section, all below), so it
can build if the card turns out small. A `large` label: no build rules.

**Plan.** The subagent reads code in the worktree, changes nothing until the plan is
posted (beyond resolving and committing the base merge's conflicts if you told it to;
a small card is built after the post, below), and writes the
complete plan in markdown (Context section restates the request; its first line is the
`Size:` line, Card size above). If the card already
has `questions` and `answers`, those are a person's answers: build them in, do not ask
again. The plan has a `## Steps` section: a numbered list of concrete implementation
steps, at most ~10, one line each. Anything needing a person's decision goes in a last
`## Open questions` section, numbered 1., 2., …; a question with sensible choices lists
2–4 of them under it as indented `- ` bullets, the recommended one first and suffixed
` (recommended)` (the board shows them as radio buttons), otherwise it is free text.
Last line `QUESTIONS: NONE` or `QUESTIONS: OPEN`.

**Post.** The subagent writes the plan to the board itself (`actor="claude-agent"`):
- `get_card` first: **a person's move wins** — lane no longer `plan` or assignee no
  longer `claude-agent` → `comment` the plan saying the card was moved during the run,
  end `STAGE: MOVED`.
- Cut the verdict line and the `## Open questions` section off; empty plan → post
  nothing, end `STAGE: FAILED`. The steps become the card's checklist, its live task
  list: the existing items as they are, plus one `{"text": <step>, "done": false}` per
  step whose text is not already on it (so a replan adds only new steps).
- OPEN → `update_card(plan=…, questions=<the cut section>, checklist=…)`, end
  `STAGE: QUESTIONS`.
- A `## Blocked by` → `update_card(plan=…, checklist=…)` (plus `questions=…` if OPEN),
  lane unchanged, end with the section and `STAGE: BLOCKED`.
- NONE → `update_card(plan=…, checklist=…, lane="develop")`, so a person sees the plan
  in `develop`. Size large → end `STAGE: PLANNED`. Size small → **build in the same
  context** (the one-agent path) following the develop rules below, ticking checklist
  items as it finishes them; its reply is the plan followed by the develop reply (summary,
  `tests:` line, `Files changed:`), last line `STAGE: BUILT` (`STAGE: BLOCKED` with a
  `## Blocked by`, or `STAGE: FAILED` if the build could not finish after the post).

It **builds nothing** unless the card is small (Card size) and posted with NONE: no code
edits, no commits of its own work, no checklist ticks. Its reply is the plan, last line
one of `STAGE: PLANNED`, `STAGE: BUILT`, `STAGE: QUESTIONS`, `STAGE: BLOCKED`,
`STAGE: MOVED`, `STAGE: FAILED`.

(you) Per verdict, after `get_card` (a person's move wins: expect `develop` after
PLANNED, `plan` after QUESTIONS, either after BLOCKED or FAILED):
- PLANNED → start the card's **develop** stage at once (below): same slot, same
  worktree (its (you) steps still run: the folder exists, so only the base merge), a new
  subagent on `sonnet` with a fresh brief from a fresh `get_card`, so the posted plan and
  checklist are in it, and `set_activity(doing="developing …")` on it. No re-list in
  between and no comment needed: the plan is on the card. If a person moved the card off
  `develop` or took it meanwhile, start nothing and leave it where they put it. If the
  reply says its plan post was refused by a permission check, the card is still in
  `plan` by no one's move: note the refused write as under "Board writes that fail",
  with the plan in that comment so the next tick can make the same write, and start
  nothing. Not a git repo → failure ("no git repo"): the card sits in `develop` with its
  plan.
- BUILT (a small card built by its plan stage) → after `get_card` expect `develop`, then
  exactly the develop BUILT handling below: the BUILT check (#176), the BUILT comment with
  the `tests:` and `build check:` lines, move to `test`. No develop subagent.
- QUESTIONS → comment "open questions: answer them in the card; the next run replans
  with them".
- BLOCKED → the blocker cards (Blocked by a person, below), plus the open-questions
  comment if it posted questions.
- FAILED → failure. Posted already (also after a failed or crashed one-agent build) → the card sits
  in `develop` and a later run develops it fresh; otherwise it stays in `plan`.
- QUESTIONS, MOVED, or PLANNED/BLOCKED/FAILED (or a crash) with the card still in `plan` →
  `git -C <repo> worktree remove <tree>` (no `--force`; will not go → leave it, say so),
  and `git -C <repo> branch -d card/<id>` only if you created the branch for this run and
  `git -C <repo> rev-list --count <base>..card/<id>` is 0 (an older branch is the record).

### develop  (in the card's worktree)

A small card arrives already built (plan stage `BUILT`, Card size) and skips this stage.
Two ways in: the plan stage just ended `PLANNED` (you start it at once, above), or the
card was already in `develop` when the run found it (a person put it there, or a plan
run failed after posting). Either way it is a new subagent with `model: "sonnet"` that
builds from the posted plan in a fresh context: it has read none of the code the plan
stage read, so its brief carries the card verbatim — description, the posted `plan`, the
checklist with its done states, questions and answers, comments — as the brief rules
above require.

(you) The worktree is `<repo-parent>/<repo-name>.worktrees/card-<id>` on branch
`card/<id>`:
- folder exists → use it;
- else `git -C <repo> worktree prune`, then `git -C <repo> worktree add <tree> card/<id>`
  if the branch exists, or `git -C <repo> worktree add -b card/<id> <tree>` from the base;
- then bring it up to date: `git -C <tree> fetch -q origin <base>` (ignore failure) and
  merge `origin/<base>` (or `<base>`) in unless already an ancestor. Conflicts → tell the
  subagent to resolve and commit them first. Merge refused (dirty tree) → `merge --abort`
  and tell it the branch is behind.

The subagent implements the card following `plan` and `answers` (later comments win),
works only inside the worktree, runs the project's tests, commits on `card/<id>` — never
pushes, never switches branch — and replies with a short summary and the test result,
last line `STAGE: BUILT` (`STAGE: BLOCKED` with a `## Blocked by` section, `STAGE: FAILED`
if it could not finish). Before the verdict line (after any `## Blocked by`) its reply
ends with its `tests:` line (step 5 below) and then a `Files changed:` section, one
repo-relative path per line exactly as
`git diff --name-only --no-renames <base>...card/<id>` prints it (a renamed file is its
old and new path; `<base>` is the ref the worktree step merged, named in the brief), test
files included; a claim such as "added test_x" must name the test file there, and it
commits before it writes the list. Say so in the brief.
**Targeted tests (#192):** the subagent runs only the tests that cover what it changed; the
test stage runs the full suite. After its last commit it takes its changed files from
that same list, `git -C <tree> diff --name-only --no-renames <base>...card/<id>`, then:
1. **Docs-only:** when the test stage's docs-only check (#163, see there) holds for them, it
   runs no tests and its reply has `tests: skipped, docs-only (<files>)`.
2. Otherwise it picks the test files. If the repo's `CLAUDE.md` has a test map (changed
   files or folders → test files), it uses the map, which wins over the heuristics. With no
   map: any test file that changed; the test file named after a changed module
   (`test_<stem>.py`, `tests/test_<stem>.py`, `<stem>_test.*`, `<stem>.test.*`,
   `<stem>.spec.*`); and any test file that imports the changed module or quotes its file
   name (the #163 grep). A changed `*.md` that the docs-only check clears adds nothing.
3. It runs the **full suite instead** when a changed file that is not docs-only maps to no
   test file, when a shared fixture or config changed (`conftest.py`, the test config, the
   dependency manifest or lockfile) or the map says "full suite" for a file, when the
   project's test command cannot be narrowed to files, or when the picked files are most of
   the suite anyway (all of its test files, or all but one).
4. It runs the project's test command narrowed to those files (e.g.
   `uv run pytest -q test_core.py test_app.py`); it may run targeted tests while it
   iterates, but the run that counts is the last one, on the committed tree.
5. Its reply has exactly one of `tests: targeted (<files>): <summary>`,
   `tests: full suite (<why>): <summary>`, `tests: skipped, docs-only (<files>)`, just
   before its `Files changed:` section; the BUILT comment carries it.
Its one board write (the plan stage's post came before): as it finishes a step it
ticks that checklist item — `get_card`,
flip that item's `done`, `update_card(checklist=<the full list>, actor="claude-agent")` —
nothing else; tell it so in the brief.
(you) After `get_card` (a person's move wins): BUILT → the BUILT check below
first, then comment the summary, its `tests:` line, the `build check:` line and the
worktree path, move to
`test` (it waits for the test slot); BLOCKED → the blocker cards
(below); FAILED → failure. The worktree stays either way.

**BUILT check (#176):** before the BUILT comment and the move, run in the worktree, with
the same `<base>` ref the worktree step merged (`origin/<base>`, or `<base>` if the fetch
failed): `git -C <tree> diff --name-only --no-renames <base>...card/<id>` (the committed
files, a rename as both paths),
`git -C <tree> diff --stat <base>...card/<id>` (pasted into the comment) and
`git -C <tree> status --porcelain` (uncommitted or untracked files). Compare the reply's
`Files changed:` list with the committed files as sets, looking for: **claimed, not
committed** (the #163 case: tests said to be added, none in the commit), **committed, not
claimed**, **uncommitted** (a dirty status), and a reply with **no `Files changed:`
section**. Outcomes:
- empty diff and a clean tree (nothing was built) → failure: `agent failed: BUILT but
  card/<id> holds no changes against <base>`; the card stays in `develop`;
- any other gap → the BUILT comment gets a line `build check: MISMATCH` listing each gap
  and the `--stat`; the card still moves to `test`, and the test brief carries the same
  note (it does not fail the stage);
- no gap → the BUILT comment gets `build check: OK (<n> files)`.

### test  (in the same worktree)

(you) Same worktree steps as develop; a card in test with neither a worktree nor a
`card/<id>` branch fails ("nothing was built: move it back to develop").

The subagent (`model: "opus"`) commits anything uncommitted in the worktree on
`card/<id>`, then runs the Skill tool with skill `code-review` and args `<level> <base>...card/<id>` (a ref range: committed
work only, whatever the cwd; the level is always named, see below). It fixes the findings it agrees with on the branch, in the
worktree, and lists those it rejects in its reply with a one-line reason each. No Skill
tool, no `code-review`, or an error → it reviews `git -C <tree> diff <base>...HEAD` itself
and says so. code-review does not know the card, so it also checks the diff against the
request, plan, answers and checklist, and runs the tests in the order below (#191),
fixing what it can itself on the branch. **Docs-only skip (#163):** it skips the test run, and only the test run (the
review and the card check still happen), when, checked after its last commit, (a) every
file in `git -C <tree> diff --name-only --no-renames <base>...card/<id>` ends in `.md`
(a renamed file lists its old and new name; a page the app serves, e.g. `docs.html`,
never counts: tests fetch it without naming it) and (b) no test file (`test_*.py`,
`conftest.py`, anything under `tests/`) quotes a changed file's name or path as a
string. For each changed file, in the Bash tool (Git Bash) from the worktree:
`grep -rnE "[\"']([^\"']*[/\\])?<name>[\"']" <the test files that exist>` (`<name>` is the
file's base name, regex-escaped); exit 1 (no match) is no hit, exit 0 is a hit, and any
other exit (an error, a missing path) counts as a hit. A mention in a comment or
docstring is no hit; a doubtful hit only runs the suite, which is safe. Then its reply
has the line `tests: skipped, docs-only (<files>)`. Otherwise it runs the suite as
always. **The test stage's full run stays mandatory (#192):** whatever develop ran
(targeted, full or none), the test stage never skips or narrows its own run; its only
exception is this docs-only check. Either way its last commit comes first: the suite's last run, or the docs-only
check, is on the tree that ships (the deploy may skip its own run on that, #156). What it cannot (unfinished
work, a decision a person must make) it leaves alone.

**One suite run, a sized review (#191):** the order is: commit leftovers, sized review,
fix the findings and the card check, commit, then the one full suite run on that last
commit (or the docs-only check instead). While fixing it may run single test files, never
the full suite before its last commit. It runs the full suite again only after a failure
it then fixed (on the new commit) or after a fix it committed after the run; the run that
counts is the last, on the tree that ships (#156). The one full run stays mandatory. A
failing test is rerun unchanged (once, that test alone) only when it is a known flake:
named as a current flake in the repo's `CLAUDE.md` or `PLAN.md`, or marked so in the test
code; a flake recorded as fixed (e.g. #165) no longer counts. Any
other failure is real: fix it or `RESULT: FAIL`, no rerun and hope. The reply names any
flake rerun. The `<level>` comes from the committed diff (`git -C <tree> diff --name-only
--no-renames <base>...card/<id>` and `--shortstat`), never left to the skill's last level:
- `low`: text-only, every changed file is prose (`*.md`, `*.txt`, or a manual page with no
  script such as `docs.html`); tests, config and pages with script (`board.html`) are code;
- `medium`: code, but small, at most ~50 changed lines (insertions + deletions) in total;
- `high`: any other code diff.
Its reply has one line `review: <level> (<n> files, +<a>/-<b>, text-only|code)`.
A brief carrying a `build check: MISMATCH` note (#176) makes it verify every claim of the
develop reply (tests said to be added, results quoted) against the diff, and say in its
reply what was missing and what it did about it.
Its review names every checklist step still unticked (one skipped on purpose says why).
Last line exactly `RESULT: PASS` or `RESULT: FAIL`. Comment its reply. FAIL → failure.

PASS → (you) ship, land, deploy, clean up — in this order:
1. **Ship:** `git -C <tree> add -A`, commit `#<id> <title>` if anything is staged
   (note whether it committed, #156),
   `git -C <tree> push -q -u origin card/<id>` (env `GIT_TERMINAL_PROMPT=0`). Any git
   error → failure, card stays in test. Comment the commit pushed. If the project's
   `land` is `pr`, then in the repo: `gh pr create --base <base> --head card/<id> --title
   "#<id> <title>" --body "<short summary from the test reply>"` (a PR for the branch
   already exists → `gh pr list --head card/<id> --state open --json url -q ".[0].url"`
   instead; only an OPEN PR is reused, a closed or merged one never is (#72); no
   http(s) URL back, e.g. empty or `null` → failure, card stays in test),
   `update_card(pr=<url>)`, and comment the URL. gh missing or failing → failure, card
   stays in test.
2. **Tested tree (#156):** first, in either land mode and before any merge, run
   `git -C <repo> merge-base --is-ancestor <base> card/<id>`. The landed tree is the
   tested one only if that exits 0 **and** ship committed nothing; any other result
   (exit 1, an error, a ship commit) means it is not. With `land` `merge`, a land that is
   skipped (the repo is off `<base>` or has uncommitted changes) makes it not tested too:
   the checkout the deploy works from is not the tested tree. A conflict needs no extra
   rule: it already implies the base moved. Before that, rerun the docs-only check of the
   test stage (#163) on `<base>...card/<id>` against the base's current tests (this covers
   a ship commit and a moved base): if it holds, the verdict is **docs-only** (no suite
   ran, none is owed). Otherwise use the logic above, except that a test reply with
   `tests: skipped, docs-only` is always **not tested** (the suite never ran on this
   branch). Hand the deploy the verdict,
   **docs-only**, **tested** or **not tested**, in the deploy step.
3. **Land:** only when `land` is `merge` — with `pr`, skip it and say "landing by pull
   request" in the deploy comment. Only if the repo is on `<base>` with nothing uncommitted
   (`git status --porcelain --untracked-files=no` empty):
   `git -C <repo> merge --no-ff card/<id> -m "Merge #<id> <title>"`; a conflict →
   `merge --abort`. Say which happened (or why it was skipped) in the deploy comment.
   Do not push yet.
4. **Deploy:** per the project instructions, by the dispatcher itself when they are
   mechanical, by a subagent (`model: "haiku"`) when they need judgement (#189). Both
   paths: local only — the test backend, or an install on a connected phone (check `adb
   devices`; none → skip the phone) — never production, a public server or an app store
   unless the instructions clearly say so. Changed neither backend nor app → deploy
   nothing. Merge, push or restart services only as the instructions say; no other edits
   or commits. With `land` `pr` it never merges into the base, whatever the rules say:
   the PR does that. Last line (subagent) or decision (dispatcher) `DEPLOY: OK`,
   `DEPLOY: SKIPPED` or `DEPLOY: FAILED`. (you) Comment it headed exactly `deployed: `,
   `deploy skipped: ` or `deploy failed: ` — the board lights the purple dot from that
   head — with one line `deployed by: dispatcher (mechanical)` or `deployed by:
   subagent (<why: judgement>)`, so a later run can audit the choice. Then push the base
   if the land merged and the deploy did not already (`git -C <repo> push -q origin
   <base>`), unless its tests failed: then leave the merge unpushed and say so in the
   comment.

   **Mechanical deploy (#189):** the dispatcher decides per deploy, from the project's
   deploy instructions (repo `CLAUDE.md` and the project `instructions`), the #156
   verdict and the landed file list. A deploy is **mechanical** only when every step the
   instructions call for is one of: a git operation on the base (the land merge already
   done, `push`, listing the changed files with `git diff --name-only origin/<base>
   HEAD`); the project's test command, run only as the #156 verdict allows; a restart or
   reload command the instructions have spelled out verbatim (one or more fixed commands, each
   run as written, e.g. `powershell -NoProfile -File restart-backend.ps1 -Delay 30`);
   choosing which of those to run by matching the changed paths against a file
   classification the instructions list (named files or globs: live on disk, unused,
   needs a restart). A changed file the classification does not cover counts as
   judgement, unless the instructions name a default for any other file. Anything else
   needs **judgement**: a connected device (`adb`, a phone or emulator install), a build,
   sign or package step, a multi-step script whose output decides the next step, any
   remote or production target, credentials, instructions that are vague or contradict
   themselves, or the dispatcher being unsure. When in doubt, use the subagent: spawn it
   exactly as before (`model: "haiku"`, the brief rules with the no-chips line, the #156
   line). Missing deploy instructions are not judgement: they mean deploy nothing
   (`deploy skipped:`), no subagent. The dispatcher's own deploy applies the #156
   verdict directly (below). It runs a test command with the longest tool timeout (or in
   the background, waiting for it), its output redirected to a file; it judges the run by
   the command's own exit code (never one hidden behind a pipe such as `| tail`) and
   reads only the file's summary line, so the run's output does not fill its context. It
   treats any failing command (git, test, push, restart: a non-zero exit) as `deploy
   failed:` with the error. It runs a restart command as given and last: a delayed one
   returns at once, and it posts the deploy comment right after, before the clean-up. It
   never creates chips or schedules anything.

   **Tests in the deploy (#156).** Whoever deploys follows the verdict. For a subagent,
   besides the brief rules (with the no-chips line), put
   one line in its brief from the verdict. Docs-only: "The change is
   docs-only and no test reads it: do not run the tests, even if the deploy instructions
   say to." Tested: "The test stage ran the full suite on exactly this tree (`<base>` had
   not moved since it merged it in, and ship committed nothing): do not run the tests,
   even if the deploy instructions say to." Not tested: "`<base>` moved since the test
   stage (or ship committed changes after it, or the land was skipped): run the tests if the deploy instructions
   call for them, before any push." The dispatcher applies the same rule itself on a
   mechanical deploy. The rule only removes a test run the instructions
   call for, never adds one. The deploy comment carries exactly one `tests:` line
   (the head `deployed: ` / `deploy skipped: ` / `deploy failed: ` is unchanged):
   `tests: skipped, docs-only` (docs-only, the rules call for tests);
   `tests: run in test stage, skipped` (tested, the rules call for tests);
   `tests: <count> passed`, e.g. `tests: 538 passed` (not tested, tests ran; a failing run
   is `tests: <the summary line>`, no push, `DEPLOY: FAILED`); `tests: none in the deploy
   rules` (the rules name no test run, any verdict). A `DEPLOY: SKIPPED` deploy has the
   line too (a deploy may merge, test and push yet skip the rest, as the Tickets repo's
   does for a docs-only change, #160). If a subagent's reply lacks the line, add it to the comment yourself
   from the verdict (`tests: skipped, docs-only` when docs-only, `tests: run in test stage, skipped` when tested, `tests: not
   reported` when not tested), so the comment always says which.
5. **Merged dot:** `update_card(merged=True)` if `git -C <repo> branch --merged <base>
   --list card/<id>` lists it. Not with `pr`: the queue step sets it once the PR merges.
6. **Clean up:** `git -C <repo> worktree remove --force <tree>`; if it will not go, say
   so in a comment (not a failure). The branch stays: it is the record.
7. Move to `verify`.

A deploy that restarts the Tickets backend itself cuts the MCP connection: if the
tools stop answering, wait until `GET /api/projects` answers again (start it as in 0 if
it does not within a minute) and carry on over HTTP, as in 0.

### Blocked by a person

Any stage may end with a `## Blocked by` section (before the questions and the verdict
line): one bullet per task only a person can do — a credential, an account, a service
to enable, a device to plug in — with details indented under it. Ask for it in every
brief. (you) For each bullet whose title is not already a card in the project:
`create_card(lane="todo", project=…, title=…, description=details + "(blocks #<id>)")`
and `link_cards(from_id=<new>, to_id=<id>, kind="blocks")`; comment the new ids on the
card and leave it in its lane. It will be skipped until they reach `verify` or `done`
(in a `pr` project: `done`, since a card there with no PR is not finished in `verify`).

### Failure

Anything that stops a stage (a crash, an empty plan, RESULT: FAIL, a git error, no git
repo for develop/test): `comment("agent failed: <why>")`, leave the lane as it is; its
slot is free for the next card. A plan run that fails after posting its plan, or a
develop stage that fails after the plan stage moved the card, leaves the card in
`develop`; a later run develops it in a fresh subagent.

## 4. Wrap up

Once, when the queue is empty and the last card in flight is done: clear your activity.
Then:
- **Ideas:** things the runs noticed but did not do — follow-ups, tech debt, a missing
  test, a next step the plan deferred. One `todo` card each (skip titles already on the
  board), description ending "(idea from #<id>)". Only real ones; none is fine.
- **Summary** in the chat: moved to verify (with deploy result), waiting for answers,
  blocked (and by what), failed (and why), skipped as assigned, ideas opened, and
  **refused writes**: each board write a permission check refused, once, with the card,
  the write, and the permission rule that would let it through (it is retried next tick),
  and any background-task chip a stage made by mistake (#159), and one line when the
  project's `Cards in flight:` value was invalid and the run fell back to 2 (#193).
  The summary reports; it never ends "waiting on you".
