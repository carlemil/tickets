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

Develop and test only ever run in the card's own git worktree. A test that passes
commits everything left in the worktree and pushes the card's branch to origin before the
card moves to verify, so what a person verifies is on the remote. Once it is in verify
the agent deploys it: the backend if the backend changed, the app to the phone if the app
changed and a phone is connected (none connected: skipped). How is up to the project's
instructions; either way the card stays in verify, with the result as a comment.

Projects run side by side, but each project has one run at a time: a card waits while
another card of its project is worked, and the highest-priority waiting card goes next.

A deploy that changes agent.py takes effect on its own: between polls, with no run going,
the agent sees the new file, frees its lock port and starts a fresh process of itself.

Any failure comments why, unassigns and turns auto advance off, so the card sits in its
lane until a person looks: no retry loop. Running out of quota is not a failure: the
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

from core import NO_PROJECT, PRIORITIES, number   # pure helpers only: the board is reached over MCP

AGENT = "claude-agent"
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
            "project's tests. Do not edit any files. Reply with what you checked and what "
            f"you found, and end with a last line of exactly {PASS} or RESULT: FAIL.",
    # not a lane: run on a card the agent just moved to verify
    "deploy": "This ticket card passed testing and is committed and pushed (the workspace "
              "note below says where its changes are). Deploy it. Look at what the card "
              "changed: if it changed the backend, deploy the backend; if it changed the "
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
# rights in the repo; its prompt asks for no edits, which is a request, not a sandbox.
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


