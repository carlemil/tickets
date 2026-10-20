import asyncio
import importlib.util
import io
import json
import os
import re
import socket
import subprocess
import sys
import threading

import pytest

import core

spec = importlib.util.spec_from_file_location("idle_gate", "skill-start/idle_gate.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

TICK = "Tickets loop tick. Read run.md and do one run of it"


@pytest.fixture
def here(tmp_path):
    return str(tmp_path / "Repo")


@pytest.fixture
def proj(here):
    import os
    os.mkdir(here)
    core.create_project("Repo", path=here.replace("\\", "/"))
    return "Repo"


def card(proj, lane, **fields):
    c = core.create_card("c", "person", lane=lane, project=proj)
    if fields:
        core.update_card(c["id"], "person", **fields)
    return c["id"]


def test_an_empty_queue_answers_the_tick_without_the_model(server, proj, here):
    card(proj, "todo")
    card(proj, "verify")
    card(proj, "done")
    line = gate.idle_line(TICK, here, server)
    assert re.fullmatch(r"tickets: nothing to do \(\d{4}-\d\d-\d\d \d\d:\d\d\)", line)
    # the folder matches however it is spelled
    assert gate.idle_line(TICK, here.upper().replace("\\", "/") + "/", server) == line


def test_the_hook_output_stops_the_tick_and_carries_the_idle_line():
    out = gate.hook_output("tickets: nothing to do (2026-10-14 08:00)")
    assert out["continue"] is False  # processing stops: the model is not called
    assert out["stopReason"] == "tickets: nothing to do (2026-10-14 08:00)"


def test_the_hook_prints_nothing_for_a_prompt_that_is_not_a_tick():
    r = subprocess.run([sys.executable, "skill-start/idle_gate.py"], capture_output=True,
                       text=True, input='{"prompt": "fix the bug", "cwd": "."}')
    assert r.returncode == 0 and r.stdout == ""


def test_an_idle_tick_prints_the_stop_json(server, proj, here, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"prompt": TICK, "cwd": here})))
    gate.main(server)
    out = capsys.readouterr().out
    assert '"continue": false' in out  # JSON's lowercase boolean, not Python's False
    data = json.loads(out)
    assert data.keys() == {"continue", "stopReason"} and data["continue"] is False
    assert re.fullmatch(r"tickets: nothing to do \(\d{4}-\d\d-\d\d \d\d:\d\d\)", data["stopReason"])


@pytest.mark.parametrize("lane", ["plan", "develop", "test"])
def test_a_ready_card_lets_the_tick_through(server, proj, here, lane):
    card(proj, lane)
    assert gate.idle_line(TICK, here, server) is None


def test_a_card_in_flight_lets_the_tick_through(server, proj, here):
    card(proj, "develop", assignee="claude-agent")
    assert gate.idle_line(TICK, here, server) is None


def test_cards_a_run_would_skip_do_not_wake_it(server, proj, here):
    card(proj, "plan", questions="1. which?")
    card(proj, "develop", assignee="someone")
    assert gate.idle_line(TICK, here, server) is not None
    card(proj, "plan", questions="1. which?", answers="this one")
    assert gate.idle_line(TICK, here, server) is None


@pytest.mark.parametrize("lane", ["plan", "develop", "test"])
def test_a_blocked_card_does_not_wake_it_until_its_blocker_is_done(server, proj, here, lane):
    blocked = card(proj, lane)
    blocker = card("Home", "todo")  # a blocker may live in another project
    core.link_cards(blocker, blocked, "blocks", "person")
    core.link_cards(blocked, card(proj, "todo"), "blocks", "person")  # it blocks another: no matter
    assert gate.idle_line(TICK, here, server) is not None
    core.update_card(blocker, "person", lane="done")
    assert gate.idle_line(TICK, here, server) is None


@pytest.mark.parametrize("lane", ["plan", "develop", "test"])
@pytest.mark.parametrize("label", ["blocked", "BLOCKED", "BlOcKeD"])
def test_a_manual_hold_stays_idle_until_removed(server, proj, here, lane, label):
    held = card(proj, lane, labels=["important", label], assignee="claude-agent")
    before = core.get_card(held)
    assert not gate.workable(dict(before, stalled=True, session="old-session"), server)
    assert gate.idle_line(TICK, here, server) is not None
    assert core.get_card(held) == before  # evaluating readiness never changes the card
    core.update_card(held, "person", labels=["important"])
    assert gate.idle_line(TICK, here, server) is None


