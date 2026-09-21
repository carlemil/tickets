"""core.py: every operation, every documented rejection, every documented no-op.

The `db` fixture in conftest.py gives each test its own database, so these run in any
order and each one builds only the cards it needs.
"""

import pytest

import core


def card(title="Wire the board", actor="ann", **kw):
    kw.setdefault("project", "Home")   # the conftest `home` fixture made it
    return core.create_card(title, actor=actor, **kw)


def projects(*names):
    for n in names:
        core.create_project(n)


# ---------- creating ----------

def test_new_card_defaults_and_created_event():
    c = card(labels=["ui"], checklist=[{"text": "sketch", "done": False}])
    assert c["lane"] == "todo", c
    assert c["project"] == "Home", c
    assert c["labels"] == ["ui"], c["labels"]
    assert [(e["kind"], e["actor"]) for e in c["events"]] == [("created", "ann")], c["events"]


def test_create_card_persists_every_optional_field():
    projects("Tickets")
    c = card(description="the long version", assignee="bob",
             project="Tickets", lane="plan")
    assert c["description"] == "the long version"
    assert c["assignee"] == "bob"
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


@pytest.mark.parametrize("bad", [{"lane": "backlog"}, {"pos": "top"}, {"pos": True}])
def test_update_rejects_a_bad_lane_and_a_pos_that_is_not_a_number(bad):
    c = card()
    with pytest.raises(ValueError):
        core.update_card(c["id"], "ann", **bad)


def test_create_rejects_a_bad_lane():
    with pytest.raises(ValueError):
        card(lane="backlog")


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


def test_a_comment_can_carry_the_output_of_the_run_that_wrote_it():
    c = card()
    core.comment(c["id"], "claude-agent", "did it", output="● Read(a.txt)")
    core.comment(c["id"], "cat", "looks good")
    a, b = core.get_card(c["id"])["events"][-2:]
    assert a["detail"] == {"text": "did it", "output": "● Read(a.txt)"}
    assert b["detail"] == {"text": "looks good"}, "nothing to show: no output key at all"


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


def test_listed_in_board_order_which_an_edit_does_not_disturb():
    a, b = card("A"), card("B", actor="bob")
    core.update_card(a["id"], "ann", title="A, touched")   # touching a card used to throw
    ids = [x["id"] for x in core.list_cards()]             # it back to the top of its column
    assert ids == [a["id"], b["id"]], ids


# ---------- ordering: the column is the queue ----------

def test_a_new_card_lands_at_the_bottom_of_its_lane():
    a, b, c = card("A"), card("B"), card("C", lane="plan")
    assert a["pos"] < b["pos"], (a["pos"], b["pos"])
    assert [x["id"] for x in core.list_cards(lane="todo")] == [a["id"], b["id"]]
    assert c["pos"] == 1, "a lane of its own starts from the top"


def test_a_lane_move_without_a_pos_lands_at_the_bottom_of_the_new_lane():
    waiting, also = card("waiting"), card("also waiting")
    for c in (waiting, also):
        core.update_card(c["id"], "ann", lane="develop")
    moved = core.update_card(card("late")["id"], "bot", lane="develop")
    assert [x["id"] for x in core.list_cards(lane="develop")] ==         [waiting["id"], also["id"], moved["id"]]


def test_an_explicit_pos_is_honoured_and_reorders_the_list():
    a, b = card("A"), card("B")
    b = core.update_card(b["id"], "ann", pos=a["pos"] - 1)   # dragged above A
    assert [x["title"] for x in core.list_cards()] == ["B", "A"]
    # and a drop between two cards takes the midpoint, writing one row
    c = core.update_card(card("C")["id"], "ann", pos=(b["pos"] + a["pos"]) / 2)
    assert [x["title"] for x in core.list_cards()] == ["B", "C", "A"], c["pos"]


def test_a_pos_only_update_is_not_history_and_not_a_reply():
    """The one that would really hurt: tidying a column must not count as answering the
    agent, or every drag would restart a card the agent is waiting on."""
    c = core.update_card(card()["id"], "bot", lane="develop", attention=True,
                         auto_advance=False)
    before = len(c["events"])
    c = core.update_card(c["id"], "ann", pos=0.5)
    assert c["pos"] == 0.5 and len(c["events"]) == before, c["events"]
    assert c["attention"] is True and c["auto_advance"] is False, c


def test_a_database_with_priority_and_no_pos_keeps_the_order_it_showed(db):
    """The live board at deploy time: the column dropped, pos backfilled from the order
    the board used to list in, so nothing jumps on the first load."""
    import sqlite3
    core.list_cards()                       # today's schema, then make it yesterday's
    old = sqlite3.connect(core.DB_PATH)
    old.execute("ALTER TABLE cards DROP COLUMN pos")
    old.execute("ALTER TABLE cards ADD COLUMN priority TEXT NOT NULL DEFAULT 'med'")
    for n, at in enumerate(("2026-01-01", "2026-03-01", "2026-02-01"), 1):
        old.execute("INSERT INTO cards (id, project, title, lane, created_by, created_at,"
                    " updated_at, priority) VALUES (?,'Home',?,'todo','ann','t',?,'high')",
                    (n, f"c{n}", at))
    old.commit()
    old.close()
    assert [c["title"] for c in core.list_cards()] == ["c2", "c3", "c1"], "newest first, as before"
    assert "priority" not in core.list_cards()[0], "the column is gone"
    core.update_card(1, "ann", pos=0)       # and the added column is writable
    assert [c["title"] for c in core.list_cards()] == ["c1", "c2", "c3"]


# ---------- projects ----------

def test_a_card_takes_its_projects_spelling_and_filters_ignore_case():
    projects("Tickets")
    t1 = card("Ship it", project="Tickets")
    t2 = card("Ship it too", actor="bob", project="tickets")
    home = card("Elsewhere")
    assert t1["project"] == t2["project"] == "Tickets", (t1, t2)
    found = sorted(x["id"] for x in core.list_cards(project="tickets"))
    assert found == sorted([t1["id"], t2["id"]]), found
    assert sorted(x["id"] for x in core.list_cards(project="TICKETS")) == found
    assert {x["id"] for x in core.list_cards(project="home")} == {home["id"]}


