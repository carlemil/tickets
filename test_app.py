"""app.py: only what the wrappers add.

core.py's behaviour is tested in test_core.py and is not re-tested through HTTP. What
lives here is the part app.py owns: status codes, the `actor` requirement, the two
exception mappings, the JSON shapes the board depends on, and the ToolError mapping the
agent sees. Plus one test of the real MCP transport, because that is the only thing a
direct function call cannot prove.
"""

import asyncio
import json
import logging
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError
from starlette.testclient import TestClient

import app
import core

WRITE_ROUTES = [
    ("post", "/api/cards", {"title": "x"}),
    ("patch", "/api/cards/1", {"lane": "plan"}),
    ("post", "/api/cards/1/comment", {"text": "hi"}),
    ("post", "/api/links", {"from_id": 1, "to_id": 2, "kind": "blocks"}),
    ("delete", "/api/links", {"from_id": 1, "to_id": 2, "kind": "blocks"}),
]


# ---------- HTTP: the happy paths ----------

def test_root_serves_the_board(client):
    r = client.get("/")
    assert r.status_code == 200
    assert 'id="board"' in r.text, "the board itself, not a placeholder"


def test_both_pages_have_the_tab_icon(client):
    for path in ["/", "/docs"]:
        assert '<link rel="icon" href="data:image/svg+xml,<svg' in client.get(path).text, path


def test_docs_page_is_served_and_covers_the_essentials(client):
    r = client.get("/docs")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    for text in ["127.0.0.1", "claude-agent", "/tickets", "worktree", *core.LANES]:
        assert text in r.text, text
    for text in ["python agent", "auto advance"]:   # the retired agent and its switch
        assert text not in r.text, text
    for text in ["this machine", "8000 is taken", "on master"]:   # nothing about one setup
        assert text not in r.text, text


