# Tickets: one run over this project's board

`/tickets-start` reads this file and follows it, once per loop tick or once, handing you the
**session id**, the **Tickets repo** (the folder holding `restart-backend.ps1`) and optionally
a **card id**. Rules only: reasons and history are in the Tickets repo's PLAN.md.
One run takes every ready card of this folder's project, up to the in-flight limit (Slots) at a
time, to `verify`, writing each step onto the board, and ends with ideas. Work unattended: during a
run never ask the user (no `AskUserQuestion`, no chat question) or wait for an answer. What needs
a person goes on the board and the run moves on: a question you would ask, or a stage reply's
question in place of its verdict line, becomes a comment plus `questions` (plan) or a
`## Blocked by` card; a reply with neither is a failure. A tick never ends "waiting on you". Every board write uses `actor="claude-agent"`; the tools are the `tickets` MCP
server's.

## 0. Board up, tools or not

- `GET http://127.0.0.1:8123/api/projects` does not answer → start it detached:
  `powershell -NoProfile -File <tickets repo>/restart-backend.ps1 -Delay 1`; wait ~12 s and ask
  again. Still down → stop, with the tail of `restart-backend.log`.
- The `tickets` tools are missing → do the whole run over the HTTP API, same calls, same
  arguments:

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

  UTF-8: in PowerShell `Invoke-RestMethod "<url>" -Method Patch -ContentType
  'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes(($fields |
  ConvertTo-Json -Depth 6)))`. Subagents posting a plan or ticking the checklist get them too.
  The summary says the run used HTTP and that `/mcp` (or a new session) brings the tools back.

### Lean board calls (#186)

