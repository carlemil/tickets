import importlib.util
import re
import socket
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
    for doc in ("docs.html", "README.md"):
        page = " ".join(open(doc, encoding="utf-8").read().split())
        assert "up to 2" in page and "Cards in flight: N" in page, doc
        assert not re.search(r"up to (3|three) (cards )?at", page, re.I), doc
        assert "among the 3" not in page, doc


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
