"""core.py: every operation, every documented rejection, every documented no-op.

The `db` fixture in conftest.py gives each test its own database, so these run in any
order and each one builds only the cards it needs.
"""

import pytest

import core


def card(title="Wire the board", actor="ann", **kw):
    return core.create_card(title, actor=actor, **kw)


# ---------- creating ----------

def test_new_card_defaults_and_created_event():
    c = card(labels=["ui"], checklist=[{"text": "sketch", "done": False}])
    assert c["lane"] == "todo" and c["priority"] == "med", c
    assert c["project"] == core.DEFAULT_PROJECT == "inbox", c
    assert c["labels"] == ["ui"], c["labels"]
    assert [(e["kind"], e["actor"]) for e in c["events"]] == [("created", "ann")], c["events"]


def test_create_card_persists_every_optional_field():
    c = card(description="the long version", assignee="bob", priority="high",
             project="Tickets", lane="plan")
    assert c["description"] == "the long version"
    assert c["assignee"] == "bob" and c["priority"] == "high"
    assert c["project"] == "Tickets" and c["lane"] == "plan"
    assert core.get_card(c["id"])["description"] == "the long version", "survives a reload"


# ---------- updating ----------

def test_lane_change_writes_one_moved_event():
    c = core.update_card(card()["id"], "bob", lane="develop")
    moved = [e for e in c["events"] if e["kind"] == "moved"]
    assert len(moved) == 1 and moved[0]["actor"] == "bob", moved
    assert moved[0]["detail"]["from"] == "todo" and moved[0]["detail"]["to"] == "develop", moved
    assert c["updated_at"] > c["created_at"], c


def test_assignee_change_writes_an_assigned_event():
    c = core.update_card(card()["id"], "bob", assignee="cat")
    evs = [e for e in c["events"] if e["kind"] == "assigned"]
    assert len(evs) == 1, c["events"]
    assert evs[0]["detail"] == {"field": "assignee", "from": None, "to": "cat"}, evs[0]


def test_assignee_can_be_cleared():
    c = card(assignee="cat")
    c = core.update_card(c["id"], "bob", assignee=None)
    assert c["assignee"] is None, c
    evs = [e for e in c["events"] if e["kind"] == "assigned"]
    assert evs[-1]["detail"] == {"field": "assignee", "from": "cat", "to": None}, evs[-1]


@pytest.mark.parametrize("field,value", [
    ("title", "Renamed"),
    ("description", "now with detail"),
    ("priority", "high"),
    ("labels", ["ui", "api"]),
])
def test_plain_fields_write_one_edited_event(field, value):
    c = card()
    before = c[field]
    c = core.update_card(c["id"], "ann", **{field: value})
    assert c[field] == value, c
    evs = [e for e in c["events"] if e["kind"] == "edited"]
    assert len(evs) == 1, c["events"]
    assert evs[0]["detail"] == {"field": field, "from": before, "to": value}, evs[0]


def test_label_diffs_are_lists_not_strings():
    c = core.update_card(card(labels=["ui"])["id"], "ann", labels=["ui", "api"])
    d = [e for e in c["events"] if e["kind"] == "edited"][0]["detail"]
    assert d["from"] == ["ui"] and d["to"] == ["ui", "api"], d


def test_update_that_changes_nothing_writes_no_event():
    c = core.update_card(card()["id"], "bob", lane="develop")
    before = len(c["events"])
    c = core.update_card(c["id"], "ann", lane="develop", title="Wire the board")
    assert len(c["events"]) == before, c["events"]


def test_unknown_field_is_rejected_by_name():
    c = card()
    with pytest.raises(ValueError, match="nope"):
        core.update_card(c["id"], "ann", nope=1)


@pytest.mark.parametrize("bad", [{"lane": "backlog"}, {"priority": "urgent"}])
def test_update_rejects_bad_lane_and_priority(bad):
    c = card()
    with pytest.raises(ValueError):
        core.update_card(c["id"], "ann", **bad)


@pytest.mark.parametrize("bad", [{"lane": "backlog"}, {"priority": "urgent"}])
def test_create_rejects_bad_lane_and_priority(bad):
    with pytest.raises(ValueError):
        card(**bad)


# ---------- checklists ----------

def test_ticking_an_item_writes_a_checked_event():
    c = card(checklist=[{"text": "sketch", "done": False}])
    c = core.update_card(c["id"], "ann", checklist=[{"text": "sketch", "done": True}])
    assert c["checklist"] == [{"text": "sketch", "done": True}], c["checklist"]
    evs = [e for e in c["events"] if e["kind"] == "checked"]
    assert len(evs) == 1 and evs[0]["detail"] == {"text": "sketch", "done": True}, evs