def test_the_docs_show_the_annotated_card_with_one_legend_entry_per_badge(client):
    import re

    import docs_card

    r = client.get("/docs/card.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"
    html = client.get("/docs").text
    assert '<img src="/docs/card.png"' in html
    # the figure sits near the top: before the first numbered section
    assert html.index('id="card-fields"') < html.index('id="what"')
    legend = html.split('id="card-fields"', 1)[1].split("</ol>", 1)[0]
    assert re.findall(r'data-field="([^"]+)"', legend) == [k for k, _ in docs_card.MARKERS]
    assert 'id="signals"' in html   # the legend links there


def test_create_card_returns_201_and_the_full_card(client):
    r = client.post("/api/cards", json={"title": "from http", "actor": "ann", "project": "Home"})
    assert r.status_code == 201, r.text
    c = r.json()
    assert c["id"] and c["lane"] == "todo" and c["events"], c


def test_create_user_returns_201_without_an_actor(client):
    # the one write that predates having an actor, so `body()`'s rule must not apply
    r = client.post("/api/users", json={"name": "zoe"})
    assert r.status_code == 201 and r.json() == {"name": "zoe"}, r.text
    assert client.get("/api/users").json() == ["zoe"]


def test_query_params_reach_the_filter(client):
    client.post("/api/cards", json={"title": "a", "actor": "ann", "project": "Home", "lane": "plan"})
    client.post("/api/cards", json={"title": "b", "actor": "ann", "project": "Home", "lane": "todo"})
    assert [c["title"] for c in client.get("/api/cards?lane=plan").json()] == ["a"]
    assert [c["title"] for c in client.get("/api/cards?assignee=nobody").json()] == []
    assert len(client.get("/api/cards?project=home").json()) == 2


def test_version_is_the_newest_event_and_a_reorder_leaves_it(client):
    assert client.get("/api/version").json() == {"event": 0}, "an empty board"
    c = client.post("/api/cards", json={"title": "a", "actor": "ann", "project": "Home"}).json()
    v = client.get("/api/version").json()["event"]
    assert v > 0
    client.post(f"/api/cards/{c['id']}/comment", json={"text": "hi", "actor": "ann"})
    w = client.get("/api/version").json()["event"]
    assert w > v
    client.patch(f"/api/cards/{c['id']}", json={"pos": 99, "actor": "ann"})
    assert client.get("/api/version").json()["event"] == w, "a place in the queue logs nothing"


def test_unlink_accepts_a_json_body_on_delete(client):
    a = client.post("/api/cards", json={"title": "a", "actor": "ann", "project": "Home"}).json()
    b = client.post("/api/cards", json={"title": "b", "actor": "ann", "project": "Home"}).json()
    body = {"from_id": a["id"], "to_id": b["id"], "kind": "blocks", "actor": "ann"}
    assert client.post("/api/links", json=body).status_code == 200
    r = client.request("DELETE", "/api/links", json=body)
    assert r.status_code == 200 and r.json()["links"] == [], r.text


def test_projects_routes_create_list_and_update(client, tmp_path):
    r = client.post("/api/projects", json={"name": "Tickets", "path": str(tmp_path),
                                           "instructions": "be brief"})
    assert r.status_code == 201, r.text
    assert r.json() == {"name": "Tickets", "path": str(tmp_path), "instructions": "be brief", "color": "#00875a",
                        "land": "merge"}
    assert [p["name"] for p in client.get("/api/projects").json()] == ["Home", "Tickets"]
    r = client.patch("/api/projects/tickets", json={"instructions": "be thorough"})
    assert r.status_code == 200 and r.json()["instructions"] == "be thorough", r.text


def test_adding_a_project_opens_a_card_for_each_missing_thing(client):
    assert client.post("/api/projects", json={"name": "Fresh"}).status_code == 201
    cards = client.get("/api/cards?project=Fresh").json()
    assert [c["title"] for c in cards] == ["Set Fresh's folder",
                                           "Tell the agent how to test Fresh",
                                           "Tell the agent how to deploy Fresh"]
    assert {c["lane"] for c in cards} == {"todo"}


def test_adding_a_configured_project_opens_no_cards(client, tmp_path):
    client.post("/api/projects", json={"name": "Fresh", "path": str(tmp_path),
                                       "instructions": "test: pytest. deploy: merge."})
    assert client.get("/api/cards?project=Fresh").json() == []


def test_a_renamed_project_is_reached_by_its_new_name(client):
    client.post("/api/projects", json={"name": "Old name"})
    c = client.post("/api/cards", json={"title": "a", "actor": "ann",
                                        "project": "Old name"}).json()
    r = client.patch("/api/projects/Old%20name", json={"name": "New/name"})
    assert r.status_code == 200 and r.json()["name"] == "New/name", r.text
    assert client.get(f"/api/cards/{c['id']}").json()["project"] == "New/name"
    r = client.patch("/api/projects/New/name", json={"instructions": "slashes survive"})
    assert r.status_code == 200, "a name with a slash is still addressable"


@pytest.mark.parametrize("body,err", [
    ({"name": ""}, "needs a name"),
    ({"name": "home"}, "already exists"),
    ({"name": "X", "path": "relative/dir"}, "absolute"),
    ({"name": "X", "bogus": 1}, "bogus"),
])
def test_bad_project_is_400(client, body, err):
    r = client.post("/api/projects", json=body)
    assert r.status_code == 400 and err in r.json()["error"], r.text


def test_unknown_project_is_404_on_update(client):
    r = client.patch("/api/projects/nope", json={"path": ""})
    assert r.status_code == 404 and r.json() == {"error": "no project 'nope'"}, r.text


def test_a_card_in_an_unconfigured_project_is_400(client):
    r = client.post("/api/cards", json={"title": "a", "actor": "ann", "project": "Nowhere"})
    assert r.status_code == 400 and "unknown project" in r.json()["error"], r.text


@pytest.mark.no_home
def test_a_fresh_board_lists_no_projects_and_refuses_a_projectless_card(client):
    assert client.get("/api/projects").json() == []
    r = client.post("/api/cards", json={"title": "a", "actor": "ann"})
    assert r.status_code == 400 and "create one first" in r.json()["error"], r.text
    assert client.get("/api/cards").json() == []


def test_the_create_card_tool_requires_a_project():
    async def go():
        async with Client(app.mcp) as c:
            tools = {t.name: t for t in (await c.list_tools()).tools}
            assert "project" in tools["create_card"].input_schema["required"]
            r = await c.call_tool("create_card", {"title": "a", "actor": "bot"})
            assert r.is_error
    asyncio.run(go())
    assert core.list_cards() == []


# ---------- HTTP: the shape the board relies on ----------

def test_list_is_lean_and_a_single_card_is_full(client):
    client.post("/api/cards", json={"title": "a", "actor": "ann", "project": "Home"})
    listed = client.get("/api/cards").json()[0]
    assert "events" not in listed and "links" not in listed, "the board's list view stays lean"
    assert listed["blocked_by"] == listed["blocking"] == [], "but carries the block marks"
    full = client.get(f"/api/cards/{listed['id']}").json()
    assert full["events"] and full["links"] == [], full


# ---------- HTTP: the two exception mappings ----------

@pytest.mark.parametrize("method,path,body", WRITE_ROUTES, ids=lambda v: str(v)[:24])
def test_every_write_requires_an_actor(client, method, path, body):
    r = client.request(method.upper(), path, json=body)
    assert r.status_code == 400, r.text
    assert r.json() == {"error": "actor is required on every write"}, r.text


def test_blank_user_name_is_400(client):
    r = client.post("/api/users", json={"name": "   "})
    assert r.status_code == 400 and r.json() == {"error": "name is required"}, r.text


def test_unknown_card_is_404_with_a_clean_body(client):
    r = client.get("/api/cards/999")
    assert r.status_code == 404
    assert r.json() == {"error": "no card 999"}, "an error message, not a stack trace"


def test_bad_lane_is_400(client):
    c = client.post("/api/cards", json={"title": "a", "actor": "ann", "project": "Home"}).json()
    r = client.patch(f"/api/cards/{c['id']}", json={"actor": "ann", "lane": "backlog"})
    assert r.status_code == 400 and "lane must be one of" in r.json()["error"], r.text


def test_unknown_field_is_400_via_the_typeerror_branch(client):
    # core.create_card raises TypeError, not ValueError, for an unexpected keyword
    r = client.post("/api/cards", json={"title": "a", "actor": "ann", "project": "Home", "bogus": 1})
    assert r.status_code == 400 and "bogus" in r.json()["error"], r.text


def test_malformed_json_is_400(client):
    r = client.post("/api/cards", content=b"{not json",
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400, r.text


def test_non_integer_id_is_404_from_the_route_converter(client):
    assert client.get("/api/cards/abc").status_code == 404


# ---------- MCP tools: the ToolError mapping ----------

def test_tools_are_plain_callables():
    c = app.create_card(title="x", actor="ann", project="Home")
    assert isinstance(c, dict) and c["lane"] == "todo", c


def test_notfound_reaches_the_agent_as_a_toolerror():
    with pytest.raises(ToolError, match="no card 999"):
        app.get_card(999)


def test_bad_input_reaches_the_agent_as_a_toolerror():
    with pytest.raises(ToolError, match="lane must be one of"):
        app.create_card(title="x", actor="ann", project="Home", lane="bogus")


def test_the_wrapper_is_what_maps_the_error():
    with pytest.raises(ToolError):
        app.create_user("")
    with pytest.raises(ValueError):          # same call, wrapper bypassed
        app.create_user.__wrapped__("")


def test_update_card_tool_can_unassign():
    c = app.create_card(title="x", actor="ann", project="Home", assignee="bob")
    c = app.update_card(id=c["id"], actor="ann", assignee="")
    assert c["assignee"] is None, 'an agent unassigns by passing ""'
    assert [e["kind"] for e in c["events"]].count("assigned") == 1, c["events"]


def test_http_unassigns_with_an_empty_string_or_null_alike(client):
    """"" used to be stored as-is, so a later null counted as a second unassign."""
    c = client.post("/api/cards", json={"title": "x", "actor": "ann", "project": "Home",
                                        "assignee": "bob"}).json()
    c = client.patch(f"/api/cards/{c['id']}", json={"actor": "ann", "assignee": ""}).json()
    assert c["assignee"] is None
    c = client.patch(f"/api/cards/{c['id']}", json={"actor": "ann", "assignee": None}).json()
    assert [e["kind"] for e in c["events"]].count("assigned") == 1, c["events"]


def test_omitted_fields_are_left_alone():
    c = app.create_card(title="x", actor="ann", project="Home", assignee="bob", description="keep me")
    c = app.update_card(id=c["id"], actor="ann", lane="plan")
    assert c["assignee"] == "bob" and c["description"] == "keep me", c


def test_every_tool_has_a_real_docstring():
    # the agent's only manual; an f-string would silently not be one
    tools = (app.list_cards, app.get_card, app.create_card, app.update_card, app.list_projects,
             app.create_user, app.comment, app.link_cards, app.unlink_cards)
    assert all(t.__doc__ and t.__doc__.strip() for t in tools)


# ---------- the real MCP transport, once ----------

def test_the_mcp_endpoint_serves_the_tools(db):
    """The one wire-level test. Direct calls prove the functions; only this proves the
    transport an agent actually connects through is mounted and wired. That is precisely
    what the FastMCP -> MCPServer rename broke once."""
    headers = {"Accept": "application/json, text/event-stream",
               "Content-Type": "application/json"}
    # Three things this needs and the route tests do not: the default testserver host is
    # rejected 421 by the DNS-rebinding guard on /mcp; the streamable-http manager needs
    # the lifespan, hence the context manager; and that manager is single-use, so this
    # builds its own app rather than burning the shared app.app the server fixture runs.
    with TestClient(app.mcp.streamable_http_app(),
                    base_url="http://127.0.0.1:8000") as c:
        r = c.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "test", "version": "1"}}})
        assert r.status_code == 200, r.text
        live = {**headers, "mcp-session-id": r.headers["mcp-session-id"],
                "MCP-Protocol-Version": "2025-06-18"}
        c.post("/mcp", headers=live,
               json={"jsonrpc": "2.0", "method": "notifications/initialized"})

        def call(payload):
            r = c.post("/mcp", headers=live, json={"jsonrpc": "2.0", "id": 9, **payload})
            assert r.status_code == 200, r.text
            return json.loads(r.text.split("data: ", 1)[1])["result"]

        listed = {t["name"] for t in call({"method": "tools/list"})["tools"]}
        assert listed == {"list_cards", "get_card", "create_card", "update_card", "list_projects",
                          "create_user", "comment", "link_cards", "unlink_cards",
                          "set_activity"}, listed

        made = call({"method": "tools/call",
                     "params": {"name": "create_card",
                                "arguments": {"title": "over mcp", "actor": "agent", "project": "Home"}}})
        assert made["isError"] is False, made
        assert core.list_cards()[0]["title"] == "over mcp", "the call reached the database"

        failed = call({"method": "tools/call",
                       "params": {"name": "get_card", "arguments": {"id": 999}}})
        assert failed["isError"] is True, failed
        assert "no card 999" in json.dumps(failed["content"]), failed

        # archive over the wire: the tool schema must carry the new argument through
        cid = core.list_cards()[0]["id"]
        done = call({"method": "tools/call",
                     "params": {"name": "update_card",
                                "arguments": {"id": cid, "actor": "agent", "archived": True}}})
        assert done["isError"] is False, done
        assert core.get_card(cid)["archived"] is True and core.list_cards() == []


