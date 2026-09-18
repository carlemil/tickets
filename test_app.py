"""app.py: only what the wrappers add.

core.py's behaviour is tested in test_core.py and is not re-tested through HTTP. What
lives here is the part app.py owns: status codes, the `actor` requirement, the two
exception mappings, the JSON shapes the board depends on, and the ToolError mapping the
agent sees. Plus one test of the real MCP transport, because that is the only thing a
direct function call cannot prove.
"""

import json

import pytest
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


def test_create_card_returns_201_and_the_full_card(client):
    r = client.post("/api/cards", json={"title": "from http", "actor": "ann"})
    assert r.status_code == 201, r.text
    c = r.json()
    assert c["id"] and c["lane"] == "todo" and c["events"], c


def test_create_user_returns_201_without_an_actor(client):
    # the one write that predates having an actor, so `body()`'s rule must not apply
    r = client.post("/api/users", json={"name": "zoe"})
    assert r.status_code == 201 and r.json() == {"name": "zoe"}, r.text
    assert client.get("/api/users").json() == ["zoe"]


def test_query_params_reach_the_filter(client):
    client.post("/api/cards", json={"title": "a", "actor": "ann", "lane": "plan"})
    client.post("/api/cards", json={"title": "b", "actor": "ann", "lane": "todo"})
    assert [c["title"] for c in client.get("/api/cards?lane=plan").json()] == ["a"]
    assert [c["title"] for c in client.get("/api/cards?assignee=nobody").json()] == []
    assert len(client.get("/api/cards?project=inbox").json()) == 2


def test_unlink_accepts_a_json_body_on_delete(client):
    a = client.post("/api/cards", json={"title": "a", "actor": "ann"}).json()
    b = client.post("/api/cards", json={"title": "b", "actor": "ann"}).json()
    body = {"from_id": a["id"], "to_id": b["id"], "kind": "blocks", "actor": "ann"}
    assert client.post("/api/links", json=body).status_code == 200
    r = client.request("DELETE", "/api/links", json=body)
    assert r.status_code == 200 and r.json()["links"] == [], r.text


def test_projects_route(client):
    client.post("/api/cards", json={"title": "a", "actor": "ann", "project": "Tickets"})
    assert client.get("/api/projects").json() == ["Tickets"]


# ---------- HTTP: the shape the board relies on ----------

def test_list_is_lean_and_a_single_card_is_full(client):
    client.post("/api/cards", json={"title": "a", "actor": "ann"})
    listed = client.get("/api/cards").json()[0]
    assert "events" not in listed and "links" not in listed, "the board's list view stays lean"
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
    c = client.post("/api/cards", json={"title": "a", "actor": "ann"}).json()
    r = client.patch(f"/api/cards/{c['id']}", json={"actor": "ann", "lane": "backlog"})
    assert r.status_code == 400 and "lane must be one of" in r.json()["error"], r.text


def test_unknown_field_is_400_via_the_typeerror_branch(client):
    # core.create_card raises TypeError, not ValueError, for an unexpected keyword
    r = client.post("/api/cards", json={"title": "a", "actor": "ann", "bogus": 1})
    assert r.status_code == 400 and "bogus" in r.json()["error"], r.text


