"""board.html driven in a real browser.

These are the only slow, environment-dependent tests in the suite, so they are kept few
and each one earns its place: the drag path, the panel's normal edits, and one lock per
bug that actually shipped. Anything that is really "does the server do X" belongs in
test_app.py instead.

The board's top-level `function` declarations (`openCard`, `patch`, `load`, `api`, …) are
on `window`, so page.evaluate can drive the page's own code rather than re-implementing it.
"""

import pytest

import core


def drag(page, card_id, lane):
    """Native HTML5 drag & drop: Playwright's drag_to() and raw mouse events do not
    produce it. Dispatching the three events with one shared DataTransfer does, because
    that object is exactly what cardNode's ondragstart writes to and the lane's ondrop
    reads from. This covers the handlers, not the browser's own gesture-to-event
    synthesis — PLAN.md task 4 verified that once with a real mouse."""
    page.evaluate("""([id, lane]) => {
        const card = document.querySelector(`.card[data-id="${id}"]`);
        const col = document.querySelector(`.lane[data-lane="${lane}"]`);
        const dt = new DataTransfer();
        const fire = (el, type) => el.dispatchEvent(
            new DragEvent(type, {bubbles: true, cancelable: true, dataTransfer: dt}));
        fire(card, "dragstart"); fire(col, "dragover"); fire(col, "drop");
    }""", [card_id, lane])


def type_into(page, selector, value):
    """Playwright's fill() fires `input` but not `change`, and every panel field saves on
    `change` — so the blur is the part that actually commits the edit."""
    page.fill(selector, value)
    page.locator(selector).blur()


def wait_saved(page, cid, field, value):
    """Every panel field PATCHes on change and re-renders the panel, so one edit must
    land before the next one touches a node the re-render is about to replace. The write
    lands first and the re-render follows, so waiting on the database alone is not enough:
    `__inflight` (installed by the page fixture) is what says the re-render has happened."""
    for _ in range(100):
        if core.get_card(cid)[field] == value:
            page.wait_for_function("() => window.__inflight === 0")
            return
        page.wait_for_timeout(50)
    raise AssertionError(f"{field} never saved as {value!r}: {core.get_card(cid)[field]!r}")


OUTSIDE = (8, 500)   # the board's left padding: outside the sheet, and on no card


def click_outside(page):
    """How a sheet is closed -- and a new card created. There is no save button."""
    page.mouse.click(*OUTSIDE)


def created(page):
    """After "create": the sheet closes and the board reloads. Returns the new card's id."""
    page.wait_for_function("() => !open && window.__inflight === 0")
    return max(c["id"] for c in core.list_cards())


def add_card(page, title):
    """New Card -> title in the new-card panel -> create, then reopen the new card from
    the board, as a person would to keep working on it. Returns once the panel shows it."""
    page.click("#add")
    page.fill("#panel input[type=text] >> nth=0", title)
    click_outside(page)
    cid = created(page)
    page.click(f'.card[data-id="{cid}"]')
    page.wait_for_function(f"() => open && open.id === {cid}")
    return cid


# ---------- normal operations ----------

def test_add_a_card_from_the_header(page):
    cid = add_card(page, "typed in the box")
    c = core.get_card(cid)
    assert c["title"] == "typed in the box" and c["lane"] == "todo", c
    assert c["events"][0]["actor"] == "ce", "the who-select supplies the actor"
    page.wait_for_selector("#panel.on")  # a new card opens for editing


def test_drag_moves_the_card_and_the_move_reaches_the_database(page):
    cid = add_card(page, "drag me")
    drag(page, cid, "develop")
    page.wait_for_selector(f'.lane[data-lane="develop"] .card[data-id="{cid}"]')
    page.wait_for_function("() => !document.querySelector('#err').classList.contains('on')")
    c = core.get_card(cid)
    assert c["lane"] == "develop", "the PATCH landed, not just the optimistic re-render"
    assert [e["kind"] for e in c["events"]].count("moved") == 1, c["events"]


def test_a_rejected_move_puts_the_card_back(page):
    cid = add_card(page, "will not move")
    click_outside(page)
    # fail the PATCH without a network error, so this tests the rollback and not the fetch
    page.evaluate("""window.fetch = () => Promise.resolve(
        new Response(JSON.stringify({error: "nope"}), {status: 400}))""")
    drag(page, cid, "verify")
    page.wait_for_selector("#err.on")
    assert page.locator(f'.lane[data-lane="todo"] .card[data-id="{cid}"]').count() == 1
    assert core.get_card(cid)["lane"] == "todo"
    assert "could not move" in page.text_content("#err")


def test_panel_edits_each_save_on_change(page):
    cid = add_card(page, "edit me")
    page.wait_for_selector("#panel.on")
    type_into(page, "#panel input[type=text] >> nth=0", "renamed in the panel")  # title
    wait_saved(page, cid, "title", "renamed in the panel")
    page.select_option("#panel select >> nth=1", "high")          # priority
    wait_saved(page, cid, "priority", "high")
    type_into(page, "#panel input[placeholder='comma, separated']", "ui, api")
    wait_saved(page, cid, "labels", ["ui", "api"])
    page.select_option("#panel select >> nth=3", "test")          # lane
    wait_saved(page, cid, "lane", "test")
    page.fill("#panel input[placeholder='+ checklist item (enter)']", "first item")
    page.press("#panel input[placeholder='+ checklist item (enter)']", "Enter")
    wait_saved(page, cid, "checklist", [{"text": "first item", "done": False}])
    page.check("#panel .chk input[type=checkbox]")
    wait_saved(page, cid, "checklist", [{"text": "first item", "done": True}])
    page.fill("#panel textarea >> nth=1", "looks good")
    page.click("#panel button:has-text('comment')")
    page.wait_for_selector("#panel .log li.comment")

    c = core.get_card(cid)
    assert c["title"] == "renamed in the panel" and c["priority"] == "high", c
    assert c["labels"] == ["ui", "api"] and c["lane"] == "test", c
    assert c["checklist"] == [{"text": "first item", "done": True}], c["checklist"]
    assert c["events"][-1]["detail"]["text"] == "looks good", c["events"][-1]
    assert {e["actor"] for e in c["events"]} == {"ce"}, "every edit is attributed"


