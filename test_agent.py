"""The board agent against the real MCP tools in-process. Claude itself is faked."""

import asyncio
import json
import re
import shutil
import socket
import subprocess
import threading

from pathlib import Path

import pytest
from mcp import Client

import agent
import app
import core


def make_repo(r):
    """A git repository with one commit on branch main, and a bare repo beside it as its
    origin, so the agent can push."""
    r.mkdir()
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"],
                 ["config", "user.name", "t"]):
        agent.git(r, *args)
    (r / "a.txt").write_text("a")
    agent.git(r, "add", ".")
    agent.git(r, "commit", "-q", "-m", "one")
    origin = r.parent / f"{r.name}.origin.git"
    agent.git(r.parent, "init", "-q", "--bare", str(origin))
    agent.git(r, "remote", "add", "origin", str(origin))
    return r


def pushed(repo, id):
    """The commit origin has on the card's branch, or None."""
    r = agent.git(repo.parent / f"{repo.name}.origin.git", "rev-parse", "--verify", "--quiet",
                  f"refs/heads/card/{id}")
    return r.stdout.strip() or None


@pytest.fixture
def ran(monkeypatch, tmp_path):
    """Fake claude: records each card it was run on (and the folder it ran in: the repo,
    "Proj", for planning, the card's worktree for the rest), answers with its lane (and a
    passing verdict in test). "Proj" is a configured project whose path is a git repo;
    "Pathless" has no path set."""
    make_repo(tmp_path / "Proj")
    core.create_project("Proj", path=str(tmp_path / "Proj"), instructions="use uv")
    core.create_project("Pathless")
    calls = []

    def fake(card, cwd, instructions=""):
        calls.append((card["id"], card["lane"], cwd.name))
        return agent.Reply(f"did {card['lane']}" + {"test": "\nRESULT: PASS",
                                                    "plan": "\nQUESTIONS: NONE",
                                                    "deploy": "\nDEPLOY: OK"}.get(card["lane"], ""),
                           f"● transcript of {card['lane']}")

    monkeypatch.setattr(agent, "run_claude", fake)
    return calls


def tick():
    async def go():
        async with Client(app.mcp) as c:
            await asyncio.gather(*await agent.tick(c, lambda: Client(app.mcp)))
    asyncio.run(go())


def card(lane, assignee=agent.AGENT, project="Proj", auto=False):
    id = core.create_card("t", "ce", lane=lane, assignee=assignee, project=project,
                          auto_advance=auto)["id"]
    path = core.get_project(project)["path"]
    if lane == "test" and path and (Path(path) / ".git").exists():   # developed: has a worktree
        agent.workspace({"id": id, "lane": "develop"}, Path(path))
    return id


def at(id, lane):
    """Where the fake claude ran for this card's stage."""
    return "Proj" if lane == "plan" else f"card-{id}"


def comments(id):
    return [e["detail"]["text"] for e in core.get_card(id)["events"] if e["kind"] == "comment"]


@pytest.mark.parametrize("lane, nxt", [("plan", "develop"), ("develop", "test"),
                                       ("test", "verify")])
def test_assigned_card_is_worked_moved_and_handed_back(ran, lane, nxt):
    id = card(lane)
    tick()
    c = core.get_card(id)
    assert (c["lane"], c["assignee"]) == (nxt, None)
    assert (c["plan"] if lane == "plan" else comments(id)[0]).startswith(f"did {lane}")
    assert ran == [(id, lane, at(id, lane))] + (
        [(id, "deploy", f"card-{id}")] if lane == "test" else []), "verify: then deployed"
    assert all(e["actor"] == agent.AGENT for e in c["events"][1:])


def test_a_card_the_agent_moves_queues_behind_the_lane_it_lands_in(ran):
    waiting = card("develop", assignee=None)
    moved = card("plan")
    tick()
    assert [c["id"] for c in core.list_cards(lane="develop")] == [waiting, moved]


def test_second_tick_does_not_retrigger_on_its_own_write(ran):
    card("plan")
    tick()
    tick()
    assert len(ran) == 1


@pytest.mark.parametrize("lane", ["todo", "verify", "done"])
def test_other_lanes_are_ignored(ran, lane):
    id = card(lane)
    tick()
    assert ran == [] and core.get_card(id)["assignee"] == agent.AGENT


def test_unassigned_and_foreign_cards_are_ignored(ran):
    card("plan", assignee=None)
    card("develop", assignee="ce")
    tick()
    assert ran == []


def test_archived_card_is_ignored(ran):
    id = card("plan")
    core.update_card(id, "ce", archived=True)
    tick()
    assert ran == []


def test_prompt_sees_comments_and_the_whole_card(ran, monkeypatch):
    id = card("develop")
    core.comment(id, "ce", "use the plan, but skip step 3")
    seen = []
    monkeypatch.setattr(agent, "run_claude", lambda c, cwd, instr: seen.append(c) or "ok")
    tick()
    assert "skip step 3" in str(seen[0]["events"])


def test_a_project_without_a_path_fails_without_moving(ran):
    id = card("plan", project="Pathless")
    tick()
    c = core.get_card(id)
    assert (c["lane"], c["assignee"]) == ("plan", None)
    assert comments(id) == ["agent failed: project 'Pathless' has no path configured"]
    assert ran == []


def test_a_configured_path_that_has_since_gone_fails_without_moving(ran, tmp_path):
    (tmp_path / "gone").mkdir()
    core.update_project("Proj", path=str(tmp_path / "gone"))
    (tmp_path / "gone").rmdir()
    id = card("plan")
    tick()
    assert core.get_card(id)["lane"] == "plan"
    assert comments(id)[0].startswith("agent failed: no repo at")
    assert ran == []


def test_the_project_path_and_instructions_reach_claude(ran, monkeypatch, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    core.update_project("Proj", path=str(other))
    id = card("plan")
    seen = []
    monkeypatch.setattr(agent, "run_claude",
                        lambda c, cwd, instr: seen.append((cwd, instr)) or "ok\nQUESTIONS: NONE")
    tick()
    assert seen == [(other, "use uv")], "the configured path, not a folder named after it"
    assert core.get_card(id)["lane"] == "develop"


def test_a_card_whose_project_differs_in_case_still_finds_it(ran, db):
    import sqlite3
    id = card("plan")
    raw = sqlite3.connect(db)                 # a card stored before projects normalised names
    raw.execute("UPDATE cards SET project='proj' WHERE id=?", (id,))
    raw.commit()
    raw.close()
    tick()
    assert ran == [(id, "plan", "Proj")]


@pytest.mark.parametrize("err", [subprocess.TimeoutExpired("claude", 3600),
                                 RuntimeError("claude exited 1: boom")])
def test_claude_failure_is_reported_and_card_handed_back(ran, monkeypatch, err):
    id = card("develop")

    def boom(card, cwd, instructions=""):
        raise err
    monkeypatch.setattr(agent, "run_claude", boom)
    tick()
    c = core.get_card(id)
    assert (c["lane"], c["assignee"]) == ("develop", None)
    assert comments(id) == [f"agent failed: {err}"]


def test_one_failure_does_not_stop_the_rest(ran, monkeypatch):
    bad, good = card("plan", project="Pathless"), card("plan")
    tick()
    assert core.get_card(bad)["lane"] == "plan"
    assert core.get_card(good)["lane"] == "develop"


def test_run_claude_wraps_a_nonzero_exit_with_stderr(monkeypatch, tmp_path):
    def fail(*a, **k):
        raise subprocess.CalledProcessError(2, "claude", output="", stderr="bad flag")
    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(RuntimeError, match="claude exited 2: bad flag"):
        agent.run_claude({"lane": "plan", "id": 1}, tmp_path)


def test_run_claude_uses_stdin_and_read_only_planning(monkeypatch, tmp_path):
    got = {}

    def fake(cmd, **k):
        got.update(cmd=cmd, **k)
        return subprocess.CompletedProcess(cmd, 0, stdout="  plan text \n")
    monkeypatch.setattr(subprocess, "run", fake)
    assert agent.run_claude({"lane": "plan", "id": 7, "title": "Ärende"}, tmp_path) == "plan text"
    assert "--dangerously-skip-permissions" not in got["cmd"]
    assert "Ärende" in got["input"] and got["cwd"] == tmp_path
    agent.run_claude({"lane": "develop", "id": 7}, tmp_path)
    assert got["cmd"][-2:] == ["--permission-mode", "auto"], "a normal agent in auto mode"
    assert "--dangerously-skip-permissions" not in got["cmd"]
    assert "plan" not in got["cmd"], "develop never runs in plan mode"


def test_run_claude_puts_project_instructions_before_the_card(monkeypatch, tmp_path):
    got = {}
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: got.update(k) or
                        subprocess.CompletedProcess(cmd, 0, stdout="ok"))
    agent.run_claude({"lane": "plan", "id": 7}, tmp_path, "never touch prod")
    assert "Project instructions (follow them):\nnever touch prod" in got["input"]
    assert got["input"].index("never touch prod") < got["input"].index("Card:")
    agent.run_claude({"lane": "plan", "id": 7}, tmp_path)
    assert "Project instructions" not in got["input"], "no empty heading when there are none"


