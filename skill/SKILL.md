---
name: tickets
description: Work this project's cards on the Tickets board (http://127.0.0.1:8123) — every ready card in plan, develop and test, through to verify, unattended. Run by hand as /tickets in a session opened in the project's folder; optional argument a card id (#12 or 12) to work only that card.
disable-model-invocation: true
---

# Tickets: drain this project's board

The board is the record, not the boss: nothing runs until a person types `/tickets`.
One run takes every ready card of this folder's project from where it stands to
`verify`, writing each step onto the board as it goes, and ends with ideas for what
to do next. Work unattended: never stop to ask the user, never wait for an answer —
what needs a person goes on the board (questions, blocker cards) and the run moves on.

Every board write uses `actor="claude-agent"`. Tools are the `tickets` MCP server's
(`list_projects`, `list_cards`, `get_card`, `update_card`, `comment`, `create_card`,
`link_cards`, `set_activity`). If they are missing, stop and tell the user to start the
backend and register the server once, at user scope:
`claude mcp add --scope user --transport http tickets http://127.0.0.1:8123/mcp`.

## 1. Find the project

`list_projects`, and take the one whose `path` is the current working directory
(compare case-insensitively, `\` and `/` alike, no trailing slash). None → stop and say:
open the board, "projects…", set this project's path to this folder. Never work
"No Project".

Its `instructions` are the project's own rules (tests, deploy, conventions). Priority,
highest first: the card's latest comments, the card (`description`, `plan`, `answers`),
the project instructions, this skill.

The **repo** is the git toplevel of the path; the **base** branch is the repo's current
branch (`git -C <repo> rev-parse --abbrev-ref HEAD`). Develop and test need a git repo:
if the path is not one, cards can still be planned but fail in develop/test.

## 2. Build the queue

`list_cards(project=…)` and keep the cards in `plan`, `develop`, `test` (with an
argument: just that card, wherever it is among those lanes). Order: `plan` first (plans are
cheap, and their questions reach a person sooner), then `develop`, then `test`; within
a lane, the board's order. Skip, and name in the
final summary:

- **waiting for answers:** `questions` filled, `answers` empty;
- **blocked:** `get_card` shows a `blocks` link *to* it from a card not in `done`;
- **assigned to a person:** `assignee` set to anyone but `claude-agent`.

Re-list after each card: people edit the board while you work, and a card you moved
may now be next (a card planned without questions goes straight on to develop). Never
work the same card twice in one run — once it fails or waits, it is done for this run.

## 3. Work a card

For each card: `update_card(assignee="claude-agent")`, then
`set_activity(card_id=…, doing="planning"|"developing"|"testing")` — clear it
(`set_activity` with no card) when the card leaves your hands. Before every board write
after a long step, `get_card` again: **a person's move wins** — if the lane changed
under you, post your output as a comment saying it was moved during the run, and leave
the card where they put it.

Each stage is done by a `general-purpose` subagent (Agent tool), so a long run does not
fill this session's context. Brief it with: the card (`get_card` output: title,
description, plan, questions, answers, comments), the project instructions, the stage's
rules below, the folder to work in (absolute path — tell it to use absolute paths or
`git -C`), and the verdict line it must end with. Continue the same subagent
(SendMessage) for the card's next stage so it keeps what it learned. You — not the
subagent — write to the board and run the git steps marked (you).

On a card's way out: `update_card(assignee="")` unless a person had it before you.

### plan  (read-only, in the repo)

(you) `git -C <repo> pull --ff-only` first; if it fails, tell the subagent the code may
be behind. The subagent reads code, changes nothing, and replies with the complete plan
in markdown (Context section restates the request). If it already has `questions` and
`answers`, those are a person's answers: build them in, do not ask again. Anything
needing a person's decision goes in a last `## Open questions` section, numbered 1., 2.,
…; last line `QUESTIONS: NONE` or `QUESTIONS: OPEN`.

(you) Cut the verdict line and the questions section off. Empty plan → failure.
- NONE → `update_card(plan=…)`, move to `develop`.
- OPEN → `update_card(plan=…, questions=…)`, comment "open questions: answer them in
  the card, then run /tickets again", leave it in `plan`.

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
(you) Comment the summary plus the worktree path, move to `test`.

### test  (in the same worktree)

(you) Same worktree steps as develop; a card in test with neither a worktree nor a
`card/<id>` branch fails ("nothing was built: move it back to develop").

The subagent reviews `git -C <tree> diff <base>...HEAD` plus anything uncommitted against
the request, plan and answers, runs the tests, and fixes what it can itself on the
branch. What it cannot (unfinished work, a decision a person must make) it leaves alone.
Last line exactly `RESULT: PASS` or `RESULT: FAIL`. Comment its reply. FAIL → failure.

PASS → (you) ship, land, deploy, clean up — in this order:
1. **Ship:** `git -C <tree> add -A`, commit `#<id> <title>` if anything is staged,
   `git -C <tree> push -q -u origin card/<id>` (env `GIT_TERMINAL_PROMPT=0`). Any git
   error → failure, card stays in test. Comment the commit pushed.
2. **Land:** only if the repo is on `<base>` with nothing uncommitted
   (`git status --porcelain --untracked-files=no` empty):
   `git -C <repo> merge --no-ff card/<id> -m "Merge #<id> <title>"`; a conflict →
   `merge --abort`. Say which happened (or why it was skipped) in the deploy comment.
   Do not push yet.
3. **Deploy:** the subagent deploys per the project instructions. Local only — the test
   backend, or an install on a connected phone (check `adb devices`; none → skip the
   phone) — never production, a public server or an app store unless the instructions
   clearly say so. Changed neither backend nor app → deploy nothing. It may merge, push
   or restart services only as the instructions say; no other edits or commits. Last
   line `DEPLOY: OK`, `DEPLOY: SKIPPED` or `DEPLOY: FAILED`. (you) Comment it headed
   exactly `deployed: `, `deploy skipped: ` or `deploy failed: ` — the board lights the
   purple dot from that head. Then push the base if the land merged and the deploy did
   not already (`git -C <repo> push -q origin <base>`).
4. **Merged dot:** `update_card(merged=True)` if `git -C <repo> branch --merged <base>
   --list card/<id>` lists it.
5. **Clean up:** `git -C <repo> worktree remove --force <tree>`; if it will not go, say
   so in a comment (not a failure). The branch stays: it is the record.
6. Move to `verify`.

A deploy that restarts the Tickets backend itself cuts the MCP connection: if the
tools stop answering, wait a minute and retry once; still down → finish the summary in
the chat and tell the user to run `/mcp` to reconnect.

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
repo for develop/test): `comment("agent failed: <why>")`, leave the lane as it is, go
to the next card.

## 4. Wrap up

Clear your activity. Then:
- **Ideas:** things the runs noticed but did not do — follow-ups, tech debt, a missing
  test, a next step the plan deferred. One `todo` card each (skip titles already on the
  board), description ending "(idea from #<id>)". Only real ones; none is fine.
- **Summary** in the chat: moved to verify (with deploy result), waiting for answers,
  blocked (and by what), failed (and why), skipped as assigned, ideas opened.