@pytest.mark.parametrize("lane", ["plan", "develop", "test"])
@pytest.mark.parametrize("release_first", ["label", "dependency"])
def test_both_holds_must_clear(server, proj, here, lane, release_first):
    held = card(proj, lane, labels=["blocked"])
    blocker = card("Home", "todo")
    core.link_cards(blocker, held, "blocks", "person")
    assert gate.idle_line(TICK, here, server) is not None
    actions = {
        "label": lambda: core.update_card(held, "person", labels=[]),
        "dependency": lambda: core.update_card(blocker, "person", lane="done"),
    }
    actions.pop(release_first)()
    assert gate.idle_line(TICK, here, server) is not None
    next(iter(actions.values()))()
    assert gate.idle_line(TICK, here, server) is None


@pytest.mark.parametrize("lane", ["plan", "develop", "test"])
def test_a_ready_card_wakes_the_run_alongside_held_cards(server, proj, here, lane):
    card(proj, lane, labels=["blocked"])
    linked = card(proj, lane)
    core.link_cards(card("Home", "todo"), linked, "blocks", "person")
    assert gate.idle_line(TICK, here, server) is not None
    card(proj, lane, labels=["unblocked"])
    assert gate.idle_line(TICK, here, server) is None


@pytest.mark.parametrize("fields", [{"assignee": "person"}, {"questions": "1. Which?"}])
def test_releasing_manual_hold_preserves_other_readiness_checks(server, proj, here, fields):
    held = card(proj, "plan", labels=["blocked"], **fields)
    core.update_card(held, "person", labels=[])
    assert gate.idle_line(TICK, here, server) is not None


def test_manual_hold_does_not_suppress_verify_pr_catch_up(server, proj, here):
    card(proj, "verify", labels=["blocked"], pr="https://example.test/pr/701")
    assert gate.idle_line(TICK, here, server) is None


def test_a_blocker_in_verify_lets_the_tick_through(server, proj, here):
    blocked = card(proj, "develop")
    blocker = card("Home", "develop")
    core.link_cards(blocker, blocked, "blocks", "person")
    assert gate.idle_line(TICK, here, server) is not None
    core.update_card(blocker, "person", lane="verify")
    assert gate.idle_line(TICK, here, server) is None


def test_a_verify_blocker_with_an_unmerged_pr_keeps_the_gate_idle(server, proj, here):
    blocked = card(proj, "develop")
    blocker = card("Home", "verify", pr="https://example.test/pr/1")  # Home: not our PR to catch up
    core.link_cards(blocker, blocked, "blocks", "person")
    assert gate.idle_line(TICK, here, server) is not None
    core.update_card(blocker, "person", merged=True)
    assert gate.idle_line(TICK, here, server) is None


def test_a_pr_project_blocker_in_verify_without_a_pr_keeps_the_gate_idle(server, proj, here):
    """#87: rework cleared its pr and a person dragged it back; it is not shipped."""
    core.create_project("Shop", land="pr")
    blocked = card(proj, "develop")
    blocker = card("Shop", "develop")
    core.link_cards(blocker, blocked, "blocks", "person")
    core.update_card(blocker, "person", lane="verify")
    assert gate.idle_line(TICK, here, server) is not None
    core.update_card(blocker, "person", pr="https://example.test/pr/3", merged=True)
    assert gate.idle_line(TICK, here, server) is None


def test_an_open_pull_request_lets_the_tick_through(server, proj, here):
    card(proj, "verify", pr="https://example.test/pr/1", merged=True)
    assert gate.idle_line(TICK, here, server) is not None
    card(proj, "verify", pr="https://example.test/pr/2")
    assert gate.idle_line(TICK, here, server) is None