def test_archived_query_param_switches_the_list(client):
    c = client.post("/api/cards", json={"title": "a", "actor": "ann", "project": "Home"}).json()
    client.patch(f"/api/cards/{c['id']}", json={"archived": True, "actor": "ann"})
    assert client.get("/api/cards").json() == []
    assert [x["id"] for x in client.get("/api/cards?archived=1").json()] == [c["id"]]


def test_update_card_tool_can_archive():
    c = app.create_card(title="a", actor="bot", project="Home")
    assert app.update_card(c["id"], "bot", archived=True)["archived"] is True
    assert app.list_cards() == [] and len(app.list_cards(archived=True)) == 1


@pytest.mark.parametrize("q", ["", "?archived=0", "?archived=", "?archived=true"])
def test_anything_but_archived_1_lists_the_active_board(client, q):
    a = client.post("/api/cards", json={"title": "active", "actor": "ann", "project": "Home"}).json()
    b = client.post("/api/cards", json={"title": "gone", "actor": "ann", "project": "Home"}).json()
    client.patch(f"/api/cards/{b['id']}", json={"archived": True, "actor": "ann"})
    assert [x["id"] for x in client.get("/api/cards" + q).json()] == [a["id"]]


def test_archived_filter_combines_with_project_over_http(client):
    for n in ("P", "Q"):
        client.post("/api/projects", json={"name": n})
    a = client.post("/api/cards", json={"title": "a", "actor": "ann", "project": "P"}).json()
    client.post("/api/cards", json={"title": "b", "actor": "ann", "project": "Q"})
    client.patch(f"/api/cards/{a['id']}", json={"archived": True, "actor": "ann"})
    got = client.get("/api/cards?archived=1&project=p").json()
    assert [x["id"] for x in got] == [a["id"]], got