def test_the_project_filter_scopes_the_board(page):
    core.create_project("Other")
    core.create_card("elsewhere", actor="ce", project="Other")
    page.evaluate("load()")
    page.wait_for_selector(".card")
    add_card(page, "in inbox")
    click_outside(page)
    page.select_option("#proj", "Other")
    page.wait_for_function("() => document.querySelectorAll('.card').length === 1")
    assert page.text_content(".card .t") == "elsewhere"
    assert page.evaluate("localStorage.getItem('project')") == "Other", "and it persists"


def test_adding_a_name_registers_it_and_selects_it(page):
    page.on("dialog", lambda d: d.accept("  zoe  "))   # prompt(), else Playwright dismisses
    who = page.locator("#who")
    page.select_option("#who", index=who.locator("option").count() - 1)  # "+ another name…"
    page.wait_for_function("() => document.querySelector('#who').value === 'zoe'")
    assert core.list_users() == ["zoe"], "registered server-side, not just locally"
    assert page.evaluate("localStorage.getItem('actor')") == "zoe"


def test_clicking_outside_commits_the_field_you_are_still_typing_in(page):
    """Fields save on `change`, so the one you are still in has not saved yet when you
    click away. Closing blurs it, and that saves it."""
    cid = add_card(page, "unsaved")
    page.wait_for_selector("#panel.on")
    page.fill("#panel input[type=text] >> nth=0", "typed, never blurred")   # no blur here
    click_outside(page)
    wait_saved(page, cid, "title", "typed, never blurred")
    assert page.locator("#panel.on").count() == 0, "and it dismissed"


# ---------- regression locks ----------

def test_the_sheet_buttons_are_not_buried_under_the_header(page):
    """#panel needs a z-index above the sticky header's 5. Without it the panel's own
    top button sat underneath the header and every click on it hit "Add to todo",
    so the panel could not be dismissed and each attempt created another card."""
    add_card(page, "open me")
    page.wait_for_selector("#panel.on .close")
    topmost = page.evaluate("""() => {
        const b = document.querySelector('#panel .close');
        const r = b.getBoundingClientRect();
        const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
        return {isTheButton: hit === b, gotInstead: hit && hit.id};
    }""")
    assert topmost["isTheButton"], f"the archive button is covered by #{topmost['gotInstead']}"
    click_outside(page)
    page.wait_for_selector("#panel.on", state="detached")
    assert len(core.list_cards()) == 1, "dismissing must not have created a second card"


def test_a_landed_patch_does_not_reopen_a_dismissed_panel(page):
    """patch() used to re-render unconditionally. Since dismissing blurs a dirty field,
    the resulting change -> patch -> renderPanel re-added .on right after the click
    removed it, and the sheet came back."""
    cid = add_card(page, "dismiss me")
    page.wait_for_selector("#panel.on")
    page.evaluate("""const original = window.fetch;
        window.fetch = (...a) => new Promise(r => setTimeout(() => r(original(...a)), 400))""")
    # `void` matters: page.evaluate awaits a returned promise, which would let the PATCH
    # finish before the dismiss and quietly test nothing
    page.evaluate("void patch({title: 'renamed while closing'})")
    click_outside(page)
    assert page.locator("#panel.on").count() == 0, "dismissed"
    page.wait_for_timeout(900)            # the PATCH lands in here
    assert page.locator("#panel.on").count() == 0, "a landed PATCH reopened a dismissed panel"
    assert core.get_card(cid)["title"] == "renamed while closing", \
        "the guard must suppress the re-render, not the write"


def test_the_actor_box_never_shows_a_name_it_has_not_stored(page):
    """renderWho() only offered the placeholder when the user list was empty, so with any
    registered name and no stored actor the box displayed that name while who() was still
    "" — and every write failed against a box that said otherwise."""
    core.ensure_user("ann")
    core.ensure_user("bob")
    page.evaluate("localStorage.removeItem('actor'); load()")
    page.wait_for_function("() => document.querySelector('#who').options.length > 2")
    assert page.eval_on_selector("#who", "s => s.value") == "", "no actor is stored"
    selected = page.eval_on_selector("#who", "s => s.selectedOptions[0].textContent")
    assert "who are you" in selected, f"the box claims to be {selected!r}"

    page.click("#add")
    page.wait_for_selector("#err.on")
    assert "say who you are first" in page.text_content("#err")
    assert page.locator("#panel.on").count() == 0, "no new-card panel without an actor"
    assert core.list_cards() == [], "and nothing was written"


def test_archive_button_removes_the_card_and_show_archived_brings_it_back(page):
    cid = add_card(page, "archive me")
    page.click("#panel button:text('archive')")
    page.wait_for_selector(f'.card[data-id="{cid}"]', state="detached")
    assert core.get_card(cid)["archived"] is True, "archived, not deleted"
    page.check("#showArch")
    page.click(f'.card[data-id="{cid}"]')
    page.click("#panel button:text('unarchive')")
    page.wait_for_selector(f'.card[data-id="{cid}"]', state="detached")
    page.uncheck("#showArch")
    page.wait_for_selector(f'.card[data-id="{cid}"]')
    assert core.get_card(cid)["archived"] is False


def test_a_failed_archive_keeps_the_card_and_says_why(page):
    cid = add_card(page, "stays put")
    page.evaluate("""window.fetch = () => Promise.resolve(
        new Response(JSON.stringify({error: "nope"}), {status: 400}))""")
    page.click("#panel button:text('archive')")
    page.wait_for_selector("#err.on")
    assert "nope" in page.text_content("#err")
    assert page.locator("#panel.on").count() == 1, "the panel stays open on failure"
    assert page.locator(f'.card[data-id="{cid}"]').count() == 1
    assert core.get_card(cid)["archived"] is False


def test_show_archived_respects_the_project_filter(page):
    for title, project in (("mine", "P"), ("theirs", "Q")):
        core.create_project(project)
        c = core.create_card(title, actor="ce", project=project)
        core.update_card(c["id"], "ce", archived=True)
    page.evaluate("localStorage.setItem('project', 'P'); load()")
    page.check("#showArch")
    page.wait_for_function("() => document.querySelectorAll('.card').length === 1")
    assert page.text_content(".card .t") == "mine"