@pytest.fixture
def dead_board():
    """A board that is down: accepts each connection and hangs up without answering.
    (A refused connect to a closed localhost port takes ~2 s on Windows.)"""
    ls = socket.socket()
    ls.bind(("127.0.0.1", 0))
    ls.listen()
    ls.settimeout(0.05)   # so the thread sees the stop flag (close() alone does not wake
    stop = threading.Event()  # a blocked accept() on every platform)

    def run():
        while not stop.is_set():
            try:
                ls.accept()[0].close()
            except TimeoutError:
                continue
            except OSError:
                return

    t = threading.Thread(target=run, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{ls.getsockname()[1]}"
    stop.set()
    t.join()
    ls.close()


def test_only_ticks_from_a_known_folder_on_a_live_board_are_answered(server, proj, here,
                                                                     dead_board):
    assert gate.idle_line("fix the bug", here, server) is None
    assert gate.idle_line(TICK, "D:/elsewhere", server) is None
    assert gate.idle_line(TICK, here, dead_board) is None  # board down


def test_the_brief_rules_require_the_no_chips_line():
    text = open("skill-start/run.md", encoding="utf-8").read()
    start = text.index("**The brief is the subagent's whole world**")
    end = text.index("On a card's way out", start)
    rules = text[start:end]
    assert "spawn_task" in rules
    assert "background-task chips" in rules
    assert "dismiss_task" in rules


def test_run_md_holds_rules_not_history():
    # #216: the dispatcher reads run.md on every working tick and pastes much of it into
    # every brief; the reasons and history behind its rules live in PLAN.md (The run).
    raw = open("skill-start/run.md", encoding="utf-8").read()
    assert len(raw) < 50000, len(raw)  # was 53,281 chars before #216
    text = " ".join(raw.split())
    # built from parts: a quoted full name would make every PLAN edit non-docs-only (#163);
    # for the same reason no test reads PLAN's text
    assert "PLAN" + ".md" in text  # it says where the reasons went
    assert not re.search(r"\b20\d\d-\d\d-\d\d\b", text)  # no dated owner's calls
    assert "the owner's call" not in text and "the owner's rule" not in text


def test_run_md_pins_the_docs_only_test_skip():
    text = " ".join(open("skill-start/run.md", encoding="utf-8").read().split())
    for needle in ("**Docs-only skip (#163):**",
                   "diff --name-only --no-renames <base>...card/<id>",
                   "never counts",
                   "any other exit (an error, a missing path) counts as a hit",
                   "`tests: skipped, docs-only (<files>)`",
                   "the verdict is **docs-only**",
                   "`tests: skipped, docs-only` is always **not tested**",
                   "`tests: skipped, docs-only` (docs-only, the rules call for tests)"):
        assert needle in text, needle


def test_run_md_pins_the_built_check():
    text = " ".join(open("skill-start/run.md", encoding="utf-8").read().split())
    for needle in ("**BUILT check (#176):**",
                   "`Files changed:` section, one repo-relative path per line",  # the brief
                   "PASS or FAIL → the BUILT check below first",  # before the comment and move
                   "A brief carrying a `build check: MISMATCH` note (#176)",  # the test stage
                   "diff --name-only --no-renames <base>...card/<id>",
                   "status --porcelain",
                   "build check: MISMATCH",
                   "build check: OK",
                   "BUILT but card/<id> holds no changes"):
        assert needle in text, needle


def test_run_md_pins_the_in_flight_setting():
    text = " ".join(open("skill-start/run.md", encoding="utf-8").read().split())
    for needle in ("**2 cards in flight** by default",
                   "`Cards in flight: N`",
                   "falls back to 2",
                   "developing (2 in flight)"):
        assert needle in text, needle
    assert "fell back to 2 (#193)" in text  # the summary's fallback line
    assert not re.search(r"\b(3|three)\b\**\s+cards\s+in\s+flight", text, re.I)
    assert not re.search(r"up to (3|three) at", text, re.I)
    # docs.html only: a test reading the README would make README edits non-docs-only (#163)
    page = " ".join(open("docs.html", encoding="utf-8").read().split())
    assert "up to 2 cards" in page and "Cards in flight: N" in page
    assert not re.search(r"up to (3|three) (cards )?at", page, re.I)
    assert "among the 3" not in page


def test_run_md_pins_one_suite_run_and_sized_review():
    text = " ".join(open("skill-start/run.md", encoding="utf-8").read().split())
    for needle in ("**One suite run, a sized review (#191):**",
                   "args `<level> <base>...card/<id>`",
                   "`low`: text-only",
                   "do not count toward the level",
                   "`medium`: code, but small, at most ~50 changed code lines",
                   "`high`: any other code diff",
                   "`review: <level> (<n> files, +<a>/-<b>, text-only|code)`",
                   "the one full suite run on that last commit",
                   "again only after a failure it then fixed",
                   "only when it is a known flake",
                   "a flake recorded as fixed (e.g. #165) no longer counts",
                   "runs the tests in the order below (#191)",
                   "Any other failure is real"):
        assert needle in text, needle
    assert "args `<base>...card/<id>`" not in text  # the old bare range, no level
    # docs.html only: a test reading the README would make README edits non-docs-only (#163)
    page = " ".join(open("docs.html", encoding="utf-8").read().split())
    assert "follows the size of the change" in page and "once, after the last commit" in page
    assert "only when it is a known flake" in page and "whatever the develop lane ran" in page


def quoted(name, source):
    # the run.md grep, `["']([^"']*[/\\])?<name>["']`, in Python
    return re.search(r"""["']([^"']*[/\\])?""" + re.escape(name) + r"""["']""", source)


def test_the_docs_only_grep_finds_the_files_the_tests_read():
    import glob
    tests = "".join(open(f, encoding="utf-8").read()
                    for f in glob.glob("test_*.py") + ["conftest.py"])
    assert quoted("run.md", tests)            # read by the test above, so never docs-only
    # built from parts: a quoted full name here would itself be a hit
    assert not quoted("PLAN" + ".md", tests)  # only named in comments
    assert not quoted("README" + ".md", tests)
    assert not quoted("STATUS" + ".md", tests)  # every card's row stays docs-only (#177)
    assert not quoted("run.md", TICK)         # a mention inside a longer string is no hit
    assert quoted("run.md", 'open(r"skill-start\\run.md")')  # a Windows path


def test_run_md_pins_the_targeted_develop_tests():
    text = " ".join(open("skill-start/run.md", encoding="utf-8").read().split())
    start = text.index("**Targeted tests (#192):**")
    end = text.index("Its one board write", start)
    develop = text[start:end]
    for needle in ("diff --name-only --no-renames <base>...card/<id>",
                   "`tests: skipped, docs-only (<files>)`",
                   "test map",
                   "full suite instead",
                   "`conftest.py`",
                   "cannot be narrowed to files",
                   "most of the suite anyway (all of its test files, or all but one)",
                   "`tests: targeted (<files>): <summary>`",
                   "`tests: full suite (<why>): <summary>`"):
        assert needle in develop, needle
    # #736: the build runs only covering tests; the check's one full run comes after it
    assert "never the full suite" in develop
    assert "The check's full run stays mandatory (#192)" in text


def test_run_md_pins_the_dispatcher_deploy():
    text = " ".join(open("skill-start/run.md", encoding="utf-8").read().split())
    for needle in ("**Mechanical deploy (#189):**", "spelled out verbatim",
                   "file classification", "When in doubt", "phone", "never production",
                   "deployed by: dispatcher (mechanical)", "deployed by: subagent (",
                   "`tests: skipped, docs-only`", "`tests: run in test stage, skipped`",
                   "`tests: <count> passed`", "`tests: none in the deploy rules`",
                   '`model: "haiku"`', "Missing deploy instructions are not judgement",
                   "non-zero exit", "applies to your own mechanical deploy",
                   "never one hidden behind a pipe",
                   "is a mechanical deploy"):
        assert needle in text, needle
    assert '4. **Deploy:** a subagent (`model: "haiku"`) deploys per the project' not in text
    # docs.html only: a test reading the README would make README edits non-docs-only (#163)
    page = " ".join(open("docs.html", encoding="utf-8").read().split())
    assert "the dispatcher deploys it itself" in page and "needs judgement" in page
    assert "is mechanical, runs on Haiku" not in page


def test_claude_md_test_map_is_complete():
    claude = open("CLAUDE.md", encoding="utf-8").read()
    rows = [line.strip() for line in claude.splitlines()
            if line.strip().startswith(("| `", "| other", "| a "))]
    mapped = "\n".join(rows)
    assert "| `*.md` |" in mapped and "| a `test_*.py` |" in mapped
    tracked = subprocess.run(["git", "ls-files", "-z"], capture_output=True, text=True,
                             check=True).stdout.split("\0")
    top = {p.split("/")[0] + ("/" if "/" in p else "") for p in tracked if p}
    for item in sorted(top):
        if item.endswith(".md") or re.match(r"test_.*\.py$", item):
            continue
        assert f"`{item}`" in mapped or (item.startswith("skill-")
                                         and "`skill-*/`" in mapped), item
    for name in re.findall(r"`(test_\w+\.py)`", mapped):
        assert os.path.exists(name), name


def test_run_md_pins_one_session_per_card():
    # #736 (reverses #190's split and #234): one agent session per card plans, builds and
    # checks it; the label picks its model for the whole session; behaviour, not wording.
    text = " ".join(open("skill-start/run.md", encoding="utf-8").read().split())
    for needle in ("one card session (#736)", "stage `plan+build`", "stage `build`",
                   "**Card size (#190):**", "both labels → `large`",
                   "Labels match case-insensitively", "at most 5 steps", "When unsure: large",
                   "`Size: small (label)`", "planning and building",
                   "the labels alone pick the session's model"):
        assert needle in text, needle
    model = text[text.index("**Model per stage"):text.index("**Card size (#190):**")]
    assert "card session" in model and "labelled `large`" in model
    assert '`model: "opus"`' in model and '`model: "sonnet"`, for the whole session' in model
    assert "both labels count as `large`" in model and "Not a git repo → `opus`" in model
    assert 'test (a card found in `test`, or a check to redo): `model: "opus"`' in model
    assert "develop: `model:" not in model  # no separate develop agent any more
    plan = text[text.index("### plan"):text.index("### develop")]
    assert "build in the same context" in plan and "`RESULT: PASS`" in plan
    assert "plan-only brief" in plan and "`STAGE: PLANNED`" in plan
    assert "start the **develop** stage" not in plan and "No develop subagent" not in plan
    develop = text[text.index("### develop"):text.index("### test")]
    for needle in ("checks its own build by the test rules", "`code-review` pass as a separate step",
                   "tested commit: <sha>", "goes straight to ship",
                   "equals `git -C <repo> rev-parse card/<id>`", "it gets a fresh test stage",
                   "FAIL → failure; the card stays in `develop`"):
        assert needle in develop, needle
    test = text[text.index("### test"):text.index("PASS → (you) ship")]
    assert "a card session's check after its build" in test and "skips it" in test
    # the queue leaves a card another session works (a /tickets-plan run) alone
    queue = text[text.index("## 2. Build the queue"):text.index("## 3. Work the cards")]
    assert "in flight elsewhere (#736)" in queue and "`stalled` false" in queue
    # every brief's first line names stage, card and model, for the token report
    brief = text[text.index("**The brief is the subagent's whole world**"):
                 text.index("On a card's way out")]
    assert "`Tickets stage: <stage> #<id> (<model>)`" in brief and "usage_report.py" in brief
    # the plan skill: Opus, plan-only, todo and plan lanes, labels large, never builds
    skill = " ".join(open("skill-plan/SKILL.md", encoding="utf-8").read().split())
    front = skill.split("---")[1]
    assert "name: tickets-plan" in front and "disable-model-invocation: true" in front
    for needle in ('`model: "opus"` for every card', "`todo` and `plan`", "640-650",
                   "**plan-only brief**", "`STAGE: PLANNED`", "`Tickets stage: plan #<id> (opus)`",
                   "adds the label `large`", "It never builds", "in flight elsewhere",
                   "run.md", "subagent_type: \"tickets-stage\""):
        assert needle in skill, needle
    start = " ".join(open("skill-start/SKILL.md", encoding="utf-8").read().split())
    assert "~/.claude/skills/tickets-plan" in start and "skill-plan" in start
    docs = open("docs.html", encoding="utf-8").read()
    assert "<code>small</code>" in docs and "<code>large</code>" in docs
    page = " ".join(docs.split())
    for needle in ("/tickets-plan", "skill-plan", "one agent session", "usage_report.py",
                   "a card labelled <code>large</code> is built and checked on Opus"):
        assert needle in page, needle
    assert "built by its plan agent" not in page


def test_run_md_pins_lean_board_calls():
    # #186: the MCP tools echo the whole card with its events; the dispatcher prints only
    # the fields it needs and loads a full card only to build a brief.
    text = " ".join(open("skill-start/run.md", encoding="utf-8").read().split())
    lean = text[text.index("### Lean board calls (#186)"):
                text.index("### Board writes that fail (#147)")]
    for needle in ("print only the fields you need",
                   "Select-Object id,lane,assignee,labels,merged,pr",
                   'Invoke-RestMethod "http://127.0.0.1:8123/api/cards/<id>" | Select-Object',
                   "keep the `ForEach-Object { $_ }`",
                   "IsNullOrWhiteSpace($_.questions)",
                   "kind -in 'comment','moved'", "read whole before the write",
                   "piped to `| Select-Object id,lane,assignee`",
                   "a full card only to build a brief", "ConvertTo-Json -Depth 8",
                   "never retried through the MCP tool",
                   "The MCP tools stay for stage subagents"):
        assert needle in lean, needle
    queue = text[text.index("## 2. Build the queue"):text.index("## 3. Work the cards")]
    assert "the trimmed queue call" in queue and "trimmed refused-write check" in queue
    work = text[text.index("## 3. Work the cards"):text.index("### plan")]
    assert "check the card again (the trimmed check" in work
    assert "your one full-card read per stage" in work
    # the dispatcher's per-verdict reads are the trimmed check, not a full get_card
    assert "Per verdict, after the trimmed check" in text
    assert "Per verdict, after `get_card`" not in text
    assert "(you) After `get_card`" not in text
    # every form starts with the quoted URL, so the allow rule matches: no `(Invoke-`, no `$c.`
    assert "`(Invoke-RestMethod" not in lean and "$c." not in lean
    docs = " ".join(open("docs.html", encoding="utf-8").read().split())
    assert "loads a whole card only to brief an agent" in docs


def test_run_md_pins_slim_stage_board_writes():
    # #203: the MCP write tools answer the whole card with its events unless `slim=True`;
    # every stage board write passes it, and the brief, the agent file and the docs say so.
    text = " ".join(open("skill-start/run.md", encoding="utf-8").read().split())
    brief = text[text.index("**The brief is the subagent's whole world**"):
                 text.index("On a card's way out")]
    for needle in ("that its board writes (`update_card`, `comment`) pass `slim=True` (#203)",
                   "only the card's `id`, `lane` and `assignee`, not the whole card",
                   "it reads the card with `get_card` when it needs it",
                   "Every stage brief carries this line too"):
        assert needle in brief, needle
    # the plan post: all three update_card forms and the MOVED comment
    post = text[text.index("**Post.**"):text.index("It **builds nothing**")]
    forms = re.findall(r"`update_card\(plan=[^`]*`", post)
    assert len(forms) == 3 and all("slim=True)`" in f for f in forms), forms
    assert 'lane="develop", slim=True)`' in post
    assert "`comment(…, slim=True)` the plan saying the card was moved during the run" in post
    # the develop stage's one board write, the checklist tick
    tick = text[text.index("Its one board write while building"):
                text.index("(you) After the trimmed check (a person's move wins): PASS or FAIL")]
    assert 'update_card(checklist=<the full list>, actor="claude-agent", slim=True)' in tick
    # the dispatcher's MCP fallback writes are slim too; its HTTP writes stay the trimmed ones
    lean = text[text.index("### Lean board calls (#186)"):text.index("### Board writes that fail (#147)")]
    for needle in ("Their writes are slim (#203)", "`slim=True`, which answers only `id`, `lane` and `assignee`",
                   "reads stay whole"):
        assert needle in lean, needle
    # the stage agent's body says it; no new tool, so its tools list is the server's as before
    agent = " ".join(open("skill-start/agents/tickets-stage.md", encoding="utf-8").read().split())
    body = agent.split("---", 2)[2]
    assert ("The board writes your brief gives you (`update_card`, `comment`; none if it gives "
            "none) pass `slim=True`") in body
    assert "Read the card with `get_card` when you need it" in body
    # docs.html only: the manual says it in the run section and in the MCP section
    page = " ".join(open("docs.html", encoding="utf-8").read().split())
    assert ("The stage agents' board writes (the plan post, a checklist tick, a comment) pass "
            "<code>slim=True</code>, so each answers with only the card's id, lane and assignee") in page
    mcp = page[page.index('<h2 id="mcp">'):page.index('<h2 id="limits">')]
    assert "Pass <code>slim=True</code> when you do not need the card back" in mcp
    assert '<code>{"id", "lane", "assignee"}</code>' in mcp


def test_run_md_pins_the_base_checkout_check():
    # #204: stages left copies of their card's edits in the base checkout and a land sat
    # skipped for hours; briefs keep stages out of it, the dispatcher checks after each reply.
    text = " ".join(open("skill-start/run.md", encoding="utf-8").read().split())
    brief = text[text.index("**The brief is the subagent's whole world**"):
                 text.index("On a card's way out")]
    for needle in ("where not to write (#204)", "never writes in the base checkout `<repo>`",
                   "`<scratchpad>/card-<id>-<stage>`", "one folder per stage",
                   "never overwrite each other", "`git -C <repo> status --porcelain`",
                   # the verdict stays the reply's last line, as every stage's rules say
                   "before any `tests:` line, `Files changed:` section and the verdict line,"
                   " which stay last"):
        assert needle in brief, needle
    start = text.index("**Base checkout check (#204).**")
    check = text[start:text.index("### plan", start)]
    for needle in ("after every stage reply",
                   "`git -C <repo> status --porcelain --untracked-files=no`",
                   "each card in flight in this run, the replying card first",
                   "`git -C <repo> diff --name-only --no-renames <base>...card/<id>`",
                   "`git -C <repo> diff --quiet card/<id> -- <path>` exits 0",
                   "`git -C <repo> diff --cached --quiet HEAD -- <path>`",
                   "`git -C <repo> diff --cached --quiet card/<id> -- <path>`",
                   "reverted at once, without asking",
                   "`git -C <repo> restore --source=HEAD --staged --worktree -- <paths>`",
                   "base checkout: reverted stray copies of card/<id>",
                   "Any other path is never touched", "each path once per run",
                   "A refused revert follows"):
        assert needle in check, needle
    # before the land and the worktree clean-up, so the land finds the checkout clean
    assert "before that card's other steps" in check and "ship and land" in check
    # and again at the land itself, which a running stage of another card may have dirtied
    assert "the land runs this check once more when it finds the checkout dirty" in check
    land = text[text.index("3. **Land:**"):text.index("4. **Deploy:**")]
    assert "not empty → run the Base checkout check (#204) once more, then look again" in land
    # untracked files are skipped, but not because the merge would ignore them: it refuses
    assert "Untracked files are not checked" in check
    assert "makes the land fail with nothing merged" in check
    wrap = text[text.index("## 4. Wrap up"):]
    assert "each base-checkout path left alone (#204" in wrap
    # docs.html only: a test reading the README would make README edits non-docs-only (#163)
    page = " ".join(open("docs.html", encoding="utf-8").read().split())
    assert "Card sessions and test agents work only in their card's worktree" in page
    assert "every stage keeps its scratch files in a folder of its own" in page
    assert "exact copy of a card's committed work is put back at once" in page
    assert "anything else is left alone and noted on the card" in page


def test_stage_agent_is_pinned():
    agent = open("skill-start/agents/tickets-stage.md", encoding="utf-8").read()
    front = agent.split("---")[1]
    fields = dict(l.split(":", 1) for l in front.strip().splitlines() if ":" in l)
    assert fields["name"].strip() == "tickets-stage"
    tools = [t.strip() for t in fields["tools"].split(",")]
    # The board's tools are named one by one, exactly the server's set: a new MCP tool
    # fails here until the stage agent lists it too.
    from mcp import Client
    import app

    async def listed():
        async with Client(app.mcp) as c:
            return sorted("mcp__tickets__" + t.name for t in (await c.list_tools()).tools)
    # its own thread: in the full suite the Chrome tests leave a loop running in this one
    worker = threading.Thread(target=lambda: server_tools.extend(asyncio.run(listed())))
    server_tools = []
    worker.start()
    worker.join()
    assert tools == ["Bash", "PowerShell", "Read", "Edit", "Write", "Grep", "Glob",
                     "Skill", "ToolSearch", "Monitor"] + server_tools
    assert "model" not in fields and "Agent" not in tools and "spawn_task" not in agent
    run = " ".join(open("skill-start/run.md", encoding="utf-8").read().split())
    assert 'subagent_type: "tickets-stage"' in run
    assert "available agent types, else `general-purpose`" in run
    assert "retried once as `general-purpose`" in run
    skill = " ".join(open("skill-start/SKILL.md", encoding="utf-8").read().split())
    assert "~/.claude/agents/tickets-stage.md" in skill
    assert "a new session is needed before runs use it" in skill
    assert "tickets-stage" in open("docs.html", encoding="utf-8").read()


def test_status_rows_merge_by_union(tmp_path):
    # #177: the change-log rows live in their own append-only file, merged with `union`, so
    # parallel cards' rows never conflict; the design document keeps a normal merge.
    # Names built from parts: a quoted full name would make every status row non-docs-only (#163).
    status = "STATUS" + ".md"
    plan = "PLAN" + ".md"
    out = subprocess.run(["git", "check-attr", "merge", "--", status, plan],
                         capture_output=True, text=True, check=True).stdout.splitlines()
    assert f"{status}: merge: union" in out
    assert f"{plan}: merge: unspecified" in out

    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}

    def git(*args, check=True):
        return subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@example.com",
                               "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false",
                               *args], cwd=tmp_path, env=env, capture_output=True,
                              text=True, check=check)

    git("init", "-q", "-b", "main")
    with open(".gitattributes", "rb") as f:
        (tmp_path / ".gitattributes").write_bytes(f.read())
    (tmp_path / status).write_text("# Status\n\n| Card | Change | State |\n|---|---|---|\n"
                                   "| #1 | first | done |\n", newline="\n")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    for branch, row in (("one", "| #2 | second | done \u2014 +1 check |\n"),
                        ("two", "| #3 | third | done \u2014 +1 check |\n")):
        git("checkout", "-q", "-b", branch, "main")
        with open(tmp_path / status, "a", encoding="utf-8", newline="\n") as f:
            f.write(row)
        git("commit", "-q", "-am", branch)
    git("checkout", "-q", "one")
    merged = git("merge", "--no-edit", "two", check=False)
    assert merged.returncode == 0, merged.stdout + merged.stderr
    text = (tmp_path / status).read_text(encoding="utf-8")
    assert "| #2 |" in text and "| #3 |" in text and "| #1 |" in text
    assert not re.search(r"^(<<<<<<<|=======|>>>>>>>)", text, re.M)
    assert text.endswith("\n")