def test_project_and_lane_filters_combine():
    projects("Tickets", "Other")
    t1 = card("Ship it", project="Tickets")
    card("Elsewhere", actor="bob", project="Other")
    assert [x["id"] for x in core.list_cards(project="Tickets", lane="todo")] == [t1["id"]]
    assert core.list_cards(project="Tickets", lane="done") == []


def test_names_already_on_cards_become_projects_once_each(db):
    """A database from before projects: its cards name projects that were only strings.
    Each name gets one project row, case variants collapsing into one, and cards keep
    the spelling they had."""
    import sqlite3
    core.list_projects()                       # creates the schema
    old = sqlite3.connect(core.DB_PATH)
    old.execute("DELETE FROM projects")
    for n, proj in enumerate(("Tickets", "tickets", "inbox"), 1):
        old.execute("INSERT INTO cards (id, project, title, lane, created_by, created_at,"
                    " updated_at) VALUES (?,?,'t','todo','ann','t','t')",
                    (n, proj))
    old.commit()
    old.close()
    names = [p["name"] for p in core.list_projects()]
    assert names == ["inbox", "Tickets"], names
    assert core.get_card(2)["project"] == "tickets", "a card's stored spelling survives"
    assert sorted(c["id"] for c in core.list_cards(project="TICKETS")) == [1, 2]


@pytest.mark.no_home
def test_a_fresh_board_has_no_projects_and_a_card_needs_one():
    assert core.list_projects() == []
    for missing in ({}, {"project": None}, {"project": ""}):
        with pytest.raises(ValueError, match="a card needs a project: create one first"):
            core.create_card("x", actor="ann", **missing)
    assert core.list_cards() == [] and core.list_projects() == [], "nothing was made"
    core.create_project("First")
    assert core.create_card("x", actor="ann", project="first")["project"] == "First"


@pytest.mark.no_home
def test_connect_invents_no_default_project(db):
    for _ in range(2):
        assert core.list_projects() == []


def test_an_old_database_keeps_its_inbox_as_an_ordinary_project(db):
    """A board from when "inbox" was the built-in default keeps it, now renamable."""
    import sqlite3
    old = sqlite3.connect(core.DB_PATH)
    old.execute("INSERT INTO cards (project, title, lane, created_by, created_at,"
                " updated_at) VALUES ('inbox','old','todo','ann','t','t')")
    old.commit()
    old.close()
    assert [p["name"] for p in core.list_projects()] == ["Home", "inbox"]
    core.update_project("inbox", name="Misc")
    assert core.list_cards()[0]["project"] == "Misc"


def test_changing_project_is_a_plain_edit():
    projects("Tickets", "Backlog")
    t1 = card("Ship it", project="Tickets")
    t1 = core.update_card(t1["id"], "cat", project="Backlog")
    assert t1["project"] == "Backlog"
    ed = [e for e in t1["events"] if e["kind"] == "edited"]
    assert len(ed) == 1, ed
    assert ed[0]["detail"] == {"field": "project", "from": "Tickets", "to": "Backlog"}, ed[0]
    assert core.update_card(t1["id"], "cat", project="backlog")["project"] == "Backlog", \
        "normalised to the project's spelling"


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


def test_rename_user_moves_every_trace_of_the_old_name():
    mine = card(actor="ce")["id"]
    core.update_card(mine, "ce", assignee="ce")
    core.comment(mine, "ce", "hi")
    core.set_activity("ce", mine, "looking")
    theirs = card(actor="zoe", assignee="zoe")["id"]
    core.update_card(theirs, "zoe", assignee="ce")      # the old name as `to`...
    core.update_card(theirs, "zoe", assignee="zoe")     # ...and as `from`
    core.set_activity("User", theirs, "already here")   # a clash on activity's primary key

    core.rename_user("ce", "User")

    c = core.get_card(mine)
    assert c["created_by"] == "User" and c["assignee"] == "User", c
    assert {e["actor"] for e in c["events"]} == {"User"}, c["events"]
    assert [e["detail"]["to"] for e in c["events"] if e["kind"] == "assigned"] == ["User"]
    t = core.get_card(theirs)
    assert t["created_by"] == "zoe" and t["assignee"] == "zoe", "zoe is untouched"
    assert {e["actor"] for e in t["events"]} == {"zoe"}, t["events"]
    assert [(e["detail"]["from"], e["detail"]["to"]) for e in t["events"]
            if e["kind"] == "assigned"] == [("zoe", "User"), ("User", "zoe")]
    assert [(a["actor"], a["doing"]) for a in core.list_activity()] == [("User", "already here")]
    assert core.list_users() == ["User", "zoe"], core.list_users()

    before = (core.get_card(mine), core.get_card(theirs), core.list_users())
    core.rename_user("ce", "User")                      # a second run changes nothing
    assert (core.get_card(mine), core.get_card(theirs), core.list_users()) == before
    core.rename_user("User", "User")                    # same name: a no-op
    for blank in ("", "  ", None):
        with pytest.raises(ValueError):
            core.rename_user(blank, "User")
        with pytest.raises(ValueError):
            core.rename_user("ce", blank)


# ---------- archiving ----------

def test_archive_hides_the_card_and_unarchive_brings_it_back():
    c = card()
    c = core.update_card(c["id"], "bob", archived=True)
    assert c["archived"] is True, c
    assert core.list_cards() == [], "archived cards are off the board"
    assert [x["id"] for x in core.list_cards(archived=True)] == [c["id"]]
    c = core.update_card(c["id"], "bob", archived=False)
    assert [x["id"] for x in core.list_cards()] == [c["id"]]
    evs = [e["detail"] for e in c["events"] if e["kind"] == "archived"]
    assert evs == [{"field": "archived", "from": False, "to": True},
                   {"field": "archived", "from": True, "to": False}], evs


def test_archived_must_be_a_bool():
    with pytest.raises(ValueError):
        core.update_card(card()["id"], "bob", archived="yes")