def test_every_event_kind_reads_as_a_sentence(page):
    """eventLine() falls back to the raw kind for anything it does not know, which is
    how a new event kind (like `archived`) ships looking broken. Emit every kind core can
    write and check none of them hits the fallback."""
    a = core.create_card("a", actor="ce", project="Home", checklist=[{"text": "x", "done": False}])
    b = core.create_card("b", actor="ce", project="Home")
    core.update_card(a["id"], "ce", lane="plan", assignee="bob", title="a2",
                     checklist=[{"text": "x", "done": True}])
    core.update_card(a["id"], "ce", checklist=[{"text": "x", "done": True},
                                                {"text": "y", "done": False}])
    core.link_cards(a["id"], b["id"], "blocks", "ce")
    core.unlink_cards(a["id"], b["id"], "blocks", "ce")
    core.update_card(a["id"], "ce", archived=True)
    core.update_card(a["id"], "ce", archived=False)
    events = [e for e in core.get_card(a["id"])["events"] if e["kind"] != "comment"]
    assert {e["kind"] for e in events} == {"created", "moved", "assigned", "edited",
                                           "checked", "linked", "unlinked", "archived"}
    lines = page.evaluate("evs => evs.map(eventLine)", events)
    leaked = [(e["kind"], l) for e, l in zip(events, lines) if l == e["kind"]]
    assert not leaked, f"rendered as a bare kind: {leaked}"
    assert "archived it" in lines and "unarchived it" in lines, lines

    page.evaluate("load()")
    page.wait_for_selector(f'.card[data-id="{a["id"]}"]')
    page.click(f'.card[data-id="{a["id"]}"]')
    page.wait_for_selector("#panel.on .log")
    assert "unarchived it" in page.text_content("#panel .log"), "and the panel shows it"


def test_assignee_and_links_from_the_panel(page):
    core.ensure_user("bob")
    other = core.create_card("other", actor="ce", project="Home")
    page.evaluate("load()")
    cid = add_card(page, "linked one")
    page.select_option("#panel select >> nth=2", "bob")           # assignee
    wait_saved(page, cid, "assignee", "bob")
    page.fill("#panel input[type=number]", str(other["id"]))
    page.select_option("#panel .links select", "blocks")
    page.click("#panel button:text('link')")
    page.wait_for_function("() => window.__inflight === 0")
    page.wait_for_selector("#panel button:text('unlink')")
    assert core.get_card(cid)["links"] == [
        {"from_id": cid, "to_id": other["id"], "kind": "blocks"}]
    page.click("#panel button:text('unlink')")
    page.wait_for_function("() => window.__inflight === 0")
    page.wait_for_selector("#panel button:text('unlink')", state="detached")
    assert core.get_card(cid)["links"] == []
    page.select_option("#panel select >> nth=2", "")              # back to nobody
    wait_saved(page, cid, "assignee", None)


def test_linking_to_a_missing_card_shows_the_error(page):
    add_card(page, "lonely")
    page.fill("#panel input[type=number]", "999")
    page.click("#panel button:text('link')")
    page.wait_for_selector("#err.on")
    assert "no card 999" in page.text_content("#err")


def test_a_blank_title_alone_creates_nothing_and_just_closes(page):
    page.click("#add")
    page.fill("#panel input[type=text] >> nth=0", "   ")
    click_outside(page)
    assert page.locator("#panel.on").count() == 0
    page.wait_for_timeout(300)
    assert core.list_cards() == []
    assert page.locator("#err.on").count() == 0, "nothing was lost, so nothing to say"


def test_a_slow_patch_response_does_not_roll_back_a_newer_panel(page):
    """patch() re-rendered from its own response unconditionally. A PATCH whose response
    arrived after a later link had re-rendered the panel put the older card back on
    screen, and the link vanished from view though it was saved."""
    other = core.create_card("other", actor="ce", project="Home")
    cid = add_card(page, "race me")
    page.wait_for_function("() => window.__inflight === 0")
    # the server writes at once; only the PATCH *response* is held back
    page.evaluate("""const original = window.fetch;
        window.fetch = (url, opts) => original(url, opts).then(r => opts && opts.method === "PATCH"
            ? new Promise(ok => setTimeout(() => ok(r), 600)) : r)""")
    page.evaluate("void patch({priority: 'high'})")
    while core.get_card(cid)["priority"] != "high":   # written, response still held
        page.wait_for_timeout(20)
    page.evaluate(f"""api("POST", "/api/links", {{from_id: {cid}, to_id: {other['id']},
                                                   kind: "blocks"}}).then(() => openCard({cid}))""")
    page.wait_for_selector("#panel button:text('unlink')")
    page.wait_for_function("() => window.__inflight === 0")   # the slow PATCH has landed
    assert page.locator("#panel button:text('unlink')").count() == 1, \
        "the late PATCH response rolled the panel back"
    assert page.eval_on_selector("#panel select >> nth=1", "s => s.value") == "high"


def test_a_stale_card_does_not_roll_back_the_board(page):
    add_card(page, "fresh title")
    click_outside(page)
    page.evaluate("""() => { const c = cards[0];
        replace({...c, title: "stale title", updated_at: "2000-01-01T00:00:00+00:00"}); }""")
    assert page.text_content(".card .t") == "fresh title"


def test_a_landed_comment_does_not_reopen_a_dismissed_panel(page):
    """The comment path set `open` and re-rendered unconditionally, so closing the panel
    while a comment was in flight brought the panel back when the response landed."""
    cid = add_card(page, "comment then close")
    page.wait_for_function("() => window.__inflight === 0")
    page.evaluate("""const original = window.fetch;
        window.fetch = (...a) => original(...a).then(r => new Promise(ok => setTimeout(() => ok(r), 500)))""")
    page.fill("#panel textarea >> nth=1", "sent while closing")
    page.click("#panel button:has-text('comment')")
    click_outside(page)
    assert page.locator("#panel.on").count() == 0, "dismissed"
    page.wait_for_function("() => window.__inflight === 0")    # the comment response has landed
    assert page.locator("#panel.on").count() == 0, "a landed comment reopened a dismissed panel"
    assert core.get_card(cid)["events"][-1]["detail"] == {"text": "sent while closing"}, \
        "the guard must suppress the re-render, not the write"


