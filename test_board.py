"""board.html driven in a real browser.

These are the only slow, environment-dependent tests in the suite, so they are kept few
and each one earns its place: the drag path, the panel's normal edits, and one lock per
bug that actually shipped. Anything that is really "does the server do X" belongs in
test_app.py instead.

The board's top-level `function` declarations (`openCard`, `patch`, `load`, `api`, …) are
on `window`, so page.evaluate can drive the page's own code rather than re-implementing it.
"""

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


def add_card(page, title):
    page.fill("#newTitle", title)
    page.click("#add")
    page.wait_for_selector(".card")
    return core.list_cards()[0]["id"]


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
    page.click("#panel .close.primary")
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
    page.select_option("#panel select >> nth=0", "high")          # priority
    wait_saved(page, cid, "priority", "high")
    type_into(page, "#panel input[placeholder='comma, separated']", "ui, api")
    wait_saved(page, cid, "labels", ["ui", "api"])
    page.select_option("#panel select >> nth=2", "test")          # lane
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
    core.create_card("elsewhere", actor="ce", project="Other")
    page.evaluate("load()")
    page.wait_for_selector(".card")
    add_card(page, "in inbox")
    page.click("#panel .close.primary")
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


def test_save_commits_the_field_you_are_still_typing_in(page):
    """What the save button is for: fields save on `change`, so the one you are still in
    has not saved yet when you reach for it. (Chrome blurs on click by itself, so this
    pins the behaviour rather than the line in board.html that guarantees it elsewhere.)"""
    cid = add_card(page, "unsaved")
    page.wait_for_selector("#panel.on")
    page.fill("#panel input[type=text] >> nth=0", "typed, never blurred")   # no blur here
    page.click("#panel .close.primary")
    wait_saved(page, cid, "title", "typed, never blurred")
    assert page.locator("#panel.on").count() == 0, "and it dismissed"


# ---------- regression locks ----------

def test_the_save_button_is_not_buried_under_the_header(page):
    """#panel needs a z-index above the sticky header's 5. Without it the panel's own
    close button sat underneath the header and every click on it hit "Add to todo",
    so the panel could not be dismissed and each attempt created another card."""
    add_card(page, "open me")
    page.wait_for_selector("#panel.on .close.primary")
    topmost = page.evaluate("""() => {
        const b = document.querySelector('#panel .close.primary');
        const r = b.getBoundingClientRect();
        const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
        return {isTheButton: hit === b, gotInstead: hit && hit.id};
    }""")
    assert topmost["isTheButton"], f"the save button is covered by #{topmost['gotInstead']}"
    page.click("#panel .close.primary")
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
    page.click("#panel .close.primary")
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

    page.fill("#newTitle", "should not be created")
    page.click("#add")
    page.wait_for_selector("#err.on")
    assert "say who you are first" in page.text_content("#err")
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
    a = core.create_card("a", actor="ce", checklist=[{"text": "x", "done": False}])
    b = core.create_card("b", actor="ce")
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
    other = core.create_card("other", actor="ce")
    page.evaluate("load()")
    cid = add_card(page, "linked one")
    page.select_option("#panel select >> nth=1", "bob")           # assignee
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
    page.select_option("#panel select >> nth=1", "")              # back to nobody
    wait_saved(page, cid, "assignee", None)


def test_linking_to_a_missing_card_shows_the_error(page):
    add_card(page, "lonely")
    page.fill("#panel input[type=number]", "999")
    page.click("#panel button:text('link')")
    page.wait_for_selector("#err.on")
    assert "no card 999" in page.text_content("#err")


def test_an_empty_title_creates_nothing(page):
    page.fill("#newTitle", "   ")
    page.click("#add")
    page.wait_for_selector("#err.on")
    assert "needs a title" in page.text_content("#err")
    assert core.list_cards() == []


def test_a_slow_patch_response_does_not_roll_back_a_newer_panel(page):
    """patch() re-rendered from its own response unconditionally. A PATCH whose response
    arrived after a later link had re-rendered the panel put the older card back on
    screen, and the link vanished from view though it was saved."""
    other = core.create_card("other", actor="ce")
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
    assert page.eval_on_selector("#panel select >> nth=0", "s => s.value") == "high"


def test_a_stale_card_does_not_roll_back_the_board(page):
    add_card(page, "fresh title")
    page.click("#panel .close.primary")
    page.evaluate("""() => { const c = cards[0];
        replace({...c, title: "stale title", updated_at: "2000-01-01T00:00:00+00:00"}); }""")
    assert page.text_content(".card .t") == "fresh title"
