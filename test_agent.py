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
    """Fake claude: records each card it was run on, answers with its lane. Repo root is tmp."""
    (tmp_path / "Proj").mkdir()
    monkeypatch.setattr(agent, "ROOT", tmp_path)
    calls = []

    def fake(card, cwd):
        calls.append((card["id"], card["lane"], cwd.name))
        return f"did {card['lane']}"

    monkeypatch.setattr(agent, "run_claude", fake)
    return calls


def tick():
    async def go():
        async with Client(app.mcp) as c:
            await agent.tick(c)
    asyncio.run(go())


def card(lane, assignee=agent.AGENT, project="Proj"):
    return core.create_card("t", "ce", lane=lane, assignee=assignee, project=project)["id"]


def comments(id):
    return [e["detail"]["text"] for e in core.get_card(id)["events"] if e["kind"] == "comment"]


@pytest.mark.parametrize("lane, nxt", [("plan", "develop"), ("develop", "test")])
def test_assigned_card_is_worked_moved_and_handed_back(ran, lane, nxt):
    id = card(lane)
    tick()
    c = core.get_card(id)
    assert (c["lane"], c["assignee"]) == (nxt, None)
    assert comments(id) == [f"did {lane}"]
    assert ran == [(id, lane, "Proj")]
    assert all(e["actor"] == agent.AGENT for e in c["events"][1:])


def test_second_tick_does_not_retrigger_on_its_own_write(ran):
    card("plan")
    tick()
    tick()
    assert len(ran) == 1


@pytest.mark.parametrize("lane", ["todo", "test", "verify", "done"])
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
    monkeypatch.setattr(agent, "run_claude", lambda c, cwd: seen.append(c) or "ok")
    tick()
    assert "skip step 3" in str(seen[0]["events"])


def test_missing_repo_fails_without_moving(ran):
    id = card("plan", project="Nowhere")
    tick()
    c = core.get_card(id)
    assert (c["lane"], c["assignee"]) == ("plan", None)
    assert comments(id)[0].startswith("agent failed: no repo at")
    assert ran == []


@pytest.mark.parametrize("err", [subprocess.TimeoutExpired("claude", 3600),
                                 RuntimeError("claude exited 1: boom")])
def test_claude_failure_is_reported_and_card_handed_back(ran, monkeypatch, err):
    id = card("develop")

    def boom(card, cwd):
        raise err
    monkeypatch.setattr(agent, "run_claude", boom)
    tick()
    c = core.get_card(id)
    assert (c["lane"], c["assignee"]) == ("develop", None)
    assert comments(id) == [f"agent failed: {err}"]


def test_one_failure_does_not_stop_the_rest(ran, monkeypatch):
    bad, good = card("plan", project="Nowhere"), card("plan")
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
