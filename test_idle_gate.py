import importlib.util
import os
import re
import socket
import subprocess
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


def test_a_blocked_card_does_not_wake_it_until_its_blocker_is_done(server, proj, here):
    blocked = card(proj, "develop")
    blocker = card("Home", "todo")  # a blocker may live in another project
    core.link_cards(blocker, blocked, "blocks", "person")
    core.link_cards(blocked, card(proj, "todo"), "blocks", "person")  # it blocks another: no matter
    assert gate.idle_line(TICK, here, server) is not None
    core.update_card(blocker, "person", lane="done")
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
                   "BUILT → the BUILT check below first",  # runs before the comment and move
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
                   "fewer slots spend fewer tokens",
                   "fewer merge conflicts at land",
                   "more slots finish a batch sooner on the clock",
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
                   "`medium`: code, but small, at most ~50 changed lines",
                   "`high`: any other code diff",
                   "`review: <level> (<n> files, +<a>/-<b>, text-only|code)`",
                   "the one full suite run on that last commit",
                   "again only after a failure it then fixed",
                   "only when it is a known flake",
                   "a flake recorded as fixed (e.g. #165) no longer counts",
                   "runs the tests in the order below (#191)",
                   "no rerun and hope"):
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
    assert "The test stage's full run stays mandatory (#192)" in text


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


def test_run_md_pins_the_small_card_path():
    text = " ".join(open("skill-start/run.md", encoding="utf-8").read().split())
    for needle in ("**Card size (#190):**", "Both labels → `large`",
                   "Labels match case-insensitively",
                   "Not a git repo → never the one-agent path",
                   "at most 5 steps", "When unsure: large", "`Size: small (label)`",
                   "planning and building", "#187 reuses this paragraph",
                   "for every large card", "unless a label is `large`"):
        assert needle in text, needle
    plan = text[text.index("### plan"):text.index("### develop")]
    assert "`STAGE: BUILT`" in plan and "build in the same context" in plan
    assert "BUILT check (#176)" in plan and "No develop subagent" in plan
    assert "builds nothing** unless the card is small" in plan
    docs = open("docs.html", encoding="utf-8").read()
    assert "<code>small</code>" in docs and "<code>large</code>" in docs
    assert "built on Opus by its plan agent" in " ".join(docs.split())


def test_stage_agent_is_pinned():
    agent = open("skill-start/agents/tickets-stage.md", encoding="utf-8").read()
    front = agent.split("---")[1]
    fields = dict(l.split(":", 1) for l in front.strip().splitlines() if ":" in l)
    assert fields["name"].strip() == "tickets-stage"
    tools = [t.strip() for t in fields["tools"].split(",")]
    assert tools == ["Bash", "PowerShell", "Read", "Edit", "Write", "Grep", "Glob",
                     "Skill", "ToolSearch", "mcp__tickets"]
    assert "model" not in fields
    run = " ".join(open("skill-start/run.md", encoding="utf-8").read().split())
    assert 'subagent_type: "tickets-stage"' in run and "`general-purpose`" in run
    assert "retried once as `general-purpose`" in run
    skill = " ".join(open("skill-start/SKILL.md", encoding="utf-8").read().split())
    assert "~/.claude/agents/tickets-stage.md" in skill and "new session" in skill
    assert "tickets-stage" in open("docs.html", encoding="utf-8").read()
