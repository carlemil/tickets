"""Board agent: plans, develops and tests cards, over MCP. uv run python agent.py

A card in `plan`, `develop` or `test` gets headless Claude Code run on it in its project's
configured path, with the project's instructions in the prompt, when it is assigned to
AGENT or has `auto_advance` on. The output becomes a comment and the card moves one lane on.

Plan is different: Claude runs in plan mode and its reply is split into the card's `plan`
and `questions` fields; the `description` stays the person's request. If the plan ends in
QUESTIONS: OPEN, or in no verdict at all, the card stays in plan for a person to fill in
`answers`; auto advance is switched off so it is not replanned every poll. The person's
reply (any edit or comment on a card waiting for them) switches it back on: see core.

While it works a card the agent assigns it to itself, and after it hands it back to
whoever had it: a person, or nobody if it was assigned to the agent.
Assigned: the card is then unassigned. That is both the human gate (read the plan, assign
again to have it built) and the loop guard (the agent never re-triggers on its own write).
Auto advance: the card keeps going, plan -> develop -> test -> verify, and stops at verify
for a person. The test stage must end in RESULT: PASS to move on.

Develop and test only ever run in the card's own git worktree. The test stage fixes what
it can and hands the card back when it cannot; a test that passes commits everything left
in the worktree and pushes the card's branch to origin before the card moves to verify, so
what a person verifies is on the remote. The agent then merges the branch into the
project's base branch itself, for every project, and pushes that once the deploy has run.
The worktree is removed once the deploy has run in it: the branch, here and on origin, is
the record, and a card sent back to develop or test gets a fresh worktree from it, as does
one whose folder went missing. Once it is in verify the agent deploys it: the backend if
the backend changed, the app to the phone if the app changed and a phone is connected (none
connected: skipped). How is up to the project's instructions; either way the card stays in
verify, with the result as a comment. Every poll re-checks git and lights the merged dot of
any card whose branch is on the base branch, however it got there.

Projects run side by side, and so do the lanes of one project: each project lane has one
run at a time, so a card waits only while another card in the same lane of its project is
worked, and the waiting card nearest the top of its column goes next. A card the agent
moves goes to the back of the next lane's queue.

A deploy that changes agent.py takes effect on its own: between polls, with no run going,
the agent sees the new file, frees its lock port and starts a fresh process of itself.

Any failure comments why, unassigns and turns auto advance off, so the card sits in its
lane until a person looks: no retry loop. A run stopped by something only a person can do
(a credential to create, a service to enable) says so under '## Blocked by': each task
becomes a card of its own in `todo`, linked as blocking this one, and this card waits in
its lane like one with open questions. Running out of quota is not a failure: the
agent comments when it will resume, pauses all runs until the limit resets, then picks the
card up again where it was.
"""

import asyncio
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path

from mcp import Client

from core import (AGENT, AGENT_LANES, DEPLOYED_HEAD, FAILED_HEAD,   # the board is reached over MCP
                  NO_PROJECT, SKIPPED_HEAD, number)

SOURCE = Path(__file__).resolve()   # watched: a deploy that changes it restarts the agent
URL = "http://127.0.0.1:8123/mcp"
POLL = 15
LOCK = ("127.0.0.1", 8124)   # held while an agent runs: a second one cannot start
NEXT = {"plan": "develop", "develop": "test", "test": "verify"}
PASS = "RESULT: PASS"
NO_QUESTIONS = "QUESTIONS: NONE"
DEPLOYED = "DEPLOY: OK"
SKIPPED = "DEPLOY: SKIPPED"
DOING = {"plan": "planning", "develop": "developing", "test": "testing"}   # the status bar
# claude -p's "You've hit your session limit · resets 10:30pm (Europe/Stockholm)"
LIMIT = re.compile(r"hit your [^\n]*?limit(?:[^\n]*?resets ([^(\n]*))?", re.I)
QUESTIONS_HEAD = re.compile(r"^#+[ \t]*open questions[ \t]*:?[ \t]*$", re.I | re.M)
BLOCKED_HEAD = re.compile(r"^#+[ \t]*blocked by[ \t]*:?[ \t]*$", re.I | re.M)
BULLET = re.compile(r"(?:[-*+]|\d+[.)])\s+(.*)")
CARD_BRANCH = re.compile(r"(?:.*/)?card/(\d+)")   # local or origin/, -> the card id
CLI_CAP = 100_000   # the transcript goes in the DB, and a develop run can print megabytes
# every lane gets this: what stops a card from outside the code becomes cards for a person
BLOCKED_ASK = (
    "\n\nIf something outside the code stops this card — a credential or account that has "
    "to be created, a service to enable, a device to plug in, a file only a person can "
    "supply — end your reply with a section headed exactly '## Blocked by', one short "
    "bullet per task, written as a task in a person's words, with the details indented "
    "under it. Each bullet is opened as a card on the board for them, and the card you are "
    "on stops until they are done, so put nothing there about work you can do yourself. "
    "If you also have '## Open questions', put this section before it.")