You, the dispatcher, read and write the board over its HTTP API and print only the fields you
need. Every command starts `Invoke-RestMethod "http://127.0.0.1:8123/…"` (URL quoted and
first, so the allow rule `PowerShell(Invoke-RestMethod "http://127.0.0.1:8123/:*)` matches);
each call is a fresh shell:
- **check** (lane, assignee, labels before a stage, a person's move, the land fields):
  `Invoke-RestMethod "http://127.0.0.1:8123/api/cards/<id>" | Select-Object
  id,lane,assignee,labels,merged,pr,blocked_by`; a linked card for a brief likewise with `id,title,lane`;
- **queue** (step 2): `Invoke-RestMethod "http://127.0.0.1:8123/api/cards?project=<name>"
  | ForEach-Object { $_ } | Where-Object lane -in 'plan','develop','test' | Select-Object
  id,title,lane,assignee,session,stalled,labels,blocked_by,@{n='questions';e={-not
  [string]::IsNullOrWhiteSpace($_.questions)}},@{n='answers';e={-not
  [string]::IsNullOrWhiteSpace($_.answers)}}` (keep the `ForEach-Object { $_ }`: it unrolls the
  array); `verify` cards for the PR catch-up with `id,pr,merged`;
- **writes** (assign and `session`, lane moves, unassign, `merged`, `deployed`, `pr`,
  comments): the UTF-8 call of §0 with the URL first (`Invoke-RestMethod
  "http://127.0.0.1:8123/api/cards/<id>" -Method Patch …`, a comment `…/comment" -Method Post
  …`) and `"actor": "claude-agent"`, piped to `| Select-Object id,lane,assignee` (a comment to
  `| Select-Object id`);
- **the refused-write check** (step 2): `Invoke-RestMethod
  "http://127.0.0.1:8123/api/cards/<id>" | ForEach-Object { $_.events } | Where-Object kind -in
  'comment','moved' | Select-Object -Last 3 kind,actor,at,@{n='to';e={$_.detail.to}},@{n='text';e={$t
  = [string]$_.detail.text; $t.Substring(0, [Math]::Min(300, $t.Length))}}` (a `moved` after
  the note makes it stale). A note that is a write to repeat (a refused plan post carries the
  plan) is read whole before the write: `… | ForEach-Object { $_.events } | Where-Object kind -eq
  'comment' | Select-Object -Last 1 | ForEach-Object { $_.detail.text }`, never re-posted from
  the cut.
- **a full card only to build a brief**: the `get_card` right before spawning a stage (step 3),
  MCP `get_card` or `Invoke-RestMethod "http://127.0.0.1:8123/api/cards/<id>" | ConvertTo-Json
  -Depth 8` (never the bare object, whose default view cuts lists to 4 items).

The MCP tools stay for stage subagents and for a session without PowerShell (the summary says so). Their writes are slim (#203): `update_card` and `comment`
take `slim=True`, which answers only `id`, `lane` and `assignee`; reads stay whole. HTTP is
your first route: a write it refused by a permission check is never retried through the MCP
tool (below), and the summary names the `PowerShell(…)` rule.

### Board writes that fail (#147)

- **Transient** (any error that is not a permission denial; a denial says so, e.g.
  "permission denied" or "blocked by the permission check"): the tools vanished, an HTTP
  error, a timeout, the backend restarting → retry the same write a few times, a few seconds
  apart, over HTTP if the tool failed. Still failing → handle it like a refused write.
- **Refused by a permission check** → never retry it in another form or through another tool
  (no tool → HTTP, no `curl` → PowerShell, no splitting it up). Instead:
  1. note it on the card if you can: `comment("board write refused by a permission check:
     <the write, e.g. move to test>; retried next tick")` (refused too → the summary names it);
  2. carry on: the card is done for this run, its slot frees for the next ready card; a note
     not posted stays in your session notes and the summary;
  3. next tick, try the **same** write first: a card still in its old lane whose latest
     `claude-agent` comment is such a note, with no human comment or lane change after it,
     gets that write (e.g. the move to `test` of a card whose BUILT summary is posted), not a
     new stage;
  4. each tick's summary names each refused write **once**, with the permission rule that
     would let it through (e.g. allow `mcp__tickets__update_card`, or the exact `Bash(…)` /
     `PowerShell(…)` command), for `/permissions`; a retry refused again is one short line.
     A tool-permission prompt that held the run is named too.

Subagents follow it too; you note their refused write on the card and retry it next tick.

## 1. Find the project

`list_projects`, and take the one whose `path` is the current working directory (compare
case-insensitively, `\` and `/` alike, no trailing slash). None → stop and say: open the board,
"projects…", set this project's path to this folder.

Priority, highest first: the card's latest comments, the card (`description`, `plan`,
`answers`), the project `instructions`, the repo's `CLAUDE.md`, this file. The in-flight limit
(Slots) is read from the project `instructions` only, never from a card.

The **repo** is the git toplevel of the path; the **base** branch is its current branch
(`git -C <repo> rev-parse --abbrev-ref HEAD`). Not a git repo → cards are planned but fail in develop/test.

### Writing card descriptions (#731)

Write descriptions for the owner: plain language explaining what is wanted, why it matters
and what the result should look like. This applies to agent-created ideas and blocker cards
as well as a rewrite the owner requests. Preserve the owner's intent and constraints; do not
rewrite an existing description unless asked. Keep implementation steps, file paths, test
commands and agent handoff instructions in the existing `plan` field or stage comments.
Include technical terms in a description only when the owner needs them to understand or
decide the request. The separate plan already carries the agent's detail; no hidden field is
needed. Continue passing both description and plan verbatim in stage briefs.

## 2. Build the queue

`list_cards(project=…)` (the trimmed queue call, Lean board calls): the cards in `plan`,
`develop`, `test` (with a card id: just that card, if in those lanes). Order: `plan`, then
`develop`, then `test`; within a lane, the board's order. Skip, and name in the summary:
- **waiting for answers:** `questions` filled, `answers` empty;
- **blocked:** `blocked_by` not empty (the server lists unfinished cards with a `blocks` link
  to this one), or a label equal to `blocked` (case-insensitive). Leave the card untouched
  in its current lane until both holds clear; removing a label alone does not clear linked blockers;
- **assigned to a person:** `assignee` set to anyone but `claude-agent`;
- **already in flight:** this run holds it (a stage agent here has it, or it waits for a slot);
- **in flight elsewhere (#736):** `assignee` `claude-agent` with a `session` that is not this
  run's and `stalled` false: another session (a `/tickets-plan` run, another dispatcher) has it;
- **released by an API error** (Failure) until its named reset time, else 30 minutes after the
  comment: its latest `claude-agent` comment starts `api error:` (read with the trimmed
  refused-write check before starting an unassigned card).

A **stalled** card (`stalled` true: `claude-agent`'s, quiet 30 minutes) that passes the queue
checks above and that this run does not
hold lost its agent. It is taken over, not skipped (#493): comment `stalled: taking over from session <old>: <what is
re-run>`, set `session` to this run's, then work its lane on the existing worktree and branch:
plan and develop get a new card session, which carries on from what `card/<id>` already holds;
test starts from the committed state (`git -C <tree> status --porcelain` clean, else commit
leftovers as the stage does and say so) and runs the full suite.
Never a card a person has, and once per run.

Re-list whenever a slot frees. A card session and the test, ship and land after it are one
piece of work; otherwise never work a card twice in one run: once it fails or waits, it is
done for this run. A card
with a live refused-write note (the trimmed refused-write check) gets that same write first, not a new stage.

Before working the queue, catch up on pull requests: each `verify` card with `pr` set and
`merged` false, `gh pr view <pr> --json state -q .state`; `MERGED` → `update_card(merged=True)`.
Failures here are ignored.

## 3. Work the cards, several at once

You are the dispatcher. Each card gets **one card session (#736)**: one **background**
subagent (Agent tool, `run_in_background: true`) that does all the card's remaining work in
one context. A `plan` card's session plans, posts and, with no open questions, builds and
checks it (stage `plan+build`); a `develop` card's session builds and checks it (stage
`build`). Checking is the test rules below, run by the same session after its build. A card
found in `test` gets a fresh test subagent (stage `test`), unless its session's check already
covers the branch head (under test). When a subagent finishes, do that card's (you) steps, or
fill the freed slot from a fresh re-list. The exception is a mechanical deploy, which you
run (step 4, #189). Never continue a finished subagent (no SendMessage).

**Stage agent (#188).** Every stage's Agent call uses `subagent_type: "tickets-stage"` when
that type is in the Agent tool's list of available agent types, else `general-purpose`. A call
refused for an unknown agent type is retried once as `general-purpose`, same brief and model.
When the fallback was used, the run's summary says so once: "tickets-stage agent not installed or not loaded:
`/tickets-start` in a new session".

**Model per stage (#83, #736).** Each Agent call sets its model:
- card session (`plan+build`, `build`): `model: "opus"` for a card labelled `large` (any case;
  both labels count as `large`); `small` or no label → `model: "sonnet"`, for the whole
  session. Not a git repo → `opus`, and it only plans.
- test (a card found in `test`, or a check to redo): `model: "opus"`.
- deploy: none when mechanical (you run it); a judgement deploy's subagent `model: "haiku"`.

**Card size (#190):** the labels alone pick the session's model. Labels match
case-insensitively; both labels → `large`. An unlabelled card is judged by the session after
reading the code, before it posts, for the plan's first line only: small when the plan has at
most 5 steps, no open questions and no `## Blocked by`, and the change touches at most ~5
files (PLAN.md, docs and tests included), adds no new module, and changes no database schema,
HTTP route or MCP tool. Anything else is large. When unsure: large. The line says it:
`Size: small (label)`, `Size: small (judged: 3 steps, 2 files)` or `Size: large (…)`; a judged
size never changes the model of a running session (`/tickets-plan` labels a card it judges
large, so its session runs on Opus).

**Slots.** At most **2 cards in flight** by default (#193). A project changes it with a line
`Cards in flight: N` in its `instructions` (N a whole number from 1 up; case-insensitive, the
first match wins, no cap); a missing, non-numeric or below-1 value falls back to 2 and the
summary says so in one line. Card
sessions run side by side within that limit, each in its card's own worktree, its check's
suite run included (a project whose tests share a port or a device sets `Cards in flight: 1`).
A card keeps its slot while it waits for the test slot. At most **one card in `test`** at a
time, from a test stage (or, after a session's PASS, from ship) through land, deploy and
clean-up to `verify`; the next waits. Land merges and pushes of
the base are yours, one card at a time. Never use the Agent tool's `isolation: "worktree"`.

Before starting each stage, including stalled takeover and cards waiting between stages,
re-read lane, assignee, labels and `blocked_by`. A non-empty `blocked_by` or a case-insensitive
`blocked` label holds the card: start nothing, leave its lane, assignment and session untouched,
and make no takeover comment. If held between stages, keep its worktree and stop advancing it
for this run. A stale agent assignment never overrides either hold. Once both holds clear,
apply the other queue readiness checks again; a person's move or assignment still wins.

Starting a card: `update_card(assignee="claude-agent", session="<session id>")`, all your
reads and writes through the trimmed calls (§0). `set_activity` holds one entry for you: the
card you started last with the count, `set_activity(card_id=…, doing="developing (2 in
flight)")` (`planning and building` for a `plan+build` session, `developing` for `build`,
`planning` when it only plans, `testing`, `landing`); repeat it at every wake while a stage runs (heartbeat, #502); clear it when the last card leaves your hands. Before every board write after a
stage, check the card again (the trimmed check): **a person's move wins**: if the lane changed
under you (other than by your own move or a session's move to `develop`), post your
output as a comment saying it was moved during the run, and leave the card there.

**The brief is the subagent's whole world**: paste everything in; never summarize the
person's words, never say "see the card". `get_card` right before spawning (your one
full-card read per stage). Its first line is exactly `Tickets stage: <stage> #<id> (<model>)`
(`plan`, `plan+build`, `build`, `test` or `deploy`; e.g. `Tickets stage: build #12 (sonnet)`):
the token report (`skill-start/usage_report.py` in the Tickets repo) counts each card's
stages by it. Then include, each under its own heading:
- the card: `#<id> <title>`, lane, labels; the description, plan, questions and answers
  **verbatim**; the checklist with each item's done state; links (parent, blocks, blocked by)
  with the linked cards' titles and lanes; every comment in order with author and time, but
  not a comment's `output`;
- the project: name, instructions verbatim, `land`, the repo path and base branch, and that
  the repo's `CLAUDE.md` holds its rules (read it first) and that the stage reads long docs
  such as `PLAN.md` and `STATUS.md` only in the sections its change touches, as that
  `CLAUDE.md` says (#215);
- where to work: the folder (absolute path; use absolute paths or `git -C`), and for a card
  session or test stage the worktree path and the `card/<id>` branch;
- where not to write (#204): a card session or test stage never writes in the base checkout
  `<repo>` (no edits, no scratch files, no git command that changes it; it may read it); every
  stage keeps scratch files only in `<scratchpad>/card-<id>-<stage>` (`plan`, `build` for a
  card session, `test`, `deploy`; your session's scratchpad, else `<system temp>/tickets-<session id>`),
  which it creates: one folder per stage, so scratch scripts never overwrite each other. Its
  reply reports the output of
  `git -C <repo> status --porcelain` (empty is good) before any `tests:` line, `Files changed:`
  section and the verdict line, which stay last;
- the stage's rules below, the `## Blocked by` request, and the verdict line it must end with;
- that a board write refused by a permission check is never retried in another form or
  through another tool: it reports the refused write in its reply and carries on;
- that it never creates background-task chips (no `spawn_task`) and never schedules anything,
  and runs unattended and never asks the user in chat: a question for a person goes in
  `## Blocked by` (plan: `## Open questions`) and the reply still ends with its verdict line. A chip made by mistake is dismissed at once (`dismiss_task`) and
  named in the reply; you note it on the card and in the summary;
- that its board writes (`update_card`, `comment`) pass `slim=True` (#203), so the reply is
  only the card's `id`, `lane` and `assignee`, not the whole card; it reads the card with
  `get_card` when it needs it. Every stage brief carries this line too.

You, not the subagent, write to the board and run the git steps marked (you); the exceptions
are a session posting its plan (and moving the card to `develop`) and ticking checklist items
as it builds.

On a card's way out: `update_card(assignee="")` unless a person had it before you.

**Base checkout check (#204).** In a git repo, after every stage reply (any verdict, a crash
too), before that card's other steps (the trimmed check, the verdict's writes, a worktree
clean-up, ship and land), run `git -C <repo> status --porcelain --untracked-files=no`; the
land runs this check once more when it finds the checkout dirty. Empty → nothing to do.
Otherwise each listed path (both paths of a rename) is a **stray copy** only when all three
hold, tried against each card in flight in this run, the replying card first:
1. `git -C <repo> diff --name-only --no-renames <base>...card/<id>` lists it;
2. `git -C <repo> diff --quiet card/<id> -- <path>` exits 0 (a file deleted on both counts);
3. `git -C <repo> diff --cached --quiet HEAD -- <path>` or
   `git -C <repo> diff --cached --quiet card/<id> -- <path>` exits 0.

Stray copies are reverted at once, without asking:
`git -C <repo> restore --source=HEAD --staged --worktree -- <paths>`, then comment on the card
whose branch matched `base checkout: reverted stray copies of card/<id>: <paths>`. Any other
path is never touched: comment on the replying card `base checkout: uncommitted changes that
are not a copy of a card's committed edits, left alone: <their status lines>`, each path once
per run, and the summary names them. A refused revert follows "Board writes that fail".
Untracked files are not checked; an untracked copy of a file the card adds makes the land
fail with nothing merged, and the deploy comment says so.

### plan  (a `plan` card's session, in the card's worktree)

(you) `git -C <repo> pull --ff-only` first (fails → tell the subagent the code may be behind). Set up the card's worktree as under develop, before spawning; note whether
`card/<id>` already existed. Not a git repo → no pull, no worktree: it only plans, in the
folder. Its model: Model per stage. Its brief (stage `plan+build`) carries the card's labels,
the Card size rule, the plan rules here, the develop rules and the test rules (its check).
A plan-only brief (`/tickets-plan`, or no git repo) carries the plan rules only.

**Plan.** The subagent reads code in the worktree, changes nothing until the plan is posted
(beyond resolving and committing the base merge's conflicts if told to), and writes the
complete plan in markdown: first line the `Size:` line (Card size), a Context section
restating the request. A card that already has `questions` and `answers`: build the answers
in, do not ask again. A `## Steps` section: a numbered list of concrete steps, at most ~10,
one line each. Anything needing a person's decision goes in a last `## Open questions`
section, numbered 1., 2., …; a question with sensible choices lists 2–4 of them as indented
`- ` bullets, the recommended one first and suffixed ` (recommended)`; otherwise free text.
A choice is never a top-level line: the board numbers top-level lines as questions and
offers radios only for the indented `- ` bullets under one (#432).
Last line `QUESTIONS: NONE` or `QUESTIONS: OPEN`.

**Post.** The subagent writes the plan to the board itself (`actor="claude-agent"`):
- `get_card` first: **a person's move wins**: lane no longer `plan` or assignee no longer
  `claude-agent` → `comment(…, slim=True)` the plan saying the card was moved during the run,
  end `STAGE: MOVED`.
- Cut the verdict line and the `## Open questions` section off; empty plan → post nothing,
  end `STAGE: FAILED`. The steps become the checklist: the existing items as they are, plus
  one `{"text": <step>, "done": false}` per step whose text is not already on it.
- OPEN → `update_card(plan=…, questions=<the cut section>, checklist=…, slim=True)`, then
  `get_card`: a choice that came out as a numbered question of its own, not an indented
  bullet under one → `update_card(questions=<the section with it indented>, slim=True)`
  (#432); end `STAGE: QUESTIONS`.
- A `## Blocked by` → `update_card(plan=…, checklist=…, slim=True)` (plus `questions=…` if
  OPEN), lane unchanged, end with the section and `STAGE: BLOCKED`.
- NONE → `update_card(plan=…, checklist=…, lane="develop", slim=True)`. A plan-only brief ends
  `STAGE: PLANNED`. Otherwise it goes on to **build in the same context** following the develop
  rules, ticking checklist items as it goes, then checks its build following the test rules;
  its reply is the plan followed by the session reply (under develop), last line `RESULT: PASS`
  or `RESULT: FAIL` (`STAGE: BLOCKED` with a `## Blocked by`, `STAGE: FAILED` if the build
  could not finish after the post).

It **builds nothing** before the plan is posted with NONE, and nothing at all from a plan-only
brief: no code edits, no commits of its own work, no checklist ticks. Its reply is the plan,
last line one of `RESULT: PASS`, `RESULT: FAIL`, `STAGE: PLANNED`, `STAGE: QUESTIONS`,
`STAGE: BLOCKED`, `STAGE: MOVED`, `STAGE: FAILED`.

(you) Per verdict, after the trimmed check (a person's move wins: expect `develop` after
PLANNED, PASS or FAIL, `plan` after QUESTIONS, either after BLOCKED or FAILED):
- PASS or FAIL → exactly the session handling under develop (the BUILT check, #176).
- PLANNED (only without a git repo here) → failure ("no git repo"): the card sits in
  `develop` with its plan. The reply says its plan post was refused by a permission check →
  note the refused write ("Board writes that fail") with the plan in that comment.
- QUESTIONS → comment "open questions: answer them in the card; the next run replans with
  them".
- BLOCKED → the blocker cards (Blocked by a person), plus the open-questions comment if it
  posted questions.
- FAILED → failure. Posted already (also a failed or crashed build after the post) → the card
  sits in `develop` for a later run; otherwise it stays in `plan`.
- QUESTIONS, MOVED, or BLOCKED/FAILED (or a crash) with the card still in `plan` →
  `git -C <repo> worktree remove <tree>` (no `--force`; will not go → leave it, say so), and
  `git -C <repo> branch -d card/<id>` only if you created the branch for this run and
  `git -C <repo> rev-list --count <base>..card/<id>` is 0.

### develop  (a `develop` card's session, in the card's worktree)

A card already in `develop` (planned by `/tickets-plan`, an earlier run, or a person) gets a
card session, stage `build`, its model per Model per stage: it builds from the posted plan,
then checks its build by the test rules (under test), in one context; its brief carries the
card verbatim (brief rules), the develop rules and the test rules. A `plan+build` session
follows the same rules once its plan is posted.

(you) The worktree is `<repo-parent>/<repo-name>.worktrees/card-<id>` on branch `card/<id>`:
- folder exists → use it;
- else `git -C <repo> worktree prune`, then `git -C <repo> worktree add <tree> card/<id>` if
  the branch exists, or `git -C <repo> worktree add -b card/<id> <tree>` from the base;
- then `git -C <tree> fetch -q origin <base>` (ignore failure) and merge `origin/<base>` (or
  `<base>`) in unless already an ancestor. Conflicts → tell the subagent to resolve and commit
  them first. Merge refused (dirty tree) → `merge --abort` and tell it the branch is behind.

The session implements the card following `plan` and `answers` (later comments win), works
only inside the worktree, commits on `card/<id>` (never pushes, never switches branch), then
checks its own build by the test rules: the sized `code-review` pass as a separate step over
the committed diff, the card check, the one full suite run on its last commit. It replies with
a short summary and the check's findings, last line `RESULT: PASS` or `RESULT: FAIL`
(`STAGE: BLOCKED` with a `## Blocked by`, `STAGE: FAILED` if it could not finish the build).
Before the verdict line (after any `## Blocked by`) its reply ends with its `review:` line, its
`tests:` line, a line `tested commit: <sha>` (`git -C <tree> rev-parse HEAD` of the commit its
counted suite run or docs-only check was on) and then a `Files changed:` section, one
repo-relative path per line exactly as `git diff --name-only --no-renames <base>...card/<id>`
prints it (a rename is its old and new path; `<base>` is the ref the worktree step merged,
named in the brief), test files included (a claim such as "added test_x" names the test file
there); it commits before it writes the list.
**Targeted tests (#192):** while building, the session runs only the tests that cover what it
changed, never the full suite; its check's one suite run (#191) comes after its last commit.
It takes its changed files from `git -C <tree> diff --name-only --no-renames
<base>...card/<id>`, then:
1. **Docs-only:** the test stage's docs-only check (#163) holds → no tests while building; the
   check's reply line is `tests: skipped, docs-only (<files>)`.
2. Otherwise it picks the test files: the repo `CLAUDE.md` test map, if any, wins; with no
   map: any changed test file; the test file named after a changed module (`test_<stem>.py`,
   `tests/test_<stem>.py`, `<stem>_test.*`, `<stem>.test.*`, `<stem>.spec.*`); any test file
   that imports the changed module or quotes its file name (the #163 grep). A changed `*.md`
   the docs-only check clears adds nothing.
3. It runs no test while building, leaving it to the check's **full suite instead**, when a
   changed file that is not docs-only maps to no test file, a shared fixture or config changed
   (`conftest.py`, the test config, the dependency manifest or lockfile) or the map says "full
   suite", the test command cannot be narrowed to files, or the picked files are most of the
   suite anyway (all of its test files, or all but one).
4. It runs the test command narrowed to those files (e.g. `uv run pytest -q test_core.py
   test_app.py`).
5. A test stage given a develop reply from an older run reads its `tests: targeted (<files>):
   <summary>` or `tests: full suite (<why>): <summary>` line as that run's; a session's own
   reply carries only its check's line.
Its one board write while building (a `plan+build` session's post came before): as it finishes a step it ticks that
checklist item: `get_card`, flip that item's `done`, `update_card(checklist=<the full list>,
actor="claude-agent", slim=True)`; nothing else.
(you) After the trimmed check (a person's move wins): PASS or FAIL → the BUILT check below first, then
comment the reply (summary, findings, `review:`, `tests:` and `tested commit:` lines), the
`build check:` line and the worktree path. Then:
- PASS → move to `test` (it waits for the test slot). It goes straight to ship (step 1) when
  the build check is OK, `git -C <tree> status --porcelain` is clean and `tested commit:`
  equals `git -C <repo> rev-parse card/<id>`; otherwise (a MISMATCH, a missing or stale
  `tested commit:`) it gets a fresh test stage, whose brief carries the note.
- FAIL → failure; the card stays in `develop` for a later run.
BLOCKED → the blocker cards; FAILED → failure. The worktree stays either way.

**BUILT check (#176):** before the BUILT comment and the move, with the `<base>` ref the
worktree step merged: `git -C <tree> diff --name-only --no-renames <base>...card/<id>`,
`git -C <tree> diff --stat <base>...card/<id>` (pasted into the comment) and
`git -C <tree> status --porcelain`. Compare the reply's `Files changed:` with the committed
files as sets: **claimed, not committed**, **committed, not claimed**, **uncommitted** (a dirty
status), and **no `Files changed:` section**. Outcomes:
- empty diff and a clean tree → failure: `agent failed: BUILT but card/<id> holds no changes
  against <base>`; the card stays in `develop`;
- any other gap → the BUILT comment gets `build check: MISMATCH` listing each gap and the
  `--stat`; the card still moves to `test`, and the test brief carries the same note;
- no gap → the BUILT comment gets `build check: OK (<n> files)`.

### test  (in the same worktree)

These rules are a card session's check after its build, and a test stage's whole job. A test
stage (a fresh `model: "opus"` subagent, stage `test`) runs for a card found in `test`, and
for a session's PASS whose check does not cover the branch head (under develop). A card found
in `test` whose latest PASS comment's `tested commit:` equals `git -C <repo> rev-parse
card/<id>`, with a clean worktree and `build check: OK`, skips it (and the worktree steps
below, whose merge would move the head) and goes to ship.

(you) Same worktree steps as develop; neither a worktree nor a `card/<id>` branch → failure
("nothing was built: move it back to develop").

The checker (the session, or the test subagent) commits anything uncommitted in the worktree on `card/<id>`,
then runs the Skill tool with skill `code-review` and args `<level> <base>...card/<id>` (the
level always named, below). It fixes the findings it agrees with on the branch and lists those
it rejects with a one-line reason each. No Skill tool, no `code-review`, or an error → it
reviews `git -C <tree> diff <base>...HEAD` itself and says so. It also checks the diff against
the request, plan, answers and checklist, and runs the tests in the order below (#191), fixing
what it can on the branch; what it cannot (unfinished work, a person's decision) it leaves.
**Docs-only skip (#163):** it skips the test run, and only that (the review and card check
still happen), when, after its last commit, (a) every file in `git -C <tree> diff --name-only
--no-renames <base>...card/<id>` ends in `.md` (a rename lists both names; a page the app
serves, e.g. `docs.html`, never counts) and (b) no test file (`test_*.py`, `conftest.py`,
anything under `tests/`) quotes a changed file's name or path as a string. For each changed
file, in the Bash tool (Git Bash) from the worktree:
`grep -rnE "[\"']([^\"']*[/\\])?<name>[\"']" <the test files that exist>` (`<name>` the base
name, regex-escaped); exit 1 is no hit, exit 0 is a hit, and any other exit (an error, a missing
path) counts as a hit. A mention in a comment or docstring is no hit. Then its reply has
`tests: skipped, docs-only (<files>)`. **The check's full run stays mandatory (#192):**
whatever ran while building, it never skips or narrows its own run except by this docs-only check,
and that run (or the check) is on its last commit.

**One suite run, a sized review (#191):** the order is: commit leftovers, sized review, fix the
findings and the card check, commit, then the one full suite run on that last commit (or the
docs-only check instead). While fixing it may run single test files, never the full suite
before its last commit. It runs the full suite again only after a failure it then fixed or a
fix it committed after the run; the run that counts is the last. A failing test is rerun
unchanged (once, alone) only when it is a known flake: named as a current flake in the repo's
`CLAUDE.md`, `PLAN.md` or `STATUS.md`, or marked so in the test code; a flake recorded as fixed (e.g. #165) no longer counts. Any other failure is real: fix it or `RESULT: FAIL`. The reply names any flake rerun. The `<level>` comes from the committed diff (`git -C
<tree> diff --numstat --no-renames <base>...card/<id>`). Prose counts as text (#217): lines of
`*.md`, `*.txt` and a manual page with no script such as `docs.html` do not count toward the
level, so only the code lines set it; tests, config and pages with script (`board.html`) are code,
and a binary file (`-` in `--numstat`, e.g. `docs-card.png`) is code with 0 lines:
- `low`: text-only, every changed file is prose;
- `medium`: code, but small, at most ~50 changed code lines (insertions + deletions, prose
  files left out);
- `high`: any other code diff.
Its reply has one line `review: <level> (<n> files, +<a>/-<b>, text-only|code)`: `<n>` all
changed files, `+<a>/-<b>` the code lines.
A brief carrying a `build check: MISMATCH` note (#176) makes it verify every claim of the
develop reply against the diff and say what was missing and what it did about it.
Its review names every checklist step still unticked. Its reply has the `tested commit: <sha>`
line too.
Last line exactly `RESULT: PASS` or `RESULT: FAIL`. (you) A test stage's reply: comment it;
FAIL → failure, the card stays in `test`. A session's reply: under develop.

PASS → (you) ship, land, deploy, clean up, in this order:
1. **Ship:** `git -C <tree> add -A`, commit `#<id> <title>` if anything is staged (note whether
   it committed), `git -C <tree> push -q -u origin card/<id>` (env `GIT_TERMINAL_PROMPT=0`).
   Any git error → failure, card stays in test. Comment the commit pushed. `land` is `pr` →
   in the repo `gh pr create --base <base> --head card/<id> --title "#<id> <title>" --body
   "<short summary from the test reply>"` (a PR for the branch exists → `gh pr list --head
   card/<id> --state open --json url -q ".[0].url"`; only an OPEN PR is reused; no http(s) URL
   back → failure, card stays in test), `update_card(pr=<url>)`, comment the URL. gh missing or
   failing → failure, card stays in test.
2. **Tested tree (#156):** first, in either land mode and before any merge, rerun the test
   stage's docs-only check (#163) on `<base>...card/<id>` against the base's current tests:
   if it holds, the verdict is **docs-only**. Otherwise run, also before any merge,
   `git -C <repo> merge-base --is-ancestor <base> card/<id>`: the
   verdict is **tested** only if it exits 0 **and** ship committed nothing **and** (with `land`
   `merge`) the land is not skipped (the repo off `<base>` or dirty); anything else is **not
   tested**, and a test reply with `tests: skipped, docs-only` is always **not tested**. Hand
   the verdict to the deploy.
3. **Land:** only when `land` is `merge` (with `pr`, skip it and say "landing by pull request"
   in the deploy comment). Only if the repo is on `<base>` with nothing uncommitted (`git
   status --porcelain --untracked-files=no` empty; not empty → run the Base checkout check
   (#204) once more, then look again): `git -C <repo> merge --no-ff card/<id> -m "Merge #<id>
   <title>"`; a conflict → `merge --abort`. Say which happened (or why skipped) in the deploy
   comment. Do not push yet.
4. **Deploy:** per the project instructions, by you when they are mechanical, by a subagent
   (`model: "haiku"`) when they need judgement (#189). Local only (the test backend, or an
   install on a connected phone: check `adb devices`, none → skip the phone), never production,
   a public server or an app store unless the instructions clearly say so. Changed neither
   backend nor app → deploy nothing. Merge, push or restart only as the instructions say; no
   other edits or commits. With `land` `pr` it never merges into the base, whatever the
   rules say: the PR does that. Last line
   (subagent) or decision (you) `DEPLOY: OK`, `DEPLOY: SKIPPED` or `DEPLOY: FAILED`. (you)
   Comment it headed exactly `deployed: `, `deploy skipped: ` or `deploy failed: ` (the board
   lights the purple dot from that head), with one line `deployed by: dispatcher (mechanical)`
   or `deployed by: subagent (<why: judgement>)`. Then push the base if the land merged and the
   deploy did not (`git -C <repo> push -q origin <base>`), unless its tests failed: then leave
   the merge unpushed and say so.

   **Mechanical deploy (#189):** decide per deploy from the deploy instructions (repo
   `CLAUDE.md` and project `instructions`), the #156 verdict and the landed file list. It is
   **mechanical** only when every step is one of: a git operation on the base (the land merge,
   `push`, `git diff --name-only origin/<base> HEAD`); the test command, as the #156 verdict
   allows; a restart or reload command spelled out verbatim in the instructions, run as
   written (e.g. `powershell -NoProfile -File restart-backend.ps1 -Delay 30`); choosing which of
   those to run by matching the changed paths against a file classification the instructions
   list (live on disk, unused, needs a restart). A changed file the classification does not
   cover is judgement, unless the instructions name a default. Anything else needs
   **judgement**: a connected device (`adb`, a phone or emulator), a build, sign or package
   step, a multi-step script whose output decides the next step, a remote or production
   target, credentials, vague or contradictory instructions, or you being unsure. When in
   doubt, use the subagent (`model: "haiku"`, the brief rules with the no-chips line, the #156
   line). Missing deploy instructions are not judgement: deploy nothing (`deploy skipped:`), no subagent. Your own deploy runs a test command with the longest tool timeout (or in the
   background), output redirected to a file, judged by its own exit code (never one hidden
   behind a pipe such as `| tail`), reading only the summary line. Any failing command
   (a non-zero exit) is `deploy failed:` with the error. A restart command runs as given and
   last, and the deploy comment follows right after, before the clean-up. Never chips or
   schedules.

   **Tests in the deploy (#156).** Put one line from the verdict in a deploy subagent's brief.
   Docs-only: "The change is docs-only and no test reads it: do not run the tests, even if the
   deploy instructions say to." Tested: "The test stage ran the full suite on exactly this tree
   (`<base>` had not moved since it merged it in, and ship committed nothing): do not run the
   tests, even if the deploy instructions say to." Not tested: "`<base>` moved since the test
   stage (or ship committed changes after it, or the land was skipped): run the tests if the
   deploy instructions call for them, before any push." The same rule
   applies to your own mechanical deploy; it only removes a test run, never adds one. The deploy
   comment carries exactly one `tests:` line: `tests: skipped, docs-only` (docs-only, the rules
   call for tests); `tests: run in test stage, skipped` (tested, the rules call for tests);
   `tests: <count> passed`, e.g. `tests: 538 passed` (not tested, tests ran; a failing run is
   `tests: <the summary line>`, no push, `DEPLOY: FAILED`); `tests: none in the deploy rules`
   (the rules name no test run). A `DEPLOY: SKIPPED` deploy has the line too. A subagent's reply
   without it → add it yourself from the verdict (`tests: skipped, docs-only`, `tests: run in
   test stage, skipped`, or `tests: not reported` when not tested).
5. **Merged dot:** `update_card(merged=True)` if `git -C <repo> branch --merged <base> --list
   card/<id>` lists it. Not with `pr`: the queue step sets it once the PR merges.
6. **Clean up:** `git -C <repo> worktree remove --force <tree>`; will not go → say so in a
   comment (not a failure). The branch stays: it is the record.
7. Move to `verify`.

A deploy that restarts the Tickets backend cuts the MCP connection: if the tools stop
answering, wait until `GET /api/projects` answers (start it as in 0 if not within a minute) and
carry on over HTTP.

### Blocked by a person

Any stage may end with a `## Blocked by` section (before the questions and the verdict line):
one bullet per task only a person can do (a credential, an account, a service to enable, a
device to plug in), details indented under it. Ask for it in every brief. (you) For each bullet
whose title is not already a card in the project: `create_card(lane="todo", project=…,
title=…, description=details + "(blocks #<id>)")` and `link_cards(from_id=<new>, to_id=<id>,
kind="blocks")`; comment the new ids on the card and leave it in its lane.

### Steps for the owner (#636)

A step only the owner can run by hand in the Claude desktop session (restart the backend, `/mcp`
reconnect, a hard refresh, watching a live tick) goes in the stage's comment under a line that
starts `Your steps:`, one step per line; with none, no such line (not even `Your steps: none`).
The board highlights the card from that line until it is done; ask for it in every brief. It is not a `## Blocked by` task (those need a person, not a command).

### Failure

Anything that stops a stage (a crash, an empty plan, RESULT: FAIL, a git error, no git repo
for develop/test): `comment("agent failed: <why>")`, leave the lane as it is; its slot is free.
A session that fails after its plan post or ends `RESULT: FAIL` leaves the card in `develop`
for a later run; a test stage's FAIL leaves it in `test`.

**API error (#493):** a stage agent that ends on one (429, overloaded, "terminated early",
"session limit") is not an ordinary failure: `comment("api error: <cause>; reset <time the error
names, else none>; worktree kept")`, keep the worktree, unassign (lane unchanged). It is not
retried this run, and never in a hot loop: a later run retries it after the reset time, else
after 30 minutes.

## 4. Wrap up

Once, when the queue is empty and the last card in flight is done: clear your activity (also at
a run's end mid-way). Then:
- **Ideas:** things the runs noticed but did not do: follow-ups, tech debt, a missing test, a
  next step the plan deferred. One `todo` card each (skip titles already on the board),
  description ending "(idea from #<id>)". Only real ones; none is fine.
- **Summary** in the chat: moved to verify (with deploy result), waiting for answers, blocked
  (and by what), failed (and why), skipped as assigned, ideas opened, and **refused writes**:
  each once, with the card, the write and the permission rule that would let it through (it is
  retried next tick); any background-task chip a stage made by mistake; one line when the
  project's `Cards in flight:` value was invalid and the run fell back to 2 (#193); each
  base-checkout path left alone (#204: not a stray copy, so a person decides); each stalled card
  taken over, each API-error release with its reset time, and each `card/<id>` worktree or branch
  with no card in plan/develop/test (named, never removed). The summary
  reports; it never ends "waiting on you".
