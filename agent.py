"""Board agent: plans and develops cards assigned to it, over MCP. uv run python agent.py

Assignment is the "go" signal. A card assigned to AGENT in `plan` or `develop` gets headless
Claude Code run on it in D:/source/<project>; the output becomes a comment, the card moves
one lane on and is unassigned. Unassigning is both the human gate (read the plan, assign
again to have it built) and the loop guard (the agent never re-triggers on its own write).
"""

import asyncio
import json
import shutil
import subprocess
import traceback
from pathlib import Path

from mcp import Client

AGENT = "claude-agent"
URL = "http://127.0.0.1:8123/mcp"
ROOT = Path("D:/source")
POLL = 15
NEXT = {"plan": "develop", "develop": "test"}

PROMPTS = {
    "plan": "Write an implementation plan for this ticket card. Read the code in the current "
            "directory as needed, but do not edit any files. Reply with the plan only.",
    "develop": "Implement this ticket card, following the plan in its comments (the latest "
               "comments win). Run the project's tests. Do not commit. Reply with a short "
               "summary of what you changed and the test result.",
}
# planning runs in the default mode, where headless edits are denied: read-only by design.
# development runs unattended and needs Bash for the tests, so it gets full rights in the repo.
FLAGS = {"plan": [], "develop": ["--dangerously-skip-permissions"]}


async def call(client, tool, **args):
    r = await client.call_tool(tool, args)
    if r.is_error:
        raise RuntimeError(r.content[0].text)
    # list and str returns come wrapped as structured {"result": ...}; dicts only as JSON text
    if r.structured_content is not None:
        return r.structured_content["result"]
    return json.loads(r.content[0].text)


def run_claude(card, cwd):
    lane = card["lane"]
    prompt = f"{PROMPTS[lane]}\n\nCard:\n{json.dumps(card, indent=2, ensure_ascii=False)}"
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
        cwd = ROOT / card["project"]
        if not cwd.is_dir():
            raise RuntimeError(f"no repo at {cwd}")
        out = await asyncio.to_thread(run_claude, card, cwd)
        await call(client, "comment", id=id, actor=AGENT, text=out or "(no output)")
        await call(client, "update_card", id=id, actor=AGENT, lane=NEXT[lane], assignee="")
    except Exception as e:  # any failure: say why, hand the card back, no retry loop
        await call(client, "comment", id=id, actor=AGENT, text=f"agent failed: {e}")
        await call(client, "update_card", id=id, actor=AGENT, assignee="")


async def tick(client):
    # ponytail: sequential, one card at a time; add a worker pool if the queue backs up.
    for c in await call(client, "list_cards", assignee=AGENT):
        if c["lane"] in NEXT:
            print(f"#{c['id']} {c['lane']}: {c['title']}", flush=True)
            await handle(client, await call(client, "get_card", id=c["id"]))


async def main():
    async with Client(URL) as client:
        await call(client, "create_user", name=AGENT)
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