# ---------- auto advance ----------

def test_auto_advance_runs_plan_to_verify_one_stage_per_tick(ran):
    id = card("plan", assignee=None, auto=True)
    for lane in ["develop", "test", "verify"]:
        tick()
        assert core.get_card(id)["lane"] == lane
    tick()
    c = core.get_card(id)
    assert ran == [(id, "plan", "Proj"), (id, "develop", f"card-{id}"),
                   (id, "test", f"card-{id}"), (id, "deploy", f"card-{id}")]
    assert (c["lane"], c["assignee"], c["auto_advance"]) == ("verify", None, True), \
        "stops at verify for a person, with the switch left as it was"


def test_an_auto_card_is_let_go_between_stages(ran):
    id = card("plan", auto=True)
    for lane in ["develop", "test", "verify"]:
        tick()
        assert (core.get_card(id)["lane"], core.get_card(id)["assignee"]) == (lane, None)


def test_auto_advance_works_a_card_assigned_to_a_person(ran):
    id = card("develop", assignee="ce", auto=True)
    tick()
    assert (core.get_card(id)["lane"], core.get_card(id)["assignee"]) == ("test", "ce")


@pytest.mark.parametrize("lane", ["todo", "verify", "done"])
def test_auto_advance_leaves_other_lanes_alone(ran, lane):
    """todo is the backlog: moving a card to plan is the go signal."""
    id = card(lane, assignee=None, auto=True)
    tick()
    assert ran == [] and core.get_card(id)["lane"] == lane


def test_an_archived_auto_card_is_ignored(ran):
    id = card("plan", assignee=None, auto=True)
    core.update_card(id, "ce", archived=True)
    tick()
    assert ran == []


@pytest.mark.parametrize("out", ["all good\nRESULT: FAIL", "all good", "",
                                 "RESULT: PASS\nbut then more", "result: pass"])
def test_the_test_stage_moves_on_only_on_a_final_pass(ran, monkeypatch, out):
    id = card("test", auto=True)
    monkeypatch.setattr(agent, "run_claude", lambda c, cwd, instr: out)
    tick()
    c = core.get_card(id)
    assert (c["lane"], c["assignee"], c["auto_advance"]) == ("test", None, False)
    said, failed = comments(id)
    assert said.startswith(out or "(no output)") and said.endswith(f"branch card/{id})")
    assert failed == "agent failed: the test stage did not end in RESULT: PASS"
    tick()
    assert len(comments(id)) == 2, "switched off, so it does not retry"


@pytest.mark.parametrize("last", ["RESULT: PASS", "**RESULT: PASS**", "`RESULT: PASS` "])
def test_a_final_pass_counts_even_in_markdown(ran, monkeypatch, last):
    id = card("test")
    monkeypatch.setattr(agent, "run_claude", lambda c, cwd, instr: "checked\n" + last)
    tick()
    assert core.get_card(id)["lane"] == "verify"


def test_a_failure_turns_auto_advance_off_and_stops_the_run(ran, monkeypatch):
    id = card("plan", assignee=None, auto=True)
    tick()

    def boom(card, cwd, instructions=""):
        raise RuntimeError("claude exited 1: boom")
    monkeypatch.setattr(agent, "run_claude", boom)
    tick()
    c = core.get_card(id)
    assert (c["lane"], c["assignee"], c["auto_advance"]) == ("develop", None, False)
    assert comments(id)[-1] == "agent failed: claude exited 1: boom"
    tick()
    assert len(comments(id)) == 2, "no retry loop"


def test_a_failure_on_a_plain_card_logs_no_auto_advance_change(ran):
    id = card("plan", project="Pathless")
    tick()
    fields = [e["detail"].get("field") for e in core.get_card(id)["events"]]
    assert "auto_advance" not in fields


def test_run_claude_tests_with_rights_and_asks_for_a_verdict(monkeypatch, tmp_path):
    got = {}
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: got.update(cmd=cmd, **k) or
                        subprocess.CompletedProcess(cmd, 0, stdout="ok"))
    agent.run_claude({"lane": "test", "id": 7}, tmp_path)
    assert "--dangerously-skip-permissions" in got["cmd"]
    p = got["input"]
    assert "RESULT: PASS" in p and "Fix what you find and can fix yourself" in p
    assert "out of your hands" in p and "stops here for a person" in p


# ---------- the plan stage ----------

def plan_with(monkeypatch, out):
    monkeypatch.setattr(agent, "run_claude", lambda c, cwd, instr: out)


def test_a_plan_without_questions_goes_in_the_plan_and_halts(ran, monkeypatch):
    id = core.create_card("t", "ce", description="make it blue", lane="plan",
                          assignee=agent.AGENT, project="Proj")["id"]
    plan_with(monkeypatch, "## Context\nmake it blue\n\n## Steps\n1. paint\nQUESTIONS: NONE")
    tick()
    c = core.get_card(id)
    assert c["plan"] == "## Context\nmake it blue\n\n## Steps\n1. paint"
    assert (c["description"], c["questions"]) == ("make it blue", ""), "the request is kept"
    assert (c["lane"], c["assignee"]) == ("develop", None)
    assert comments(id) == ["plan written"]
    tick()
    assert core.get_card(id)["lane"] == "develop", "halted: nothing picks it up in develop"


@pytest.mark.parametrize("auto", [False, True])
def test_open_questions_keep_the_card_in_plan_for_a_person(ran, monkeypatch, auto):
    id = card("plan", auto=auto)
    plan_with(monkeypatch, "## Steps\n1. x\n## Open questions\n- red or blue?\nQUESTIONS: OPEN")
    tick()
    c = core.get_card(id)
    assert (c["plan"], c["questions"]) == ("## Steps\n1. x", "1. red or blue?")
    assert (c["lane"], c["assignee"], c["auto_advance"]) == ("plan", None, False)
    assert comments(id)[-1].startswith("the plan has open questions")
    n = len(c["events"])
    tick()
    assert len(core.get_card(id)["events"]) == n, "not replanned every poll"


def test_open_questions_on_a_plain_card_log_no_auto_advance_change(ran, monkeypatch):
    id = card("plan")
    plan_with(monkeypatch, "x\nQUESTIONS: OPEN")
    tick()
    assert "auto_advance" not in [e["detail"].get("field") for e in core.get_card(id)["events"]]


def test_a_plan_with_no_verdict_counts_as_open_and_keeps_every_line(ran, monkeypatch):
    id = card("plan")
    plan_with(monkeypatch, "## Steps\n1. paint")
    tick()
    c = core.get_card(id)
    assert (c["lane"], c["plan"], c["questions"]) == ("plan", "## Steps\n1. paint", "")


@pytest.mark.parametrize("last", ["**QUESTIONS: NONE**", "`QUESTIONS: NONE` "])
def test_a_markdown_verdict_counts(ran, monkeypatch, last):
    id = card("plan")
    plan_with(monkeypatch, "steps\n" + last)
    tick()
    assert (core.get_card(id)["lane"], core.get_card(id)["plan"]) == ("develop", "steps")


def test_an_auto_card_with_no_questions_carries_on_into_develop(ran):
    id = card("plan", auto=True)
    tick()
    c = core.get_card(id)
    assert (c["lane"], c["assignee"], c["auto_advance"]) == ("develop", None, True)


@pytest.mark.parametrize("out", ["", "QUESTIONS: NONE", "## Open questions\n- x\nQUESTIONS: OPEN"])
def test_an_empty_plan_fails_and_leaves_the_old_one_alone(ran, monkeypatch, out):
    id = card("plan")
    core.update_card(id, "ce", plan="keep me")
    plan_with(monkeypatch, out)
    tick()
    c = core.get_card(id)
    assert (c["lane"], c["plan"], c["questions"]) == ("plan", "keep me", "")
    assert comments(id) == ["agent failed: planning produced no plan"]


