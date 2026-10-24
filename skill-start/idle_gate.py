"""UserPromptSubmit hook: an idle Tickets loop tick never reaches the model.

A tick re-reads the whole session from cache just to answer "nothing to do", which
costs hundreds of thousands of tokens in a long session. This answers it for free:
when the prompt is a loop tick and the board has nothing a run would take, the hook
stops it (`continue: false`) with the idle line as the stop reason. Anything
uncertain — board down, no project for this folder, a card that might be workable —
lets the tick through, and the run itself decides. Stdlib only: it runs on the system python, not the venv.
A tick that should end the loop (#823: its deadline passed, or plan, develop and test
are empty) goes through too: only the model can delete the loop's job.
"""
import json
import re
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


def workable(card, base):
    """Mirrors run.md's queue: a card a run would take, or a PR it would catch up on."""
    if card["lane"] == "verify":
        return bool(card["pr"]) and not card["merged"]
    if card["lane"] not in ("plan", "develop", "test"):
        return False
    if any(label.casefold() == "blocked" for label in card.get("labels", [])):
        return False  # an explicit manual hold, including stalled agent assignments
    if card["questions"] and not card["answers"]:
        return False  # waiting for a person's answers
    if card["assignee"] not in (None, "", "claude-agent"):
        return False
    # blocked by an unfinished card, any project: the server's own rule (#35, #62)
    return not card["blocked_by"]


def clock():
    """Local time as the tick prompt writes it, so the two compare as strings."""
    return time.strftime("%Y-%m-%d %H:%M")


def idle_line(prompt, cwd, base=BASE):
    """The idle answer when this tick has nothing to do, else None (let the model run)."""
    if not prompt.lstrip().startswith(TICK):
        return None
    now = clock()
    until = re.search(r"loop ends at (\d{4}-\d\d-\d\d \d\d:\d\d)", prompt)
    if until and now >= until.group(1):
        return None  # the model stops the loop
    try:
        project = next((p for p in get(base, "/api/projects")
                        if p["path"] and norm(p["path"]) == norm(cwd)), None)
        if project is None:
            return None
        cards = get(base, "/api/cards?project=" + urllib.parse.quote(project["name"]))
        if not any(c["lane"] in ("plan", "develop", "test") for c in cards):
            return None  # an empty board: the model stops the loop
        if any(workable(c, base) for c in cards):
            return None
    except Exception:
        return None
    return "tickets: nothing to do (%s)" % now


def hook_output(line):
    """The hook's JSON: stop before the model, with the idle line as the stop reason."""
    return {"continue": False, "stopReason": line}


def main(base=BASE):
    data = json.load(sys.stdin)
    line = idle_line(data.get("prompt", ""), data.get("cwd", ""), base)
    if line:
        print(json.dumps(hook_output(line)))


if __name__ == "__main__":
    main()