def test_an_archived_card_still_opens_by_id(client):
    a = client.post("/api/cards", json={"title": "a", "actor": "ann", "project": "Home"}).json()
    client.patch(f"/api/cards/{a['id']}", json={"archived": True, "actor": "ann"})
    r = client.get(f"/api/cards/{a['id']}")
    assert r.status_code == 200 and r.json()["archived"] is True, r.text


@pytest.mark.parametrize("bad", ["yes", 1, None])
def test_non_bool_archived_is_400(client, bad):
    a = client.post("/api/cards", json={"title": "a", "actor": "ann", "project": "Home"}).json()
    r = client.patch(f"/api/cards/{a['id']}", json={"archived": bad, "actor": "ann"})
    assert r.status_code == 400 and "archived" in r.json()["error"], r.text


def test_archiving_an_unknown_card_is_404(client):
    r = client.patch("/api/cards/999", json={"archived": True, "actor": "ann"})
    assert r.status_code == 404, r.text


def test_link_with_a_missing_field_is_400(client):
    r = client.post("/api/links", json={"from_id": 1, "actor": "ann"})
    assert r.status_code == 400, r.text


def test_users_are_listed_sorted(client):
    for n in ("zoe", "ann", "mia"):
        client.post("/api/users", json={"name": n})
    assert client.get("/api/users").json() == ["ann", "mia", "zoe"]


def test_update_card_tool_can_unarchive_and_omitting_archived_leaves_it():
    c = app.create_card(title="a", actor="bot", project="Home")
    app.update_card(c["id"], "bot", archived=True)
    assert app.update_card(c["id"], "bot", lane="plan")["archived"] is True,         "archived=None means not passed, like every other field"
    assert app.update_card(c["id"], "bot", archived=False)["archived"] is False
    assert [x["id"] for x in app.list_cards()] == [c["id"]]


def test_list_cards_tool_combines_archived_with_project():
    core.create_project("P")
    core.create_project("Q")
    a = app.create_card(title="a", actor="bot", project="P")
    app.create_card(title="b", actor="bot", project="Q")
    app.update_card(a["id"], "bot", archived=True)
    assert [x["id"] for x in app.list_cards(project="p", archived=True)] == [a["id"]]
    assert app.list_cards(project="q", archived=True) == []