def test_run_claude_plans_in_plan_mode_and_asks_for_a_questions_verdict(monkeypatch, tmp_path):
    got = {}
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: got.update(cmd=cmd, **k) or
                        subprocess.CompletedProcess(cmd, 0, stdout="ok"))
    agent.run_claude({"lane": "plan", "id": 7}, tmp_path)
    assert got["cmd"][-2:] == ["--permission-mode", "plan"]
    assert "QUESTIONS: NONE" in got["input"] and "## Open questions" in got["input"]


# ---------- the status bar ----------

@pytest.mark.parametrize("lane, doing", [("plan", "planning"), ("develop", "developing"),
                                         ("test", "testing")])
def test_activity_is_shown_while_claude_runs_and_cleared_after(ran, monkeypatch, lane, doing):
    id = card(lane)
    seen = []
    monkeypatch.setattr(agent, "run_claude", lambda c, cwd, instr:
                        seen.append(core.list_activity()) or "x\nQUESTIONS: NONE\nRESULT: PASS")
    tick()
    assert [(r["actor"], r["card_id"], r["doing"]) for r in seen[0]] == \
        [(agent.doer("Proj", lane), id, doing)]
    assert core.list_activity() == []


def test_activity_is_cleared_when_the_card_fails(ran, monkeypatch):
    card("develop", project="Pathless")
    tick()
    assert core.list_activity() == []


def test_activity_is_cleared_even_when_handling_crashes(ran, monkeypatch):
    card("develop")

    async def crash(client, card):
        raise ConnectionError("server went away")
    monkeypatch.setattr(agent, "handle", crash)
    tick()   # logged, not raised: one run crashing leaves the others and the poll going
    assert core.list_activity() == [] and agent.running == {}


# ---------- deleted projects ----------

@pytest.mark.parametrize("auto", [False, True])
def test_cards_in_no_project_are_never_worked(ran, auto):
    id = card("plan", auto=auto)
    core.delete_project("Proj")
    tick()
    c = core.get_card(id)
    assert ran == [] and comments(id) == [], "left alone, silently"
    assert (c["lane"], c["project"]) == ("plan", core.NO_PROJECT)
    assert core.list_activity() == []


def test_a_card_moved_into_plan_by_a_person_is_picked_up(ran):
    id = card("todo", assignee=None)
    core.update_card(id, "ce", lane="plan")        # the drag on the board
    tick()
    c = core.get_card(id)
    assert ran == [(id, "plan", "Proj")]
    assert (c["lane"], c["auto_advance"]) == ("develop", True), "and it keeps going"


# ---------- a person moves the card while the agent works on it ----------

@pytest.mark.parametrize("lane, moved_to, out", [
    ("test", "develop", "not done\nRESULT: FAIL"),     # what happened to card #3
    ("test", "develop", "fine\nRESULT: PASS"),
    ("develop", "plan", "built it"),
])
def test_a_card_moved_during_the_run_is_left_where_the_person_put_it(ran, monkeypatch,
                                                                      lane, moved_to, out):
    id = card(lane, auto=True)

    def slow(c, cwd, instructions=""):
        core.update_card(id, "ce", lane=moved_to)   # the person, mid-run
        return out
    monkeypatch.setattr(agent, "run_claude", slow)
    tick()
    c = core.get_card(id)
    assert (c["lane"], c["assignee"], c["auto_advance"]) == (moved_to, None, True), \
        "not moved on, not switched off; only let go of"
    [said] = comments(id)
    assert said.startswith(out) and f"after the card moved to {moved_to}" in said


def test_a_card_still_in_its_lane_is_handled_as_before(ran):
    id = card("develop", auto=True)
    tick()
    assert core.get_card(id)["lane"] == "test"
    assert "moved to" not in comments(id)[0]


# attention: the dot that says "an agent handed this back, waiting for you"

@pytest.mark.parametrize("lane", ["plan", "develop", "test"])
def test_a_card_handed_back_asks_for_attention(ran, lane):
    id = card(lane)
    tick()
    assert core.get_card(id)["attention"]


def test_open_questions_ask_for_attention(ran, monkeypatch):
    id = card("plan", auto=True)
    plan_with(monkeypatch, "x\nQUESTIONS: OPEN")
    tick()
    assert core.get_card(id)["attention"]


def test_a_failure_asks_for_attention(ran):
    id = card("plan", project="Pathless", auto=True)
    tick()
    assert core.get_card(id)["attention"]


def test_an_auto_card_asks_only_when_it_stops_at_verify(ran):
    id = card("plan", auto=True)
    for lane in ["develop", "test"]:
        tick()
        c = core.get_card(id)
        assert (c["lane"], c["attention"]) == (lane, False), "still moving: nothing to ask"
    tick()
    c = core.get_card(id)
    assert (c["lane"], c["attention"]) == ("verify", True)


def test_a_card_moved_during_the_run_does_not_ask(ran, monkeypatch):
    id = card("develop", auto=True)

    def slow(c, cwd, instructions=""):
        core.update_card(id, "ce", lane="todo")
        return "built"
    monkeypatch.setattr(agent, "run_claude", slow)
    tick()
    assert not core.get_card(id)["attention"], "the person is already on it"


# plan, questions and answers: split out of the reply, and the person's reply restarts it

@pytest.mark.parametrize("out, plan, questions", [
    ("steps\n## Open questions\n- a?\n- b?\nQUESTIONS: OPEN", "steps", "1. a?\n2. b?"),
    ("steps\n### open questions:\n- a?\nQUESTIONS: OPEN", "steps", "1. a?"),
    ("steps\n## Open questions\nNone.\nQUESTIONS: NONE", "steps", ""),
    ("steps\n## Open questions\n\n\n- a?\nQUESTIONS: OPEN", "steps", "1. a?"),
    ("## Open questions\n\n## Later\nx\nQUESTIONS: OPEN", "", "## Later\nx"),
    ("steps\n## Open questions in the text is not a heading\nQUESTIONS: OPEN",
     "steps\n## Open questions in the text is not a heading", ""),
    ("steps\nno verdict", "steps\nno verdict", ""),
])
def test_split_plan(out, plan, questions):
    assert agent.split_plan(out)[:2] == (plan, questions)


def test_a_clean_replan_keeps_the_answered_questions_as_the_record(ran, monkeypatch):
    id = card("plan")
    core.update_card(id, "ce", plan="old", questions="- red?", answers="blue")
    plan_with(monkeypatch, "paint it blue\nQUESTIONS: NONE")
    tick()
    c = core.get_card(id)
    assert (c["plan"], c["questions"], c["answers"]) == ("paint it blue", "1. red?", "blue")


def test_the_prompt_tells_claude_where_each_part_goes(monkeypatch, tmp_path):
    got = {}
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: got.update(k) or
                        subprocess.CompletedProcess(cmd, 0, "x", ""))
    agent.run_claude({"lane": "plan", "answers": "blue"}, tmp_path)
    assert "`questions` and `answers`" in got["input"] and "## Open questions" in got["input"]
    assert '"answers": "blue"' in got["input"]
    agent.run_claude({"lane": "develop"}, tmp_path)
    assert "`plan` the plan to follow" in got["input"]
    assert "## Blocked by" in got["input"], "every lane is asked what stops it"


def test_answering_open_questions_gets_the_card_replanned(ran, monkeypatch):
    id = card("plan", auto=True)
    plan_with(monkeypatch, "steps\n## Open questions\n- red?\nQUESTIONS: OPEN")
    tick()
    assert core.get_card(id)["auto_advance"] is False
    core.update_card(id, "ce", answers="blue")
    c = core.get_card(id)
    assert (c["attention"], c["auto_advance"]) == (False, True), "the reply hands it back"
    plan_with(monkeypatch, "paint it blue\nQUESTIONS: NONE")
    tick()
    c = core.get_card(id)
    assert (c["lane"], c["plan"], c["answers"]) == ("develop", "paint it blue", "blue")


def test_a_comment_on_a_failed_card_gets_it_retried(ran):
    id = card("develop", project="Pathless")
    tick()
    assert comments(id)[-1].startswith("agent failed")
    core.update_project("Pathless", path=core.get_project("Proj")["path"])
    core.comment(id, "ce", "path set, try again")
    tick()
    assert core.get_card(id)["lane"] == "test"


@pytest.mark.parametrize("lane, nxt", [("plan", "develop"), ("develop", "test"),
                                       ("test", "verify")])
def test_the_agents_own_move_forward_does_not_switch_auto_advance_on(ran, lane, nxt):
    id = card(lane)
    tick()
    c = core.get_card(id)
    assert (c["lane"], c["auto_advance"]) == (nxt, False), "handed back, it stays stopped"
    tick()
    assert core.get_card(id)["lane"] == nxt