def test_a_database_from_before_archiving_gains_the_column(db):
    import sqlite3
    old = sqlite3.connect(core.DB_PATH)
    old.execute("DROP TABLE IF EXISTS cards")
    old.execute("CREATE TABLE cards (id INTEGER PRIMARY KEY, project TEXT NOT NULL,"
                " title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', lane TEXT NOT NULL,"
                " assignee TEXT, created_by TEXT NOT NULL, created_at TEXT NOT NULL,"
                " updated_at TEXT NOT NULL,"
                " labels TEXT NOT NULL DEFAULT '[]', checklist TEXT NOT NULL DEFAULT '[]')")
    old.execute("INSERT INTO cards VALUES (1,'inbox','old','','todo',NULL,'ann','t','t','[]','[]')")
    old.commit()
    old.close()
    assert [(c["title"], c["archived"], c["auto_advance"]) for c in core.list_cards()] == [
        ("old", False, False)],         "existing cards survive and come back unarchived"
    core.update_card(1, "ann", archived=True)   # the added column is writable
    assert core.list_cards() == [] and core.list_cards() == [], "and the second connect is a no-op"


def test_new_cards_start_unarchived():
    assert card()["archived"] is False


def test_archiving_twice_writes_one_event():
    c = core.update_card(card()["id"], "bob", archived=True)
    before = c["updated_at"], len(c["events"])
    c = core.update_card(c["id"], "bob", archived=True)
    assert (c["updated_at"], len(c["events"])) == before, "a repeat archive is a no-op"


def test_unarchiving_an_active_card_is_a_no_op():
    c = card()
    assert core.update_card(c["id"], "bob", archived=False)["events"] == c["events"]


@pytest.mark.parametrize("bad", ["yes", 1, 0, None, "true"])
def test_archived_rejects_everything_but_a_bool(bad):
    c = card()
    with pytest.raises(ValueError):
        core.update_card(c["id"], "bob", archived=bad)
    assert core.get_card(c["id"])["archived"] is False, "and nothing was written"


def test_archived_cards_stay_fully_usable():
    """Archived means off the board, not frozen: the panel opens it, edits and comments
    still land, and links to it still resolve."""
    a = core.update_card(card()["id"], "ann", archived=True)
    b = card("Active", actor="bob")
    assert core.get_card(a["id"])["title"] == "Wire the board"
    assert core.update_card(a["id"], "ann", lane="done")["lane"] == "done"
    assert core.comment(a["id"], "ann", "still here")["events"][-1]["kind"] == "comment"
    core.link_cards(b["id"], a["id"], "blocks", "ann")
    assert core.get_card(a["id"])["links"] == core.get_card(b["id"])["links"] != []


def test_archived_filter_combines_with_the_others():
    projects("Tickets", "Other")
    a = core.update_card(card(project="Tickets", lane="plan")["id"], "ann", archived=True)
    card("Active same project", actor="bob", project="Tickets", lane="plan")
    core.update_card(card("Archived elsewhere", project="Other")["id"], "ann", archived=True)
    assert [x["id"] for x in core.list_cards(project="tickets", archived=True)] == [a["id"]]
    assert [x["id"] for x in core.list_cards(lane="plan", archived=True,
                                             project="Tickets")] == [a["id"]]
    assert core.list_cards(lane="done", archived=True) == []


def test_a_project_is_listed_whatever_its_cards_are_doing():
    # so the project filter can still reach it with "show archived" on, or before any card
    projects("Old", "Empty")
    core.update_card(card(project="Old")["id"], "ann", archived=True)
    assert [p["name"] for p in core.list_projects()] == ["Empty", "Home", "Old"]


def test_archived_cannot_be_set_at_creation():
    with pytest.raises(TypeError):
        core.create_card("x", actor="ann", project="Home", archived=True)


# ---------- more updating corner cases ----------

def test_one_update_with_several_fields_writes_one_event_each():
    # a move back, so the forward-move auto advance rule adds no event of its own
    c = core.update_card(card(lane="test")["id"], "bob", lane="develop", assignee="cat", title="New")
    kinds = sorted(e["kind"] for e in c["events"][1:])
    assert kinds == ["assigned", "edited", "moved"], c["events"]


def test_a_no_op_update_does_not_bump_updated_at():
    c = card()
    assert core.update_card(c["id"], "bob", title=c["title"])["updated_at"] == c["updated_at"]


def test_a_rejected_update_writes_nothing():
    c = card()
    with pytest.raises(ValueError):
        core.update_card(c["id"], "bob", title="half", lane="nowhere")
    assert core.get_card(c["id"]) == c, "validation runs before any field is written"


def test_removing_a_checklist_item_is_an_edit():
    c = card(checklist=[{"text": "a", "done": False}, {"text": "b", "done": True}])
    c = core.update_card(c["id"], "ann", checklist=[{"text": "a", "done": False}])
    ed = [e["detail"] for e in c["events"] if e["kind"] == "edited"]
    assert ed == [{"field": "checklist", "from": ["a", "b"], "to": ["a"]}], ed


def test_unticking_writes_a_checked_event_with_done_false():
    c = card(checklist=[{"text": "a", "done": True}])
    c = core.update_card(c["id"], "ann", checklist=[{"text": "a", "done": False}])
    assert c["events"][-1]["detail"] == {"text": "a", "done": False}, c["events"]


def test_text_round_trips_unchanged():
    t = "Ärende ✓ \"quoted\" <b>not html</b>\nsecond line"
    c = card(t, description=t, labels=[t])
    c = core.get_card(c["id"])
    assert c["title"] == c["description"] == c["labels"][0] == t, c


# ---------- configuring projects ----------

def test_create_project_with_path_and_instructions(tmp_path):
    p = core.create_project("  Tickets  ", path=str(tmp_path), instructions="run uv run pytest")
    assert p == {"name": "Tickets", "path": str(tmp_path), "instructions": "run uv run pytest", "color": "#00875a"}
    assert core.get_project("tickets") == p, "looked up ignoring case"
    assert p in core.list_projects()


def test_path_and_instructions_are_optional():
    assert core.create_project("Bare") == {"name": "Bare", "path": "", "instructions": "", "color": "#00875a"}


@pytest.mark.parametrize("name", ["", "   ", None])
def test_a_project_needs_a_name(name):
    with pytest.raises(ValueError, match="needs a name"):
        core.create_project(name)


@pytest.mark.parametrize("name", ["Tickets", "TICKETS", "home", "Home"])
def test_project_names_are_unique_ignoring_case(name):
    projects("Tickets")
    with pytest.raises(ValueError, match="already exists"):
        core.create_project(name)


def test_a_relative_path_is_rejected():
    with pytest.raises(ValueError, match="absolute"):
        core.create_project("Rel", path="some/dir")


