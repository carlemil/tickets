"""Board agent: plans, develops and tests cards, over MCP. uv run python agent.py

A card in `plan`, `develop` or `test` gets headless Claude Code run on it in its project's
configured path, with the project's instructions in the prompt, when it is assigned to
AGENT or has `auto_advance` on. The output becomes a comment and the card moves one lane on.

Plan is different: Claude runs in plan mode and its plan replaces the card's description
(the old text survives in the `edited` event). If the plan ends in QUESTIONS: OPEN, or in
no verdict at all, the card stays in plan for a person to answer them in the description
and hand it back; auto advance is switched off so it is not replanned every poll.

Assigned: the card is then unassigned. That is both the human gate (read the plan, assign
again to have it built) and the loop guard (the agent never re-triggers on its own write).
Auto advance: the card keeps going, plan -> develop -> test -> verify, and stops at verify
for a person. The test stage must end in RESULT: PASS to move on.

Any failure comments why, unassigns and turns auto advance off, so the card sits in its
lane until a person looks: no retry loop.
"""

import asyncio
import json
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

PROMPTS = {
    "plan": "Plan the implementation of this ticket card. Read the code in the current "
            "directory as needed. Nobody can answer you during this run, so do not ask "
            "questions: list anything you need a person to decide under a heading "
            "'## Open questions'. Your final reply is saved verbatim as the card's new "
            "description, replacing it, so reply with the complete plan in markdown (restate "
            "the request in its Context section), not a summary or a pointer to a file. End "
            f"with a last line of exactly {NO_QUESTIONS} if nothing is left open, otherwise "
            "QUESTIONS: OPEN.",
    "develop": "Implement this ticket card, following the plan in its description and any "
               "later comments (the latest comments win). Run the project's tests. Do not "
               "commit. Reply with a short summary of what you changed and the test result.",
    "test": "Check that this ticket card is done: review the uncommitted changes (git diff, "
            "git status) against the card and the plan in its comments, and run the "
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


async def handle(client, card):
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
        out = await asyncio.to_thread(run_claude, card, cwd, proj["instructions"])
        # a run takes minutes; if a person moved the card meanwhile, their move wins: keep
        # the output as a comment, but do not move, unassign or switch off a card that is
        # no longer where this run found it
        now = (await call(client, "get_card", id=id))["lane"]
        if now != lane:
            await call(client, "comment", id=id, actor=AGENT, text=f"{out or '(no output)'}"
                       f"\n\n(this {lane} run finished after the card moved to {now}: "
                       "left as it is)")
            return
        if lane == "plan":
            if not out:
                raise RuntimeError("planning produced no plan")   # never blank a description
            lines = out.splitlines()
            verdict = lines[-1].strip(" *`")
            plan = "\n".join(lines[:-1]).strip() if verdict.startswith("QUESTIONS:") else out
            await call(client, "update_card", id=id, actor=AGENT, description=plan)
            if verdict != NO_QUESTIONS:
                await call(client, "comment", id=id, actor=AGENT, text="the plan has open "
                           "questions: answer them in the description, then assign the card back")
                off = {"auto_advance": False} if card["auto_advance"] else {}
                await call(client, "update_card", id=id, actor=AGENT, assignee="", **off)
                return
            await call(client, "comment", id=id, actor=AGENT, text="plan written to the description")
        else:
            await call(client, "comment", id=id, actor=AGENT, text=out or "(no output)")
        if lane == "test" and [ln.strip(" *`") for ln in out.splitlines()[-1:]] != [PASS]:
            raise RuntimeError(f"the test stage did not end in {PASS}")
        nxt = NEXT[lane]
        # an auto-advancing card keeps its assignee until it reaches the last stage it can
        if card["auto_advance"] and nxt in NEXT:
            await call(client, "update_card", id=id, actor=AGENT, lane=nxt)
        else:
            await call(client, "update_card", id=id, actor=AGENT, lane=nxt, assignee="")
    except Exception as e:  # any failure: say why, hand the card back, no retry loop
        await call(client, "comment", id=id, actor=AGENT, text=f"agent failed: {e}")
        await call(client, "update_card", id=id, actor=AGENT, assignee="", auto_advance=False)


async def tick(client):
    # ponytail: sequential, one card at a time; add a worker pool if the queue backs up.
    for c in await call(client, "list_cards"):
        if c["project"].lower() == NO_PROJECT.lower():
            continue   # its project was deleted: off limits, silently
        if c["lane"] in NEXT and (c["assignee"] == AGENT or c["auto_advance"]):
            print(f"#{c['id']} {c['lane']}: {c['title']}", flush=True)
            await call(client, "set_activity", actor=AGENT, card_id=c["id"], doing=DOING[c["lane"]])
            try:
                await handle(client, await call(client, "get_card", id=c["id"]))
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
