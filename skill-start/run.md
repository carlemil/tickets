# Tickets: one run over this project's board

Not a skill of its own: `/tickets-start` reads this file and follows it, once per loop
tick or once when asked. Whoever hands it to you gives three things: the **session id**,
the **Tickets repo** (the folder holding `restart-backend.ps1`), and optionally a **card
id**. The board is the record, not the boss: nothing runs until a person types
`/tickets-start`.
One run takes every ready card of this folder's project, up to three at a time, from
where it stands to `verify`, writing each step onto the board as it goes, and ends
with ideas for what to do next. Work unattended: never stop to ask the user, never wait for an answer —
what needs a person goes on the board (questions, blocker cards) and the run moves on.

Every board write uses `actor="claude-agent"`. Tools are the `tickets` MCP server's
(`list_projects`, `list_cards`, `get_card`, `update_card`, `comment`, `create_card`,
`link_cards`, `set_activity`).

## 0. Board up, tools or not

A missing board never stops a run:
- `GET http://127.0.0.1:8123/api/projects` does not answer → start it detached (a
  uvicorn started straight from a tool call dies with the call):
  `powershell -NoProfile -File <tickets repo>/restart-backend.ps1 -Delay 1`, ; wait ~12 s and
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
  6)))`. Subagents ticking the checklist get the same instructions. The summary says the
  run used HTTP and that `/mcp` (or a new session) brings the tools back; if the server
  is not registered at all, `/tickets-start` registers it.

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

The **repo** is the git toplevel of the path; the **base** branch is the repo's current
branch (`git -C <repo> rev-parse --abbrev-ref HEAD`). Develop and test need a git repo:
if the path is not one, cards can still be planned but fail in develop/test.

## 2. Build the queue

`list_cards(project=…)` and keep the cards in `plan`, `develop`, `test` (with a
card id: just that card, wherever it is among those lanes). Order: `plan` first (plans are
cheap, and their questions reach a person sooner), then `develop`, then `test`; within
a lane, the board's order. Skip, and name in the
final summary:

- **waiting for answers:** `questions` filled, `answers` empty;
- **blocked:** `get_card` shows a `blocks` link *to* it from a card not in `done`;
- **assigned to a person:** `assignee` set to anyone but `claude-agent`;
- **already in flight** in this run (see 3).

Re-list whenever a slot frees: people edit the board while you work, and a card you
moved may now be next (a card planned without questions goes straight on to develop).
Never work the same card twice in one run — once it fails or waits, it is done for this
run.

Before working the queue, catch up on pull requests: for each of the project's `verify` cards with
`pr` set and `merged` false, `gh pr view <pr> --json state -q .state`; `MERGED` →
`update_card(merged=True)`. Any failure here is ignored.

## 3. Work the cards, several at once

You are the dispatcher. Each stage of a card runs in its own **background** subagent
(Agent tool, `general-purpose`, `run_in_background: true`), so several cards move at
once and a long run does not fill this session's context. When one finishes you are
notified: do that card's (you) steps for the stage, then start its next stage by
continuing the same subagent (SendMessage, so it keeps what it learned), or, when the
card is out of your hands, fill the freed slot from a fresh re-list.

**Slots.** At most **3 cards in flight**. Plan stages are read-only and may always run
side by side. At most **one card in `test`** at a time — from the test stage through
ship, land, deploy and clean-up to `verify` — because tests, the deploy and service
restarts share ports, devices and the base checkout; a card whose next stage is test
waits (its subagent idle) until that slot is free. Land merges and pushes of the base are
yours, one card at a time: never interleave two cards' land/deploy sequences. Each card
works in its own worktree (below); do not use the Agent tool's `isolation: "worktree"` —
the `card/<id>` branch and its worktree path are the record.

Starting a card: `update_card(assignee="claude-agent", session="<session id>")`
(the board's link back to this session's transcript). `set_activity` holds one entry
for you, so point it at the card you started last and say how many are in flight:
`set_activity(card_id=…, doing="developing (3 in flight)")` (`planning`, `developing`,
`testing`); clear it (`set_activity` with no card) when the last card leaves your hands.
Before every board write after a stage, `get_card` again: **a person's move wins** — if
the lane changed under you, post your output as a comment saying it was moved during the
run, and leave the card where they put it.

Brief each stage's subagent with: the card (`get_card` output: title,
description, plan, questions, answers, checklist, comments), the project instructions, the
stage's rules below, the folder to work in (absolute path — tell it to use absolute paths or
`git -C`), and the verdict line it must end with. You — not the
subagent — write to the board and run the git steps marked (you); the one exception is
the develop subagent ticking checklist items.

On a card's way out: `update_card(assignee="")` unless a person had it before you.

### plan  (read-only, in the repo)

(you) `git -C <repo> pull --ff-only` first; if it fails, tell the subagent the code may
be behind. The subagent reads code, changes nothing, and replies with the complete plan
in markdown (Context section restates the request). If it already has `questions` and
`answers`, those are a person's answers: build them in, do not ask again. The plan has a
`## Steps` section: a numbered list of concrete implementation steps, at most ~10, one
line each. Anything
needing a person's decision goes in a last `## Open questions` section, numbered 1., 2.,
…; a question with sensible choices lists 2–4 of them under it as indented `- ` bullets,
the recommended one first and suffixed ` (recommended)` (the board shows them as radio
buttons), otherwise it is free text. Last line `QUESTIONS: NONE` or `QUESTIONS: OPEN`.