def run_claude(card, cwd, instructions=""):
    lane = card["lane"]
    project = f"Project instructions (follow them):\n{instructions}\n\n" if instructions else ""
    prompt = (f"{PROMPTS[lane]}\n\n{project}"
              f"Card:\n{json.dumps(card, indent=2, ensure_ascii=False)}")
    try:
        return subprocess.run(
            [shutil.which("claude") or "claude", "-p", *FLAGS[lane]], input=prompt, cwd=cwd,
            capture_output=True, text=True, encoding="utf-8", timeout=3600, check=True,
        ).stdout.strip()
    except subprocess.CalledProcessError as e:
        text = e.stderr or e.stdout or ""
        if m := LIMIT.search(text):
            raise OutOfQuota(text, resume_at(m[1], datetime.now())) from e
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
    Planning only reads, so it runs in the repo. Develop and test never run anywhere but
    the worktree: a folder that is not a git repository, or a test with no worktree to
    test, fails the card."""
    if card["lane"] == "plan":
        return path, "", None, None
    top = git(path, "rev-parse", "--show-toplevel")
    if top.returncode:
        raise RuntimeError(f"{path} is not a git repository: develop and test only run in "
                           "a git worktree")
    top = Path(top.stdout.strip())
    root = top.parent / f"{top.name}.worktrees" / f"card-{card['id']}"
    branch = f"card/{card['id']}"
    base = git(top, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if not root.is_dir():
        if card["lane"] == "test":
            raise RuntimeError(f"there is no worktree at {root} to test: move the card back "
                               "to develop to build it in one")
        have = git(top, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0
        made = git(top, "worktree", "add", str(root), branch) if have else \
            git(top, "worktree", "add", "-b", branch, str(root))
        if made.returncode:
            raise RuntimeError(f"could not make a worktree at {root}: {made.stderr.strip()}")
    note = (f"Workspace: a git worktree of its own for this card, on branch {branch}, made "
            f"from {base}. ")
    note += (f"Commit your work on {branch} when you are done. Do not push, merge or switch "
             "branches." if card["lane"] == "develop" else
             f"This card's changes are the commits on {branch} since it left {base} (git diff "
             f"{base}...HEAD, git log {base}..HEAD) plus anything uncommitted (git status).")
    return root / path.relative_to(top), note, root, base


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


async def handle(client, card, back=""):
    """`back` is who had the card before the agent took it: every way out gives it back."""
    lane, id = card["lane"], card["id"]
    try:
        name = card["project"].lower()   # a card from before projects may differ in case
        proj = next((p for p in await call(client, "list_projects")
                     if p["name"].lower() == name), None)
        if not proj or not proj["path"]:
            raise RuntimeError(f"project {card['project']!r} has no path configured")
        cwd = Path(proj["path"])
        if not cwd.is_dir():
            raise RuntimeError(f"no repo at {cwd}")
        cwd, note, tree, base = workspace(card, cwd)
        notes = "\n\n".join(x for x in (proj["instructions"], note) if x)
        out = await asyncio.to_thread(run_claude, card, cwd, notes)
        # so a person knows where to look, and what to merge
        where = f"\n\n(worked in {tree}, branch card/{id})" if tree else ""
        # a run takes minutes; if a person moved the card meanwhile, their move wins: keep
        # the output as a comment, but do not move or switch off a card that is no longer
        # where this run found it. Only let go of it, unless a person took it meanwhile.
        now = await call(client, "get_card", id=id)
        if now["lane"] != lane:
            await call(client, "comment", id=id, actor=AGENT, text=f"{out or '(no output)'}"
                       f"\n\n(this {lane} run finished after the card moved to {now['lane']}: "
                       "left as it is)")
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
                           "questions: fill in the answers and the card is replanned")
                off = {"auto_advance": False} if card["auto_advance"] else {}
                await call(client, "update_card", id=id, actor=AGENT, assignee=back,
                           attention=True, **off)
                return
            # no questions left: the last round's questions and answers stay, as the record
            # of what was decided
            await call(client, "update_card", id=id, actor=AGENT, plan=plan)
            await call(client, "comment", id=id, actor=AGENT, text="plan written")
        else:
            await call(client, "comment", id=id, actor=AGENT, text=(out or "(no output)") + where)
        if lane == "test":
            if [ln.strip(" *`") for ln in out.splitlines()[-1:]] != [PASS]:
                raise RuntimeError(f"the test stage did not end in {PASS}")
            sha = await asyncio.to_thread(ship, card, tree)
            await call(client, "comment", id=id, actor=AGENT,
                       text=f"committed and pushed card/{id} to origin ({sha})")
        nxt = NEXT[lane]
        # an auto-advancing card goes on to the next stage, which the next poll picks up
        if card["auto_advance"] and nxt in NEXT:
            await call(client, "update_card", id=id, actor=AGENT, lane=nxt, assignee=back)
        else:
            # handed back: the dot says it is waiting for you. auto_advance is passed as it
            # is, since a forward move would switch it on, and this card is meant to stop
            await call(client, "update_card", id=id, actor=AGENT, lane=nxt, assignee=back,
                       attention=True, auto_advance=card["auto_advance"])
        if nxt == "verify":
            await deploy(client, card, cwd, notes, base)
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
        await call(client, "comment", id=id, actor=AGENT, text=f"agent failed: {e}")
        await call(client, "update_card", id=id, actor=AGENT, assignee=back, auto_advance=False,
                   attention=True)


# project (lower case) -> its one run in flight. A project's cards run one at a time, so
# two runs never share its tests, ports or database; different projects run side by side.
# ponytail: per process; two agent processes would each keep their own.
running = {}
# quota is the account's, not a project's: while it is out, no run starts anywhere
paused_until = None


def pause(at):
    global paused_until
    paused_until = max(paused_until or at, at)
    print(f"out of quota until {paused_until:%Y-%m-%d %H:%M}", flush=True)


def doer(project):
    """The status bar keeps one entry per actor, and runs overlap across projects."""
    return f"{AGENT} ({project})"


async def deploy(client, card, cwd, notes, base):
    """The last step, on a card just moved to verify. It stays in verify either way: the
    comment says what was deployed, or why the deploy failed. The card's merged and
    deployed dots are set from it: merged from git, since a deploy can merge and push and
    still fail after; deployed from the verdict."""
    id = card["id"]
    await call(client, "set_activity", actor=doer(card["project"]), card_id=id, doing="deploying")
    try:
        out = await asyncio.to_thread(run_claude, {**card, "lane": "deploy"}, cwd, notes)
        verdict = [ln.strip(" *`") for ln in out.splitlines()[-1:]]
    except OutOfQuota as e:
        # not retried: verify cards are not polled, so a person runs the deploy again
        pause(e.at)
        await call(client, "comment", id=id, actor=AGENT, text=f"deploy postponed: out of "
                   f"quota, resuming at {e.at:%H:%M}; redeploy by hand ({e})")
        await call(client, "update_card", id=id, actor=AGENT, attention=True)
        return
    except Exception as e:
        out, verdict = f"{e}", []
    head = {(DEPLOYED,): "deployed: ", (SKIPPED,): "deploy skipped: "}.get(tuple(verdict),
                                                                          "deploy failed: ")
    # the worktree shares refs with the repo, so the deploy's push is already in origin/<base>
    merged = git(cwd, "merge-base", "--is-ancestor", f"card/{id}", f"origin/{base}").returncode == 0
    await call(client, "comment", id=id, actor=AGENT, text=head + (out or "(no output)"))
    # a comment is a reply and clears the dot: set it again, verify waits for a person
    await call(client, "update_card", id=id, actor=AGENT, attention=True, merged=merged,
               deployed=verdict == [DEPLOYED])


async def tick(client, connect):
    """Start a run for each card waiting on the agent whose project has none going. Each
    run opens its own client with `connect`, as it outlives this poll. Returns the runs
    started: main leaves them going, the tests wait for them."""
    global paused_until
    if paused_until:
        if datetime.now() < paused_until:
            return []
        paused_until = None
        print("quota back, resuming", flush=True)
        await call(client, "set_activity", actor=AGENT)
    started = []
    # highest priority first: it takes its project's one slot. Ties keep list_cards' order.
    cards = sorted(await call(client, "list_cards"), key=lambda c: -PRIORITIES.index(c["priority"]))
    for c in cards:
        key = c["project"].lower()
        if key == NO_PROJECT.lower():
            continue   # its project was deleted: off limits, silently
        if c["lane"] in NEXT and (c["assignee"] == AGENT or c["auto_advance"]) \
                and key not in running:
            running[key] = asyncio.create_task(work(connect, c, key))
            started.append(running[key])
    return started


async def work(connect, c, key):
    try:
        async with connect() as client:
            print(f"#{c['id']} {c['lane']}: {c['title']}", flush=True)
            who = doer(c["project"])
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


def stale(mtime):
    """Has agent.py changed on disk since this process read it, with nothing in flight?
    A deploy that changes the agent only takes effect in a new process; a run in progress
    (or a quota pause, whose end time lives in this process) waits for its poll."""
    return not running and not paused_until and SOURCE.stat().st_mtime != mtime


def restart(lock):
    """Hand over to a fresh agent. The lock port goes first, or the new process would exit
    as 'already running'."""
    print("agent.py changed on disk: restarting", flush=True)
    lock.close()
    subprocess.Popen([sys.executable, "-u", str(SOURCE)], cwd=str(SOURCE.parent))
    raise SystemExit(0)


def only_one(addr=LOCK):
    """Hold a port for as long as this process lives, so only one agent runs. Two agents
    would each work the same card (the one-run-per-project limit is per process); the
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
    print(f"{AGENT} polling {URL} every {POLL}s", flush=True)
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