def test_a_landed_comment_does_not_hijack_another_open_card(page):
    other = core.create_card("other", actor="ce", project="Home")
    cid = add_card(page, "commented on")
    page.wait_for_function("() => window.__inflight === 0")
    page.evaluate("""const original = window.fetch;
        window.fetch = (url, o) => original(url, o).then(r => String(url).includes('/comment')
            ? new Promise(ok => setTimeout(() => ok(r), 500)) : r)""")
    page.fill("#panel textarea >> nth=1", "for the first card")
    page.click("#panel button:has-text('comment')")
    page.evaluate(f"openCard({other['id']})")
    page.wait_for_function(f"() => open && open.id === {other['id']}")
    page.wait_for_function("() => window.__inflight === 0")
    assert page.evaluate("open.id") == other["id"], "the comment response switched the panel back"
    assert core.get_card(cid)["events"][-1]["detail"] == {"text": "for the first card"}


def test_adding_a_card_leaves_the_archived_view(page):
    """A new card is active, so adding one while "show archived" is ticked used to
    create it out of sight. Adding switches back to the active board."""
    page.check("#showArch")
    cid = add_card(page, "new while viewing archive")
    assert not page.is_checked("#showArch"), "back on the active board"
    page.wait_for_selector(f'.card[data-id="{cid}"]')
    page.wait_for_selector("#panel.on")
    assert core.get_card(cid)["archived"] is False


# ---------- the new-card panel ----------

def test_the_header_has_no_title_box(page):
    assert page.locator("header input:not([type=checkbox])").count() == 0,         "the title is typed in the panel, not the header"


def test_add_opens_an_empty_panel_and_writes_nothing(page):
    assert page.text_content("#add") == "New Card"
    page.click("#add")
    page.wait_for_selector("#panel.on")
    assert page.input_value("#panel input[type=text] >> nth=0") == ""
    assert page.evaluate("document.activeElement.placeholder") == "what needs doing?", \
        "the title field has focus, ready to type"
    assert "new card" in page.text_content("#panel .sub")
    for gone in ("save", "create", "archive"):
        assert page.locator(f"#panel button:text-is('{gone}')").count() == 0, gone
    heads = page.locator("#panel h3").all_text_contents()
    for shown in ("links", "activity", "say something"):   # all a saved card shows
        assert shown in heads, (shown, heads)
    assert core.list_cards() == [], "nothing is written until create"


def test_cancel_discards_the_draft(page):
    page.click("#add")
    page.fill("#panel input[type=text] >> nth=0", "never mind")
    page.click("#panel button:text-is('cancel')")
    assert page.locator("#panel.on").count() == 0
    page.wait_for_timeout(300)
    assert core.list_cards() == []


def test_every_field_in_the_new_card_panel_is_created(page):
    core.ensure_user("bob")
    core.create_project("Tickets")
    page.evaluate("localStorage.setItem('project', 'Tickets'); load()")
    page.wait_for_function("() => users.includes('bob')")
    page.click("#add")
    page.fill("#panel input[type=text] >> nth=0", "  full card  ")
    page.fill("#panel textarea", "the long version")
    page.select_option("#panel select >> nth=1", "high")
    page.select_option("#panel select >> nth=2", "bob")
    page.select_option("#panel select >> nth=3", "plan")
    page.fill("#panel input[placeholder='comma, separated']", "ui, api")
    item = "#panel input[placeholder='+ checklist item (enter)']"
    page.fill(item, "first")
    page.press(item, "Enter")
    page.fill(item, "second")
    page.press(item, "Enter")
    page.check("#panel .chk input[type=checkbox] >> nth=0")
    assert core.list_cards() == [], "still a draft"
    click_outside(page)
    c = core.get_card(created(page))
    assert (c["title"], c["description"], c["priority"], c["assignee"], c["lane"]) == (
        "full card", "the long version", "high", "bob", "plan"), c
    assert c["labels"] == ["ui", "api"] and c["project"] == "Tickets", c
    assert c["checklist"] == [{"text": "first", "done": True},
                              {"text": "second", "done": False}], c["checklist"]
    assert [(e["actor"], e["kind"]) for e in c["events"]] == [("ce", "created")], \
        "one created event, not a create followed by edits"
    page.wait_for_selector(f'.lane[data-lane="plan"] .card[data-id="{c["id"]}"]')
    assert page.locator("#panel.on").count() == 0, "create saves and closes the sheet"
    page.click(f'.card[data-id="{c["id"]}"]')                # reopened: nothing to press
    page.wait_for_selector("#panel button:text-is('archive')")
    for gone in ("save", "create"):
        assert page.locator(f"#panel button:text-is('{gone}')").count() == 0, gone


def test_typing_a_title_then_clicking_create_keeps_the_title(page):
    """The title saves on `change`, which fires as focus leaves it -- on the same click
    that hits create. The draft must not re-render there and lose the click."""
    page.click("#add")
    page.type("#panel input[type=text] >> nth=0", "typed then create")   # no blur
    click_outside(page)
    created(page)
    assert core.list_cards()[0]["title"] == "typed then create"


def test_enter_in_the_title_creates(page):
    page.click("#add")
    page.type("#panel input[type=text] >> nth=0", "by enter")
    page.press("#panel input[type=text] >> nth=0", "Enter")
    created(page)
    assert [c["title"] for c in core.list_cards()] == ["by enter"]


def test_a_double_click_outside_makes_one_card(page):
    page.click("#add")
    page.fill("#panel input[type=text] >> nth=0", "only once")
    page.mouse.dblclick(*OUTSIDE)
    created(page)
    page.wait_for_function("() => window.__inflight === 0")
    assert len(core.list_cards()) == 1


def test_a_failed_create_keeps_the_draft(page):
    page.click("#add")
    page.fill("#panel input[type=text] >> nth=0", "keep me")
    page.evaluate("""window.fetch = () => Promise.resolve(
        new Response(JSON.stringify({error: "nope"}), {status: 400}))""")
    click_outside(page)
    page.wait_for_selector("#err.on")
    assert page.input_value("#panel input[type=text] >> nth=0") == "keep me"
    assert page.locator("#panel.on").count() == 1, "the sheet stays open"
    assert page.evaluate("open.creating") is False, "and another click out retries"