def test_malformed_json_is_400(client):
    r = client.post("/api/cards", content=b"{not json",
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400, r.text


def test_non_integer_id_is_404_from_the_route_converter(client):
    assert client.get("/api/cards/abc").status_code == 404


# ---------- MCP tools: the ToolError mapping ----------

def test_tools_are_plain_callables():
    c = app.create_card(title="x", actor="ann")
    assert isinstance(c, dict) and c["lane"] == "todo", c


def test_notfound_reaches_the_agent_as_a_toolerror():
    with pytest.raises(ToolError, match="no card 999"):
        app.get_card(999)


def test_bad_input_reaches_the_agent_as_a_toolerror():
    with pytest.raises(ToolError, match="lane must be one of"):
        app.create_card(title="x", actor="ann", lane="bogus")


def test_the_wrapper_is_what_maps_the_error():
    with pytest.raises(ToolError):
        app.create_user("")
    with pytest.raises(ValueError):          # same call, wrapper bypassed
        app.create_user.__wrapped__("")


def test_update_card_tool_can_unassign():
    c = app.create_card(title="x", actor="ann", assignee="bob")
    c = app.update_card(id=c["id"], actor="ann", assignee="")
    assert c["assignee"] is None, 'an agent unassigns by passing ""'
    assert [e["kind"] for e in c["events"]].count("assigned") == 1, c["events"]


def test_omitted_fields_are_left_alone():
    c = app.create_card(title="x", actor="ann", assignee="bob", description="keep me")
    c = app.update_card(id=c["id"], actor="ann", lane="plan")
    assert c["assignee"] == "bob" and c["description"] == "keep me", c


def test_every_tool_has_a_real_docstring():
    # the agent's only manual; an f-string would silently not be one
    tools = (app.list_cards, app.get_card, app.create_card, app.update_card,
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
        assert listed == {"list_cards", "get_card", "create_card", "update_card",
                          "create_user", "comment", "link_cards", "unlink_cards"}, listed

        made = call({"method": "tools/call",
                     "params": {"name": "create_card",
                                "arguments": {"title": "over mcp", "actor": "agent"}}})
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
    c = client.post("/api/cards", json={"title": "a", "actor": "ann"}).json()
    client.patch(f"/api/cards/{c['id']}", json={"archived": True, "actor": "ann"})
    assert client.get("/api/cards").json() == []
    assert [x["id"] for x in client.get("/api/cards?archived=1").json()] == [c["id"]]


def test_update_card_tool_can_archive():
    c = app.create_card(title="a", actor="bot")
    assert app.update_card(c["id"], "bot", archived=True)["archived"] is True
    assert app.list_cards() == [] and len(app.list_cards(archived=True)) == 1


@pytest.mark.parametrize("q", ["", "?archived=0", "?archived=", "?archived=true"])
def test_anything_but_archived_1_lists_the_active_board(client, q):
    a = client.post("/api/cards", json={"title": "active", "actor": "ann"}).json()
    b = client.post("/api/cards", json={"title": "gone", "actor": "ann"}).json()
    client.patch(f"/api/cards/{b['id']}", json={"archived": True, "actor": "ann"})
    assert [x["id"] for x in client.get("/api/cards" + q).json()] == [a["id"]]


def test_archived_filter_combines_with_project_over_http(client):
    a = client.post("/api/cards", json={"title": "a", "actor": "ann", "project": "P"}).json()
    client.post("/api/cards", json={"title": "b", "actor": "ann", "project": "Q"})
    client.patch(f"/api/cards/{a['id']}", json={"archived": True, "actor": "ann"})
    got = client.get("/api/cards?archived=1&project=p").json()
    assert [x["id"] for x in got] == [a["id"]], got


def test_an_archived_card_still_opens_by_id(client):
    a = client.post("/api/cards", json={"title": "a", "actor": "ann"}).json()
    client.patch(f"/api/cards/{a['id']}", json={"archived": True, "actor": "ann"})
    r = client.get(f"/api/cards/{a['id']}")
    assert r.status_code == 200 and r.json()["archived"] is True, r.text


@pytest.mark.parametrize("bad", ["yes", 1, None])
def test_non_bool_archived_is_400(client, bad):
    a = client.post("/api/cards", json={"title": "a", "actor": "ann"}).json()
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
    c = app.create_card(title="a", actor="bot")
    app.update_card(c["id"], "bot", archived=True)
    assert app.update_card(c["id"], "bot", lane="plan")["archived"] is True,         "archived=None means not passed, like every other field"
    assert app.update_card(c["id"], "bot", archived=False)["archived"] is False
    assert [x["id"] for x in app.list_cards()] == [c["id"]]


def test_list_cards_tool_combines_archived_with_project():
    a = app.create_card(title="a", actor="bot", project="P")
    app.create_card(title="b", actor="bot", project="Q")
    app.update_card(a["id"], "bot", archived=True)
    assert [x["id"] for x in app.list_cards(project="p", archived=True)] == [a["id"]]
    assert app.list_cards(project="q", archived=True) == []


def test_update_card_tool_rejects_a_non_bool_archived():
    c = app.create_card(title="a", actor="bot")
    with pytest.raises(ToolError, match="archived"):
        app.update_card(c["id"], "bot", archived="yes")