def test_a_path_that_is_not_a_folder_is_rejected(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("x")
    for bad in (tmp_path / "missing", f):
        with pytest.raises(ValueError, match="no folder"):
            core.create_project("X", path=str(bad))
    assert [p["name"] for p in core.list_projects()] == ["Home"], "nothing was created"


def test_update_path_and_instructions(tmp_path):
    projects("Tickets")
    p = core.update_project("tickets", path=str(tmp_path), instructions="be brief")
    assert p == {"name": "Tickets", "path": str(tmp_path), "instructions": "be brief", "color": "#00875a"}
    assert core.update_project("Tickets", path="")["path"] == "", "a path can be cleared"


def test_update_rejects_a_bad_path_and_writes_nothing(tmp_path):
    core.create_project("Tickets", path=str(tmp_path))
    with pytest.raises(ValueError):
        core.update_project("Tickets", path=str(tmp_path / "missing"), instructions="lost")
    assert core.get_project("Tickets") == {"name": "Tickets", "path": str(tmp_path),
                                           "instructions": "", "color": "#00875a"}


def test_rename_carries_every_card_including_archived_and_writes_no_events():
    projects("Tickets")
    a = card(project="Tickets")
    b = core.update_card(card("b", project="Tickets")["id"], "ann", archived=True)
    other = card("c")
    before = {id: core.get_card(id) for id in (a["id"], b["id"])}
    core.update_project("Tickets", name="Board")
    for id, was in before.items():
        now = core.get_card(id)
        assert now["project"] == "Board", now
        assert now["events"] == was["events"] and now["updated_at"] == was["updated_at"]
    assert core.get_card(other["id"])["project"] == "Home", "other projects untouched"
    assert [p["name"] for p in core.list_projects()] == ["Board", "Home"]
    with pytest.raises(core.NotFound):
        core.get_project("Tickets")


def test_a_case_only_rename_respells_the_project_and_its_cards():
    projects("tickets")
    c = card(project="tickets")
    assert core.update_project("tickets", name="Tickets")["name"] == "Tickets"
    assert core.get_card(c["id"])["project"] == "Tickets"


def test_rename_onto_another_project_is_rejected():
    projects("Tickets", "Board")
    c = card(project="Tickets")
    with pytest.raises(ValueError, match="already exists"):
        core.update_project("Tickets", name="board")
    assert core.get_card(c["id"])["project"] == "Tickets", "nothing moved"


def test_any_project_can_be_renamed_and_configured(tmp_path):
    c = card()
    assert core.update_project("Home", name="Misc", path=str(tmp_path)) == {
        "name": "Misc", "path": str(tmp_path), "instructions": "", "color": "#0052cc"}
    assert core.get_card(c["id"])["project"] == "Misc"


def test_update_project_rejects_unknown_fields_and_unknown_projects():
    with pytest.raises(ValueError, match="bogus"):
        core.update_project("Home", bogus=1)
    with pytest.raises(core.NotFound):
        core.update_project("nope", path="")


def test_a_card_cannot_name_an_unconfigured_project():
    with pytest.raises(ValueError, match="unknown project 'Nowhere'"):
        card(project="Nowhere")
    c = card()
    with pytest.raises(ValueError, match="unknown project"):
        core.update_card(c["id"], "ann", project="Nowhere")
    with pytest.raises(ValueError, match="a card needs a project"):
        core.update_card(c["id"], "ann", project=None)
    assert core.get_card(c["id"]) == c, "and nothing was written"
    assert len(core.list_cards()) == 1


# ---------- auto advance ----------

def test_auto_advance_defaults_off_and_can_be_set_at_creation():
    assert card()["auto_advance"] is False
    assert card(auto_advance=True)["auto_advance"] is True


def test_auto_advance_toggles_and_logs_an_edit():
    c = core.update_card(card()["id"], "bob", auto_advance=True)
    assert c["auto_advance"] is True
    c = core.update_card(c["id"], "bob", auto_advance=False)
    evs = [e["detail"] for e in c["events"] if e["detail"].get("field") == "auto_advance"]
    assert evs == [{"field": "auto_advance", "from": False, "to": True},
                   {"field": "auto_advance", "from": True, "to": False}], evs


@pytest.mark.parametrize("bad", ["yes", 1, None])
def test_auto_advance_rejects_everything_but_a_bool(bad):
    c = card()
    with pytest.raises(ValueError, match="auto_advance must be true or false"):
        core.update_card(c["id"], "bob", auto_advance=bad)
    with pytest.raises(ValueError, match="auto_advance must be true or false"):
        core.create_card("x", actor="ann", project="Home", auto_advance=bad)
    assert core.get_card(c["id"])["auto_advance"] is False
    assert len(core.list_cards()) == 1, "the bad create wrote nothing"


def test_a_database_with_archived_but_not_auto_advance_gains_it(db):
    import sqlite3
    core.list_cards()   # create today's schema, then take the newest column back out
    old = sqlite3.connect(core.DB_PATH)
    old.execute("ALTER TABLE cards DROP COLUMN auto_advance")
    old.execute("INSERT INTO cards (project, title, lane, created_by, created_at,"
                " updated_at, archived) VALUES ('inbox','old','todo','ann','t','t',1)")
    old.commit()
    old.close()
    [c] = core.list_cards(archived=True)
    assert (c["archived"], c["auto_advance"]) == (True, False)
    assert core.update_card(c["id"], "ann", auto_advance=True)["auto_advance"] is True


# ---------- attention ----------

def flagged():
    return core.update_card(card()["id"], "agent", attention=True)


def test_attention_is_off_by_default_and_set_without_a_log_entry():
    c = card()
    assert c["attention"] is False
    c = core.update_card(c["id"], "agent", attention=True)
    assert c["attention"] is True
    assert [e["kind"] for e in c["events"]] == ["created"], "a notification, not history"


@pytest.mark.parametrize("change", [dict(lane="plan"), dict(title="new"), dict(assignee="bob"),
                                    dict(description="answer"), dict(auto_advance=True)])
def test_any_other_change_clears_attention(change):
    c = core.update_card(flagged()["id"], "ce", **change)
    assert c["attention"] is False
    assert not any(e["detail"].get("field") == "attention" for e in c["events"])


def test_a_comment_clears_attention():
    c = flagged()
    core.comment(c["id"], "ce", "red")
    assert core.get_card(c["id"])["attention"] is False


def test_an_unchanged_write_leaves_attention_on():
    c = flagged()
    assert core.update_card(c["id"], "ce", title=c["title"])["attention"] is True


def test_attention_set_with_other_changes_stays_on():
    c = core.update_card(card()["id"], "agent", lane="verify", assignee="", attention=True)
    assert (c["lane"], c["attention"]) == ("verify", True)


def test_attention_can_be_cleared_explicitly():
    assert core.update_card(flagged()["id"], "ce", attention=False)["attention"] is False


@pytest.mark.parametrize("bad", ["yes", 1, None])
def test_attention_rejects_everything_but_a_bool(bad):
    c = card()
    with pytest.raises(ValueError, match="attention must be true or false"):
        core.update_card(c["id"], "bob", attention=bad)


def test_a_reply_to_a_waiting_card_turns_auto_advance_on_and_logs_it():
    c = core.update_card(card(lane="plan")["id"], "agent", attention=True)
    c = core.update_card(c["id"], "ce", answers="blue")
    assert (c["attention"], c["auto_advance"]) == (False, True)
    assert [e["detail"] for e in c["events"] if e["detail"].get("field") == "auto_advance"] == [
        {"field": "auto_advance", "from": False, "to": True}]
    assert c["events"][-1]["actor"] == "ce"


def test_a_comment_is_a_reply_too():
    c = core.update_card(card(lane="develop")["id"], "agent", attention=True)
    core.comment(c["id"], "ce", "try red")
    c = core.get_card(c["id"])
    assert (c["attention"], c["auto_advance"]) == (False, True)
    assert [e["kind"] for e in c["events"]][-2:] == ["comment", "edited"]


def test_a_reply_can_keep_auto_advance_off():
    c = core.update_card(card(lane="plan")["id"], "agent", attention=True)
    c = core.update_card(c["id"], "ce", answers="blue", auto_advance=False)
    assert (c["attention"], c["auto_advance"]) == (False, False)


@pytest.mark.parametrize("lane", ["todo", "done"])   # verify: a forward move, which does switch it on
def test_a_reply_outside_the_agent_lanes_leaves_auto_advance_off(lane):
    c = core.update_card(card(lane="develop")["id"], "agent", attention=True)
    c = core.update_card(c["id"], "ce", lane=lane)
    assert (c["attention"], c["auto_advance"]) == (False, False)
    c = core.update_card(core.update_card(c["id"], "agent", attention=True)["id"], "ce", title="x")
    assert c["auto_advance"] is False
    core.update_card(c["id"], "agent", attention=True)
    core.comment(c["id"], "ce", "done")
    assert core.get_card(c["id"])["auto_advance"] is False


def test_an_edit_to_a_card_not_waiting_leaves_auto_advance_alone():
    c = core.update_card(card(lane="develop")["id"], "ce", title="x")
    core.comment(c["id"], "ce", "hi")
    assert core.get_card(c["id"])["auto_advance"] is False


def test_a_reply_to_an_auto_card_logs_no_change():
    c = core.update_card(card(lane="test", auto_advance=True)["id"], "agent", attention=True)
    c = core.update_card(c["id"], "ce", title="x")
    assert not any(e["detail"].get("field") == "auto_advance" for e in c["events"])


# ---------- plan, questions, answers ----------

def test_plan_questions_and_answers_start_empty_and_log_edits():
    c = card()
    assert (c["plan"], c["questions"], c["answers"]) == ("", "", "")
    c = core.update_card(c["id"], "agent", plan="p", questions="q?")
    c = core.update_card(c["id"], "ce", answers="a")
    assert (c["plan"], c["questions"], c["answers"], c["description"]) == ("p", "q?", "a", "")
    assert [e["detail"]["field"] for e in c["events"] if e["kind"] == "edited"] == [
        "plan", "questions", "answers"]


@pytest.mark.parametrize("bad", [None, 1, ["x"]])
def test_plan_fields_reject_anything_but_text(bad):
    c = card()
    for f in ("plan", "questions", "answers"):
        with pytest.raises(ValueError, match=f"{f} must be text"):
            core.update_card(c["id"], "ann", **{f: bad})


def test_a_database_without_the_plan_fields_gains_them(db):
    import sqlite3
    core.list_cards()
    old = sqlite3.connect(core.DB_PATH)
    for col in ("plan", "questions", "answers"):
        old.execute(f"ALTER TABLE cards DROP COLUMN {col}")
    old.execute("INSERT INTO cards (project, title, lane, created_by, created_at,"
                " updated_at) VALUES ('inbox','old','todo','ann','t','t')")
    old.commit()
    old.close()
    [c] = core.list_cards()
    assert (c["plan"], c["questions"], c["answers"]) == ("", "", "")



def test_assigning_a_new_name_registers_it():
    c = core.update_card(card()["id"], "ann", assignee="helper-bot")
    assert "helper-bot" in core.list_users()
    card(assignee="other-bot")
    assert "other-bot" in core.list_users()
    core.update_card(c["id"], "ann", assignee=None)
    assert core.list_users().count("helper-bot") == 1

# ---------- project colors ----------

def test_new_projects_take_the_least_used_palette_color():
    assert core.get_project("Home")["color"] == core.PALETTE[0]
    for i, name in enumerate(["B", "C", "D"], 1):
        assert core.create_project(name)["color"] == core.PALETTE[i]
    core.update_project("C", color="#123456")          # frees palette[2] up again
    assert core.create_project("E")["color"] == core.PALETTE[2]


def test_the_palette_wraps_once_every_color_is_used():
    for i in range(1, len(core.PALETTE)):
        core.create_project(f"P{i}")
    assert core.create_project("Wrapped")["color"] == core.PALETTE[0]


def test_a_chosen_color_is_kept_and_lowercased():
    assert core.create_project("Mine", color="#AABBCC")["color"] == "#aabbcc"
    assert core.update_project("Mine", color="#00FF00")["color"] == "#00ff00"


@pytest.mark.parametrize("bad", ["red", "#abc", "#12345g", "123456", "#1234567", None, ""])
def test_a_bad_color_is_refused_on_update_and_writes_nothing(bad):
    with pytest.raises(ValueError, match="color must be #rrggbb"):
        core.update_project("Home", color=bad, instructions="lost")
    assert core.get_project("Home") == {"name": "Home", "path": "", "instructions": "",
                                        "color": core.PALETTE[0]}


@pytest.mark.parametrize("bad", ["red", "#abc", "#12345g"])
def test_a_bad_color_is_refused_on_create(bad):
    with pytest.raises(ValueError, match="color must be #rrggbb"):
        core.create_project("X", color=bad)
    assert [p["name"] for p in core.list_projects()] == ["Home"]


@pytest.mark.no_home
def test_an_older_database_gains_colors_for_its_projects(db):
    import sqlite3
    old = sqlite3.connect(db)
    old.execute("CREATE TABLE projects (name TEXT PRIMARY KEY COLLATE NOCASE,"
                " path TEXT NOT NULL DEFAULT '', instructions TEXT NOT NULL DEFAULT '')")
    old.executemany("INSERT INTO projects (name) VALUES (?)", [("b",), ("a",), ("c",)])
    old.commit()
    old.close()
    assert [(p["name"], p["color"]) for p in core.list_projects()] == [
        ("a", core.PALETTE[0]), ("b", core.PALETTE[1]), ("c", core.PALETTE[2])]
    assert core.list_projects() == core.list_projects(), "assigned once, stable after"


@pytest.mark.no_home
def test_projects_backfilled_from_cards_get_colors_too(db):
    import sqlite3
    core.list_projects()
    raw = sqlite3.connect(db)
    raw.execute("INSERT INTO cards (project, title, lane, created_by, created_at,"
                " updated_at) VALUES ('Legacy','old','todo','ann','t','t')")
    raw.commit()
    raw.close()
    assert core.list_projects() == [{"name": "Legacy", "path": "", "instructions": "",
                                     "color": core.PALETTE[0]}]


# ---------- activity ----------

def test_activity_is_set_replaced_and_cleared_per_actor():
    a, b = card("a")["id"], card("b", lane="develop")["id"]
    core.set_activity("bot", a, "planning")
    core.set_activity("bot2", b, "developing")
    core.set_activity("bot", b, " testing ")          # replaces bot's row, stripped
    got = {r["actor"]: (r["card_id"], r["doing"], r["title"], r["lane"], r["project"])
           for r in core.list_activity()}
    assert got == {"bot": (b, "testing", "b", "develop", "Home"),
                   "bot2": (b, "developing", "b", "develop", "Home")}
    assert core.set_activity("bot") == [r for r in core.list_activity()]
    assert [r["actor"] for r in core.list_activity()] == ["bot2"]
    core.set_activity("bot")                            # clearing twice is a no-op
    assert len(core.list_activity()) == 1


def test_activity_writes_no_event_and_no_updated_at():
    c = card()
    core.set_activity("bot", c["id"], "planning")
    after = core.get_card(c["id"])
    assert [e["kind"] for e in after["events"]] == ["created"]
    assert after["updated_at"] == c["updated_at"]


def test_activity_rejects_bad_input():
    c = card()["id"]
    with pytest.raises(core.NotFound):
        core.set_activity("bot", 999, "planning")
    for blank in ("", "  ", None):
        with pytest.raises(ValueError):
            core.set_activity(blank, c, "planning")
        with pytest.raises(ValueError):
            core.set_activity("bot", c, blank)
    assert core.list_activity() == []


def test_activity_shows_the_card_as_it_is_now():
    c = card()["id"]
    core.set_activity("bot", c, "planning")
    core.update_card(c, "ann", title="renamed", lane="develop")
    assert (core.list_activity()[0]["title"], core.list_activity()[0]["lane"]) == ("renamed", "develop")


# ---------- deleting a project ----------

def test_deleting_a_project_moves_every_card_to_no_project():
    projects("Doomed")
    a = card("a", project="Doomed")["id"]
    b = card("b", project="Doomed")["id"]
    core.update_card(b, "ann", archived=True)
    keep = card("c")["id"]
    assert core.delete_project("doomed") == {"deleted": "Doomed", "moved": 2}
    assert [p["name"] for p in core.list_projects()] == ["Home", core.NO_PROJECT]
    assert core.get_project(core.NO_PROJECT)["color"] == core.NO_PROJECT_COLOR
    assert {core.get_card(i)["project"] for i in (a, b)} == {core.NO_PROJECT}
    assert core.get_card(b)["archived"] is True, "archived cards move and stay archived"
    assert core.get_card(keep)["project"] == "Home"
    assert [e["kind"] for e in core.get_card(a)["events"]] == ["created"], "no card events"


def test_deleting_an_empty_project_creates_no_no_project():
    projects("Empty")
    assert core.delete_project("Empty") == {"deleted": "Empty", "moved": 0}
    assert [p["name"] for p in core.list_projects()] == ["Home"]


def test_a_second_delete_reuses_no_project_and_keeps_its_settings():
    projects("A", "B")
    card(project="A")
    card(project="B")
    core.delete_project("A")
    core.update_project(core.NO_PROJECT, instructions="leave these alone")
    core.delete_project("B")
    assert len(core.list_cards(project=core.NO_PROJECT)) == 2
    assert core.get_project(core.NO_PROJECT)["instructions"] == "leave these alone"


def test_no_project_cannot_be_deleted_in_any_case():
    projects("A")
    card(project="A")
    core.delete_project("A")
    for spelling in (core.NO_PROJECT, "no project", "NO PROJECT"):
        with pytest.raises(ValueError, match="cannot be deleted"):
            core.delete_project(spelling)
    assert len(core.list_cards(project=core.NO_PROJECT)) == 1


def test_deleting_an_unknown_project_is_not_found():
    with pytest.raises(core.NotFound):
        core.delete_project("nope")


def test_a_deleted_projects_name_can_be_used_again():
    projects("Again")
    old = card(project="Again")["id"]
    core.delete_project("Again")
    projects("Again")
    assert core.list_cards(project="Again") == []
    assert core.get_card(old)["project"] == core.NO_PROJECT


# ---------- moving into plan starts auto advance ----------

@pytest.mark.parametrize("frm", ["todo", "develop", "test", "verify", "done"])
def test_moving_into_plan_turns_auto_advance_on(frm):
    c = card(lane=frm)["id"]
    after = core.update_card(c, "ann", lane="plan")
    assert after["auto_advance"] is True
    ev = [e for e in after["events"] if e["detail"].get("field") == "auto_advance"]
    assert [(e["actor"], e["detail"]["from"], e["detail"]["to"]) for e in ev] == [("ann", False, True)]


def test_a_move_into_plan_can_say_no_auto_advance():
    c = card()["id"]
    assert core.update_card(c, "ann", lane="plan", auto_advance=False)["auto_advance"] is False


@pytest.mark.parametrize("frm, to", [("todo", "develop"), ("todo", "verify"), ("plan", "develop"),
                                     ("develop", "test"), ("test", "verify")])
def test_moving_forward_turns_auto_advance_on(frm, to):
    c = core.update_card(card(lane=frm)["id"], "ann", lane=to)
    assert c["auto_advance"] is True
    assert [e["detail"] for e in c["events"] if e["detail"].get("field") == "auto_advance"] == [
        {"field": "auto_advance", "from": False, "to": True}]


@pytest.mark.parametrize("frm", ["todo", "plan", "test", "verify"])
def test_moving_to_done_leaves_auto_advance_off(frm):
    assert core.update_card(card(lane=frm)["id"], "ann", lane="done")["auto_advance"] is False


@pytest.mark.parametrize("frm, to", [("develop", "todo"), ("verify", "develop"),
                                     ("test", "develop"), ("done", "verify"), ("plan", "todo")])
def test_moving_back_leaves_auto_advance_alone(frm, to):
    assert core.update_card(card(lane=frm)["id"], "ann", lane=to)["auto_advance"] is False


def test_a_forward_move_can_say_no_auto_advance():
    c = core.update_card(card(lane="develop")["id"], "agent", lane="test", auto_advance=False)
    assert c["auto_advance"] is False
    assert not any(e["detail"].get("field") == "auto_advance" for e in c["events"])


def test_moving_to_done_does_not_switch_an_auto_card_off():
    c = core.update_card(card(lane="verify", auto_advance=True)["id"], "ann", lane="done")
    assert c["auto_advance"] is True, "left as it was: the agent never works done anyway"


def test_a_card_already_in_plan_is_not_switched_back_on():
    """The agent stops a card with open questions in plan and switches auto advance off;
    any later write that repeats lane=plan must not restart it behind the person's back."""
    c = card(lane="plan")["id"]
    core.update_card(c, "ann", lane="plan", title="edited while waiting")
    assert core.get_card(c)["auto_advance"] is False


def test_creating_a_card_in_plan_does_not_switch_it_on():
    assert card(lane="plan")["auto_advance"] is False, "a move turns it on, not a create"


def test_questions_are_stored_numbered_from_1_whoever_writes_them():
    id = core.create_card("t", "ce", project="Home")["id"]
    assert core.update_card(id, "bot", questions="- red?\n  - which red\n* blue?")["questions"] \
        == "1. red?\n  - which red\n2. blue?"
    assert core.update_card(id, "ce", questions="3. only one?")["questions"] == "1. only one?"
    assert core.update_card(id, "ce", questions="")["questions"] == ""


# ---------- a new request on a verified card replans it (#60) ----------

@pytest.mark.parametrize("field, value", [("description", "a different ask"),
                                          ("answers", "1. blue"),
                                          ("checklist", [{"text": "and this", "done": False}])])
def test_a_persons_update_to_a_verified_card_sends_it_back_to_plan(field, value):
    c = core.update_card(card(lane="verify")["id"], "ann", **{field: value})
    assert (c["lane"], c["assignee"], c["auto_advance"]) == ("plan", core.AGENT, True)
    assert [(e["actor"], e["detail"]["from"], e["detail"]["to"]) for e in c["events"]
            if e["kind"] == "moved"] == [("ann", "verify", "plan")]


def test_a_persons_comment_on_a_verified_card_sends_it_back_to_plan():
    c = core.comment(card(lane="verify")["id"], "ann", "not what I meant")
    assert (c["lane"], c["assignee"], c["auto_advance"]) == ("plan", core.AGENT, True)
    assert [e["kind"] for e in c["events"]] == ["created", "comment", "moved", "assigned",
                                                "edited"]


@pytest.mark.parametrize("lane", ["todo", "plan", "develop", "test", "done"])
def test_only_verify_cards_are_replanned(lane):
    c = card(lane=lane)["id"]
    assert core.update_card(c, "ann", description="a different ask")["lane"] == lane
    assert core.comment(c, "ann", "and this too")["lane"] == lane


def test_the_agents_own_writes_never_replan_a_verified_card():
    """Its deploy step comments on the card it just deployed: that must not bounce it."""
    c = card(lane="verify")["id"]
    assert core.comment(c, core.AGENT, "deployed: ok")["lane"] == "verify"
    assert core.update_card(c, core.AGENT, checklist=[{"text": "shipped", "done": True}],
                            merged=True, deployed=True)["lane"] == "verify"


def test_a_lane_in_the_same_write_wins():
    c = card(lane="verify")["id"]
    assert core.update_card(c, "ann", description="typo fixed", lane="verify")["lane"] == "verify"
    assert core.update_card(c, "ann", description="done with it", lane="done")["lane"] == "done"


def test_an_unchanged_field_replans_nothing():
    c = card(lane="verify", description="as asked")["id"]
    after = core.update_card(c, "ann", description="as asked", title="a better title")
    assert (after["lane"], after["assignee"]) == ("verify", None)


@pytest.mark.parametrize("field, value", [("title", "renamed"), ("labels", ["ui"]),
                                          ("plan", "step one"), ("questions", "- red?"),
                                          ("attention", True), ("assignee", "bob")])
def test_other_fields_leave_a_verified_card_where_it_is(field, value):
    assert core.update_card(card(lane="verify")["id"], "ann", **{field: value})["lane"] == "verify"


def test_a_replanned_card_is_rework_at_the_bottom_of_plan():
    waiting = card(lane="plan")["id"]
    c = core.update_card(shipped()["id"], "ann", description="a different ask")
    assert (c["merged"], c["deployed"], c["attention"]) == (False, False, False)
    assert c["pos"] > core.get_card(waiting)["pos"], "behind the card already waiting"


# ---------- merged and deployed ----------

def shipped(lane="verify"):
    c = card(lane=lane)
    return core.update_card(c["id"], "agent", attention=True, merged=True, deployed=True)


def test_merged_and_deployed_are_off_by_default():
    c = card()
    assert (c["merged"], c["deployed"]) == (False, False)
    assert {"merged", "deployed"} <= set(core.list_cards()[0])


def test_setting_them_is_logged_and_keeps_attention_set_in_the_same_write():
    c = shipped()
    assert (c["merged"], c["deployed"], c["attention"]) == (True, True, True)
    got = [(e["kind"], e["detail"]["field"], e["detail"]["to"]) for e in c["events"][1:]]
    assert got == [("edited", "merged", True), ("edited", "deployed", True)]


@pytest.mark.parametrize("lane", ["plan", "develop", "test"])
def test_rework_clears_them(lane):
    c = core.update_card(shipped()["id"], "ce", lane=lane)
    assert (c["merged"], c["deployed"]) == (False, False)


@pytest.mark.parametrize("lane", ["done", "todo"])
def test_a_move_out_of_the_agent_lanes_keeps_them(lane):
    c = core.update_card(shipped()["id"], "ce", lane=lane)
    assert (c["merged"], c["deployed"]) == (True, True)


def test_a_move_between_agent_lanes_or_an_edit_keeps_them():
    c = core.update_card(shipped("develop")["id"], "ce", lane="test")
    assert (c["merged"], c["deployed"]) == (True, True)
    c = core.update_card(shipped()["id"], "ce", title="renamed")
    assert (c["merged"], c["deployed"]) == (True, True)


def test_rework_that_sets_them_in_the_same_write_wins():
    c = core.update_card(shipped()["id"], "ce", lane="develop", merged=True)
    assert (c["merged"], c["deployed"]) == (True, False)


@pytest.mark.parametrize("f", ["merged", "deployed"])
@pytest.mark.parametrize("bad", ["yes", 1, None])
def test_merged_and_deployed_reject_everything_but_a_bool(f, bad):
    with pytest.raises(ValueError, match=f"{f} must be true or false"):
        core.update_card(card()["id"], "bob", **{f: bad})


def test_a_database_without_merged_and_deployed_gains_them(db):
    import sqlite3
    core.list_cards()
    old = sqlite3.connect(core.DB_PATH)
    old.execute("ALTER TABLE cards DROP COLUMN merged")
    old.execute("ALTER TABLE cards DROP COLUMN deployed")
    old.execute("INSERT INTO cards (project, title, lane, created_by, created_at,"
                " updated_at) VALUES ('inbox','old','todo','ann','t','t')")
    old.commit()
    old.close()
    [c] = core.list_cards()
    assert (c["merged"], c["deployed"]) == (False, False)
    assert core.update_card(c["id"], "ann", deployed=True)["deployed"] is True


# ---------- the deployed dot follows the deploy comment ----------

def commented(text, actor=core.AGENT, lane="verify"):
    return core.comment(card(lane=lane)["id"], actor, text)


def test_the_agents_deploy_comment_lights_the_deployed_dot():
    c = commented("deployed: backend restarted")
    assert (c["lane"], c["deployed"], c["merged"]) == ("verify", True, False), "git sets merged"
    assert [(e["kind"], e["detail"].get("field")) for e in c["events"][1:]] == [
        ("comment", None), ("edited", "deployed")]


@pytest.mark.parametrize("text", ["deploy failed: adb not found",
                                  "deploy skipped: no phone connected"])
def test_a_failed_or_skipped_deploy_leaves_it_dark(text):
    c = commented(text)
    assert (c["lane"], c["deployed"]) == ("verify", False)
    assert [e["kind"] for e in c["events"][1:]] == ["comment"], "nothing changed, nothing logged"


def test_a_redeploy_that_fails_darkens_the_dot_again():
    id = card(lane="verify")["id"]
    core.comment(id, core.AGENT, "deployed: ok")
    assert core.comment(id, core.AGENT, "deploy failed: the restart died")["deployed"] is False


def test_deploying_twice_writes_one_event():
    id = card(lane="verify")["id"]
    core.comment(id, core.AGENT, "deployed: ok")
    c = core.comment(id, core.AGENT, "deployed: ok again")
    fields = [e["detail"].get("field") for e in c["events"]]
    assert (c["deployed"], fields.count("deployed")) == (True, 1)


def test_only_the_agents_comment_lights_it():
    c = commented("deployed: I did it myself", actor="ann")
    assert (c["deployed"], c["lane"]) == (False, "plan"), "a person's comment is a new request"


def test_an_ordinary_agent_comment_leaves_it_alone():
    assert commented("the tests pass")["deployed"] is False


def test_the_dot_survives_the_agents_own_follow_up_write():
    """`deploy()` comments, then writes what git knows: that must not undo the dot."""
    id = card(lane="verify")["id"]
    core.comment(id, core.AGENT, "deployed: ok")
    c = core.update_card(id, core.AGENT, attention=True, merged=True)
    assert (c["merged"], c["deployed"], c["attention"]) == (True, True, True)


# ---------- a new project's setup cards ----------

def test_a_bare_project_gets_a_card_for_each_missing_thing():
    core.create_project("Fresh")
    cards = core.setup_cards("Fresh")
    assert [c["title"] for c in cards] == ["Set Fresh's folder",
                                           "Tell the agent how to test Fresh",
                                           "Tell the agent how to deploy Fresh"]
    for c in cards:
        assert (c["lane"], c["assignee"], c["auto_advance"]) == ("todo", None, False)
        assert (c["created_by"], c["project"]) == ("board", "Fresh")
        assert "projects…" in c["description"]


def test_a_fully_configured_project_gets_no_cards(tmp_path):
    core.create_project("Fresh", path=str(tmp_path),
                        instructions="test with pytest; deploy by merging to master")
    assert core.setup_cards("Fresh") == []
    assert core.list_cards(project="Fresh") == []


def test_only_what_is_missing_gets_a_card(tmp_path):
    core.create_project("Fresh", path=str(tmp_path), instructions="run uv run pytest")
    [c] = core.setup_cards("Fresh")
    assert c["title"] == "Tell the agent how to deploy Fresh"


def test_the_cards_take_the_projects_own_spelling():
    core.create_project("Tickets")
    assert {c["project"] for c in core.setup_cards("tickets")} == {"Tickets"}


def test_setup_cards_for_an_unknown_project_is_not_found():
    with pytest.raises(core.NotFound):
        core.setup_cards("nope")