def test_cancelling_while_create_is_in_flight_does_not_reopen(page):
    page.click("#add")
    page.fill("#panel input[type=text] >> nth=0", "created anyway")
    page.evaluate("""const original = window.fetch;
        window.fetch = (...a) => original(...a).then(r => new Promise(ok => setTimeout(() => ok(r), 500)))""")
    click_outside(page)
    page.click("#panel button:text-is('cancel')")
    page.wait_for_function("() => window.__inflight === 0")
    assert page.locator("#panel.on").count() == 0, "the cancelled panel came back"
    assert [c["title"] for c in core.list_cards()] == ["created anyway"], \
        "cancel after create was sent does not undo the write"



# ---------- projects ----------

def human_click(page, selector):
    """A click at human speed. page.click() presses and releases at once, so it cannot
    see a re-render that lands between mousedown and mouseup and swallows the click."""
    page.locator(selector).scroll_into_view_if_needed()   # coordinates only hit what is on screen
    box = page.locator(selector).bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.wait_for_timeout(150)
    page.mouse.up()


def project_block(page, name):
    return page.locator("#panel .project").filter(
        has=page.locator(f"h3:text-is('{name}')"))


def test_the_projects_panel_lists_and_edits_projects(page, tmp_path):
    core.create_project("Tickets")
    page.evaluate("load()")
    page.click("#projects")
    page.wait_for_selector("#panel.on .project")
    assert page.locator("#panel .project h3").all_text_contents() == [
        "Home", "Tickets", "new project"]
    b = project_block(page, "Tickets")
    b.locator("input >> nth=1").fill(str(tmp_path))
    b.locator("textarea").fill("run uv run pytest -q")
    b.locator("input >> nth=1").focus()      # leaving the textarea fires its change
    page.wait_for_function("() => window.__inflight === 0")
    page.click("#panel button:text-is('done')")   # flushes the path, still focused
    page.wait_for_function("() => window.__inflight === 0")
    assert core.get_project("Tickets") == {"name": "Tickets", "path": str(tmp_path),
                                           "instructions": "run uv run pytest -q", "color": "#00875a"}
    assert page.locator("#panel.on").count() == 0


def test_a_bad_path_is_refused_and_the_field_shows_what_is_stored(page, tmp_path):
    core.create_project("Tickets", path=str(tmp_path))
    page.evaluate("load()")
    page.click("#projects")
    path = project_block(page, "Tickets").locator("input >> nth=1")
    path.fill("not/absolute")
    path.blur()
    page.wait_for_selector("#err.on")
    assert "absolute" in page.text_content("#err")
    page.wait_for_function("() => window.__inflight === 0")
    assert project_block(page, "Tickets").locator("input >> nth=1").input_value() == str(tmp_path)
    assert core.get_project("Tickets")["path"] == str(tmp_path)


def test_add_a_project_from_the_panel(page, tmp_path):
    page.click("#projects")
    new = project_block(page, "new project")
    new.locator("input >> nth=0").fill("  Fresh  ")
    new.locator("input >> nth=1").fill(str(tmp_path))
    new.locator("textarea").fill("be careful")
    human_click(page, "#panel button:text-is('add project')")
    page.wait_for_selector("#panel .project h3:text-is('Fresh')")
    assert core.get_project("Fresh") == {"name": "Fresh", "path": str(tmp_path),
                                         "instructions": "be careful", "color": "#00875a"}
    assert "Fresh" in page.locator("#proj option").all_text_contents(), "the filter has it"
    assert project_block(page, "new project").locator("input >> nth=0").input_value() == ""


def test_a_duplicate_project_keeps_what_was_typed(page):
    page.click("#projects")
    new = project_block(page, "new project")
    new.locator("input >> nth=0").fill("HOME")
    new.locator("textarea").fill("typed")
    page.click("#panel button:text-is('add project')")
    page.wait_for_selector("#err.on")
    assert "already exists" in page.text_content("#err")
    assert new.locator("textarea").input_value() == "typed"
    assert [p["name"] for p in core.list_projects()] == ["Home"]


def test_an_edit_then_a_human_click_on_add_project_is_not_lost(page, tmp_path):
    """The field's change fires as focus leaves it, on the same click that hits "add
    project". Re-rendering on that save's response would swap the button out between
    mousedown and mouseup, and the add would silently not happen."""
    core.create_project("Tickets")
    page.evaluate("load()")
    page.click("#projects")
    project_block(page, "Tickets").locator("textarea").fill("edited")   # still focused
    project_block(page, "new project").locator("input >> nth=0").fill("Second")
    project_block(page, "Tickets").locator("textarea").focus()
    project_block(page, "Tickets").locator("textarea").fill("edited, then add")
    human_click(page, "#panel button:text-is('add project')")
    page.wait_for_selector("#panel .project h3:text-is('Second')")
    assert core.get_project("Tickets")["instructions"] == "edited, then add"


def test_renaming_a_project_moves_its_cards_and_the_filter_follows(page):
    core.create_project("Old")
    cid = core.create_card("mine", actor="ce", project="Old")["id"]
    page.evaluate("localStorage.setItem('project', 'Old'); load()")
    page.wait_for_selector(f'.card[data-id="{cid}"]')
    page.click("#projects")
    name = project_block(page, "Old").locator("input >> nth=0")
    name.fill("New")
    name.blur()
    page.wait_for_function("() => localStorage.getItem('project') === 'New'")
    page.wait_for_function("() => window.__inflight === 0")
    assert core.get_card(cid)["project"] == "New"
    assert page.locator(f'.card[data-id="{cid}"]').count() == 1, "still on the filtered board"
    assert page.eval_on_selector("#proj", "s => s.value") == "New"
    # a later edit in the same block addresses the new name
    instr = project_block(page, "Old").locator("textarea")   # the heading keeps its old text
    instr.fill("after the rename")
    instr.blur()
    page.wait_for_function("() => window.__inflight === 0")
    assert core.get_project("New")["instructions"] == "after the rename"


def test_the_card_panel_moves_a_card_between_projects(page):
    core.create_project("Other")
    cid = add_card(page, "moving")
    page.select_option("#panel select >> nth=0", "Other")   # project, under the title
    wait_saved(page, cid, "project", "Other")
    assert "Other" in page.text_content("#panel .sub")