def test_a_person_moving_a_handed_back_card_on_starts_the_agent(ran):
    id = card("plan")
    tick()
    assert core.get_card(id)["lane"] == "develop"
    agent.workspace({"id": id, "lane": "develop"}, Path(core.get_project("Proj")["path"]))
    core.update_card(id, "ce", lane="test")   # built it by hand, in the card's worktree
    assert core.get_card(id)["auto_advance"] is True
    tick()
    assert core.get_card(id)["lane"] == "verify"


# ---------- the agent assigns itself while it works ----------

@pytest.mark.parametrize("lane", ["plan", "develop", "test"])
def test_the_agent_is_assigned_while_it_works(ran, monkeypatch, lane):
    id = card(lane, assignee=None, auto=True)
    seen = []

    def look(c, cwd, instructions=""):
        seen.append((c["assignee"], core.get_card(id)["assignee"]))
        return "x\nQUESTIONS: NONE\nRESULT: PASS"
    monkeypatch.setattr(agent, "run_claude", look)
    tick()
    assert seen[:1] == [(agent.AGENT, agent.AGENT)]
    took = [e for e in core.get_card(id)["events"] if e["kind"] == "assigned"]
    assert [(e["actor"], e["detail"]["to"]) for e in took][:1] == [(agent.AGENT, agent.AGENT)]


@pytest.mark.parametrize("how", ["next stage", "questions", "failure", "reached verify"])
def test_a_persons_card_goes_back_to_them_on_every_way_out(ran, monkeypatch, how):
    id = card("test" if how == "reached verify" else "plan", assignee="ce", auto=True,
              project="Pathless" if how == "failure" else "Proj")
    if how == "questions":
        plan_with(monkeypatch, "x\nQUESTIONS: OPEN")
    tick()
    assert core.get_card(id)["assignee"] == "ce"


def test_a_card_a_person_took_meanwhile_stays_theirs(ran, monkeypatch):
    id = card("develop", assignee=None, auto=True)

    def slow(c, cwd, instructions=""):
        core.update_card(id, "bob", lane="plan", assignee="bob")
        return "built"
    monkeypatch.setattr(agent, "run_claude", slow)
    tick()
    assert core.get_card(id)["assignee"] == "bob"


def test_a_card_assigned_to_the_agent_ends_unassigned(ran):
    id = card("plan")
    tick()
    assert core.get_card(id)["assignee"] is None


def test_the_agent_assigning_itself_registers_it(ran):
    card("plan", assignee=None, auto=True)
    tick()
    assert agent.AGENT in core.list_users()


# ---------- a git worktree per card ----------

@pytest.fixture
def repo(tmp_path):
    """A project that is a git repository with one commit, on branch main, and an origin."""
    r = make_repo(tmp_path / "Repo")
    core.create_project("Repo", path=str(r))
    return r


@pytest.fixture
def where(monkeypatch):
    """Fake claude that records where it ran and what it was told, and edits a file."""
    seen = []

    def fake(card, cwd, instructions=""):
        seen.append((card["lane"], cwd, instructions))
        if card["lane"] != "deploy":   # a deploy edits nothing
            (cwd / f"{card['lane']}.txt").write_text("x")
        return {"test": "ok\nRESULT: PASS", "plan": "p\nQUESTIONS: NONE",
                "deploy": "shipped\nDEPLOY: OK"}.get(card["lane"], "built")
    monkeypatch.setattr(agent, "run_claude", fake)
    return seen


