"""UserPromptSubmit hook: an idle Tickets loop tick never reaches the model.

A tick re-reads the whole session from cache just to answer "nothing to do", which
costs hundreds of thousands of tokens in a long session. This answers it for free:
when the prompt is a loop tick and the board has nothing a run would take, the hook
blocks the prompt and shows the idle line instead. Anything uncertain — board down,
no project for this folder, a card that might be workable — lets the tick through,
and the run itself decides. Stdlib only: it runs on the system python, not the venv.
"""
import json
import sys
import time
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8123"
TICK = "Tickets loop tick."


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=3) as r:
        return json.load(r)


def norm(path):
    return path.replace("\\", "/").rstrip("/").lower()


def workable(card):
    """Mirrors run.md's queue: a card a run would take, or a PR it would catch up on.
    Blocked cards still count (telling needs a get_card each): rare, and the run skips them."""
    if card["lane"] == "verify":
        return bool(card["pr"]) and not card["merged"]
    if card["lane"] not in ("plan", "develop", "test"):
        return False
    if card["questions"] and not card["answers"]:
        return False  # waiting for a person's answers
    return card["assignee"] in (None, "", "claude-agent")


def idle_line(prompt, cwd, base=BASE):
    """The idle answer when this tick has nothing to do, else None (let the model run)."""
    if not prompt.lstrip().startswith(TICK):
        return None
    try:
        project = next((p for p in get(base, "/api/projects")
                        if p["path"] and norm(p["path"]) == norm(cwd)), None)
        if project is None:
            return None
        cards = get(base, "/api/cards?project=" + urllib.parse.quote(project["name"]))
    except Exception:
        return None
    if any(workable(c) for c in cards):
        return None
    return "tickets: nothing to do (%s)" % time.strftime("%Y-%m-%d %H:%M")


if __name__ == "__main__":
    data = json.load(sys.stdin)
    line = idle_line(data.get("prompt", ""), data.get("cwd", ""))
    if line:
        print(json.dumps({"decision": "block", "reason": line}))