def test_update_card_tool_rejects_a_non_bool_archived():
    c = app.create_card(title="a", actor="bot", project="Home")
    with pytest.raises(ToolError, match="archived"):
        app.update_card(c["id"], "bot", archived="yes")


def test_list_projects_tool_gives_the_agent_path_and_instructions(tmp_path):
    core.create_project("Tickets", path=str(tmp_path), instructions="run the tests first")
    assert {"name": "Tickets", "path": str(tmp_path),
            "instructions": "run the tests first", "color": "#00875a",
            "land": "merge"} in app.list_projects()


def test_create_card_tool_rejects_an_unconfigured_project():
    with pytest.raises(ToolError, match="unknown project 'Nowhere'"):
        app.create_card(title="x", actor="bot", project="Nowhere")


def test_the_retired_agent_fields_are_gone_from_rest_and_the_tools(client):
    c = client.post("/api/cards", json={"title": "a", "actor": "ann", "project": "Home"}).json()
    for f in core.LEGACY_COLUMNS:
        assert f not in c
        r = client.patch(f"/api/cards/{c['id']}", json={f: True, "actor": "ann"})
        assert r.status_code == 400 and f in r.json()["error"], r.text
        assert f not in app.update_card.__doc__ and f not in app.create_card.__doc__


def test_pos_reorders_over_http_and_must_be_a_number(client):
    a = client.post("/api/cards", json={"title": "a", "actor": "ann", "project": "Home"}).json()
    b = client.post("/api/cards", json={"title": "b", "actor": "ann", "project": "Home"}).json()
    r = client.patch(f"/api/cards/{b['id']}", json={"pos": a["pos"] - 1, "actor": "ann"})
    assert r.status_code == 200, r.text
    assert [c["title"] for c in client.get("/api/cards").json()] == ["b", "a"]
    bad = client.patch(f"/api/cards/{b['id']}", json={"pos": "top", "actor": "ann"})
    assert bad.status_code == 400 and "pos" in bad.json()["error"], bad.text


def test_plan_fields_over_the_tool_and_what_it_tells_agents():
    c = core.create_card("a", actor="ann", project="Home", description="the ask")
    c = app.update_card(c["id"], "bot", plan="p", questions="q?", answers="")
    assert (c["description"], c["plan"], c["questions"]) == ("the ask", "p", "q?")
    doc = app.update_card.__doc__
    for said in ("`description` is the request", "`plan` is the", "`answers` is the person"):
        assert said in doc


def test_project_colors_over_rest(client):
    r = client.post("/api/projects", json={"name": "Red", "color": "#FF0000"})
    assert r.status_code == 201 and r.json()["color"] == "#ff0000", r.text
    r = client.patch("/api/projects/Red", json={"color": "crimson"})
    assert r.status_code == 400 and "color" in r.json()["error"], r.text
    r = client.patch("/api/projects/Red", json={"color": "#00ff00"})
    assert r.status_code == 200 and r.json()["color"] == "#00ff00", r.text
    assert {p["name"]: p["color"] for p in client.get("/api/projects").json()} == {
        "Home": core.PALETTE[0], "Red": "#00ff00"}


# ---------- activity: the status bar's feed ----------

def test_activity_route_lists_what_agents_are_doing(client):
    assert client.get("/api/activity").json() == []
    c = client.post("/api/cards", json={"title": "a", "actor": "ann", "project": "Home"}).json()
    app.set_activity("bot", c["id"], "planning")
    [row] = client.get("/api/activity").json()
    assert (row["actor"], row["card_id"], row["doing"], row["title"]) == ("bot", c["id"], "planning", "a")
    app.set_activity("bot")
    assert client.get("/api/activity").json() == []


def test_activity_can_be_set_and_cleared_over_http(client):
    """The fallback for an agent whose MCP connection failed: set_activity as a POST."""
    c = client.post("/api/cards", json={"title": "a", "actor": "ann", "project": "Home"}).json()
    r = client.post("/api/activity", json={"actor": "bot", "card_id": c["id"], "doing": "planning"})
    assert r.status_code == 200, r.text
    assert [(a["actor"], a["doing"]) for a in r.json()] == [("bot", "planning")]
    assert client.post("/api/activity", json={"actor": "bot"}).json() == []
    assert client.post("/api/activity", json={"card_id": c["id"], "doing": "x"}).status_code == 400
    assert client.post("/api/activity", json={"actor": "bot", "card_id": 999,
                                              "doing": "x"}).status_code == 404


def test_set_activity_tool_errors_reach_the_agent():
    with pytest.raises(ToolError, match="no card 999"):
        app.set_activity("bot", 999, "planning")
    c = core.create_card("a", "ann", project="Home")["id"]
    with pytest.raises(ToolError, match="doing is required"):
        app.set_activity("bot", c)


