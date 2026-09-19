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

Any failure comments why, unassigns and turns auto advance off, so the card sits in its
lane until a person looks: no retry loop.
"""

import asyncio
import json
import re
import shutil
import subprocess
import traceback
from pathlib import Path

from mcp import Client

from core import NO_PROJECT   # a constant only: the agent reaches the board over MCP

AGENT = "claude-agent"
URL = "http://127.0.0.1:8123/mcp"
POLL = 15
NEXT = {"plan": "develop", "develop": "test", "test": "verify"}
PASS = "RESULT: PASS"
NO_QUESTIONS = "QUESTIONS: NONE"
DOING = {"plan": "planning", "develop": "developing", "test": "testing"}   # the status bar
QUESTIONS_HEAD = re.compile(r"^#+[ \t]*open questions[ \t]*:?[ \t]*$", re.I | re.M)

PROMPTS = {
    "plan": "Plan the implementation of this ticket card. Its `description` is the request. "
            "If it already has `questions` and `answers`, those are a person's answers to "
            "your previous round: build them into the plan and do not ask them again. Read "
            "the code in the current directory as needed. Nobody can answer you during this "
            "run, so do not ask questions. Your final reply is saved as the card's `plan`, "
            "so reply with the complete plan in markdown (restate the request in its Context "
            "section), not a summary or a pointer to a file. If anything needs a person's "
            "decision, put it in a last section headed exactly '## Open questions', one "
            "bullet each: that section is saved apart, as the card's `questions`. End with "
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
}
# planning runs in plan mode (what /plan switches on): read-only by design.
# development is a normal agent in auto mode: it edits and runs the tests on its own, with
# Claude Code's auto-mode checks still on. Testing needs Bash for the tests and gets full
# rights in the repo; its prompt asks for no edits, which is a request, not a sandbox.
FLAGS = {"plan": ["--permission-mode", "plan"], "develop": ["--permission-mode", "auto"],
         "test": ["--dangerously-skip-permissions"]}


async def call(client, tool, **args):
    r = await client.call_tool(tool, args)
    if r.is_error:
        raise RuntimeError(r.content[0].text)
    # list and str returns come wrapped as structured {"result": ...}; dicts only as JSON text
    if r.structured_content is not None:
        return r.structured_content["result"]
    return json.loads(r.content[0].text)


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
        raise RuntimeError(f"claude exited {e.returncode}: {(e.stderr or e.stdout)[-2000:]}") from e


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          encoding="utf-8")


def workspace(card, path):
    """Where a stage runs, and what the prompt is told about it: (cwd, note, worktree).

    Develop and test get a git worktree of their own per card, next to the repo
    (<repo>.worktrees/card-<id>, branch card/<id>, made from the repo's current branch), so
    one card's changes never mix with another's or with a person's work in the repo.
    Planning only reads, so it runs in the repo. A card that went through develop before
    worktrees has its changes in the repo itself, so its test runs there. A folder that is
    not a git repository is worked in place, as before worktrees."""
    top = git(path, "rev-parse", "--show-toplevel")
    if card["lane"] == "plan" or top.returncode:
        return path, "" if card["lane"] == "plan" else (
            "Workspace: this folder is not a git repository. Do not commit."), None
    top = Path(top.stdout.strip())
    root = top.parent / f"{top.name}.worktrees" / f"card-{card['id']}"
    branch = f"card/{card['id']}"
    base = git(top, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if not root.is_dir():
        if card["lane"] == "test":
            return path, ("Workspace: this card was built before cards got a worktree of "
                          "their own, so its changes are uncommitted in this folder (git "
                          "diff, git status), possibly mixed with other work."), None
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
    return root / path.relative_to(top), note, root


def split_plan(out):
    """Claude's plan reply -> (plan, questions, open?). The verdict line is dropped, and the
    '## Open questions' section, which the prompt asks for last, becomes the questions."""
    lines = out.splitlines()
    verdict = lines[-1].strip(" *`")
    body = "\n".join(lines[:-1]) if verdict.startswith("QUESTIONS:") else out
    m = QUESTIONS_HEAD.search(body)
    plan, questions = (body[:m.start()], body[m.end():]) if m else (body, "")
    is_open = verdict != NO_QUESTIONS
    return plan.strip(), questions.strip() if is_open else "", is_open


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
        cwd, note, tree = workspace(card, cwd)
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
        if lane == "test" and [ln.strip(" *`") for ln in out.splitlines()[-1:]] != [PASS]:
            raise RuntimeError(f"the test stage did not end in {PASS}")
        nxt = NEXT[lane]
        # an auto-advancing card goes on to the next stage, which the next poll picks up
        if card["auto_advance"] and nxt in NEXT:
            await call(client, "update_card", id=id, actor=AGENT, lane=nxt, assignee=back)
        else:
            # handed back: the dot says it is waiting for you. auto_advance is passed as it
            # is, since a forward move would switch it on, and this card is meant to stop
            await call(client, "update_card", id=id, actor=AGENT, lane=nxt, assignee=back,
                       attention=True, auto_advance=card["auto_advance"])
    except Exception as e:  # any failure: say why, hand the card back, no retry loop
        await call(client, "comment", id=id, actor=AGENT, text=f"agent failed: {e}")
        await call(client, "update_card", id=id, actor=AGENT, assignee=back, auto_advance=False,
                   attention=True)


async def tick(client):
    # ponytail: sequential, one card at a time; add a worker pool if the queue backs up.
    for c in await call(client, "list_cards"):
        if c["project"].lower() == NO_PROJECT.lower():
            continue   # its project was deleted: off limits, silently
        if c["lane"] in NEXT and (c["assignee"] == AGENT or c["auto_advance"]):
            print(f"#{c['id']} {c['lane']}: {c['title']}", flush=True)
            await call(client, "set_activity", actor=AGENT, card_id=c["id"], doing=DOING[c["lane"]])
            try:
                # the card shows who is on it: the agent takes it for the run, and hands it
                # back after to whoever had it, a person, or nobody if that was the agent
                back = "" if c["assignee"] in (None, AGENT) else c["assignee"]
                await call(client, "update_card", id=c["id"], actor=AGENT, assignee=AGENT)
                await handle(client, await call(client, "get_card", id=c["id"]), back)
            finally:
                await call(client, "set_activity", actor=AGENT)


async def main():
    async with Client(URL) as client:
        await call(client, "create_user", name=AGENT)
        await call(client, "set_activity", actor=AGENT)   # a crashed run may have left one
    print(f"{AGENT} polling {URL} every {POLL}s", flush=True)
    while True:
        try:  # a fresh client per poll, so a backend restart does not kill the agent
            async with Client(URL) as client:
                await tick(client)
        except Exception:
            traceback.print_exc()
        await asyncio.sleep(POLL)


if __name__ == "__main__":
    asyncio.run(main())
