"""MCP tools and HTTP routes. Both are thin wrappers over core — no SQL lives here."""

import functools
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse

import core

mcp = MCPServer("tickets")
BOARD = Path(__file__).parent / "board.html"


def route(path, methods):
    """Register an HTTP route, mapping core's exceptions to status codes in one place."""

    def wrap(fn):
        @mcp.custom_route(path, methods=methods)
        @functools.wraps(fn)
        async def handler(request):
            try:
                return await fn(request)
            except core.NotFound as e:
                return JSONResponse({"error": str(e)}, status_code=404)
            except (ValueError, TypeError) as e:  # bad JSON and bad field names land here too
                return JSONResponse({"error": str(e)}, status_code=400)

        return handler

    return wrap


def tool(fn):
    """Register an MCP tool. Core's errors become ToolError, so the agent is told what it got
    wrong; anything else stays a crash the SDK masks and logs."""

    @mcp.tool()
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (core.NotFound, ValueError) as e:
            raise ToolError(str(e)) from e

    return wrapper


async def body(request):
    data = await request.json()
    if not data.get("actor"):
        raise ValueError("actor is required on every write")
    return data


@route("/", methods=["GET"])
async def index(request):
    return FileResponse(BOARD) if BOARD.exists() else PlainTextResponse("board.html not built yet")


@route("/api/cards", methods=["GET"])
async def api_list_cards(request):
    q = request.query_params
    return JSONResponse(core.list_cards(lane=q.get("lane"), assignee=q.get("assignee"),
                                        label=q.get("label"), project=q.get("project"),
                                        archived=q.get("archived") == "1"))


@route("/api/cards", methods=["POST"])
async def api_create_card(request):
    return JSONResponse(core.create_card(**await body(request)), status_code=201)


@route("/api/cards/{id:int}", methods=["GET"])
async def api_get_card(request):
    return JSONResponse(core.get_card(request.path_params["id"]))


@route("/api/cards/{id:int}", methods=["PATCH"])
async def api_update_card(request):
    return JSONResponse(core.update_card(request.path_params["id"], **await body(request)))


@route("/api/cards/{id:int}/comment", methods=["POST"])
async def api_comment(request):
    return JSONResponse(core.comment(request.path_params["id"], **await body(request)))


@route("/api/links", methods=["POST"])
async def api_link(request):
    return JSONResponse(core.link_cards(**await body(request)))


@route("/api/links", methods=["DELETE"])
async def api_unlink(request):
    return JSONResponse(core.unlink_cards(**await body(request)))


@route("/api/users", methods=["GET"])
async def api_users(request):
    return JSONResponse(core.list_users())


@route("/api/users", methods=["POST"])
async def api_create_user(request):
    # no `actor` here: registering a name is the one write that predates having one
    return JSONResponse({"name": core.ensure_user((await request.json()).get("name"))},
                        status_code=201)


@route("/api/projects", methods=["GET"])
async def api_projects(request):
    return JSONResponse(core.list_projects())


@tool
def list_cards(project: str | None = None, lane: str | None = None,
               assignee: str | None = None, label: str | None = None,
               archived: bool = False) -> list[dict]:
    """List cards, most recently changed first. Filters combine; omit one to ignore it.

    Lanes in order: todo -> plan -> develop -> test -> verify -> done.
    `project` scopes the board and is matched case-insensitively; work is grouped by
    project and new cards land in "inbox" unless told otherwise. `label` matches one
    label on a card. Archived cards are hidden; pass archived=True to list only those.
    Cards come back without their activity log — use get_card for that.
    """
    return core.list_cards(lane=lane, assignee=assignee, label=label, project=project,
                           archived=archived)


@tool
def get_card(id: int) -> dict:
    """Get one card with its full `events` activity log (chronological) and its `links`."""
    return core.get_card(id)


@tool
def create_card(title: str, actor: str, description: str = "", lane: str = core.LANES[0],
                assignee: str | None = None, priority: str = "med",
                labels: list[str] | None = None, checklist: list[dict] | None = None,
                project: str = core.DEFAULT_PROJECT) -> dict:
    """Create a card. `actor` is you: pass your own agent name, it is recorded as the author.

    `lane` is one of todo -> plan -> develop -> test -> verify -> done, and normally starts
    at todo. `priority` is low, med or high. `project` groups related work and defaults to
    "inbox" — pass the project you are working on so the card is not orphaned.
    `checklist` items are {"text": str, "done": bool}. `assignee` is a person's name.
    """
    return core.create_card(title=title, actor=actor, description=description, lane=lane,
                            assignee=assignee, priority=priority, labels=labels,
                            checklist=checklist, project=project)


@tool
def update_card(id: int, actor: str, title: str | None = None, description: str | None = None,
                lane: str | None = None, assignee: str | None = None, priority: str | None = None,
                labels: list[str] | None = None, checklist: list[dict] | None = None,
                project: str | None = None, archived: bool | None = None) -> dict:
    """Change a card: this is how you move it between lanes, assign it, and tick checklist items.

    `actor` is you — every change is logged under that name. Pass only the fields you are
    changing; omitted fields are left alone, and a field passed unchanged records nothing.

    `lane` moves the card, in order todo -> plan -> develop -> test -> verify -> done.
    `assignee` assigns it to a person; pass "" to unassign. `priority` is low, med or high.
    `checklist` REPLACES the whole list, so send every item back, not just the one you
    ticked: get_card first, flip the `done` you want, send the full list. `labels` likewise
    replaces the whole list. `archived=True` takes the card off the board without deleting
    it; `archived=False` puts it back.
    """
    fields = {k: v for k, v in dict(
        title=title, description=description, lane=lane, assignee=assignee, priority=priority,
        labels=labels, checklist=checklist, project=project, archived=archived).items()
        if v is not None}
    if fields.get("assignee") == "":
        fields["assignee"] = None   # None already means "not passed", so "" is how you unassign
    return core.update_card(id, actor, **fields)


@tool
def create_user(name: str) -> str:
    """Register a person or agent by name so they appear on the board before writing anything.

    You do not need this to work: any `actor` you pass to another tool registers itself on
    its first write. Use it to put a name on the board ahead of time — your own agent name
    so a human can assign you work, or a teammate you are about to assign a card to.
    Registering a name that already exists does nothing. Returns the name as stored.
    """
    return core.ensure_user(name)


@tool
def comment(id: int, actor: str, text: str) -> dict:
    """Add a comment to a card's activity log. `actor` is you — it is who the comment is from."""
    return core.comment(id, actor, text)


@tool
def link_cards(from_id: int, to_id: int, kind: str, actor: str) -> dict:
    """Link two cards. `kind` is "parent" (from_id is the parent of to_id) or "blocks"
    (from_id blocks to_id). Re-linking the same pair does nothing. `actor` is you."""
    return core.link_cards(from_id, to_id, kind, actor)


@tool
def unlink_cards(from_id: int, to_id: int, kind: str, actor: str) -> dict:
    """Remove a link created by link_cards. `kind` is "parent" or "blocks" and must match the
    link you are removing. Removing a link that is not there does nothing. `actor` is you."""
    return core.unlink_cards(from_id, to_id, kind, actor)


# The lane order is spelled out in the docstrings above because they are the agent's only
# manual, and an f-string is not a docstring. This keeps that copy honest if LANES changes.
assert all(" -> ".join(core.LANES) in f.__doc__ for f in (list_cards, create_card, update_card))

app = mcp.streamable_http_app()