def test_develop_runs_in_a_worktree_of_its_own_on_a_card_branch(repo, where):
    id = card("develop", project="Repo")
    tick()
    tree = repo.parent / "Repo.worktrees" / f"card-{id}"
    [(lane, cwd, told)] = where
    assert (lane, cwd) == ("develop", tree)
    assert agent.git(tree, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == f"card/{id}"
    assert f"Commit your work on card/{id}" in told and "made from main" in told
    assert not (repo / "develop.txt").exists(), "the repo itself is untouched"
    assert comments(id)[-1].endswith(f"(worked in {tree}, branch card/{id})")


def test_two_cards_never_share_a_worktree(repo, where):
    a, b = card("develop", project="Repo"), card("develop", project="Repo")
    tick()
    tick()
    assert {cwd.name for _, cwd, _ in where} == {f"card-{a}", f"card-{b}"}


def test_test_runs_in_the_cards_worktree_and_is_told_what_to_review(repo, where):
    id = card("develop", project="Repo", auto=True)
    tick()
    tick()
    (_, dev, _), (lane, cwd, told) = where[:2]
    assert (lane, cwd) == ("test", dev)
    assert f"git diff main...HEAD" in told
    assert core.get_card(id)["lane"] == "verify"


def test_rework_reuses_the_cards_worktree_and_branch(repo, where):
    id = card("develop", project="Repo")
    tick()
    core.update_card(id, "ce", lane="develop", assignee=agent.AGENT)
    tick()
    assert where[0][1] == where[1][1]


def test_a_branch_left_without_its_worktree_is_picked_up_again(repo, where):
    id = card("develop", project="Repo")
    agent.git(repo, "branch", f"card/{id}")
    tick()
    assert where[0][1].name == f"card-{id}"
    assert comments(id)[-1].startswith("built")


def test_a_card_in_test_with_no_worktree_fails_and_the_repo_is_untouched(repo, where):
    id = core.create_card("t", "ce", lane="test", assignee=agent.AGENT, project="Repo",
                          auto_advance=True)["id"]   # built before worktrees, or removed
    tick()
    c = core.get_card(id)
    assert (c["lane"], c["auto_advance"]) == ("test", False)
    assert comments(id)[-1].startswith("agent failed: there is no worktree at")
    assert where == [] and pushed(repo, id) is None


def test_planning_stays_in_the_repo(repo, where):
    card("plan", project="Repo")
    tick()
    [(lane, cwd, told)] = where
    assert (lane, cwd, told) == ("plan", repo, "")


@pytest.mark.parametrize("lane", ["develop", "test"])
def test_a_folder_that_is_not_a_repo_is_never_developed_or_tested(ran, where, tmp_path, lane):
    (tmp_path / "Plain").mkdir()
    core.create_project("Plain", path=str(tmp_path / "Plain"))
    id = card(lane, project="Plain")
    tick()
    assert comments(id) == [f"agent failed: {tmp_path / 'Plain'} is not a git repository: "
                            "develop and test only run in a git worktree"]
    assert where == [] and core.get_card(id)["lane"] == lane
    assert list((tmp_path / "Plain").iterdir()) == []


def test_a_folder_that_is_not_a_repo_can_still_be_planned(ran, where, tmp_path):
    (tmp_path / "Plain").mkdir()
    core.create_project("Plain", path=str(tmp_path / "Plain"))
    id = card("plan", project="Plain")
    tick()
    assert core.get_card(id)["lane"] == "develop"


def test_a_rerun_brings_the_branch_up_to_date_with_the_base(repo, where):
    id = card("develop", project="Repo")
    tick()
    (repo / "c.txt").write_text("c")
    agent.git(repo, "add", ".")
    agent.git(repo, "commit", "-q", "-m", "two")
    core.update_card(id, "ce", lane="develop", assignee=agent.AGENT)
    tick()
    _, tree, told = where[1]
    assert (tree / "c.txt").exists(), "the base's new commit is in the card's worktree"
    assert "brought up to date" in told


def test_a_conflicting_base_is_left_for_the_run_to_resolve(repo, where):
    id = card("develop", project="Repo")
    tick()
    tree = repo.parent / "Repo.worktrees" / f"card-{id}"
    (tree / "a.txt").write_text("the card's line")
    agent.git(tree, "commit", "-qam", "card edits a.txt")
    (repo / "a.txt").write_text("the base's line")
    agent.git(repo, "commit", "-qam", "base edits a.txt")
    core.update_card(id, "ce", lane="develop", assignee=agent.AGENT)
    tick()
    assert agent.git(tree, "rev-parse", "--verify", "--quiet", "MERGE_HEAD").returncode == 0
    assert "left conflicts" in where[1][2]
    assert core.get_card(id)["lane"] == "test", "a conflict does not fail the card"
    core.update_card(id, "ce", lane="develop", assignee=agent.AGENT)
    tick()
    assert "is unfinished" in where[2][2], "the next run is told to finish it, not to remerge"


def test_the_test_stage_gets_the_base_as_it_is_now(repo, where):
    id = card("develop", project="Repo", auto=True)
    tick()
    (repo / "c.txt").write_text("c")
    agent.git(repo, "add", ".")
    agent.git(repo, "commit", "-q", "-m", "two")
    tick()
    lane, cwd, _ = where[1]
    assert lane == "test"
    assert "c.txt" in agent.git(repo, "ls-tree", "--name-only", f"card/{id}").stdout


def test_a_worktree_that_cannot_be_made_fails_the_card(repo, where):
    id = card("develop", project="Repo")
    (repo.parent / "Repo.worktrees").write_text("a file where the folder should go")
    tick()
    assert comments(id)[-1].startswith("agent failed: could not make a worktree")
    assert where == []


# ---------- commit and push before verify ----------

def test_a_pass_commits_everything_and_pushes_before_verify(repo, where):
    id = card("develop", project="Repo", auto=True)
    tick()                                        # develop: writes develop.txt, no commit
    tree = where[0][1]
    (tree / "a.txt").write_text("changed")        # a modified file, uncommitted
    tick()                                        # test: writes test.txt, then PASS
    c = core.get_card(id)
    assert c["lane"] == "verify"
    assert pushed(repo, id) == agent.git(repo, "rev-parse", f"card/{id}").stdout.strip(),         "origin has the card's branch at the new commit"
    files = agent.git(repo, "show", "--name-only", "--format=%s", f"card/{id}").stdout.split()
    assert files[:2] == [f"#{id}", "t"] and {"a.txt", "develop.txt", "test.txt"} <= set(files)
    assert comments(id)[-2].startswith(f"committed and pushed card/{id} to origin (")
    assert not (repo / "develop.txt").exists(), "the repo itself is untouched"


def test_commits_the_develop_stage_made_are_pushed_too(repo, where, monkeypatch):
    id = card("test", project="Repo")
    tree = repo.parent / "Repo.worktrees" / f"card-{id}"
    (tree / "b.txt").write_text("b")
    agent.git(tree, "add", ".")
    agent.git(tree, "commit", "-q", "-m", "developed")
    monkeypatch.setattr(agent, "run_claude", lambda c, cwd, i: "fine\nRESULT: PASS")
    tick()                                        # nothing left to commit: only a push
    assert pushed(repo, id) == agent.git(repo, "rev-parse", f"card/{id}").stdout.strip()
    assert agent.git(repo, "log", "-1", "--format=%s", f"card/{id}").stdout.strip() == "developed"
    assert core.get_card(id)["lane"] == "verify"


def test_a_failed_test_is_neither_committed_nor_pushed(repo, monkeypatch):
    id = card("test", project="Repo")
    tree = repo.parent / "Repo.worktrees" / f"card-{id}"

    def fails(c, cwd, i):
        (cwd / "half.txt").write_text("x")
        return "broken\nRESULT: FAIL"
    monkeypatch.setattr(agent, "run_claude", fails)
    tick()
    assert core.get_card(id)["lane"] == "test"
    assert pushed(repo, id) is None
    assert "half.txt" in agent.git(tree, "status", "--porcelain").stdout
    assert tree.exists(), "a card that did not pass keeps its worktree"


def test_a_push_that_fails_keeps_the_card_out_of_verify(repo, where):
    agent.git(repo, "remote", "remove", "origin")
    id = card("test", project="Repo", auto=True)
    tick()
    c = core.get_card(id)
    assert (c["lane"], c["auto_advance"], c["attention"]) == ("test", False, True)
    assert comments(id)[-1].startswith("agent failed: git push failed:")


# ---------- the worktree goes when the card leaves test ----------

def test_a_pass_removes_the_worktree_after_the_deploy(repo, where):
    id = card("develop", project="Repo", auto=True)
    tick()
    tree = where[0][1]
    tick()
    assert where[-1][:2] == ("deploy", tree), "the deploy still ran in the worktree"
    assert not tree.exists()
    assert f"card-{id}" not in agent.git(repo, "worktree", "list").stdout
    assert agent.git(repo, "rev-parse", "--verify", "--quiet", f"card/{id}").returncode == 0
    assert pushed(repo, id), "the branch, here and on origin, is the record"


def test_a_worktree_that_will_not_go_does_not_fail_the_card(repo, where, monkeypatch):
    id = card("test", project="Repo", auto=True)
    monkeypatch.setattr(agent, "prune", lambda r, t: "in use by another process")
    tick()
    assert core.get_card(id)["lane"] == "verify"
    assert comments(id)[-1].startswith("could not remove the worktree")
    assert comments(id)[-1].endswith("in use by another process")


# ---------- one run per project lane at a time ----------

def test_a_lane_runs_one_card_at_a_time_in_order(ran):
    a, b = card("develop"), card("develop")
    worked = lambda: sorted(r for r in ran if r[1] != "deploy")
    tick()
    assert len(worked()) == 1, "one waits for the other"
    tick()
    assert worked() == [(a, "develop", f"card-{a}"), (b, "develop", f"card-{b}")]


def test_the_top_card_of_the_column_goes_first(ran):
    last, first, middle = card("develop"), card("develop"), card("develop")
    core.update_card(first, "ce", pos=-1)    # dragged to the top, after everything was made
    core.update_card(middle, "ce", pos=0)
    for n, want in enumerate([first, middle, last], 1):
        tick()
        assert len(ran) == n and ran[-1][0] == want


def test_a_top_card_that_cannot_run_does_not_block(ran):
    core.update_card(card("develop", assignee=None), "ce", pos=-1)
    below = card("develop")
    tick()
    assert [r[0] for r in ran] == [below]


def test_a_busy_projects_top_card_does_not_block_another_project(ran, repo):
    core.update_card(card("develop"), "ce", pos=-1)
    other = card("develop", project="Repo")
    agent.running[("proj", "develop")] = "busy"
    try:
        tick()
    finally:
        del agent.running[("proj", "develop")]
    assert [r[0] for r in ran] == [other]


def test_a_busy_lane_does_not_block_another_lane_of_the_project(ran):
    core.update_card(card("develop"), "ce", pos=-1)
    other = card("plan")
    agent.running[("proj", "develop")] = "busy"
    try:
        tick()
    finally:
        del agent.running[("proj", "develop")]
    assert [r[0] for r in ran] == [other]


def test_a_card_waits_while_its_project_lane_is_busy(ran):
    id = card("develop")
    agent.running[("proj", "develop")] = "a run started by an earlier poll"
    try:
        tick()
    finally:
        del agent.running[("proj", "develop")]
    assert ran == [] and core.get_card(id)["lane"] == "develop"
    tick()
    assert core.get_card(id)["lane"] == "test"


def test_a_card_moved_mid_run_does_not_get_a_second_run(ran):
    """#76: the slot is the project's lane, so a card moved while its run was going became
    eligible again under the new lane's key — two runs, one card, one worktree."""
    id = card("develop")
    agent.running[("proj", "develop")] = "its develop run, still going"
    agent.working.add(id)
    try:
        core.update_card(id, "ce", lane="plan")
        tick()
    finally:
        del agent.running[("proj", "develop")]
        agent.working.discard(id)
    assert ran == [], "the run in flight owns the card, whatever lane it sits in now"


def test_the_card_is_free_again_once_its_run_ends(ran):
    id = card("develop")
    tick()
    assert [r[1] for r in ran] == ["develop"] and agent.working == set()
    core.update_card(id, "ce", lane="plan")   # a move back re-arms auto advance
    tick()
    assert [r[1] for r in ran] == ["develop", "plan"], "not held for good"


def test_a_busy_project_is_matched_ignoring_case(ran, db):
    import sqlite3
    id = card("develop")
    raw = sqlite3.connect(db)
    raw.execute("UPDATE cards SET project='PROJ' WHERE id=?", (id,))
    raw.commit()
    raw.close()
    agent.running[("proj", "develop")] = "busy"
    try:
        tick()
    finally:
        del agent.running[("proj", "develop")]
    assert ran == []


def test_different_projects_run_side_by_side(ran, repo, monkeypatch):
    both = threading.Barrier(2, timeout=10)   # passes only if both runs are going at once

    def meet(c, cwd, i):
        both.wait()
        return "built"
    monkeypatch.setattr(agent, "run_claude", meet)
    a, b = card("develop"), card("develop", project="Repo")
    tick()
    assert [core.get_card(x)["lane"] for x in (a, b)] == ["test", "test"]
    assert agent.running == {}


def test_two_lanes_of_one_project_run_side_by_side(ran, monkeypatch):
    both = threading.Barrier(2, timeout=10)   # passes only if both runs are going at once
    seen = []

    def meet(c, cwd, i):
        both.wait()
        seen.append(core.list_activity())
        return "did it\nQUESTIONS: NONE"
    monkeypatch.setattr(agent, "run_claude", meet)
    a, b = card("plan"), card("develop")
    tick()
    assert [core.get_card(x)["lane"] for x in (a, b)] == ["develop", "test"]
    assert agent.running == {}
    # one status bar row per run: distinct actors, distinct cards
    want = [(agent.doer("Proj", "plan"), a), (agent.doer("Proj", "develop"), b)]
    assert sorted((r["actor"], r["card_id"]) for r in max(seen, key=len)) == sorted(want)


def test_a_project_lane_is_free_again_after_a_failure(ran, monkeypatch):
    bad, good = card("develop"), card("develop")

    def once(c, cwd, i):
        if c["id"] == bad:
            raise RuntimeError("boom")
        return "built"
    monkeypatch.setattr(agent, "run_claude", once)
    tick()
    tick()
    assert comments(bad)[-1] == "agent failed: boom"
    assert core.get_card(good)["lane"] == "test" and agent.running == {}


# ---------- deploy at the end of test, before verify ----------

def test_a_card_moved_to_verify_is_deployed_from_its_worktree(repo, where, monkeypatch):
    id = card("test", project="Repo")
    during = []
    fake = agent.run_claude

    def look(c, cwd, i):
        if c["lane"] == "deploy":
            got = core.get_card(id)
            during.append((got["lane"], pushed(repo, id), core.list_activity()[0]["doing"]))
        return fake(c, cwd, i)
    monkeypatch.setattr(agent, "run_claude", look)
    tick()
    tree = repo.parent / "Repo.worktrees" / f"card-{id}"
    assert [(lane, cwd) for lane, cwd, _ in where] == [("test", tree), ("deploy", tree)]
    assert f"branch card/{id}" in where[1][2], "told where the changes are"
    [(lane, sha, doing)] = during
    assert (lane, doing) == ("test", "deploying") and sha, "pushed and deployed, then moved"
    c = core.get_card(id)
    assert (c["lane"], c["attention"]) == ("verify", True)
    assert comments(id)[-1] == "deployed: shipped\nDEPLOY: OK"
    assert core.list_activity() == [] and agent.running == {}


@pytest.mark.parametrize("out", ["no phone, backend failed\nDEPLOY: FAILED", "", "DEPLOY: OK\nmore"])
def test_a_failed_deploy_leaves_the_card_in_verify_and_says_so(repo, monkeypatch, out):
    id = card("test", project="Repo", auto=True)
    monkeypatch.setattr(agent, "run_claude", lambda c, cwd, i:
                        out if c["lane"] == "deploy" else "fine\nRESULT: PASS")
    tick()
    c = core.get_card(id)
    assert (c["lane"], c["attention"], c["auto_advance"]) == ("verify", True, True)
    assert comments(id)[-1] == "deploy failed: " + (out or "(no output)")
    tick()
    assert len([x for x in comments(id) if x.startswith("deploy")]) == 1, "deployed once"


def test_a_deploy_that_crashes_leaves_the_card_in_verify(repo, monkeypatch):
    id = card("test", project="Repo")

    def run(c, cwd, i):
        if c["lane"] == "deploy":
            raise RuntimeError("claude exited 1: adb not found")
        return "fine\nRESULT: PASS"
    monkeypatch.setattr(agent, "run_claude", run)
    tick()
    assert core.get_card(id)["lane"] == "verify"
    assert comments(id)[-1] == "deploy failed: claude exited 1: adb not found"


@pytest.mark.parametrize("lane", ["plan", "develop"])
def test_only_a_move_to_verify_deploys(ran, lane):
    card(lane)
    tick()
    assert [r[1] for r in ran] == [lane]


def test_a_failed_test_is_not_deployed(ran, monkeypatch):
    card("test")
    lanes = []
    monkeypatch.setattr(agent, "run_claude", lambda c, cwd, i: lanes.append(c["lane"]) or "RESULT: FAIL")
    tick()
    assert lanes == ["test"]


def test_a_card_moved_off_test_during_the_run_is_not_deployed(ran, monkeypatch):
    id = card("test", auto=True)
    lanes = []

    def slow(c, cwd, i):
        lanes.append(c["lane"])
        core.update_card(id, "ce", lane="develop")
        return "fine\nRESULT: PASS"
    monkeypatch.setattr(agent, "run_claude", slow)
    tick()
    assert lanes == ["test"] and core.get_card(id)["lane"] == "develop"


def test_run_claude_deploys_with_rights_and_skips_a_missing_phone(monkeypatch, tmp_path):
    got = {}
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: got.update(cmd=cmd, **k) or
                        subprocess.CompletedProcess(cmd, 0, stdout="ok"))
    agent.run_claude({"lane": "deploy", "id": 7}, tmp_path, "deploy with ./ship.sh")
    assert got["cmd"][-1:] == ["--dangerously-skip-permissions"]
    p = got["input"]
    assert "backend" in p and "phone is connected" in p and "skip the phone" in p
    assert "DEPLOY: OK" in p and "deploy with ./ship.sh" in p
    assert "may merge, push or restart services" in p, "a deploy that merges is not refused"
    assert "Do not push or switch" not in p, "develop's rule is not the deploy's"
    assert "local test backend" in p and "unless the project's instructions" in p, \
        "deploy is local unless production is spelled out"
    assert "## Blocked by" in p, "a deploy that cannot reach a thing says so too"


