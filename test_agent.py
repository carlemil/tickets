"""The board agent against the real MCP tools in-process. Claude itself is faked."""

import asyncio
import subprocess

import pytest
from mcp import Client

import agent
import app
import core


@pytest.fixture
def ran(monkeypatch, tmp_path):
    """Fake claude: records each card it was run on, answers with its lane (and a passing
    verdict in test). "Proj" is a configured project whose path is a real folder;
    "Pathless" has no path set."""
    (tmp_path / "Proj").mkdir()
    core.create_project("Proj", path=str(tmp_path / "Proj"), instructions="use uv")
    core.create_project("Pathless")
    calls = []

    def fake(card, cwd, instructions=""):
        calls.append((card["id"], card["lane"], cwd.name))
        return f"did {card['lane']}" + {"test": "\nRESULT: PASS",
                                        "plan": "\nQUESTIONS: NONE"}.get(card["lane"], "")

    monkeypatch.setattr(agent, "run_claude", fake)
    return calls


def tick():
    async def go():
        async with Client(app.mcp) as c:
            await agent.tick(c)
    asyncio.run(go())


def card(lane, assignee=agent.AGENT, project="Proj", auto=False):
    return core.create_card("t", "ce", lane=lane, assignee=assignee, project=project,
                            auto_advance=auto)["id"]


def comments(id):
    return [e["detail"]["text"] for e in core.get_card(id)["events"] if e["kind"] == "comment"]


@pytest.mark.parametrize("lane, nxt", [("plan", "develop"), ("develop", "test"),
                                       ("test", "verify")])
def test_assigned_card_is_worked_moved_and_handed_back(ran, lane, nxt):
    id = card(lane)
    tick()
    c = core.get_card(id)
    assert (c["lane"], c["assignee"]) == (nxt, None)
    assert (c["description"] if lane == "plan" else comments(id)[0]).startswith(f"did {lane}")
    assert ran == [(id, lane, "Proj")]
    assert all(e["actor"] == agent.AGENT for e in c["events"][1:])


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
    (tmp_path / "Proj").rmdir()
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
    assert "--dangerously-skip-permissions" in got["cmd"]


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
    assert ran == [(id, "plan", "Proj"), (id, "develop", "Proj"), (id, "test", "Proj")]
    assert (c["lane"], c["assignee"], c["auto_advance"]) == ("verify", None, True), \
        "stops at verify for a person, with the switch left as it was"


def test_auto_advance_keeps_the_agent_assigned_until_verify(ran):
    id = card("plan", auto=True)
    tick()
    assert core.get_card(id)["assignee"] == agent.AGENT
    tick()
    tick()
    assert (core.get_card(id)["lane"], core.get_card(id)["assignee"]) == ("verify", None)


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
    assert comments(id) == [out or "(no output)",
                            "agent failed: the test stage did not end in RESULT: PASS"]
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
    assert "RESULT: PASS" in got["input"] and "Do not edit" in got["input"]


# ---------- the plan stage ----------

def plan_with(monkeypatch, out):
    monkeypatch.setattr(agent, "run_claude", lambda c, cwd, instr: out)


def test_a_plan_without_questions_replaces_the_description_and_halts(ran, monkeypatch):
    id = core.create_card("t", "ce", description="make it blue", lane="plan",
                          assignee=agent.AGENT, project="Proj")["id"]
    plan_with(monkeypatch, "## Context\nmake it blue\n\n## Steps\n1. paint\nQUESTIONS: NONE")
    tick()
    c = core.get_card(id)
    assert c["description"] == "## Context\nmake it blue\n\n## Steps\n1. paint"
    assert (c["lane"], c["assignee"]) == ("develop", None)
    assert comments(id) == ["plan written to the description"]
    edit = next(e for e in c["events"] if e["detail"].get("field") == "description")
    assert edit["detail"]["from"] == "make it blue", "the old description survives in the log"
    tick()
    assert core.get_card(id)["lane"] == "develop", "halted: nothing picks it up in develop"


@pytest.mark.parametrize("auto", [False, True])
def test_open_questions_keep_the_card_in_plan_for_a_person(ran, monkeypatch, auto):
    id = card("plan", auto=auto)
    plan_with(monkeypatch, "## Steps\n1. x\n## Open questions\n- red or blue?\nQUESTIONS: OPEN")
    tick()
    c = core.get_card(id)
    assert c["description"] == "## Steps\n1. x\n## Open questions\n- red or blue?"
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
    assert (c["lane"], c["description"]) == ("plan", "## Steps\n1. paint")


@pytest.mark.parametrize("last", ["**QUESTIONS: NONE**", "`QUESTIONS: NONE` "])
def test_a_markdown_verdict_counts(ran, monkeypatch, last):
    id = card("plan")
    plan_with(monkeypatch, "steps\n" + last)
    tick()
    assert (core.get_card(id)["lane"], core.get_card(id)["description"]) == ("develop", "steps")


def test_an_auto_card_with_no_questions_carries_on_into_develop(ran):
    id = card("plan", auto=True)
    tick()
    c = core.get_card(id)
    assert (c["lane"], c["assignee"], c["auto_advance"]) == ("develop", agent.AGENT, True)


def test_an_empty_plan_fails_and_leaves_the_description_alone(ran, monkeypatch):
    id = core.create_card("t", "ce", description="keep me", lane="plan",
                          assignee=agent.AGENT, project="Proj")["id"]
    plan_with(monkeypatch, "")
    tick()
    c = core.get_card(id)
    assert (c["lane"], c["description"]) == ("plan", "keep me")
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
    assert [(r["actor"], r["card_id"], r["doing"]) for r in seen[0]] == [(agent.AGENT, id, doing)]
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
    with pytest.raises(Exception, match="server went away|unhandled errors"):  # may arrive grouped
        tick()
    assert core.list_activity() == []
