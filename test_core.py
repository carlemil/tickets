"""Self-check for core.py: uv run python test_core.py"""

import tempfile
from pathlib import Path

import core

tmp = Path(tempfile.mkdtemp()) / "test_tickets.db"
core.DB_PATH = str(tmp)

try:
    # 1. a new card lands in todo with a `created` event naming the actor
    c = core.create_card("Wire the board", actor="ann", labels=["ui"], checklist=[{"text": "sketch", "done": False}])
    assert c["lane"] == "todo" and c["priority"] == "med", c
    assert c["project"] == core.DEFAULT_PROJECT == "inbox", c
    assert c["labels"] == ["ui"], c["labels"]
    assert [(e["kind"], e["actor"]) for e in c["events"]] == [("created", "ann")], c["events"]

    # 2. a lane change writes exactly one `moved` event with old and new lane
    c = core.update_card(c["id"], "bob", lane="develop")
    moved = [e for e in c["events"] if e["kind"] == "moved"]
    assert len(moved) == 1, moved
    assert moved[0]["actor"] == "bob", moved[0]
    assert moved[0]["detail"]["from"] == "todo" and moved[0]["detail"]["to"] == "develop", moved[0]
    assert c["updated_at"] > c["created_at"], c

    # 3. ticking a checklist item persists and writes a `checked` event
    c = core.update_card(c["id"], "ann", checklist=[{"text": "sketch", "done": True}])
    assert c["checklist"] == [{"text": "sketch", "done": True}], c["checklist"]
    checked = [e for e in c["events"] if e["kind"] == "checked"]
    assert len(checked) == 1 and checked[0]["detail"] == {"text": "sketch", "done": True}, checked

    # 4. an update that changes nothing writes no event
    before = len(c["events"])
    c = core.update_card(c["id"], "ann", lane="develop", title="Wire the board")
    assert len(c["events"]) == before, c["events"]

    # 5. invalid lane and invalid priority raise
    for bad in (dict(lane="backlog"), dict(priority="urgent")):
        try:
            core.update_card(c["id"], "ann", **bad)
            raise AssertionError(f"expected ValueError for {bad}")
        except ValueError:
            pass
    try:
        core.create_card("nope", actor="ann", lane="backlog")
        raise AssertionError("expected ValueError for create with bad lane")
    except ValueError:
        pass

    # 6. a self-link raises, and so does a bad link kind
    try:
        core.link_cards(c["id"], c["id"], "blocks", "ann")
        raise AssertionError("expected ValueError for self-link")
    except ValueError:
        pass
    other = core.create_card("Second card", actor="bob", lane="plan", labels=["api"])
    try:
        core.link_cards(c["id"], other["id"], "duplicates", "ann")
        raise AssertionError("expected ValueError for bad link kind")
    except ValueError:
        pass
    linked = core.link_cards(c["id"], other["id"], "blocks", "ann")
    assert linked["links"] == [{"from_id": c["id"], "to_id": other["id"], "kind": "blocks"}], linked["links"]
    assert core.get_card(other["id"])["links"] == linked["links"], "link visible from both ends"

    # linking the same pair and kind twice: one row, one event
    twice = core.link_cards(c["id"], other["id"], "blocks", "ann")
    assert twice["links"] == linked["links"], twice["links"]
    assert len([e for e in twice["events"] if e["kind"] == "linked"]) == 1, twice["events"]

    # unlink round-trips, and a repeat unlink is a silent no-op
    unlinked = core.unlink_cards(c["id"], other["id"], "blocks", "ann")
    assert unlinked["links"] == [], unlinked["links"]
    assert core.get_card(other["id"])["links"] == [], "link gone from both ends"
    evs = [e for e in unlinked["events"] if e["kind"] == "unlinked"]
    assert len(evs) == 1 and evs[0]["detail"] == {"to": other["id"], "kind": "blocks"}, evs
    before = len(unlinked["events"])
    again = core.unlink_cards(c["id"], other["id"], "blocks", "ann")
    assert len(again["events"]) == before, "repeat unlink must write no event"
    # an absent link is a no-op, but an unknown kind is bad input
    try:
        core.unlink_cards(c["id"], other["id"], "duplicates", "ann")
        raise AssertionError("expected ValueError for bad unlink kind")
    except ValueError:
        pass

    # 7. an unknown card id raises NotFound, distinguishable from ValueError
    for call in (lambda: core.get_card(9999),
                 lambda: core.update_card(9999, "ann", lane="done"),
                 lambda: core.comment(9999, "ann", "hi"),
                 lambda: core.link_cards(9999, c["id"], "blocks", "ann")):
        try:
            call()
            raise AssertionError("expected NotFound")
        except core.NotFound:
            pass
    assert not issubclass(core.NotFound, ValueError), "NotFound must be distinguishable from ValueError"

    # 8. comment appends a `comment` event visible in the activity log
    core.comment(c["id"], "cat", "looks good")
    log = core.get_card(c["id"])["events"]
    assert log[-1]["kind"] == "comment", log[-1]
    assert log[-1]["actor"] == "cat" and log[-1]["detail"]["text"] == "looks good", log[-1]
    assert log == sorted(log, key=lambda e: e["id"]), "events must be chronological"

    # 9. list_cards filters by lane and by label
    assert [x["id"] for x in core.list_cards(lane="develop")] == [c["id"]]
    assert [x["id"] for x in core.list_cards(lane="plan")] == [other["id"]]
    assert [x["id"] for x in core.list_cards(label="api")] == [other["id"]]
    assert [x["id"] for x in core.list_cards(label="ui")] == [c["id"]]
    assert core.list_cards(label="nope") == []
    assert len(core.list_cards()) == 2

    # the renamed fifth lane is a real lane
    assert core.LANES[4] == "verify", core.LANES
    moved = core.update_card(c["id"], "ann", lane="verify")
    assert moved["lane"] == "verify"
    assert [x["id"] for x in core.list_cards(lane="verify")] == [c["id"]]

    # projects: free text, stored casing preserved, matched case-insensitively
    t1 = core.create_card("Ship it", actor="ann", project="Tickets")
    t2 = core.create_card("Ship it too", actor="bob", project="tickets")
    assert t1["project"] == "Tickets" and t2["project"] == "tickets", (t1, t2)
    found = sorted(x["id"] for x in core.list_cards(project="tickets"))
    assert found == sorted([t1["id"], t2["id"]]), found
    assert sorted(x["id"] for x in core.list_cards(project="TICKETS")) == found
    assert {x["id"] for x in core.list_cards(project="inbox")} == {c["id"], other["id"]}
    # filters combine
    assert {x["id"] for x in core.list_cards(project="Tickets", lane="todo")} == set(found)
    assert core.list_cards(project="Tickets", lane="done") == []

    # list_projects collapses the case difference to one entry
    projects = core.list_projects()
    assert len(projects) == 2, projects
    assert projects[0] == "inbox" and projects[1].lower() == "tickets", projects

    # changing project is a normal diffed field -> one `edited` event, no new kind
    t1 = core.update_card(t1["id"], "cat", project="Backlog")
    assert t1["project"] == "Backlog"
    ed = [e for e in t1["events"] if e["kind"] == "edited"]
    assert len(ed) == 1 and ed[0]["detail"] == {"field": "project", "from": "Tickets", "to": "Backlog"}, ed
    assert core.list_projects() == ["Backlog", "inbox", "tickets"], core.list_projects()

    # users self-register from the actor on any mutation; ensure_user is just the explicit door
    core.create_card("Nothing to see", actor="dora")
    assert "dora" in core.list_users(), core.list_users()
    core.ensure_user("ann")  # already present via her events
    core.ensure_user("zed")  # never touched a card
    assert core.list_users() == ["ann", "bob", "cat", "dora", "zed"], core.list_users()

    print("OK - all core checks passed")
finally:
    for p in tmp.parent.glob("*"):
        p.unlink()
    tmp.parent.rmdir()