# ---------- open questions are numbered from 1 ----------

@pytest.mark.parametrize("given, numbered", [
    ("- a?\n- b?", "1. a?\n2. b?"),
    ("* a?\n+ b?\n3) c?", "1. a?\n2. b?\n3. c?"),
    ("4. a?\n9. b?", "1. a?\n2. b?"),                               # renumbered from 1
    ("1. a?\n   - detail\n2. b?", "1. a?\n   - detail\n2. b?"),  # sub-bullets kept
    ("Decide:\n- a?", "Decide:\n1. a?"),                            # text kept
    ("", ""),
    ("-not a bullet", "-not a bullet"),
])
def test_questions_are_numbered_from_1(given, numbered):
    assert agent.number(given) == numbered


def test_the_plan_prompt_asks_for_numbered_questions(monkeypatch, tmp_path):
    got = {}
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: got.update(**k) or
                        subprocess.CompletedProcess(cmd, 0, stdout="ok"))
    agent.run_claude({"lane": "plan", "id": 7}, tmp_path)
    assert "numbered list starting at 1 (1. 2. 3.)" in got["input"]


# ---------- what only a person can do becomes cards of its own ----------

LONG = "x" * 120


@pytest.mark.parametrize("out, left, items", [
    ("built\nRESULT: PASS", "built\nRESULT: PASS", []),
    ("built\n## Blocked by\n- Make an OAuth client\n  put the id in .env\n- Enable the API",
     "built", [("Make an OAuth client", "put the id in .env"), ("Enable the API", "")]),
    ("did it\n## Blocked by\n- Plug the phone in\n\nRESULT: PASS",   # the verdict stays last
     "did it\nRESULT: PASS", [("Plug the phone in", "")]),
    ("steps\n## Blocked by\n- Get a key\n## Open questions\n- red?\nQUESTIONS: OPEN",
     "steps\n## Open questions\n- red?\nQUESTIONS: OPEN", [("Get a key", "")]),
    ("steps\n### blocked by:\n1. Get a key\n2) And a secret\nQUESTIONS: NONE",
     "steps\nQUESTIONS: NONE", [("Get a key", ""), ("And a secret", "")]),
    ("steps\n## Blocked by\nNothing, it is all mine.\nRESULT: PASS",   # no bullets: untouched
     "steps\n## Blocked by\nNothing, it is all mine.\nRESULT: PASS", []),
    (f"## Blocked by\n- {LONG}", "", [(LONG[:99] + "…", "")]),
])
def test_blockers(out, left, items):
    assert agent.blockers(out) == (left, items)


def test_a_blocked_run_opens_a_card_for_the_person(ran, monkeypatch):
    id = card("develop")
    plan_with(monkeypatch, "built what I could\n## Blocked by\n"
                           "- Create a Google OAuth client\n  put its id in .env")
    tick()
    [new] = core.list_cards(lane="todo", project="Proj")
    assert (new["title"], new["assignee"], new["auto_advance"], new["created_by"]) == \
        ("Create a Google OAuth client", None, False, agent.AGENT)
    assert new["description"] == f"put its id in .env\n\n(blocks #{id} t)"
    assert [(l["from_id"], l["to_id"], l["kind"]) for l in core.get_card(id)["links"]] == \
        [(new["id"], id, "blocks")]
    assert comments(id)[0] == f"blocked: opened #{new['id']} in todo for what a person has " \
                              "to do first"
    assert comments(id)[1].startswith("built what I could\n\n(worked in "), \
        "the section is taken out of the run's own comment"
    c = core.get_card(id)
    assert (c["lane"], c["attention"], c["auto_advance"]) == ("develop", True, False)