def test_delete_project_route(client):
    client.post("/api/projects", json={"name": "Old/one"})
    client.post("/api/cards", json={"title": "a", "actor": "ann", "project": "Old/one"})
    r = client.delete("/api/projects/old/one")
    # the card above plus the three setup cards adding the project opened
    assert r.status_code == 200 and r.json() == {"deleted": "Old/one", "moved": 4}, r.text
    assert {c["project"] for c in client.get("/api/cards").json()} == {core.NO_PROJECT}
    assert client.delete("/api/projects/Old/one").status_code == 404
    r = client.delete("/api/projects/" + core.NO_PROJECT)
    assert r.status_code == 400 and "cannot be deleted" in r.json()["error"], r.text


def test_update_card_tool_sets_merged_and_deployed_and_rejects_non_bools():
    c = app.create_card(title="x", actor="ann", project="Home")
    c = app.update_card(id=c["id"], actor="claude-agent", merged=True, deployed=True)
    assert (c["merged"], c["deployed"]) == (True, True)
    with pytest.raises(ToolError, match="deployed must be true or false"):
        app.update_card(id=c["id"], actor="ann", deployed="yes")


def test_update_card_tool_sets_the_session():
    c = app.create_card(title="x", actor="ann", project="Home")
    c = app.update_card(id=c["id"], actor="claude-agent", assignee="claude-agent",
                        session="0f1e2d3c-4b5a")
    assert c["session"] == "0f1e2d3c-4b5a"
    assert app.update_card(id=c["id"], actor="ann", lane="plan")["session"] == "0f1e2d3c-4b5a"
    assert "session" in app.update_card.__doc__


def test_update_card_tool_sets_the_pr():
    c = app.create_card(title="x", actor="ann", project="Home")
    url = "https://github.com/o/r/pull/7"
    assert app.update_card(id=c["id"], actor="claude-agent", pr=url)["pr"] == url
    assert "`pr`" in app.update_card.__doc__ and "`land`" in app.list_projects.__doc__


def test_a_reworked_pr_card_drops_its_pr_over_rest(client):
    c = client.post("/api/cards", json={"title": "x", "actor": "ann", "project": "Home",
                                        "lane": "verify"}).json()
    url = "https://github.com/o/r/pull/7"
    r = client.patch(f"/api/cards/{c['id']}", json={"actor": "ann", "pr": url, "merged": True})
    assert r.json()["pr"] == url, r.text
    r = client.patch(f"/api/cards/{c['id']}", json={"actor": "ann", "lane": "develop"})
    assert r.status_code == 200, r.text
    assert (r.json()["pr"], r.json()["merged"]) == ("", False)


def test_project_land_over_rest(client):
    r = client.post("/api/projects", json={"name": "Shop", "land": "pr"})
    assert r.status_code == 201 and r.json()["land"] == "pr", r.text
    r = client.patch("/api/projects/Shop", json={"land": "merge"})
    assert r.status_code == 200 and r.json()["land"] == "merge", r.text
    for bad in ({"land": "squash"}, {"land": None}):
        r = client.patch("/api/projects/Shop", json=bad)
        assert r.status_code == 400 and "'merge' or 'pr'" in r.text, r.text
    r = client.post("/api/projects", json={"name": "Bad", "land": "x"})
    assert r.status_code == 400, r.text
    assert [p["land"] for p in client.get("/api/projects").json()] == ["merge", "merge"]