PROMPTS = {
    "plan": "Plan the implementation of this ticket card. Its `description` is the request. "
            "If it already has `questions` and `answers`, those are a person's answers to "
            "your previous round: build them into the plan and do not ask them again. Read "
            "the code in the current directory as needed. Nobody can answer you during this "
            "run, so do not ask questions. Your final reply is saved as the card's `plan`, "
            "so reply with the complete plan in markdown (restate the request in its Context "
            "section), not a summary or a pointer to a file. If anything needs a person's "
            "decision, put it in a last section headed exactly '## Open questions', as a "
            "numbered list starting at 1 (1. 2. 3.), one question each: that section is saved "
            "apart, as the card's `questions`. End with "
            f"a last line of exactly {NO_QUESTIONS} if nothing is left open, otherwise "
            "QUESTIONS: OPEN.",
    "develop": "Implement this ticket card. Its `description` is the request, `plan` the "
               "plan to follow and `answers` a person's decisions on the plan's `questions`; "
               "later comments win over all of them. Run the project's tests. Reply with a "
               "short summary of what you changed and the test result.",
    "test": "Check that this ticket card is done: review this card's changes (the workspace "
            "note below says where they are) against the card's request, plan and answers, "
            "and run the "
            "project's tests. Fix what you find and can fix yourself — a failing test, a "
            "defect, a merge the workspace note says is unfinished — here on the card's "
            "branch: a pass commits and pushes your fixes with the rest of the card. What is "
            "out of your hands — work left unfinished, a fix that needs a person's decision "
            "— you leave alone and end in RESULT: FAIL, and the card stops here for a "
            "person. Reply with what you checked, what you found and what you fixed, and end "
            f"with a last line of exactly {PASS} or RESULT: FAIL.",
    # not a lane: the last step of the test stage, before the card moves to verify
    "deploy": "This ticket card passed testing and is committed and pushed (the workspace "
              "note below says where its changes are). Deploy it. Deploy means the local "
              "test backend and a local install on a connected phone. Never deploy to "
              "production, a public server, or an app store unless the project's "
              "instructions clearly say this deploy is a production one. Look at what the "
              "card changed: if it changed the backend, deploy the backend; if it changed the "
              "mobile app, install it on the phone, but only if a phone is connected (check, "
              "e.g. adb devices): with no phone connected, skip the phone and say so. If it "
              "changed neither, deploy nothing. Follow the project instructions on how to "
              "deploy. Change nothing beyond what those instructions say: they may merge, "
              "push or restart services, and then you do exactly that; edit no files and "
              "make no other commits. Reply with what you deployed and "
              f"what you skipped, and end with a last line of exactly {DEPLOYED} if it is "
              f"on the test backend or a device, {SKIPPED} if nothing needed deploying or "
              "no phone was connected, or DEPLOY: FAILED if a deploy failed.",
}
# planning runs in plan mode (what /plan switches on): read-only by design.
# development is a normal agent in auto mode: it edits and runs the tests on its own, with
# Claude Code's auto-mode checks still on. Testing needs Bash for the tests and gets full
# rights in the repo: it fixes what it can on the card's branch and hands back the rest.
FLAGS = {"plan": ["--permission-mode", "plan"], "develop": ["--permission-mode", "auto"],
         "test": ["--dangerously-skip-permissions"],
         "deploy": ["--dangerously-skip-permissions"]}   # deploy tools, adb: unattended


async def call(client, tool, **args):
    r = await client.call_tool(tool, args)
    if r.is_error:
        raise RuntimeError(r.content[0].text)
    # list and str returns come wrapped as structured {"result": ...}; dicts only as JSON text
    if r.structured_content is not None:
        return r.structured_content["result"]
    return json.loads(r.content[0].text)


class OutOfQuota(RuntimeError):
    """claude hit the account's usage limit: `at` is when to try again."""
    def __init__(self, text, at):
        super().__init__(text.strip().splitlines()[0])
        self.at = at


def resume_at(text, now):
    """The reset time in claude's limit message ("10:30pm", "10pm", "Sep 21, 10am") -> when
    to resume, 1 minute after it. Unreadable: in 30 minutes, and a still-empty quota just
    pauses again then.
    ponytail: ignores the "(Europe/Stockholm)" zone and reads the time as this machine's
    local time, which is the account's here; zoneinfo needs tzdata on Windows, add it if
    the two ever differ."""
    m = re.search(r"(?:([a-z]{3}) (\d{1,2}),?\s*)?(\d{1,2})(?::(\d\d))?\s*([ap]m)",
                  text or "", re.I)
    try:
        mon, day, h, mi, ap = m.groups()
        at = now.replace(hour=int(h) % 12 + 12 * (ap.lower() == "pm"), minute=int(mi or 0),
                         second=0, microsecond=0)
        if mon:
            d = datetime.strptime(f"{mon} {day}", "%b %d")
            at = at.replace(month=d.month, day=d.day)
            if at < now - timedelta(days=1):
                at = at.replace(year=at.year + 1)   # Jan 2 read on Dec 30
        elif at < now - timedelta(hours=1):
            at += timedelta(days=1)                 # 1am read at 11pm
    except (AttributeError, ValueError):
        return now + timedelta(minutes=30)
    # a reset just passed (clocks differ): wait a little, not a day, nor a busy loop
    return max(at, now + timedelta(minutes=4)) + timedelta(minutes=1)