def test_a_new_card_lands_in_the_filtered_project_or_the_chosen_one(page):
    core.create_project("Tickets")
    core.create_project("Other")
    page.evaluate("localStorage.setItem('project', 'tickets'); load()")   # any case
    page.wait_for_function("() => projects.length === 3")
    page.click("#add")
    assert page.eval_on_selector("#panel select >> nth=0", "s => s.value") == "Tickets"
    page.fill("#panel input[type=text] >> nth=0", "picked elsewhere")
    page.select_option("#panel select >> nth=0", "Other")
    click_outside(page)
    created(page)
    assert core.list_cards()[0]["project"] == "Other"


def test_a_stale_filter_falls_back_to_the_first_project_for_new_cards(page):
    core.create_project("Alpha")
    page.evaluate("localStorage.setItem('project', 'Gone'); load()")
    page.wait_for_function("() => document.querySelector('#proj').value === 'Gone'")
    page.click("#add")
    assert page.eval_on_selector("#panel select >> nth=0", "s => s.value") == "Alpha"


def test_a_human_click_outside_after_typing_closes_the_panel(page):
    """Leaving the field fires its PATCH on mousedown; a re-render landing before mouseup
    used to eat the click that should close the sheet. At human speed, it still closes."""
    cid = add_card(page, "human save")
    page.fill("#panel input[type=text] >> nth=0", "typed, then saved by a person")
    page.mouse.move(*OUTSIDE)
    page.mouse.down()
    page.wait_for_timeout(150)
    page.mouse.up()
    page.wait_for_function("() => window.__inflight === 0")
    assert page.locator("#panel.on").count() == 0, "the click outside was lost"
    assert core.get_card(cid)["title"] == "typed, then saved by a person"


def test_a_render_deferred_by_a_held_pointer_still_happens(page):
    cid = add_card(page, "deferred")
    page.fill("#panel input[type=text] >> nth=0", "renamed under a held pointer")
    box = page.locator("#panel .sub").bounding_box()   # plain text: no click action
    page.mouse.move(box["x"] + 5, box["y"] + 5)
    page.mouse.down()                                  # blurs the title: PATCH goes out
    wait_saved_db = lambda: core.get_card(cid)["title"] == "renamed under a held pointer"
    for _ in range(100):
        if wait_saved_db():
            break
        page.wait_for_timeout(20)
    page.wait_for_timeout(200)
    page.mouse.up()
    page.wait_for_function(
        "() => [...document.querySelectorAll('#panel .log li')].some(li => li.textContent.includes('renamed under'))")


def test_a_press_on_a_select_does_not_hold_back_renders(page):
    """A select's native popup can swallow the pointerup, so a pointerdown on one must not
    count as a held pointer, or the panel would stop re-rendering until the next click."""
    cid = add_card(page, "select press")
    page.eval_on_selector("#panel select >> nth=1", """s => s.dispatchEvent(
        new PointerEvent("pointerdown", {bubbles: true}))""")     # and no pointerup, ever
    page.select_option("#panel select >> nth=1", "high")
    wait_saved(page, cid, "priority", "high")
    page.wait_for_function(
        "() => document.querySelector('#panel .log').textContent.includes('from med to high')")


# ---------- auto advance ----------

AUTO = "#panel .auto-sw input"


def test_auto_advance_switch_saves_shows_a_pill_and_logs(page):
    cid = add_card(page, "hands off")
    assert page.locator(AUTO).is_checked() is False
    page.check(AUTO)
    wait_saved(page, cid, "auto_advance", True)
    assert page.locator(f'.card[data-id="{cid}"] .pill.auto').count() == 1
    assert "turned auto advance on" in page.text_content("#panel .log")
    assert page.locator(AUTO).is_checked(), "the re-render keeps it ticked"
    page.uncheck(AUTO)
    wait_saved(page, cid, "auto_advance", False)
    assert page.locator(f'.card[data-id="{cid}"] .pill.auto').count() == 0
    assert "turned auto advance off" in page.text_content("#panel .log")


def test_a_new_card_can_start_with_auto_advance(page):
    page.click("#add")
    assert page.locator(AUTO).is_checked() is False, "off by default"
    page.fill("#panel input[type=text] >> nth=0", "auto from the start")
    page.check(AUTO)
    click_outside(page)
    created(page)
    [c] = core.list_cards()
    assert (c["title"], c["auto_advance"]) == ("auto from the start", True)


def test_a_new_card_without_the_switch_is_created_off(page):
    cid = add_card(page, "plain")
    assert core.get_card(cid)["auto_advance"] is False


def test_the_switch_shows_what_the_agent_left(page):
    """The agent turns it off when a stage fails; the panel must show that, not a stale tick."""
    cid = core.create_card("x", actor="ce", project="Home", auto_advance=True)["id"]
    core.update_card(cid, "claude-agent", auto_advance=False)
    page.evaluate("load()")
    page.click(f'.card[data-id="{cid}"]')
    page.wait_for_selector(AUTO)
    assert page.locator(AUTO).is_checked() is False
    assert page.locator(f'.card[data-id="{cid}"] .pill.auto').count() == 0
    assert "claude-agent turned auto advance off" in page.text_content("#panel .log")


def test_ticking_auto_right_after_typing_a_title_keeps_both(page):
    cid = add_card(page, "first")
    page.fill("#panel input[type=text] >> nth=0", "renamed")   # still focused
    human_click(page, AUTO)          # blurs the title (a save) and ticks, in one press
    wait_saved(page, cid, "auto_advance", True)
    assert core.get_card(cid)["title"] == "renamed"


# ---------- no default project ----------

@pytest.mark.no_home
def test_new_card_with_no_projects_asks_for_one_first(page, tmp_path):
    page.click("#add")
    page.wait_for_selector("#err.on")
    assert "create a project first" in page.text_content("#err")
    assert page.locator("#panel .project h3").all_text_contents() == ["new project"]
    assert page.evaluate("document.activeElement.placeholder") == "name", "ready to type"
    page.keyboard.type("First")
    page.click("#panel button:text-is('add project')")
    page.wait_for_selector("#panel .project h3:text-is('First')")
    page.click("#panel button:text-is('done')")
    cid = add_card(page, "now it works")
    assert core.get_card(cid)["project"] == "First"
    assert core.list_projects()[0]["name"] == "First"


def test_the_project_is_chosen_right_under_the_title(page):
    core.create_project("Other")
    page.evaluate("load()")
    page.click("#add")
    heads = page.locator("#panel h3").all_text_contents()
    assert heads[:2] == ["title", "project"], heads
    assert page.locator("#panel select >> nth=0 >> option").all_text_contents() == [
        "Home", "Other"]