def test_briefs_keep_questions_on_the_card_not_in_the_session():
    # #448: a question in the main session blocks every in-flight card and leaves no record;
    # the run never asks in chat, and every brief and the agent file send a question for a
    # person to the card's blocker/open-questions section, the reply still ending in a verdict.
    # Key terms only, not phrasing (#215).
    text = " ".join(open("skill-start/run.md", encoding="utf-8").read().split())
    head = text[:text.index("## 0.")]  # the run-wide rules
    for term in ("`AskUserQuestion`", "`questions`", "`## Blocked by`", "verdict", "failure"):
        assert term in head, term
    brief = text[text.index("**The brief is the subagent's whole world**"):
                 text.index("On a card's way out")]
    for term in ("`## Blocked by`", "`## Open questions`", "verdict line"):
        assert term in brief, term
    agent = " ".join(open("skill-start/agents/tickets-stage.md", encoding="utf-8").read().split())
    body = agent.split("---", 2)[2]
    for term in ("`## Blocked by`", "`## Open questions`", "verdict line"):
        assert term in body, term


def test_run_md_pins_stalled_takeover_and_api_error_release():
    # #493: behaviour the run relies on, not wording
    text = " ".join(open("skill-start/run.md", encoding="utf-8").read().split())
    queue = text[text.index("## 2. Build the queue"):text.index("## 3. Work the cards")]
    assert "stalled" in queue and "session" in queue       # the queue reads both fields
    assert "taken over, not skipped" in queue              # a stalled card is not a skip
    assert "full suite" in queue                           # test restarts on committed state
    assert "a person has" in queue                         # never a person's card
    assert "`api error:`" in queue                         # a released card waits for its reset
    failure = text[text.index("**API error (#493):**"):text.index("## 4. Wrap up")]
    for needle in ("429", "overloaded", "terminated early", "session limit", "worktree kept",
                   "unassign", "reset", "30 minutes"):
        assert needle in failure, needle
    assert "never in a hot loop" in failure
    wrap = text[text.index("## 4. Wrap up"):]
    assert "clear your activity" in wrap and "never removed" in wrap


def test_run_md_pins_the_queue_order():
    # #737: closest to done first, so a resumed run lands nearly done cards before new work
    text = " ".join(open("skill-start/run.md", encoding="utf-8").read().split())
    queue = text[text.index("## 2. Build the queue"):text.index("## 3. Work the cards")]
    assert "`test`, then `develop`, then `plan`" in queue
    assert "Order: `plan`, then" not in queue
    page = " ".join(open("docs.html", encoding="utf-8").read().split())
    assert "closest to done first" in page