def test_adding_an_item_is_an_edit_not_a_tick():
    c = card(checklist=[{"text": "sketch", "done": False}])
    c = core.update_card(c["id"], "ann", checklist=[
        {"text": "sketch", "done": False}, {"text": "build", "done": False}])
    assert [e["kind"] for e in c["events"] if e["kind"] == "checked"] == [], c["events"]
    ed = [e for e in c["events"] if e["kind"] == "edited"]
    assert len(ed) == 1 and ed[0]["detail"]["field"] == "checklist", ed
    assert ed[0]["detail"]["from"] == ["sketch"] and ed[0]["detail"]["to"] == ["sketch", "build"]


def test_ticking_and_adding_in_one_call_writes_both_events():
    c = card(checklist=[{"text": "sketch", "done": False}])
    c = core.update_card(c["id"], "ann", checklist=[
        {"text": "sketch", "done": True}, {"text": "build", "done": False}])
    kinds = [e["kind"] for e in c["events"]]
    assert kinds.count("checked") == 1 and kinds.count("edited") == 1, c["events"]


# ---------- links ----------

def test_self_link_is_rejected():
    c = card()
    with pytest.raises(ValueError):
        core.link_cards(c["id"], c["id"], "blocks", "ann")


@pytest.mark.parametrize("op", [core.link_cards, core.unlink_cards])
def test_unknown_link_kind_is_rejected(op):
    a, b = card(), card("Second card", actor="bob")
    with pytest.raises(ValueError):
        op(a["id"], b["id"], "duplicates", "ann")


@pytest.mark.parametrize("kind", core.LINK_KINDS)
def test_link_is_visible_from_both_ends(kind):
    a, b = card(), card("Second card", actor="bob")
    linked = core.link_cards(a["id"], b["id"], kind, "ann")
    assert linked["links"] == [{"from_id": a["id"], "to_id": b["id"], "kind": kind}], linked
    assert core.get_card(b["id"])["links"] == linked["links"], "visible from both ends"


def test_relinking_the_same_pair_is_a_silent_no_op():
    a, b = card(), card("Second card", actor="bob")
    once = core.link_cards(a["id"], b["id"], "blocks", "ann")
    twice = core.link_cards(a["id"], b["id"], "blocks", "ann")
    assert twice["links"] == once["links"], twice["links"]
    assert len([e for e in twice["events"] if e["kind"] == "linked"]) == 1, twice["events"]


def test_unlink_round_trips_and_repeating_it_writes_nothing():
    a, b = card(), card("Second card", actor="bob")
    core.link_cards(a["id"], b["id"], "blocks", "ann")
    gone = core.unlink_cards(a["id"], b["id"], "blocks", "ann")
    assert gone["links"] == [], gone["links"]
    assert core.get_card(b["id"])["links"] == [], "gone from both ends"
    evs = [e for e in gone["events"] if e["kind"] == "unlinked"]
    assert len(evs) == 1 and evs[0]["detail"] == {"to": b["id"], "kind": "blocks"}, evs
    before = len(gone["events"])
    again = core.unlink_cards(a["id"], b["id"], "blocks", "ann")
    assert len(again["events"]) == before, "repeat unlink must write no event"


# ---------- unknown ids ----------

def test_unknown_id_raises_notfound_everywhere():
    c = card()
    for call in (lambda: core.get_card(9999),
                 lambda: core.update_card(9999, "ann", lane="done"),
                 lambda: core.comment(9999, "ann", "hi"),
                 lambda: core.link_cards(9999, c["id"], "blocks", "ann"),
                 lambda: core.link_cards(c["id"], 9999, "blocks", "ann"),
                 lambda: core.unlink_cards(9999, c["id"], "blocks", "ann"),
                 lambda: core.unlink_cards(c["id"], 9999, "blocks", "ann")):
        with pytest.raises(core.NotFound):
            call()


def test_notfound_is_not_a_valueerror():
    assert not issubclass(core.NotFound, ValueError), "HTTP maps them to different codes"


# ---------- activity log ----------

def test_comment_appends_to_the_log_in_order():
    c = card()
    core.comment(c["id"], "cat", "looks good")
    log = core.get_card(c["id"])["events"]
    assert log[-1]["kind"] == "comment", log[-1]
    assert log[-1]["actor"] == "cat" and log[-1]["detail"]["text"] == "looks good", log[-1]
    assert log == sorted(log, key=lambda e: e["id"]), "events must be chronological"