def test_log_config_timestamps_the_access_row():
    """#81: uvicorn's rows say when. Built by hand, not dictConfig, which would re-point
    logging for the rest of the suite."""
    from uvicorn.logging import AccessFormatter

    f = json.loads((Path(__file__).parent / "log-config.json").read_text())["formatters"]["access"]
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0,
                            '%s - "%s %s HTTP/%s" %d',
                            ("127.0.0.1:54099", "GET", "/api/activity", "1.1", 200), None)
    line = AccessFormatter(fmt=f["fmt"], datefmt=f["datefmt"]).format(rec)
    assert re.match(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d ", line), line
    assert "/api/activity" in line


# ---------- board settings: host on the LAN ----------

def test_settings_get_reports_the_setting_and_an_unknown_bind_in_tests(client):
    s = client.get("/api/settings").json()
    assert s["lan"] is False
    assert s["bound"] is None, "the suite's servers are not started by uvicorn's CLI"
    assert s["pending"] is False and s["listening_lan"] is False and s["can_restart"] is False
    assert isinstance(s["urls"], list) and s["port"]


def test_settings_patch_saves_accepts_the_boards_actor_and_does_not_restart_unknown(
        client, restarts):
    r = client.patch("/api/settings", json={"lan": True, "actor": "User"})
    assert r.status_code == 200, r.text
    assert r.json()["lan"] is True and r.json()["restarting"] is False
    assert core.get_settings() == {"lan": True}
    assert restarts == [], "a backend that does not know its bind never restarts itself"
    assert core.list_activity() == [] and core.last_event() == 0   # not card activity


@pytest.mark.parametrize("body", [{"lan": "yes"}, {"lan": None}, {"port": 1}, [True], "x"])
def test_settings_patch_rejects_bad_input_with_400(client, body):
    r = client.patch("/api/settings", json=body)
    assert r.status_code == 400 and r.json()["error"]
    assert core.get_settings() == {"lan": False}


def test_turning_lan_on_restarts_a_loopback_backend_onto_every_address(
        client, restarts, monkeypatch):
    monkeypatch.setattr(app, "bound_host", lambda: "127.0.0.1")
    monkeypatch.setattr(app, "can_restart", lambda: True)
    r = client.patch("/api/settings", json={"lan": True}).json()
    assert r["pending"] is True and r["restarting"] is True and restarts == [1]
    # the same value again (a second tab, an agent): still pending, but no second restart
    r = client.patch("/api/settings", json={"lan": True}).json()
    assert r["pending"] is True and r["restarting"] is False and restarts == [1]
    r = client.patch("/api/settings", json={"actor": "User"}).json()
    assert r["restarting"] is False and restarts == [1], "a PATCH that changes nothing"
    # off and on again is two changes: the way to retry a restart that failed
    client.patch("/api/settings", json={"lan": False})
    assert restarts == [1], "off again matches the running 127.0.0.1: nothing to restart"
    client.patch("/api/settings", json={"lan": True})
    assert restarts == [1, 1]


def test_turning_lan_off_restarts_a_lan_backend_and_a_matching_setting_does_not(
        client, restarts, monkeypatch):
    monkeypatch.setattr(app, "bound_host", lambda: "0.0.0.0")
    monkeypatch.setattr(app, "can_restart", lambda: True)
    core.update_settings(lan=True)
    s = client.get("/api/settings").json()
    assert s["listening_lan"] is True and s["pending"] is False
    assert client.patch("/api/settings", json={"lan": True}).json()["restarting"] is False
    assert restarts == []
    assert client.patch("/api/settings", json={"lan": False}).json()["restarting"] is True
    assert restarts == [1]


def test_a_pending_change_that_cannot_restart_says_so_and_does_not_try(
        client, restarts, monkeypatch):
    monkeypatch.setattr(app, "bound_host", lambda: "127.0.0.1")
    monkeypatch.setattr(app, "can_restart", lambda: False)   # not Windows, or no script
    r = client.patch("/api/settings", json={"lan": True}).json()
    assert r["pending"] is True and r["restarting"] is False and restarts == []


def test_settings_urls_are_the_lan_addresses_on_the_requests_port(client, monkeypatch):
    monkeypatch.setattr(app, "lan_addresses", lambda: ["10.0.0.7", "192.168.1.5"])
    s = TestClient(app.app, base_url="http://127.0.0.1:8123").get("/api/settings").json()
    assert s["port"] == 8123
    assert s["urls"] == ["http://10.0.0.7:8123/", "http://192.168.1.5:8123/"]


@pytest.mark.parametrize("argv, host", [
    (["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8123"], "0.0.0.0"),
    (["uvicorn", "app:app", "--host=127.0.0.1"], "127.0.0.1"),
    (["pytest", "-q"], None),
    (["uvicorn", "app:app", "--host"], None),
])
def test_bound_host_reads_uvicorns_command_line(argv, host):
    assert app.bound_host(argv) == host


@pytest.mark.parametrize("argv, port", [
    (["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8123"], 8123),
    (["uvicorn", "app:app", "--port=8124"], 8124),
    (["uvicorn", "app:app", "--host", "127.0.0.1"], 8000),   # uvicorn's default
    (["uvicorn", "app:app", "--port", "x"], None),
])
def test_bound_port_reads_uvicorns_command_line(argv, port):
    assert app.bound_port(argv) == port


def live_like(monkeypatch, *, port="8123"):
    """This process made to look like the board restart-backend.ps1 runs: uvicorn's CLI on
    Windows, with its settings.json beside the script."""
    monkeypatch.setattr(app.sys, "argv", ["uvicorn", "app:app", "--host", "127.0.0.1",
                                          "--port", port])
    monkeypatch.setattr(app.os, "name", "nt")
    monkeypatch.setattr(core, "DB_PATH", str(app.RESTART_SCRIPT.parent / "tickets.db"))


def test_can_restart_only_the_backend_the_script_manages(monkeypatch):
    live_like(monkeypatch)
    assert app.can_restart() is True
    monkeypatch.setattr(app.sys, "argv", ["pytest"])
    assert app.can_restart() is False, "an unknown bind never restarts"


def test_a_trial_server_on_another_port_never_restarts_the_live_board(monkeypatch):
    """restart-backend.ps1 kills whatever serves 8123: run from a worktree's 8124 server it
    would replace the live board with the worktree's code."""
    live_like(monkeypatch, port="8124")
    assert app.can_restart() is False


def test_a_database_elsewhere_never_restarts(monkeypatch, tmp_path):
    """The script reads settings.json beside itself: a backend writing its settings anywhere
    else would restart onto the old address."""
    live_like(monkeypatch)
    monkeypatch.setattr(core, "DB_PATH", str(tmp_path / "t.db"))
    assert app.can_restart() is False


def test_mcp_stays_on_this_computer_while_the_board_answers_the_lan(db):
    """With the LAN setting on, the backend binds 0.0.0.0. /mcp still refuses a request
    addressed to a LAN IP (the SDK's DNS-rebinding guard), while the board's API answers."""
    headers = {"Accept": "application/json, text/event-stream",
               "Content-Type": "application/json"}
    with TestClient(app.mcp.streamable_http_app(), base_url="http://192.168.1.5:8123") as c:
        r = c.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "test", "version": "1"}}})
        assert r.status_code == 421, r.text
        assert c.get("/api/cards").status_code == 200
        assert c.get("/").status_code == 200, "the board page itself"