def test_a_blocked_card_waits_in_its_lane(ran, monkeypatch):
    id = card("plan", auto=True)
    plan_with(monkeypatch, "## Steps\n1. x\n## Blocked by\n- Get a Slack token\nQUESTIONS: NONE")
    tick()
    c = core.get_card(id)
    assert (c["lane"], c["attention"], c["auto_advance"]) == ("plan", True, False)
    assert c["plan"] == "## Steps\n1. x"
    core.comment(id, "ce", "token is in .env now")
    assert core.get_card(id)["auto_advance"] is True, "the person's reply restarts it"
    plan_with(monkeypatch, "## Steps\n1. x\nQUESTIONS: NONE")
    tick()
    assert core.get_card(id)["lane"] == "develop"


def test_the_same_blocker_is_not_opened_twice(ran, monkeypatch):
    id = card("develop")
    plan_with(monkeypatch, "## Blocked by\n- Get a Slack token")
    tick()
    core.update_card(id, "ce", assignee=agent.AGENT)   # a person sends it round again
    tick()
    assert [c["title"] for c in core.list_cards(lane="todo", project="Proj")] == \
        ["Get a Slack token"]


def test_a_deploy_that_is_blocked_opens_a_card(repo, monkeypatch):
    id = card("test", project="Repo")
    monkeypatch.setattr(agent, "run_claude", lambda c, cwd, i:
                        "backend is up\n## Blocked by\n- Plug the phone in\nDEPLOY: OK"
                        if c["lane"] == "deploy" else "fine\nRESULT: PASS")
    tick()
    c = core.get_card(id)
    assert (c["lane"], c["attention"]) == ("verify", True), "a deploy has no next lane to stop"
    assert comments(id)[-2] == "deployed: backend is up\nDEPLOY: OK", "the verdict still reads"
    [new] = core.list_cards(lane="todo", project="Repo")
    assert new["title"] == "Plug the phone in"
    assert comments(id)[-1] == f"blocked: opened #{new['id']} in todo for what a person has " \
                               "to do first"


# ---------- only one agent runs ----------

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()


def test_a_second_agent_refuses_to_start_while_one_runs():
    addr = free_port()
    first = agent.only_one(addr)
    try:
        with pytest.raises(SystemExit, match="already running"):
            agent.only_one(addr)
    finally:
        first.close()


def test_the_lock_is_free_again_once_the_agent_is_gone():
    addr = free_port()
    agent.only_one(addr).close()
    agent.only_one(addr).close()


def test_a_second_agent_process_exits_without_touching_the_board(tmp_path):
    """The real thing: agent.py started twice. The second exits at once, before main()."""
    import sys
    try:
        held = agent.only_one()   # stands in for the running agent
    except SystemExit:
        held = None               # the live agent holds it already: just as good
    try:
        r = subprocess.run([sys.executable, "agent.py"], capture_output=True, text=True,
                           timeout=60, cwd=Path(agent.__file__).parent)
    finally:
        if held:
            held.close()
    assert r.returncode == 1 and "already running" in r.stderr
    assert "polling" not in r.stdout


# ---------- the agent restarts itself when agent.py changes ----------

def test_an_unchanged_agent_py_does_not_restart():
    assert not agent.stale(agent.SOURCE.stat().st_mtime)


def test_a_changed_agent_py_restarts_between_polls():
    assert agent.stale(0)   # any other mtime: the file on disk is not the one we read


def test_a_run_in_flight_holds_the_restart_off():
    agent.running["repo"] = "a run"
    try:
        assert not agent.stale(0)
    finally:
        del agent.running["repo"]


def test_a_quota_pause_holds_the_restart_off(monkeypatch):
    monkeypatch.setattr(agent, "paused_until",
                        agent.datetime.now() + agent.timedelta(hours=1))
    assert not agent.stale(0)


def test_the_restart_frees_the_lock_before_starting_the_new_agent(monkeypatch):
    addr = free_port()
    lock = agent.only_one(addr)
    started = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **k: started.append(cmd))
    with pytest.raises(SystemExit):
        agent.restart(lock)
    assert started and started[0][-1] == str(agent.SOURCE)
    agent.only_one(addr).close()   # free: the new agent can take the lock


# ---------- the merged and deployed dots ----------

def deploys(repo, monkeypatch, out, merge=False):
    """A card in test whose deploy replies `out`, after merging it into main and pushing
    that if `merge`."""
    id = card("test", project="Repo")

    def run(c, cwd, i):
        if c["lane"] != "deploy":
            (cwd / "t.txt").write_text("x")
            return "fine\nRESULT: PASS"
        if merge:
            agent.git(repo, "merge", "-q", "--no-ff", f"card/{id}", "-m", "merge")
            agent.git(repo, "push", "-q", "origin", "main")
        if isinstance(out, Exception):
            raise out
        return out
    monkeypatch.setattr(agent, "run_claude", run)
    tick()
    return core.get_card(id)


def test_a_merged_and_deployed_card_lights_both(repo, monkeypatch):
    c = deploys(repo, monkeypatch, "backend restarted\nDEPLOY: OK", merge=True)
    assert (c["lane"], c["attention"], c["merged"], c["deployed"]) == ("verify", True, True, True)


def test_a_deploy_without_a_merge_is_not_on_master(repo, monkeypatch):
    c = deploys(repo, monkeypatch, "installed on the phone\nDEPLOY: OK")
    assert (c["merged"], c["deployed"]) == (False, True)


def test_a_skipped_deploy_is_not_a_failure(repo, monkeypatch):
    c = deploys(repo, monkeypatch, "no phone connected\n**DEPLOY: SKIPPED**", merge=True)
    assert (c["lane"], c["attention"], c["merged"], c["deployed"]) == ("verify", True, True, False)
    assert comments(c["id"])[-1] == "deploy skipped: no phone connected\n**DEPLOY: SKIPPED**"


def test_a_deploy_that_fails_after_the_merge_is_still_on_master(repo, monkeypatch):
    c = deploys(repo, monkeypatch, "restart failed\nDEPLOY: FAILED", merge=True)
    assert (c["merged"], c["deployed"]) == (True, False)
    assert comments(c["id"])[-1].startswith("deploy failed: ")


def test_a_deploy_that_crashes_lights_neither(repo, monkeypatch):
    c = deploys(repo, monkeypatch, RuntimeError("claude exited 1"))
    assert (c["merged"], c["deployed"]) == (False, False)
    assert comments(c["id"])[-1] == "deploy failed: claude exited 1"


def test_merged_only_once_the_merge_is_pushed(repo, monkeypatch):
    id = card("test", project="Repo")

    def run(c, cwd, i):
        if c["lane"] == "deploy":   # merged locally, never pushed: not on origin
            agent.git(repo, "merge", "-q", "--no-ff", f"card/{id}", "-m", "merge")
            return "DEPLOY: OK"
        (cwd / "t.txt").write_text("x")
        return "fine\nRESULT: PASS"
    monkeypatch.setattr(agent, "run_claude", run)
    tick()
    assert core.get_card(id)["merged"] is False


def test_the_deploy_prompt_knows_the_skipped_verdict(monkeypatch, tmp_path):
    got = {}
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: got.update(**k) or
                        subprocess.CompletedProcess(cmd, 0, stdout="ok"))
    agent.run_claude({"lane": "deploy", "id": 7}, tmp_path)
    assert "DEPLOY: SKIPPED" in got["input"] and "DEPLOY: FAILED" in got["input"]


# ---------- out of quota ----------

QUOTA = "You've hit your session limit · resets 10:30pm (Europe/Stockholm)\n"


@pytest.fixture(autouse=True)
def not_paused():
    agent.paused_until = None
    yield
    agent.paused_until = None