(you) Cut the verdict line and the questions section off. Empty plan → failure. The
steps become the card's checklist, its live task list: the existing items as they are,
plus one `{"text": <step>, "done": false}` per step whose text is not already on it (so a
replan adds only new steps); pass it as `checklist=…` in the same `update_card`.
- NONE → `update_card(plan=…, checklist=…)`, move to `develop`.
- OPEN → `update_card(plan=…, questions=…, checklist=…)`, comment "open questions: answer them in
  the card; the next run replans with them", leave it in `plan`.

### develop  (in the card's worktree)

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
pushes, never switches branch — and replies with a short summary and the test result.
Its one board write: as it finishes a step it ticks that checklist item — `get_card`,
flip that item's `done`, `update_card(checklist=<the full list>, actor="claude-agent")` —
nothing else; tell it so in the brief.
(you) Comment the summary plus the worktree path, move to `test`.

### test  (in the same worktree)

(you) Same worktree steps as develop; a card in test with neither a worktree nor a
`card/<id>` branch fails ("nothing was built: move it back to develop").

The subagent commits anything uncommitted in the worktree on `card/<id>`, then runs the
Skill tool with skill `code-review` and args `<base>...card/<id>` (a ref range: committed
work only, whatever the cwd). It fixes the findings it agrees with on the branch, in the
worktree, and lists those it rejects in its reply with a one-line reason each. No Skill
tool, no `code-review`, or an error → it reviews `git -C <tree> diff <base>...HEAD` itself
and says so. code-review does not know the card, so it also checks the diff against the
request, plan, answers and checklist, and runs the tests, fixing what it can itself on
the branch. What it cannot (unfinished work, a decision a person must make) it leaves alone.
Its review names every checklist step still unticked (one skipped on purpose says why).
Last line exactly `RESULT: PASS` or `RESULT: FAIL`. Comment its reply. FAIL → failure.

PASS → (you) ship, land, deploy, clean up — in this order:
1. **Ship:** `git -C <tree> add -A`, commit `#<id> <title>` if anything is staged,
   `git -C <tree> push -q -u origin card/<id>` (env `GIT_TERMINAL_PROMPT=0`). Any git
   error → failure, card stays in test. Comment the commit pushed. If the project's
   `land` is `pr`, then in the repo: `gh pr create --base <base> --head card/<id> --title
   "#<id> <title>" --body "<short summary from the test reply>"` (a PR for the branch
   already exists → `gh pr view card/<id> --json url -q .url` instead),
   `update_card(pr=<url>)`, and comment the URL. gh missing or failing → failure, card
   stays in test.
2. **Land:** only when `land` is `merge` — with `pr`, skip it and say "landing by pull
   request" in the deploy comment. Only if the repo is on `<base>` with nothing uncommitted
   (`git status --porcelain --untracked-files=no` empty):
   `git -C <repo> merge --no-ff card/<id> -m "Merge #<id> <title>"`; a conflict →
   `merge --abort`. Say which happened (or why it was skipped) in the deploy comment.
   Do not push yet.
3. **Deploy:** the subagent deploys per the project instructions. Local only — the test
   backend, or an install on a connected phone (check `adb devices`; none → skip the
   phone) — never production, a public server or an app store unless the instructions
   clearly say so. Changed neither backend nor app → deploy nothing. It may merge, push
   or restart services only as the instructions say; no other edits or commits. With
   `land` `pr` it never merges into the base, whatever the rules say: the PR does that. Last
   line `DEPLOY: OK`, `DEPLOY: SKIPPED` or `DEPLOY: FAILED`. (you) Comment it headed
   exactly `deployed: `, `deploy skipped: ` or `deploy failed: ` — the board lights the
   purple dot from that head. Then push the base if the land merged and the deploy did
   not already (`git -C <repo> push -q origin <base>`).
4. **Merged dot:** `update_card(merged=True)` if `git -C <repo> branch --merged <base>
   --list card/<id>` lists it. Not with `pr`: the queue step sets it once the PR merges.
5. **Clean up:** `git -C <repo> worktree remove --force <tree>`; if it will not go, say
   so in a comment (not a failure). The branch stays: it is the record.
6. Move to `verify`.

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
card and leave it in its lane. It will be skipped until they are `done`.

### Failure

Anything that stops a stage (a crash, an empty plan, RESULT: FAIL, a git error, no git
repo for develop/test): `comment("agent failed: <why>")`, leave the lane as it is; its
slot is free for the next card.

## 4. Wrap up

Once, when the queue is empty and the last card in flight is done: clear your activity.
Then:
- **Ideas:** things the runs noticed but did not do — follow-ups, tech debt, a missing
  test, a next step the plan deferred. One `todo` card each (skip titles already on the
  board), description ending "(idea from #<id>)". Only real ones; none is fine.
- **Summary** in the chat: moved to verify (with deploy result), waiting for answers,
  blocked (and by what), failed (and why), skipped as assigned, ideas opened.
