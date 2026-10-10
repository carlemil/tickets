"""Makes docs-card.png, the annotated card in docs.html: `uv run python docs_card.py`.

A real screenshot of the board's own sheet, never a drawing of it: a card with every field
filled goes into a throwaway database, the board is served from this process on a free
port (never the live board on 8123), Chrome opens the card, and a numbered badge is drawn
onto each field. docs.html explains the fields under the same numbers, in MARKERS order.
test_board.py opens the same card and fails when the sheet shows a field MARKERS does not
name or a badge no longer finds its field, so the figure cannot drift silently -- but a
change to how the sheet looks still needs this script run again by hand.
"""

import socket
import tempfile
import threading
import time
from pathlib import Path

import core

OUT = Path(__file__).parent / "docs-card.png"
PROJECT = "Tickets"

# (key, target) in badge order, which is the sheet's top-to-bottom order. key is the data-field of the docs.html legend entry with
# the same number; target finds the element: "h3:<label>" a sheet field by its heading,
# "btn:<text>" a header button, anything else a CSS selector inside the sheet.
MARKERS = [
    ("signals", "#panel .head .id"),
    ("title", "#panel .head input"),
    ("archive", "btn:archive"),
    ("move to done", "btn:move to done"),
    ("close", "btn:close"),
    ("origin", "#panel > .sub"),
    ("project", "h3:project"),
    ("description", "h3:description"),
    ("activity", "h3:activity"),
    ("say something", "h3:say something"),
    ("plan", "h3:plan"),
    ("open questions", "h3:open questions"),
    ("your answers", "h3:your answers"),
    ("assignee", "h3:assignee"),
    ("lane", "h3:lane"),
    ("labels", "h3:labels"),
    ("checklist", "h3:checklist"),
    ("links", "h3:links"),
]

# Draws the badges; returns the keys whose target is not on the sheet (none, when the
# figure still fits the board). A field heading, or a target flush with the sheet's left edge, gets its badge
# in the gutter beside it, any other one on its top-left corner.
MARK_JS = """markers => {
  const panel = document.querySelector("#panel");
  const find = t => {
    if (t.startsWith("h3:"))
      return [...panel.querySelectorAll("h3")].find(h => h.textContent === t.slice(3));
    if (t.startsWith("btn:"))
      return [...panel.querySelectorAll(".head button")].find(b => b.textContent === t.slice(4));
    return panel.querySelector(t);
  };
  panel.querySelectorAll(".docs-badge").forEach(b => b.remove());
  const box = panel.getBoundingClientRect();
  const edge = box.left + parseFloat(getComputedStyle(panel).paddingLeft);
  const missing = [];
  markers.forEach(([key, target], i) => {
    const el = find(target);
    if (!el || !el.getClientRects().length) return missing.push(key);
    const r = el.getBoundingClientRect();
    const b = Object.assign(document.createElement("span"),
      { className: "docs-badge", textContent: i + 1 });
    const gutter = target.startsWith("h3:") || Math.abs(r.left - edge) < 3;
    const x = gutter ? r.left - 30 : r.left - 10;
    const y = gutter ? r.top + r.height / 2 - 11 : r.top - 11;
    Object.assign(b.style, {
      position: "absolute", left: (x - box.left + panel.scrollLeft) + "px",
      top: (y - box.top + panel.scrollTop) + "px", zIndex: 5,
      width: "22px", height: "22px", borderRadius: "50%", background: "#de350b",
      color: "#fff", font: "700 12px/22px system-ui, sans-serif", textAlign: "center",
      boxShadow: "0 0 0 2px #fff, 0 1px 3px rgba(0,0,0,.3)",
    });
    panel.append(b);
  });
  return missing;
}"""

# The sheet laid out flat for the shot: its full height, no scrolling, a gutter for badges.
SHOT_CSS = """
  #panel { position: absolute !important; inset: 0 auto auto 0 !important; width: 720px;
           overflow: visible !important; box-shadow: none; border-radius: 0;
           padding-left: 44px !important; }
  #panel .head { position: relative !important; margin-left: -44px !important;
                 padding: 20px 20px 12px 44px !important; }
"""


def build_card():
    """Every field the sheet has, filled, on the current database. Returns the card id."""
    if not any(p["name"] == PROJECT for p in core.list_projects()):
        core.create_project(PROJECT, path="D:/source/Tickets", color="#0052cc")
    user, agent = "User", core.AGENT
    parent = core.create_card("Docs overhaul", user, project=PROJECT)["id"]
    c = core.create_card(
        "Docs: annotated image of a card", user, project=PROJECT,
        description="Near the top of docs.html, add an image of a card with every field "
                    "visible, and explain what each field is for.",
    )["id"]
    after = core.create_card("Docs: annotated board overview", user, project=PROJECT)["id"]
    core.link_cards(c, parent, "parent", user)
    core.link_cards(c, after, "blocks", user)
    core.update_card(c, user, lane="plan")
    core.update_card(
        c, agent, assignee=agent, labels=["docs", "figure"],
        plan="## Context\nA screenshot of the real sheet, with numbered badges.\n\n"
             "## Steps\n1. Script the screenshot.\n2. Add the figure and its legend.\n"
             "3. Test that every field is explained.",
        questions="- Where should the figure go?\n  - right after the contents (recommended)\n"
                  "  - in section 3, the board",
        checklist=[{"text": "Script the screenshot", "done": True},
                   {"text": "Add the figure and its legend", "done": True},
                   {"text": "Test that every field is explained", "done": False}],
    )
    core.update_card(c, user, answers="1. right after the contents")
    core.update_card(c, agent, lane="develop")
    core.update_card(c, agent, lane="test")
    core.comment(c, agent, "Built and tested: 433 passed.",
                 output="> uv run pytest -q\n433 passed in 212.4s")
    core.update_card(c, agent, lane="verify", assignee=None, merged=True, deployed=True,
                     session="3f9c2a71-5d0e-4b8a-9c61-2e7f0a1b4d58",
                     pr="https://github.com/example/tickets/pull/97")
    core.comment(c, user, "Looks right, checking the deployed docs now.")
    core.set_activity(agent, c, "checking the deploy")
    return c


def main():
    import uvicorn
    from playwright.sync_api import sync_playwright

    import app

    with tempfile.TemporaryDirectory() as tmp:
        core.DB_PATH = str(Path(tmp) / "docs-card.db")
        card = build_card()
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        srv = uvicorn.Server(uvicorn.Config(app.app, host="127.0.0.1", port=port,
                                            log_level="warning"))
        threading.Thread(target=srv.run, daemon=True).start()
        while not srv.started:
            time.sleep(0.01)
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(channel="chrome")
                page = browser.new_page(viewport={"width": 1000, "height": 2400})
                page.goto(f"http://127.0.0.1:{port}")
                page.wait_for_selector("#board .lane")
                page.evaluate("id => openCard(id)", card)
                page.wait_for_selector("#panel.on h3")
                page.wait_for_selector("#panel .head .work.working")
                page.add_style_tag(content=SHOT_CSS)
                page.evaluate("document.activeElement && document.activeElement.blur()")
                missing = page.evaluate(MARK_JS, MARKERS)
                if missing:
                    raise SystemExit(f"not on the sheet: {missing}")
                page.locator("#panel").screenshot(path=str(OUT), animations="disabled")
                browser.close()
        finally:
            srv.should_exit = True
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
