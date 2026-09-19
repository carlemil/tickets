"""The claim the whole project exists to make: one card, two surfaces, one history.

PLAN.md task 4 established this by hand — a human dragged a card with a mouse, an agent
moved it further over MCP, and the activity log carried both. That was recorded as prose
and never re-run. This is that story as a test.
"""

import app
import core
from test_board import add_card, drag


def test_a_human_and_an_agent_share_one_attributed_card(page):
    # --- the human, in the browser ---
    cid = add_card(page, "Verify the real mouse drag")
    page.click("#panel .shut")   # closes the sheet
    drag(page, cid, "develop")
    page.wait_for_selector(f'.lane[data-lane="develop"] .card[data-id="{cid}"]')
    assert core.get_card(cid)["lane"] == "develop"

    # --- the agent, through the MCP tools, on the same card ---
    app.update_card(id=cid, actor="claude-agent", lane="verify")
    app.update_card(id=cid, actor="claude-agent", assignee="ce")
    app.comment(id=cid, actor="claude-agent",
                text="Dragged by hand, verified by agent. Shared state works.")

    assert [(e["actor"], e["kind"]) for e in core.get_card(cid)["events"]] == [
        ("ce", "created"),
        ("ce", "moved"),             # the mouse
        ("ce", "edited"),            # a forward move switches auto advance on
        ("claude-agent", "moved"),   # the agent
        ("claude-agent", "assigned"),
        ("claude-agent", "comment"),
    ], core.get_card(cid)["events"]

    # both actors self-registered, from opposite surfaces
    assert core.list_users() == ["ce", "claude-agent"], core.list_users()

    # --- and the human sees the agent's work ---
    page.reload()
    page.wait_for_selector(f'.lane[data-lane="verify"] .card[data-id="{cid}"]')
    page.evaluate("id => openCard(id)", cid)
    page.wait_for_selector("#panel.on .log li.comment")
    log = page.text_content("#panel .log")
    assert "claude-agent" in log and "ce" in log, log
    assert "Shared state works." in log, log
    assert page.text_content(f'.card[data-id="{cid}"] .pill.who') == "ce", "assigned by the agent"


def test_an_agent_archives_and_the_human_board_follows(page):
    cid = add_card(page, "agent will archive this")
    page.click("#panel .shut")   # closes the sheet
    app.update_card(id=cid, actor="claude-agent", archived=True)
    page.reload()
    page.wait_for_selector("#board .lane")
    assert page.locator(f'.card[data-id="{cid}"]').count() == 0, "gone from the board"
    page.check("#showArch")
    page.wait_for_selector(f'.card[data-id="{cid}"]')
    page.click(f'.card[data-id="{cid}"]')
    page.click("#panel button:text('unarchive')")          # the human brings it back
    page.uncheck("#showArch")
    page.wait_for_selector(f'.card[data-id="{cid}"]')
    assert [(e["actor"], e["kind"]) for e in core.get_card(cid)["events"]][-2:] == [
        ("claude-agent", "archived"), ("ce", "archived")]