class Reply(str):
    """What claude answered, which is all a caller needs, with the run's CLI transcript on
    `.cli` for the card's output box. A str, so every caller that only wants the answer
    (`split_plan`, the verdict checks) reads exactly what it did before the transcript
    existed; read the transcript with `getattr(out, "cli", "")`."""
    def __new__(cls, text, cli=""):
        r = super().__new__(cls, text)
        r.cli = cli
        return r


def _brief(s, n=160):
    """One line, short enough to read: a tool's arguments or a tool result in the box."""
    s = " ".join(f"{s}".split())
    return s if len(s) <= n else s[:n] + "…"


def _blocks(e):
    return e.get("message", {}).get("content", []) if isinstance(e.get("message"), dict) else []


def transcript(out):
    """stream-json -> what a person watching the CLI would have seen: the text claude wrote,
    a line per tool call and its result, and what the run cost. A line that is not a JSON
    event (a warning claude printed) is kept as it is."""
    lines = []
    for raw in out.splitlines():
        e = None
        if raw.strip().startswith("{"):
            try:
                e = json.loads(raw)
            except ValueError:
                e = None
        if not isinstance(e, dict):
            if raw.strip():
                lines.append(raw.rstrip())
            continue
        if e.get("type") == "assistant":
            for b in _blocks(e):
                kind = b.get("type")
                if kind == "text" and b.get("text", "").strip():
                    lines.append(b["text"].strip())
                elif kind == "thinking":
                    lines.append("● thinking…")
                elif kind == "tool_use":
                    args = json.dumps(b.get("input", {}), ensure_ascii=False)
                    lines.append(f"● {b.get('name', 'tool')}({_brief(args)})")
        elif e.get("type") == "user":
            for b in _blocks(e):
                if b.get("type") != "tool_result":
                    continue
                body = b.get("content")
                if isinstance(body, list):
                    body = " ".join(x.get("text", "") for x in body if isinstance(x, dict))
                lines.append(f"  ⎿ {_brief(body or '(no output)')}")
        elif e.get("type") == "result":
            cost = e.get("total_cost_usd")
            lines.append(f"● done in {round((e.get('duration_ms') or 0) / 1000)}s"
                         + (f" · ${cost:.2f}" if isinstance(cost, (int, float)) else ""))
    text = "\n".join(lines)
    # the tail is the part worth keeping: the tests that ran and how it ended
    return text if len(text) <= CLI_CAP else \
        f"(… {len(text) - CLI_CAP} characters dropped)\n{text[-CLI_CAP:]}"


def reply(out):
    """claude's final answer: the `result` event's text. No such event, or output that is
    not stream-json at all -> the whole output, so a caller never gets less than plain
    `-p` gave it."""
    for raw in reversed(out.splitlines()):
        if not raw.strip().startswith("{"):
            continue
        try:
            e = json.loads(raw)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get("type") == "result" and f"{e.get('result') or ''}".strip():
            return e["result"].strip()
    return out.strip()


def run_claude(card, cwd, instructions=""):
    lane = card["lane"]
    project = f"Project instructions (follow them):\n{instructions}\n\n" if instructions else ""
    prompt = (f"{PROMPTS[lane]}{BLOCKED_ASK}\n\n{project}"
              f"Card:\n{json.dumps(card, indent=2, ensure_ascii=False)}")
    try:
        # stream-json prints an event per step, so the run's whole transcript is kept, not
        # just its last message. The new flags go before FLAGS[lane]: the lane's own flags
        # stay last, where the tests look for them.
        out = subprocess.run(
            [shutil.which("claude") or "claude", "-p", "--output-format", "stream-json",
             "--verbose", *FLAGS[lane]], input=prompt, cwd=cwd,
            capture_output=True, text=True, encoding="utf-8", timeout=3600, check=True,
        ).stdout
        return Reply(reply(out), transcript(out))
    except subprocess.CalledProcessError as e:
        text = e.stderr or e.stdout or ""
        if m := LIMIT.search(text):
            # m[0], not text: the limit line itself, since stderr's first line is whatever
            # claude printed first — a SessionStart hook's json, say, which says nothing
            raise OutOfQuota(m[0], resume_at(m[1], datetime.now())) from e
        raise RuntimeError(f"claude exited {e.returncode}: {(e.stderr or e.stdout)[-2000:]}") from e


def git(repo, *args):
    # no prompt: a push that needs credentials fails instead of hanging the run
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          encoding="utf-8", env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})