def test_lan_addresses_leave_out_loopback_link_local_and_duplicates(monkeypatch):
    infos = [(2, 1, 6, "", (a, 0)) for a in
             ["127.0.0.1", "192.168.1.5", "169.254.3.4", "10.0.0.7", "192.168.1.5"]]
    monkeypatch.setattr(app.socket, "getaddrinfo", lambda *a, **k: infos)

    class Routed:   # the UDP connect that finds the default route's address
        def __init__(self, *a): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def connect(self, addr): pass
        def getsockname(self): return ("172.16.0.9", 5000)

    monkeypatch.setattr(app.socket, "socket", Routed)
    assert app.lan_addresses() == ["172.16.0.9", "10.0.0.7", "192.168.1.5"], \
        "the default route's address first, then the rest"


def test_a_default_route_address_also_found_by_name_is_listed_once(monkeypatch):
    monkeypatch.setattr(app.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("192.168.1.5", 0))])

    class Routed:
        def __init__(self, *a): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def connect(self, addr): pass
        def getsockname(self): return ("192.168.1.5", 5000)

    monkeypatch.setattr(app.socket, "socket", Routed)
    assert app.lan_addresses() == ["192.168.1.5"]


def test_lan_addresses_survive_a_machine_with_no_network(monkeypatch):
    def fail(*a, **k):
        raise OSError("no network")
    monkeypatch.setattr(app.socket, "getaddrinfo", fail)
    monkeypatch.setattr(app.socket, "socket", fail)
    assert app.lan_addresses() == []


def test_the_suite_never_runs_the_real_restart(restarts):
    app.restart_backend()   # the conftest guard stands in for it
    assert restarts == [1]


def test_restart_script_reads_the_lan_setting():
    ps = (Path(app.__file__).parent / "restart-backend.ps1").read_text(encoding="utf-8")
    assert "settings.json" in ps and "'0.0.0.0'" in ps and "--host, $BindHost" in ps
    assert "--host, 127.0.0.1" not in ps, "the bind is no longer hard-coded"
    assert "falling back to 127.0.0.1" in ps, "a LAN bind that fails still leaves a board"


@pytest.mark.skipif(not shutil.which("powershell"), reason="needs Windows PowerShell")
@pytest.mark.parametrize("text, host", [
    (None, "127.0.0.1"),                       # no file: off by default
    ('{"lan": true}', "0.0.0.0"),
    ('{"lan": false}', "127.0.0.1"),
    ('{"lan": "true"}', "127.0.0.1"),          # only a real JSON true, as core reads it
    ('{"lan": 1}', "127.0.0.1"),
    ("not json {", "127.0.0.1"),               # broken: logged, stays on this computer
    ("", "127.0.0.1"),
])
def test_restart_script_binds_the_lan_only_for_a_real_true(tmp_path, text, host):
    """Runs only the script's bind choice, cut out of it into a temp folder, so the kill and
    start that follow it can never run here. $PSScriptRoot is then that temp folder."""
    ps = (Path(app.__file__).parent / "restart-backend.ps1").read_text(encoding="utf-8")
    start = ps.index("# The board's settings")
    end = ps.index("# (end of the bind choice")
    chunk = ps[start:end]
    assert "Stop-Process" not in chunk and "Start-Process" not in chunk
    (tmp_path / "choose.ps1").write_text(
        'function Log($m) { "LOG $m" }\n' + chunk + "\n\"HOST=$BindHost\"\n", encoding="utf-8")
    if text is not None:
        (tmp_path / "settings.json").write_text(text, encoding="utf-8")
    out = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                          str(tmp_path / "choose.ps1")], capture_output=True, text=True,
                         timeout=60).stdout
    assert f"HOST={host}" in out, out
