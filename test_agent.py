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
    assert (c["plan"] if lane == "plan" else comments(id)[0]).startswith(f"did {lane}")
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
    assert ran == [(id, "plan", "Proj"), (id, "develop", "Proj"), (id, "test", "Proj")]
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
    assert (c["plan"], c["questions"]) == ("## Steps\n1. x", "- red or blue?")
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
    ("steps\n## Open questions\n- a?\n- b?\nQUESTIONS: OPEN", "steps", "- a?\n- b?"),
    ("steps\n### open questions:\n- a?\nQUESTIONS: OPEN", "steps", "- a?"),
    ("steps\n## Open questions\nNone.\nQUESTIONS: NONE", "steps", ""),
    ("steps\n## Open questions\n\n\n- a?\nQUESTIONS: OPEN", "steps", "- a?"),
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
    assert (c["plan"], c["questions"], c["answers"]) == ("paint it blue", "- red?", "blue")


def test_the_prompt_tells_claude_where_each_part_goes(monkeypatch, tmp_path):
    got = {}
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: got.update(k) or
                        subprocess.CompletedProcess(cmd, 0, "x", ""))
    agent.run_claude({"lane": "plan", "answers": "blue"}, tmp_path)
    assert "`questions` and `answers`" in got["input"] and "## Open questions" in got["input"]
    assert '"answers": "blue"' in got["input"]
    agent.run_claude({"lane": "develop"}, tmp_path)
    assert "`plan` the plan to follow" in got["input"]


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
    core.update_card(id, "ce", lane="test")
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
    assert seen == [(agent.AGENT, agent.AGENT)]
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
    """A project that is a git repository with one commit, on branch main."""
    r = tmp_path / "Repo"
    r.mkdir()
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"],
                 ["config", "user.name", "t"]):
        agent.git(r, *args)
    (r / "a.txt").write_text("a")
    agent.git(r, "add", ".")
    agent.git(r, "commit", "-q", "-m", "one")
    core.create_project("Repo", path=str(r))
    return r


@pytest.fixture
def where(monkeypatch):
    """Fake claude that records where it ran and what it was told, and edits a file."""
    seen = []

    def fake(card, cwd, instructions=""):
        seen.append((card["lane"], cwd, instructions))
        (cwd / f"{card['lane']}.txt").write_text("x")
        return {"test": "ok\nRESULT: PASS", "plan": "p\nQUESTIONS: NONE"}.get(card["lane"], "built")
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
    assert {cwd.name for _, cwd, _ in where} == {f"card-{a}", f"card-{b}"}


def test_test_runs_in_the_cards_worktree_and_is_told_what_to_review(repo, where):
    id = card("develop", project="Repo", auto=True)
    tick()
    tick()
    (_, dev, _), (lane, cwd, told) = where
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


def test_a_card_built_before_worktrees_is_tested_in_the_repo(repo, where):
    card("test", project="Repo")
    tick()
    [(lane, cwd, told)] = where
    assert (lane, cwd) == ("test", repo) and "before cards got a worktree" in told


def test_planning_stays_in_the_repo(repo, where):
    card("plan", project="Repo")
    tick()
    [(lane, cwd, told)] = where
    assert (lane, cwd, told) == ("plan", repo, "")


def test_a_folder_that_is_not_a_repo_is_worked_in_place(ran, where, tmp_path):
    card("develop")
    tick()
    [(lane, cwd, told)] = where
    assert cwd == tmp_path / "Proj" and "not a git repository. Do not commit." in told


def test_a_worktree_that_cannot_be_made_fails_the_card(repo, where):
    id = card("develop", project="Repo")
    (repo.parent / "Repo.worktrees").write_text("a file where the folder should go")
    tick()
    assert comments(id)[-1].startswith("agent failed: could not make a worktree")
    assert where == []