def workspace(card, path):
    """Where a stage runs, and what the prompt is told about it: (cwd, note, worktree,
    base branch).

    Develop and test get a git worktree of their own per card, next to the repo
    (<repo>.worktrees/card-<id>, branch card/<id>, made from the repo's current branch), so
    one card's changes never mix with another's or with a person's work in the repo.
    Each run first merges the base branch in (see catch_up), so a card is built and tested
    on current code. Planning only reads, so it runs in the repo, pulled up to date first
    (see refresh). Develop and test never
    run anywhere but the worktree: a folder that is not a git repository fails the card. A
    worktree whose folder went missing is re-made from the card's branch, in either lane;
    only a card in test with no branch at all fails, since nothing was ever built."""
    if card["lane"] == "plan":
        return path, refresh(path), None, None
    top = git(path, "rev-parse", "--show-toplevel")
    if top.returncode:
        raise RuntimeError(f"{path} is not a git repository: develop and test only run in "
                           "a git worktree")
    top = Path(top.stdout.strip())
    root = top.parent / f"{top.name}.worktrees" / f"card-{card['id']}"
    branch = f"card/{card['id']}"
    base = git(top, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if not root.is_dir():
        # a worktree folder deleted by hand leaves its registration behind, and git then
        # refuses to make the card another one on the same branch
        git(top, "worktree", "prune")
        have = git(top, "rev-parse", "--verify", "--quiet",
                   f"refs/heads/{branch}").returncode == 0
        if card["lane"] == "test" and not have:
            raise RuntimeError(f"there is no worktree at {root} and no branch {branch} to "
                               "test: move the card back to develop to build it in one")
        made = git(top, "worktree", "add", str(root), branch) if have else \
            git(top, "worktree", "add", "-b", branch, str(root))
        if made.returncode:
            raise RuntimeError(f"could not make a worktree at {root}: {made.stderr.strip()}")
    note = (f"Workspace: a git worktree of its own for this card, on branch {branch}, made "
            f"from {base}. ")
    note += catch_up(root, base)
    note += (f"Commit your work on {branch} when you are done. Do not push or switch "
             f"branches; merging {base} into your branch is fine."
             if card["lane"] == "develop" else
             f"This card's changes are the commits on {branch} since it left {base} (git diff "
             f"{base}...HEAD, git log {base}..HEAD) plus anything uncommitted (git status).")
    return root / path.relative_to(top), note, root, base


def catch_up(tree, base):
    """Bring the card's branch up to date with `base` before a run, so the card is built on
    current code and the deploy's merge back is clean. Best effort: no origin, offline, or a
    dirty tree just leaves the branch where it was. Conflicts are left in the worktree for
    the run to resolve. Returns the line the prompt is told about it."""
    if git(tree, "rev-parse", "--verify", "--quiet", "MERGE_HEAD").returncode == 0:
        return (f"A merge of {base} here is unfinished: resolve the conflicts (git status) "
                "and commit it before anything else. ")
    git(tree, "fetch", "-q", "origin", base)          # no origin: ignored, the local ref is used
    ref = next((r for r in (f"origin/{base}", base)
                if git(tree, "rev-parse", "--verify", "--quiet", r).returncode == 0), None)
    if not ref or git(tree, "merge-base", "--is-ancestor", ref, "HEAD").returncode == 0:
        return ""                                      # nothing to catch up to, or already in
    r = git(tree, "merge", "-m", f"merge {ref} into card branch", ref)
    if r.returncode == 0:
        return f"Your branch was brought up to date with {ref} before this run. "
    if git(tree, "rev-parse", "--verify", "--quiet", "MERGE_HEAD").returncode == 0:
        return (f"Merging {ref} in left conflicts: resolve them (git status lists the files) "
                "and commit the merge before anything else. ")
    git(tree, "merge", "--abort")                      # never started (dirty tree): harmless
    return f"Could not merge {ref} in ({(r.stderr or r.stdout).strip()[-200:]}); your branch is behind it. "


def refresh(repo):
    """Before planning: fast-forward the repo's current branch to its upstream, so the plan
    reads current code (#110). Best effort, and never a merge: the repo is a person's
    checkout, so no upstream, offline, a diverged branch or an uncommitted file in the way
    just leaves it as it is. Returns the line the prompt is told about it.
    ponytail: the pull can lose the .git/index.lock race with a deploy's merge in the same
    repo; it then fails and the planner is told the code may be behind."""
    up = git(repo, "rev-parse", "--abbrev-ref", "--verify", "--quiet", "@{u}")
    if up.returncode:
        return ""                                      # not a repo, or nothing to pull from
    up = up.stdout.strip()
    before = git(repo, "rev-parse", "HEAD").stdout
    r = git(repo, "pull", "--ff-only", "--no-rebase", "-q")   # fetch + fast-forward
    if r.returncode:
        return (f"Could not pull {up} ({(r.stderr or r.stdout).strip()[-200:]}); the code "
                "here may be behind it. ")
    return (f"The repo was pulled up to date with {up} before this run. "
            if git(repo, "rev-parse", "HEAD").stdout != before else "")


def landed(path):
    """Card ids whose branch is on the project's base branch. The base is the repo's current
    branch, as in `workspace`; the local ref and origin's both count, so a merge that has
    not been pushed yet still counts as on the base. No fetch: no network on a poll, and a
    merge done here is visible here."""
    top = git(path, "rev-parse", "--show-toplevel")
    if top.returncode:
        return set()                      # no path, no repo: nothing to light
    top = Path(top.stdout.strip())
    base = git(top, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    ids = set()
    for ref in ([base, f"origin/{base}"] if base else []):
        if git(top, "rev-parse", "--verify", "--quiet", ref).returncode:
            continue
        out = git(top, "branch", "-a", "--merged", ref, "--list", "card/*", "*/card/*",
                  "--format=%(refname:short)")
        ids |= {int(m[1]) for b in out.stdout.split() if (m := CARD_BRANCH.fullmatch(b))}
    return ids


def land(repo, card, base):
    """Merge the card's branch into the project's base branch in the main checkout, at the
    end of the test stage (#106). Every project, not just the ones whose instructions say
    so. Never touches a person's work: the repo must be on `base` with nothing uncommitted,
    or the merge is skipped and the comment says why. Returns (merged?, the line to say).
    ponytail: the merge can lose the .git/index.lock race a develop's `worktree add` can
    lose too; it is then skipped with git's error and a person merges it by hand."""
    top = git(repo, "rev-parse", "--show-toplevel")
    if top.returncode:
        return False, f"not merged: {repo} is not a git repository"
    top, branch = Path(top.stdout.strip()), f"card/{card['id']}"
    if card["id"] in landed(top):
        return False, f"already on {base}"
    on = git(top, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if on != base:
        return False, f"not merged: {top} is on {on}, not {base}"
    if git(top, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        return False, f"not merged: {top} has uncommitted work on {base}"
    r = git(top, "merge", "--no-ff", branch, "-m", f"Merge #{card['id']} {card['title']}")
    if r.returncode:
        git(top, "merge", "--abort")
        return False, f"not merged: {branch} does not merge into {base} cleanly"
    return True, f"merged {branch} into {base}"


def ship(card, tree):
    """Commit everything left in the card's worktree and push its branch to origin: done
    before a card moves to verify. Returns the commit pushed."""
    branch = f"card/{card['id']}"
    steps = [["add", "-A"]]
    if git(tree, "status", "--porcelain").stdout.strip():
        steps.append(["commit", "-q", "-m", f"#{card['id']} {card['title']}"])
    steps.append(["push", "-q", "-u", "origin", branch])
    for args in steps:
        r = git(tree, *args)
        if r.returncode:
            raise RuntimeError(f"git {args[0]} failed: {(r.stderr or r.stdout).strip()[-2000:]}")
    return git(tree, "rev-parse", "--short", "HEAD").stdout.strip()


def prune(repo, tree):
    """Remove the card's worktree once the test stage is done with it. Run from the repo,
    not the worktree: git cannot delete the folder the call's own cwd is in. The branch
    stays, here and on origin, so nothing is lost — a card sent back to develop gets a fresh
    worktree from it (see workspace). Forced because `ship` has just committed and pushed
    everything, so what is left is ignored build output. Returns git's complaint if the
    folder would not go (a file held open, say), "" if it went: a worktree left behind does
    not fail a card that passed."""
    r = git(repo, "worktree", "remove", "--force", str(tree))
    return "" if r.returncode == 0 else (r.stderr or r.stdout).strip()[-500:]


def split_plan(out):
    """Claude's plan reply -> (plan, questions, open?). The verdict line is dropped, and the
    '## Open questions' section, which the prompt asks for last, becomes the questions."""
    lines = out.splitlines()
    verdict = lines[-1].strip(" *`")
    body = "\n".join(lines[:-1]) if verdict.startswith("QUESTIONS:") else out
    m = QUESTIONS_HEAD.search(body)
    plan, questions = (body[:m.start()], body[m.end():]) if m else (body, "")
    is_open = verdict != NO_QUESTIONS
    return plan.strip(), number(questions.strip()) if is_open else "", is_open


def blockers(out):
    """A reply -> (the reply with its '## Blocked by' section taken out, [(title, body)…]).
    The section is the bullet list under the heading: a top-level bullet starts a task, the
    lines indented under it are its details, and the first other non-blank line ends it —
    the next heading, or the run's verdict line, which stays last in the reply where the
    lane checks look for it. A heading with no bullets under it takes nothing out."""
    m = BLOCKED_HEAD.search(out)
    if not m:
        return out, []
    rest, items, used = out[m.end():], [], 0
    for ln in rest.splitlines(keepends=True):
        if not ln[:1].isspace() and (b := BULLET.match(ln)):
            items.append([b[1].strip(" *`"), []])
        elif ln.strip() and ln[:1].isspace() and items:
            items[-1][1].append(ln.strip())
        elif ln.strip():
            break                      # the next heading or the verdict: the section ends
        used += len(ln)
    if not items:
        return out, []
    return (out[:m.start()] + rest[used:]).strip(), \
        [(t if len(t) <= 100 else t[:99] + "…", "\n".join(body)) for t, body in items]


async def open_blockers(client, card, items):
    """Cards for what a person has to do before this card can go on: in `todo`, unassigned,
    auto advance off — where `core.setup_cards` puts what only a person can fix, and a lane
    the agent never polls — each linked as blocking the card it came from.
    ponytail: a task already on the board under that title in the project is skipped, so a
    rerun does not open it twice; reworded on the next run, it opens twice. Good enough for
    a board one person reads."""
    have = {c["title"].casefold()
            for c in await call(client, "list_cards", project=card["project"])}
    opened = []
    for title, body in items:
        if title.casefold() in have:
            continue
        new = await call(client, "create_card", title=title, actor=AGENT,
                         project=card["project"],
                         description=f"{body}\n\n(blocks #{card['id']} {card['title']})".strip())
        await call(client, "link_cards", from_id=new["id"], to_id=card["id"], kind="blocks",
                   actor=AGENT)
        opened.append(new["id"])
    if opened:
        await call(client, "comment", id=card["id"], actor=AGENT,
                   text="blocked: opened " + ", ".join(f"#{i}" for i in opened)
                        + " in todo for what a person has to do first")


async def handle(client, card, back=""):
    """`back` is who had the card before the agent took it: every way out gives it back."""
    lane, id = card["lane"], card["id"]
    cli = ""   # the run's CLI transcript: set once claude has run, so a failure can carry it
    try:
        name = card["project"].lower()   # a card from before projects may differ in case
        proj = next((p for p in await call(client, "list_projects")
                     if p["name"].lower() == name), None)
        if not proj or not proj["path"]:
            raise RuntimeError(f"project {card['project']!r} has no path configured")
        repo = Path(proj["path"])   # the main checkout: the deploy merges the branch there
        if not repo.is_dir():
            raise RuntimeError(f"no repo at {repo}")
        cwd, note, tree, base = workspace(card, repo)
        notes = "\n\n".join(x for x in (proj["instructions"], note) if x)
        out = await asyncio.to_thread(run_claude, card, cwd, notes)
        cli = getattr(out, "cli", "")
        # the tasks for a person are opened whatever happened to this card meanwhile
        out, blocked = blockers(out)
        if blocked:
            await open_blockers(client, card, blocked)
        # so a person knows where to look, and what to merge
        # in test the worktree goes with the deploy: name the branch, which stays
        where = ((f"\n\n(worked in {tree}, branch card/{id})" if lane == "develop"
                  else f"\n\n(branch card/{id})") if tree else "")
        # a run takes minutes; if a person moved the card meanwhile, their move wins: keep
        # the output as a comment, but do not move or switch off a card that is no longer
        # where this run found it. Only let go of it, unless a person took it meanwhile.
        now = await call(client, "get_card", id=id)
        if now["lane"] != lane:
            await call(client, "comment", id=id, actor=AGENT, text=f"{out or '(no output)'}"
                       f"\n\n(this {lane} run finished after the card moved to {now['lane']}: "
                       "left as it is)", output=cli)
            if now["assignee"] == AGENT:
                await call(client, "update_card", id=id, actor=AGENT, assignee=back,
                           auto_advance=now["auto_advance"])
            return
        if lane == "plan":
            plan, questions, is_open = split_plan(out) if out else ("", "", True)
            if not plan:
                raise RuntimeError("planning produced no plan")   # never blank a plan
            if is_open:
                await call(client, "update_card", id=id, actor=AGENT, plan=plan,
                           questions=questions)
                await call(client, "comment", id=id, actor=AGENT, text="the plan has open "
                           "questions: fill in the answers and the card is replanned",
                           output=cli)
                off = {"auto_advance": False} if card["auto_advance"] else {}
                await call(client, "update_card", id=id, actor=AGENT, assignee=back,
                           attention=True, **off)
                return
            # no questions left: the last round's questions and answers stay, as the record
            # of what was decided
            await call(client, "update_card", id=id, actor=AGENT, plan=plan)
            await call(client, "comment", id=id, actor=AGENT, text="plan written", output=cli)
        else:
            await call(client, "comment", id=id, actor=AGENT, text=(out or "(no output)") + where,
                       output=cli)
        if blocked:   # only a person can do it: the card waits here, like one with questions
            await call(client, "update_card", id=id, actor=AGENT, assignee=back, attention=True,
                       auto_advance=False)
            return
        if lane == "test":
            if [ln.strip(" *`") for ln in out.splitlines()[-1:]] != [PASS]:
                raise RuntimeError(f"the test stage did not end in {PASS}")
            sha = await asyncio.to_thread(ship, card, tree)
            await call(client, "comment", id=id, actor=AGENT,
                       text=f"committed and pushed card/{id} to origin ({sha})")
        nxt = NEXT[lane]
        # the phone gets the build before the card reaches verify: a card in verify is
        # always one a person can pick up and check
        if nxt == "verify":
            # the deploy runs in the worktree; the worktree goes after it, its work pushed
            if await deploy(client, card, repo, cwd, notes, base) and \
                    (left := await asyncio.to_thread(prune, repo, tree)):
                await call(client, "comment", id=id, actor=AGENT,
                           text=f"could not remove the worktree {tree}: {left}")
        # an auto-advancing card goes on to the next stage, which the next poll picks up
        if card["auto_advance"] and nxt in NEXT:
            await call(client, "update_card", id=id, actor=AGENT, lane=nxt, assignee=back)
        else:
            # handed back: the dot says it is waiting for you. auto_advance is passed as it
            # is, since a forward move would switch it on, and this card is meant to stop
            await call(client, "update_card", id=id, actor=AGENT, lane=nxt, assignee=back,
                       attention=True, auto_advance=card["auto_advance"])
    except OutOfQuota as e:   # not a failure: pause, and the card is taken again after
        pause(e.at)
        await call(client, "set_activity", actor=AGENT, card_id=id,
                   doing=f"out of quota until {e.at:%H:%M}")
        await call(client, "comment", id=id, actor=AGENT,
                   text=f"out of quota: resuming at {e.at:%H:%M} ({e})")
        # as it was: an auto card is still auto, a card assigned to the agent still is
        await call(client, "update_card", id=id, actor=AGENT,
                   assignee=back if card["auto_advance"] else AGENT)
    except Exception as e:  # any failure: say why, hand the card back, no retry loop
        await call(client, "comment", id=id, actor=AGENT, text=f"agent failed: {e}", output=cli)
        await call(client, "update_card", id=id, actor=AGENT, assignee=back, auto_advance=False,
                   attention=True)


# (project lower case, lane) -> its one run in flight. A project's cards run one at a time
# per lane, so two runs never share a lane's tests, ports or worktree; different lanes and
# different projects run side by side.
# ponytail: per process; two agent processes would each keep their own. Lanes of one
# project do share the repo, so a develop `git worktree add` can lose an .git/index.lock
# race with a deploy's merge: the card fails with the git error; retry the git call if it bites.
running = {}
# the cards those runs are on. The slot key is the lane, so a card moved to another lane
# mid-run becomes eligible again under the new lane's key: without this it would get a
# second run, in the same worktree, while the first was still going (#76, and #33 before it)
working = set()
# quota is the account's, not a project's: while it is out, no run starts anywhere
paused_until = None
# the longest a reset time is taken on trust before a run looks again (see pause)
MAX_PAUSE = timedelta(hours=1)


def log(msg):
    """Every row of agent.log carries a timestamp, like the backend's (#81)."""
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def pause(at):
    """When to try again. The runs in flight when the quota goes each report their own
    session's reset, and those differ — three once said 17:21 and a fourth 22:31, and taking
    the latest parked the loop for six hours with the quota back an hour in. So take the
    earliest: the first moment work might be possible again. And never sit on a parsed time
    for longer than MAX_PAUSE without looking, since nothing re-checks before it: a run that
    is still out pauses again with a fresh reading, which costs one fast failure, where
    trusting a bad reading costs hours."""
    global paused_until
    paused_until = min(paused_until or at, at, datetime.now() + MAX_PAUSE)
    log(f"out of quota until {paused_until:%Y-%m-%d %H:%M}")


def doer(project, lane):
    """The status bar keeps one entry per actor, and runs overlap across project lanes."""
    return f"{AGENT} ({project} {lane})"


async def deploy(client, card, repo, cwd, notes, base):
    """The last step of the test stage, just before the card is moved to verify. The card
    lands in verify either way: the comment says what was deployed, or why the deploy failed.

    It merges the card's branch into the base branch first, for every project (#106), so
    merging is code and not per-project prose, and the deploy run — which restarts a backend
    or publishes a site — sees the merge. The push waits until after the run: a deploy that
    tests on the base branch has to be able to `reset --hard ORIG_HEAD`. The card's merged
    dot is read back from git; the deployed dot the board sets from the comment's first word
    (`core.comment`), so it does not depend on this process being as new as the code. Returns
    whether the deploy ran: a deploy postponed by the quota is redone by hand in the
    worktree, so it keeps it."""
    id = card["id"]
    # the card is the test card: the deploy updates that run's row and holds its slot
    await call(client, "set_activity", actor=doer(card["project"], card["lane"]), card_id=id,
               doing="deploying")
    done, note = await asyncio.to_thread(land, repo, card, base)
    notes = f"{notes}\n\nThis card's branch: {note}. Do not merge it again."
    cli, blocked = "", []
    try:
        out = await asyncio.to_thread(run_claude, {**card, "lane": "deploy"}, cwd, notes)
        cli = getattr(out, "cli", "")
        out, blocked = blockers(out)
        verdict = [ln.strip(" *`") for ln in out.splitlines()[-1:]]
    except OutOfQuota as e:
        # not retried: verify cards are not polled, so a person runs the deploy again
        pause(e.at)
        await call(client, "comment", id=id, actor=AGENT, text=f"deploy postponed: out of "
                   f"quota, resuming at {e.at:%H:%M}; redeploy by hand ({e})\n\n({note}, "
                   f"not pushed)")
        await call(client, "update_card", id=id, actor=AGENT, attention=True)
        return False
    except Exception as e:
        out, verdict = f"{e}", []
    head = {(DEPLOYED,): DEPLOYED_HEAD, (SKIPPED,): SKIPPED_HEAD}.get(tuple(verdict),
                                                                      FAILED_HEAD)
    if done and head != FAILED_HEAD:   # a failed deploy may have undone it: leave it local
        r = await asyncio.to_thread(git, repo, "push", "origin", base)
        note += "; pushed" if r.returncode == 0 else \
            f"; push failed ({(r.stderr or r.stdout).strip()[-200:]})"
    merged = card["id"] in await asyncio.to_thread(landed, repo)
    # the head sets the deployed dot on the way in; this write only adds what git knows
    await call(client, "comment", id=id, actor=AGENT,
               text=f"{head}{out or '(no output)'}\n\n({note})", output=cli)
    if blocked:   # after the deploy's own result: a deploy has no next lane to stop
        await open_blockers(client, card, blocked)
    # a comment is a reply and clears the dot: set it again, verify waits for a person
    await call(client, "update_card", id=id, actor=AGENT, attention=True, merged=merged)
    return True


async def light_merged(client, cards):
    """The blue dot follows git on every poll, not just at deploy time: a card merged by
    hand, or merged after its deploy ran, stayed dark forever (#106). Only ever lights the
    dot — a move back into an agent lane is what clears it (`core.update_card`), and a
    branch deleted after its merge must not darken a card that is on the base branch."""
    dark = [c for c in cards if not c["merged"] and c["lane"] not in AGENT_LANES]
    if not dark:
        return
    # ponytail: one git call per project per poll, forever, for cards that will never be
    # merged; key a cache on the base branch's sha if it ever shows up in the log
    paths = {p["name"].lower(): p["path"] for p in await call(client, "list_projects")}
    seen = {}
    for c in dark:
        proj = c["project"].lower()
        if proj not in seen:
            path = paths.get(proj)
            seen[proj] = await asyncio.to_thread(landed, Path(path)) if path else set()
        if c["id"] in seen[proj]:
            # attention passed as it is: this is git catching up, not a reply to a person
            await call(client, "update_card", id=c["id"], actor=AGENT, merged=True,
                       attention=c["attention"])


async def tick(client, connect):
    """Start a run for each card waiting on the agent whose project lane has none going. Each
    run opens its own client with `connect`, as it outlives this poll. Returns the runs
    started: main leaves them going, the tests wait for them."""
    global paused_until
    if paused_until:
        if datetime.now() < paused_until:
            return []
        paused_until = None
        log("quota back, resuming")
        await call(client, "set_activity", actor=AGENT)
    started = []
    # list_cards comes back in board order, so the first card eligible for a project lane is
    # the one nearest the top of its column: that is the one that takes the lane's slot
    cards = await call(client, "list_cards")
    for c in cards:
        key = (c["project"].lower(), c["lane"])
        if key[0] == NO_PROJECT.lower():
            continue   # its project was deleted: off limits, silently
        if c["lane"] in NEXT and (c["assignee"] == AGENT or c["auto_advance"]) \
                and key not in running and c["id"] not in working:
            working.add(c["id"])   # here, not in work(): the task may not have run by the next poll
            running[key] = asyncio.create_task(work(connect, c, key))
            started.append(running[key])
    await light_merged(client, cards)
    return started


async def work(connect, c, key):
    try:
        async with connect() as client:
            log(f"#{c['id']} {c['lane']}: {c['title']}")
            who = doer(c["project"], c["lane"])
            await call(client, "set_activity", actor=who, card_id=c["id"], doing=DOING[c["lane"]])
            try:
                # the card shows who is on it: the agent takes it for the run, and hands it
                # back after to whoever had it, a person, or nobody if that was the agent
                back = "" if c["assignee"] in (None, AGENT) else c["assignee"]
                await call(client, "update_card", id=c["id"], actor=AGENT, assignee=AGENT)
                await handle(client, await call(client, "get_card", id=c["id"]), back)
            finally:
                await call(client, "set_activity", actor=who)
    except Exception:   # the backend went away mid-run, say: the next poll tries again
        traceback.print_exc()
    finally:
        del running[key]
        working.discard(c["id"])


def stale(mtime):
    """Has agent.py changed on disk since this process read it, with nothing in flight?
    A deploy that changes the agent only takes effect in a new process; a run in progress
    (or a quota pause, whose end time lives in this process) waits for its poll."""
    return not running and not paused_until and SOURCE.stat().st_mtime != mtime


def restart(lock):
    """Hand over to a fresh agent. The lock port goes first, or the new process would exit
    as 'already running'."""
    log("agent.py changed on disk: restarting")
    lock.close()
    # ponytail: same interpreter, so a deploy that also changes pyproject.toml/uv.lock
    # restarts into an unsynced venv; switch to `uv run` if deps start moving with the agent
    subprocess.Popen([sys.executable, "-u", str(SOURCE)], cwd=str(SOURCE.parent))
    raise SystemExit(0)


def only_one(addr=LOCK):
    """Hold a port for as long as this process lives, so only one agent runs. Two agents
    would each work the same card (the one-run-per-project-lane limit is per process); the
    port is freed by the OS however the agent ends, crash included."""
    s = socket.socket()
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):   # Windows: no one may share the port
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        s.bind(addr)
    except OSError:
        s.close()
        raise SystemExit(f"another {AGENT} is already running (it holds {addr[0]}:{addr[1]}); "
                         "stop it first, or leave it running")
    s.listen()
    return s


async def main(lock=None):
    async with Client(URL) as client:
        await call(client, "create_user", name=AGENT)
        for a in await call(client, "set_activity", actor=AGENT):   # a crashed run's leftovers
            if a["actor"].startswith(AGENT):
                await call(client, "set_activity", actor=a["actor"])
    log(f"{AGENT} polling {URL} every {POLL}s")
    mtime = SOURCE.stat().st_mtime
    while True:
        try:  # a fresh client per poll, so a backend restart does not kill the agent
            async with Client(URL) as client:
                await tick(client, lambda: Client(URL))
        except Exception:
            traceback.print_exc()
        await asyncio.sleep(POLL)
        if lock and stale(mtime):
            restart(lock)


if __name__ == "__main__":
    lock = only_one()   # kept referenced for the process's life
    asyncio.run(main(lock))