def test_a_card_created_in_another_project_switches_the_filter_to_it(page):
    core.create_project("Other")
    page.evaluate("localStorage.setItem('project', 'Home'); load()")
    page.wait_for_function("() => projects.length === 2")
    page.click("#add")
    page.fill("#panel input[type=text] >> nth=0", "lands elsewhere")
    page.select_option("#panel select >> nth=0", "Other")
    click_outside(page)
    cid = created(page)
    assert page.evaluate("localStorage.getItem('project')") == "Other"
    assert page.eval_on_selector("#proj", "s => s.value") == "Other"
    assert page.locator(f'.card[data-id="{cid}"]').count() == 1, "the new card is on screen"


def test_all_projects_stays_all_projects_after_a_create(page):
    core.create_project("Other")
    page.evaluate("load()")
    page.click("#add")
    page.fill("#panel input[type=text] >> nth=0", "anywhere")
    page.select_option("#panel select >> nth=0", "Other")
    click_outside(page)
    created(page)
    assert page.eval_on_selector("#proj", "s => s.value") == ""


# ---------- the new-card sheet shows everything ----------

def test_links_and_comments_on_a_new_card_are_sent_on_create(page):
    other = core.create_card("other", actor="ce", project="Home")["id"]
    page.evaluate("load()")
    page.click("#add")
    page.fill("#panel input[type=text] >> nth=0", "with extras")
    page.fill("#panel input[type=number]", str(other))
    page.select_option("#panel .links select", "blocks")
    page.click("#panel button:text-is('link')")
    page.wait_for_selector(f"#panel .links li:has-text('blocks → #{other}')")
    page.fill("#panel .say-box", "queued first")
    page.click("#panel button:text-is('comment')")
    page.wait_for_selector("#panel .log .say:text-is('queued first')")
    assert page.input_value("#panel .say-box") == ""
    page.fill("#panel .say-box", "typed, never sent")        # left in the box
    assert [c["id"] for c in core.list_cards()] == [other], "still a draft"
    click_outside(page)
    cid = created(page)
    c = core.get_card(cid)
    assert [(l["from_id"], l["to_id"], l["kind"]) for l in c["links"]] == [(cid, other, "blocks")]
    assert [e["detail"]["text"] for e in c["events"] if e["kind"] == "comment"] == [
        "queued first", "typed, never sent"]
    assert all(e["actor"] == "ce" for e in c["events"])


def test_a_new_card_refuses_a_link_to_a_missing_card_right_away(page):
    page.click("#add")
    page.fill("#panel input[type=number]", "999")
    page.click("#panel button:text-is('link')")
    page.wait_for_selector("#err.on")
    assert "no card 999" in page.text_content("#err")
    assert page.locator("#panel .links li:has-text('#999')").count() == 0
    assert page.evaluate("open.links") == []


def test_unlink_on_a_new_card_drops_the_queued_link(page):
    other = core.create_card("other", actor="ce", project="Home")["id"]
    page.click("#add")
    page.fill("#panel input[type=text] >> nth=0", "no links after all")
    page.fill("#panel input[type=number]", str(other))
    page.click("#panel button:text-is('link')")
    page.click("#panel button:text-is('unlink')")
    page.wait_for_function("() => open.links.length === 0")
    click_outside(page)
    assert core.get_card(created(page))["links"] == []


def test_the_same_link_twice_on_a_new_card_is_queued_once(page):
    other = core.create_card("other", actor="ce", project="Home")["id"]
    page.click("#add")
    for _ in range(2):
        page.fill("#panel input[type=number]", str(other))
        page.click("#panel button:text-is('link')")
        page.wait_for_function("() => open.links.length === 1")
    page.wait_for_function("() => window.__inflight === 0")
    assert page.evaluate("open.links.length") == 1


def test_a_failed_create_keeps_the_sheet_and_everything_in_it(page):
    other = core.create_card("other", actor="ce", project="Home")["id"]
    page.click("#add")
    page.fill("#panel input[type=text] >> nth=0", "doomed")
    page.fill("#panel input[type=number]", str(other))
    page.click("#panel button:text-is('link')")
    page.fill("#panel .say-box", "keep me")
    core.update_project("Home", name="Moved")           # the draft's project is now gone
    click_outside(page)
    page.wait_for_selector("#err.on")
    assert "unknown project" in page.text_content("#err")
    assert page.locator("#panel.on").count() == 1
    assert page.input_value("#panel .say-box") == "keep me"
    assert page.evaluate("open.links.length") == 1
    assert [c["id"] for c in core.list_cards()] == [other], "nothing was created"


# ---------- click outside to close ----------

def test_a_comment_left_in_the_box_is_posted_on_close(page):
    cid = add_card(page, "say it")
    page.fill("#panel .say-box", "unsent, but not lost")
    click_outside(page)
    page.wait_for_function("() => window.__inflight === 0")
    assert [e["detail"]["text"] for e in core.get_card(cid)["events"]
            if e["kind"] == "comment"] == ["unsent, but not lost"]


def test_an_empty_draft_just_closes(page):
    page.click("#add")
    page.select_option("#panel select >> nth=1", "high")   # a default changed: still empty
    click_outside(page)
    assert page.locator("#panel.on").count() == 0
    page.wait_for_timeout(300)
    assert core.list_cards() == []


@pytest.mark.parametrize("fill", [
    ("#panel textarea >> nth=0", "a description"),
    ("#panel .say-box", "a comment"),
    ("#panel input[placeholder='comma, separated']", "ui"),
])
def test_a_draft_with_content_but_no_title_stays_open(page, fill):
    page.click("#add")
    page.fill(*fill)
    click_outside(page)
    page.wait_for_selector("#err.on")
    assert "needs a title" in page.text_content("#err")
    assert page.locator("#panel.on").count() == 1
    assert page.input_value(fill[0]) == fill[1], "nothing typed was lost"
    assert page.evaluate("document.activeElement.placeholder") == "what needs doing?"
    page.keyboard.type("titled now")
    click_outside(page)
    created(page)
    assert [c["title"] for c in core.list_cards()] == ["titled now"]