# ---------- listing and filtering ----------

def test_filters_by_lane_and_label():
    a = card(labels=["ui"])
    b = card("Second card", actor="bob", lane="plan", labels=["api"])
    core.update_card(a["id"], "ann", lane="develop")
    assert [x["id"] for x in core.list_cards(lane="develop")] == [a["id"]]
    assert [x["id"] for x in core.list_cards(lane="plan")] == [b["id"]]
    assert [x["id"] for x in core.list_cards(label="api")] == [b["id"]]
    assert [x["id"] for x in core.list_cards(label="ui")] == [a["id"]]
    assert core.list_cards(label="nope") == []
    assert len(core.list_cards()) == 2


def test_filters_by_assignee():
    mine = card(assignee="ann")
    card("Someone else's", actor="bob", assignee="bob")
    unassigned = card("Nobody's", actor="bob")
    assert [x["id"] for x in core.list_cards(assignee="ann")] == [mine["id"]]
    # assignee=None means "do not filter", NOT "unassigned" — a foot-gun worth pinning
    assert len(core.list_cards(assignee=None)) == 3, "None is the absence of a filter"
    assert unassigned["assignee"] is None


def test_the_renamed_fifth_lane_is_real():
    assert core.LANES[4] == "verify", core.LANES
    c = core.update_card(card()["id"], "ann", lane="verify")
    assert c["lane"] == "verify"
    assert [x["id"] for x in core.list_cards(lane="verify")] == [c["id"]]


def test_listed_most_recently_changed_first():
    a, b = card("A"), card("B", actor="bob")
    core.update_card(a["id"], "ann", title="A, touched")   # an explicit update, because
    ids = [x["id"] for x in core.list_cards()]             # ISO creation stamps can tie
    assert ids == [a["id"], b["id"]], ids


# ---------- projects ----------

def test_project_casing_is_preserved_but_matched_case_insensitively():
    t1 = card("Ship it", project="Tickets")
    t2 = card("Ship it too", actor="bob", project="tickets")
    inbox = card("Unfiled")
    assert t1["project"] == "Tickets" and t2["project"] == "tickets", (t1, t2)
    found = sorted(x["id"] for x in core.list_cards(project="tickets"))
    assert found == sorted([t1["id"], t2["id"]]), found
    assert sorted(x["id"] for x in core.list_cards(project="TICKETS")) == found
    assert {x["id"] for x in core.list_cards(project="inbox")} == {inbox["id"]}


def test_project_and_lane_filters_combine():
    t1 = card("Ship it", project="Tickets")
    card("Elsewhere", actor="bob", project="Other")
    assert [x["id"] for x in core.list_cards(project="Tickets", lane="todo")] == [t1["id"]]
    assert core.list_cards(project="Tickets", lane="done") == []


def test_list_projects_collapses_case_variants():
    card("Ship it", project="Tickets")
    card("Ship it too", actor="bob", project="tickets")
    card("Unfiled")
    projects = core.list_projects()
    assert len(projects) == 2, projects
    assert projects[0] == "inbox" and projects[1].lower() == "tickets", projects


def test_changing_project_is_a_plain_edit():
    t1 = card("Ship it", project="Tickets")
    t1 = core.update_card(t1["id"], "cat", project="Backlog")
    assert t1["project"] == "Backlog"
    ed = [e for e in t1["events"] if e["kind"] == "edited"]
    assert len(ed) == 1, ed
    assert ed[0]["detail"] == {"field": "project", "from": "Tickets", "to": "Backlog"}, ed[0]
    assert core.list_projects() == ["Backlog"], core.list_projects()


# ---------- users ----------

def test_actors_self_register_on_any_mutation():
    card(actor="dora")
    assert core.list_users() == ["dora"], core.list_users()


def test_ensure_user_is_the_explicit_door():
    card(actor="ann")
    core.ensure_user("ann")   # already present via her events
    core.ensure_user("zed")   # never touched a card
    assert core.list_users() == ["ann", "zed"], core.list_users()


def test_ensure_user_strips_and_rejects_blanks():
    assert core.ensure_user("  eve  ") == "eve", "a name is stripped before it is stored"
    for blank in ("", "   ", None):
        with pytest.raises(ValueError):
            core.ensure_user(blank)
    assert core.list_users() == ["eve"], core.list_users()
