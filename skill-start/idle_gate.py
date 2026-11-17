"""UserPromptSubmit hook: an idle Tickets loop tick never reaches the model.

A tick re-reads the whole session from cache just to answer "nothing to do", which
costs hundreds of thousands of tokens in a long session. This answers it for free:
when the prompt is a loop tick and the board has nothing a run would take, the hook
stops it (`continue: false`) with the idle line as the stop reason. Anything
uncertain — board down, no project for this folder, a card that might be workable —
lets the tick through, and the run itself decides. Stdlib only: it runs on the system python, not the venv.
A tick that should end the loop goes through too, since only the model can delete the
loop's job: its deadline passed (#823), or it is the second idle tick in a row (#1056),
which the gate counts per session in a small state file and tells the model about.
"""
import json
import os
import re
import sys
import tempfile
import time
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8123"
TICK = "Tickets loop tick."
STATE = os.path.join(tempfile.gettempdir(), "tickets-idle-gate")
IDLE_STOP = 2  # idle ticks in a row that end the loop (#1056)
STOP_NOTE = ("tickets idle gate: this is the second tick in a row with nothing to work on: "
             "stop the loop (idle twice).")


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
        if any(workable(c, base) for c in cards):
            return None
    except Exception:
        return None
    return "tickets: nothing to do (%s)" % now  # an empty board is idle too (#1056)


def hook_output(line):
    """The hook's JSON: stop before the model, with the idle line as the stop reason."""
    return {"continue": False, "stopReason": line}


def stop_output():
    """The hook's JSON on the last idle tick: let it through, telling the model to stop."""
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                   "additionalContext": STOP_NOTE}}


def count_idle(session, idle, state=STATE):
    """Idle ticks in a row for this session, this one included; a busy tick resets to 0."""
    path = os.path.join(state, re.sub(r"[^\w-]", "_", session))
    try:
        with open(path) as f:
            n = int(f.read()) + 1 if idle else 0
    except (OSError, ValueError):
        n = 1 if idle else 0
    try:
        os.makedirs(state, exist_ok=True)
        with open(path, "w") as f:
            f.write(str(0 if n >= IDLE_STOP else n))
    except OSError:
        pass
    return n


def decide(prompt, cwd, session, base=BASE, state=STATE):
    """The hook's JSON for this prompt, or None to let it through untouched."""
    if not prompt.lstrip().startswith(TICK):
        return None
    line = idle_line(prompt, cwd, base)
    if not session:
        return hook_output(line) if line else None
    if count_idle(session, bool(line), state) >= IDLE_STOP:
        return stop_output()
    return hook_output(line) if line else None


def main(base=BASE, state=STATE):
    data = json.load(sys.stdin)
    out = decide(data.get("prompt", ""), data.get("cwd", ""), data.get("session_id", ""),
                 base, state)
    if out:
        print(json.dumps(out))


if __name__ == "__main__":
    main()