def test_a_draft_that_must_stay_swallows_the_click_on_a_card(page):
    other = core.create_card("behind", actor="ce", project="Home")["id"]
    page.evaluate("load()")
    page.click("#add")
    page.fill("#panel textarea >> nth=0", "no title yet")
    page.click(f'.card[data-id="{other}"]')
    page.wait_for_selector("#err.on")
    page.wait_for_timeout(300)
    assert page.evaluate("open && open.id") is None, "the draft was not replaced"
    assert page.input_value("#panel textarea >> nth=0") == "no title yet"


def test_clicking_another_card_saves_this_one_and_opens_that(page):
    other = core.create_card("next", actor="ce", project="Home")["id"]
    cid = add_card(page, "first")
    page.fill("#panel input[type=text] >> nth=0", "first, edited")
    page.click(f'.card[data-id="{other}"]')
    page.wait_for_function(f"() => open && open.id === {other}")
    wait_saved(page, cid, "title", "first, edited")
    assert page.input_value("#panel input[type=text] >> nth=0") == "next"


def test_clicking_a_card_while_a_draft_is_open_creates_it_and_opens_that(page):
    other = core.create_card("next", actor="ce", project="Home")["id"]
    page.evaluate("load()")
    page.click("#add")
    page.fill("#panel input[type=text] >> nth=0", "made on the way")
    page.click(f'.card[data-id="{other}"]')
    page.wait_for_function(f"() => open && open.id === {other}")
    page.wait_for_function("() => window.__inflight === 0")
    assert sorted(c["title"] for c in core.list_cards()) == ["made on the way", "next"]
    assert page.locator("#panel.on").count() == 1, "the clicked card stays open"


def test_clicks_inside_the_sheet_never_close_it(page):
    cid = add_card(page, "stay")
    for target in ("#panel h3 >> nth=0", "#panel .sub", "#panel .log"):
        page.click(target)
        assert page.locator("#panel.on").count() == 1, target
    page.select_option("#panel select >> nth=1", "low")   # a field whose save re-renders
    wait_saved(page, cid, "priority", "low")
    page.click("#panel h3 >> nth=0")
    assert page.locator("#panel.on").count() == 1


def test_clicking_the_error_bar_does_not_close_the_sheet(page):
    add_card(page, "stay")
    page.evaluate("showErr('something')")
    page.click("#err")
    assert page.locator("#panel.on").count() == 1


def test_the_projects_sheet_also_closes_on_an_outside_click(page):
    page.click("#projects")
    page.wait_for_selector("#panel.on .project")
    click_outside(page)
    assert page.locator("#panel.on").count() == 0


# ---------- project colors ----------

def tag(page, cid):
    return page.locator(f'.card[data-id="{cid}"] .pill.proj')


def rgb(hex_):
    return "rgb(%d, %d, %d)" % tuple(int(hex_[i:i + 2], 16) for i in (1, 3, 5))


def test_a_card_is_tagged_with_its_projects_name_and_color(page):
    core.create_project("Pale", color="#fff0b3")
    dark = core.create_card("a", actor="ce", project="Home")["id"]
    light = core.create_card("b", actor="ce", project="Pale")["id"]
    page.evaluate("load()")
    page.wait_for_selector(f'.card[data-id="{light}"]')
    assert tag(page, dark).text_content() == "Home"
    assert tag(page, dark).evaluate("e => getComputedStyle(e).backgroundColor") == rgb(core.PALETTE[0])
    assert tag(page, dark).evaluate("e => getComputedStyle(e).color") == "rgb(255, 255, 255)"
    assert tag(page, light).evaluate("e => getComputedStyle(e).backgroundColor") == rgb("#fff0b3")
    assert tag(page, light).evaluate("e => getComputedStyle(e).color") == rgb("#172b4d"), \
        "dark text on a light color"


def test_changing_a_projects_color_recolors_its_cards(page):
    cid = core.create_card("a", actor="ce", project="Home")["id"]
    page.evaluate("load()")
    page.click("#projects")
    picker = project_block(page, "Home").locator("input[type=color]")
    assert picker.input_value() == core.PALETTE[0]
    picker.evaluate("e => { e.value = '#6554c0'; e.dispatchEvent(new Event('change')) }")
    page.wait_for_function("() => window.__inflight === 0")
    assert core.get_project("Home")["color"] == "#6554c0"
    assert tag(page, cid).evaluate("e => getComputedStyle(e).backgroundColor") == rgb("#6554c0")


def test_a_new_project_gets_a_palette_color_unless_one_is_picked(page):
    page.click("#projects")
    new = project_block(page, "new project")
    new.locator("input >> nth=0").fill("Auto")
    page.click("#panel button:text-is('add project')")
    page.wait_for_selector("#panel .project h3:text-is('Auto')")
    assert core.get_project("Auto")["color"] == core.PALETTE[1], "not the untouched black"
    new = project_block(page, "new project")
    new.locator("input >> nth=0").fill("Picked")
    new.locator("input[type=color]").evaluate(
        "e => { e.value = '#123456'; e.dispatchEvent(new Event('input')) }")
    page.click("#panel button:text-is('add project')")
    page.wait_for_selector("#panel .project h3:text-is('Picked')")
    assert core.get_project("Picked")["color"] == "#123456"


def test_the_tag_follows_a_card_moved_to_another_project(page):
    core.create_project("Other")
    cid = add_card(page, "moving")
    page.select_option("#panel select >> nth=0", "Other")
    wait_saved(page, cid, "project", "Other")
    assert tag(page, cid).text_content() == "Other"
    assert tag(page, cid).evaluate("e => getComputedStyle(e).backgroundColor") == rgb(core.PALETTE[1])


def test_the_project_tag_sits_after_the_priority_and_auto_chips(page):
    plain = core.create_card("a", actor="ce", project="Home", priority="high",
                             assignee="bob", labels=["ui"])["id"]
    auto = core.create_card("b", actor="ce", project="Home", auto_advance=True)["id"]
    page.evaluate("load()")
    page.wait_for_selector(f'.card[data-id="{auto}"]')
    chips = lambda cid: page.locator(f'.card[data-id="{cid}"] .meta > *').all_text_contents()
    assert chips(plain) == [f"#{plain}", "bob", "high", "Home", "ui"], chips(plain)
    assert chips(auto) == [f"#{auto}", "med", "auto", "Home"], chips(auto)