@pytest.mark.parametrize("text, now, want", [
    ("10:30pm", "2026-09-19 20:22", "2026-09-19 22:31"),     # later today
    ("10:30pm", "2026-09-20 00:10", "2026-09-20 22:31"),     # read after midnight: today
    ("1am", "2026-09-19 23:00", "2026-09-20 01:01"),         # passed today: tomorrow
    ("10pm", "2026-09-19 20:00", "2026-09-19 22:01"),
    ("12am", "2026-09-19 20:00", "2026-09-20 00:01"),
    ("12pm", "2026-09-19 10:00", "2026-09-19 12:01"),
    ("Sep 21, 10am", "2026-09-19 20:00", "2026-09-21 10:01"),
    ("Jan 2, 3pm", "2026-12-30 20:00", "2027-01-02 15:01"),
    ("10:30pm", "2026-09-19 22:31", "2026-09-19 22:36"),     # just passed: a short wait
    ("soon", "2026-09-19 20:00", "2026-09-19 20:30"),        # unreadable: 30 minutes
    (None, "2026-09-19 20:00", "2026-09-19 20:30"),
])
def test_resume_at(text, now, want):
    from datetime import datetime
    got = agent.resume_at(text, datetime.fromisoformat(now))
    assert got == datetime.fromisoformat(want)


@pytest.mark.parametrize("out, err", [("", QUOTA), (QUOTA, ""),
                                      ("", "You've hit your weekly limit\n")])
def test_run_claude_raises_out_of_quota(monkeypatch, tmp_path, out, err):
    from datetime import datetime

    def fail(*a, **k):
        raise subprocess.CalledProcessError(1, "claude", output=out, stderr=err)
    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(agent.OutOfQuota) as e:
        agent.run_claude({"lane": "develop", "id": 1}, tmp_path)
    assert str(e.value) == (out or err).strip() and e.value.at > datetime.now()


def quota(monkeypatch, lane=None):
    """claude is out of quota for 2 hours: in every stage, or only in `lane`."""
    from datetime import datetime, timedelta
    at = datetime.now() + timedelta(hours=2)

    def out(card, cwd, instructions=""):
        if lane in (None, card["lane"]):
            raise agent.OutOfQuota(QUOTA, at)
        return "ok\nRESULT: PASS"
    monkeypatch.setattr(agent, "run_claude", out)
    return at


def test_out_of_quota_is_not_a_failure_and_pauses(ran, monkeypatch):
    id = card("develop", assignee="ce", auto=True)
    at = quota(monkeypatch)
    tick()
    c = core.get_card(id)
    assert (c["lane"], c["assignee"], c["auto_advance"], c["attention"]) == \
        ("develop", "ce", True, False)
    assert comments(id) == [f"out of quota: resuming at {at:%H:%M} ({QUOTA.strip()})"]
    assert agent.paused_until == at
    assert [(a["actor"], a["card_id"]) for a in core.list_activity()] == [(agent.AGENT, id)]


def test_a_card_assigned_to_the_agent_stays_assigned(ran, monkeypatch):
    id = card("plan")
    quota(monkeypatch)
    tick()
    c = core.get_card(id)
    assert (c["lane"], c["assignee"], c["auto_advance"]) == ("plan", agent.AGENT, False)


def test_the_pause_holds_every_project_then_the_card_goes_on(ran, monkeypatch):
    from datetime import datetime, timedelta
    core.create_project("Other", path=str(core.get_project("Proj")["path"]))
    id = card("develop", auto=True)
    quota(monkeypatch)
    tick()
    ran.clear()
    monkeypatch.undo()   # claude has quota again...
    monkeypatch.setattr(agent, "run_claude", lambda c, cwd, i="": ran.append(c["id"]) or "ok")
    other = card("plan", project="Other")
    tick()
    assert ran == [] and core.get_card(id)["lane"] == "develop", "...but the pause holds"
    agent.paused_until = datetime.now() - timedelta(seconds=1)
    tick()
    assert sorted(ran) == sorted([id, other]) and core.get_card(id)["lane"] == "test"
    assert agent.paused_until is None and core.list_activity() == [], "status bar cleared"


def test_out_of_quota_in_deploy_postpones_it(ran, monkeypatch):
    id = card("test", auto=True)
    at = quota(monkeypatch, lane="deploy")
    tick()
    c = core.get_card(id)
    assert c["lane"] == "verify" and c["attention"] and not c["deployed"]
    assert comments(id)[-1].startswith(f"deploy postponed: out of quota, resuming at {at:%H:%M}")
    assert agent.paused_until == at
    tree = Path(core.get_project("Proj")["path"])
    assert (tree.parent / "Proj.worktrees" / f"card-{id}").exists(), "redeploy by hand: in it"


# ---------- the run's CLI transcript ----------

STREAM = "\n".join([
    json.dumps({"type": "system", "subtype": "init", "model": "opus"}),
    json.dumps({"type": "assistant", "message": {"content": [
        {"type": "thinking", "thinking": "hm"},
        {"type": "text", "text": "Reading the file."},
        {"type": "tool_use", "name": "Read", "input": {"file_path": "a.txt"}}]}}),
    json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "content": [{"type": "text", "text": "line one\nline two"}]}]}}),
    "warning: something claude printed plainly",
    json.dumps({"type": "result", "subtype": "success", "duration_ms": 42000,
                "total_cost_usd": 0.31, "result": "did develop"}),
])


def test_transcript_renders_the_run_as_the_cli_showed_it():
    got = agent.transcript(STREAM).splitlines()
    assert got == ["● thinking…", "Reading the file.", '● Read({"file_path": "a.txt"})',
                   "  ⎿ line one line two", "warning: something claude printed plainly",
                   "● done in 42s · $0.31"]


def test_reply_is_the_result_event_and_falls_back_to_the_raw_output():
    assert agent.reply(STREAM) == "did develop"
    assert agent.reply("  just text \n") == "just text", "not stream-json: all of it"
    assert agent.reply('{"type": "assistant", "message": {"content": []}}') \
        == '{"type": "assistant", "message": {"content": []}}', "no result event: all of it"


def test_a_run_with_no_tool_calls_is_just_what_claude_said():
    out = '\n'.join(['{"type": "assistant", "message": {"content": [{"type": "text",'
                     ' "text": "nothing to do"}]}}',
                     '{"type": "result", "duration_ms": 900, "result": "nothing to do"}'])
    assert agent.transcript(out).splitlines() == ["nothing to do", "● done in 1s"]


def test_a_transcript_over_the_cap_keeps_its_tail(monkeypatch):
    monkeypatch.setattr(agent, "CLI_CAP", 50)
    out = "\n".join(f"line {i}" for i in range(100))
    got = agent.transcript(out)
    assert got.startswith("(… ") and "characters dropped)" in got.splitlines()[0]
    assert got.endswith("line 99") and len(got.splitlines()[1:]) < 100


def test_run_claude_asks_for_the_stream_so_the_whole_run_is_kept(monkeypatch, tmp_path):
    got = {}
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: got.update(cmd=cmd, **k) or
                        subprocess.CompletedProcess(cmd, 0, stdout=STREAM))
    out = agent.run_claude({"lane": "develop", "id": 7}, tmp_path)
    assert "--output-format stream-json --verbose" in " ".join(got["cmd"])
    assert out == "did develop", "the caller still reads just the answer"
    assert '● Read({"file_path": "a.txt"})' in out.cli


@pytest.mark.parametrize("lane", ["plan", "develop", "test"])
def test_the_comment_of_a_run_carries_its_transcript(ran, lane):
    id = card(lane)
    tick()
    said = [e["detail"] for e in core.get_card(id)["events"] if e["kind"] == "comment"]
    assert said[0]["output"] == f"● transcript of {lane}"
    if lane == "test":   # one transcript per run: not on the commit-and-push line after it
        assert "output" not in said[1]


def test_a_failed_run_still_shows_what_claude_printed(ran, monkeypatch):
    """A test stage that does not pass fails the card after the run: the failure comment
    carries the transcript, so a person can see where it went wrong."""
    id = card("test")
    monkeypatch.setattr(agent, "run_claude",
                        lambda c, cwd, i="": agent.Reply("nope\nRESULT: FAIL",
                                                        "● Bash(pytest)\n  ⎿ 1 failed"))
    tick()
    said = [e["detail"] for e in core.get_card(id)["events"] if e["kind"] == "comment"]
    assert said[-1]["text"].startswith("agent failed:")
    assert said[-1]["output"] == "● Bash(pytest)\n  ⎿ 1 failed"


def test_a_comment_with_nothing_to_show_carries_no_output_at_all(ran, monkeypatch):
    id = card("develop")
    monkeypatch.setattr(agent, "run_claude", lambda c, cwd, i="": "did it, printed nothing")
    tick()
    said = [e["detail"] for e in core.get_card(id)["events"] if e["kind"] == "comment"]
    assert list(said[0]) == ["text"], "no empty box on the board"
    assert said[0]["text"].startswith("did it, printed nothing")


def test_log_stamps_each_row(capsys):
    agent.log("#81 plan: log")
    out = capsys.readouterr().out.strip()
    assert re.match(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d #81 plan: log$", out), out
