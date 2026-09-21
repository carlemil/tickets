"""MCP tools and HTTP routes. Both are thin wrappers over core — no SQL lives here."""

import functools
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse

import core

mcp = MCPServer("tickets")
BOARD = Path(__file__).parent / "board.html"
DOCS = Path(__file__).parent / "docs.html"


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


@route("/docs", methods=["GET"])
async def docs(request):
    return FileResponse(DOCS)


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


@route("/api/activity", methods=["GET"])
async def api_activity(request):
    return JSONResponse(core.list_activity())


@route("/api/projects", methods=["GET"])
async def api_projects(request):
    return JSONResponse(core.list_projects())


# Project config is not card activity, so these writes carry no `actor` and log no event.
@route("/api/projects", methods=["POST"])
async def api_create_project(request):
    return JSONResponse(core.create_project(**await request.json()), status_code=201)


@route("/api/projects/{name:path}", methods=["DELETE"])
async def api_delete_project(request):
    return JSONResponse(core.delete_project(request.path_params["name"]))


@route("/api/projects/{name:path}", methods=["PATCH"])
async def api_update_project(request):
    return JSONResponse(core.update_project(request.path_params["name"],
                                            **await request.json()))


@tool
def list_cards(project: str | None = None, lane: str | None = None,
               assignee: str | None = None, label: str | None = None,
               archived: bool = False) -> list[dict]:
    """List cards in the board's own order — each lane's top card first, which is the
    order to take them in. Filters combine; omit one to ignore it.

    Lanes in order: todo -> plan -> develop -> test -> verify -> done.
    `project` scopes the board and is matched case-insensitively; work is grouped by
    project (see list_projects). `label` matches one label on a card. Archived cards are hidden; pass archived=True to list only those.
    Cards come back without their activity log — use get_card for that.
    """
    return core.list_cards(lane=lane, assignee=assignee, label=label, project=project,
                           archived=archived)


@tool
def get_card(id: int) -> dict:
    """Get one card with its full `events` activity log (chronological) and its `links`.

    What a card's text fields hold: `description` is the request, what a person wants done.
    `plan` is the implementation plan, `questions` the plan's open questions for a person,
    `answers` that person's answers to them. Build from `plan` and `answers`, not from
    questions a person has already answered.
    """
    return core.get_card(id)


@tool
def create_card(title: str, actor: str, project: str, description: str = "",
                lane: str = core.LANES[0], assignee: str | None = None,
                labels: list[str] | None = None, checklist: list[dict] | None = None,
                auto_advance: bool = False) -> dict:
    """Create a card. `actor` is you: pass your own agent name, it is recorded as the author.

    `lane` is one of todo -> plan -> develop -> test -> verify -> done, and normally starts
    at todo; the card lands at the bottom of it. `project` is required: the project the
    work belongs to. It must be a configured project (list_projects); an unknown name is an
    error, not a new project, and if there are none, ask a person to create one on the
    board — agents do not configure projects.
    `description` is the request: what needs doing and why. A plan, its open questions and
    their answers have fields of their own, set later with update_card.
    `checklist` items are {"text": str, "done": bool}. `assignee` is a person's name.
    `auto_advance=True` lets the board agent carry the card plan -> develop -> test ->
    verify on its own once it reaches plan.
    """
    return core.create_card(title=title, actor=actor, description=description, lane=lane,
                            assignee=assignee, labels=labels,
                            checklist=checklist, project=project, auto_advance=auto_advance)


@tool
def update_card(id: int, actor: str, title: str | None = None, description: str | None = None,
                lane: str | None = None, assignee: str | None = None,
                labels: list[str] | None = None, checklist: list[dict] | None = None,
                project: str | None = None, archived: bool | None = None,
                auto_advance: bool | None = None, attention: bool | None = None,
                plan: str | None = None, questions: str | None = None,
                answers: str | None = None, merged: bool | None = None,
                deployed: bool | None = None) -> dict:
    """Change a card: this is how you move it between lanes, assign it, and tick checklist items.

    `actor` is you — every change is logged under that name. Pass only the fields you are
    changing; omitted fields are left alone, and a field passed unchanged records nothing.

    `lane` moves the card, in order todo -> plan -> develop -> test -> verify -> done.
    It lands at the bottom of the lane it arrives in, behind the cards already waiting there.
    Moving a card forward (except into done), or back into plan, also turns auto_advance
    on, so the board agent takes it up; pass auto_advance=False (or its current value) in
    the same call to move it without that.
    A card in verify is finished work, so a person's update to its `description`,
    `answers` or `checklist` (or a comment on it) is a new request: the card goes back
    to plan, assigned to the agent, to be planned and built again. Pass `lane` in the
    same call to write to a verify card without that.
    `assignee` assigns it to a person or an agent, by name; pass "" to unassign. A name
    not yet on the board is registered as a user by assigning it. When you start work on a
    card, assign it to yourself so the board shows who is on it; when you are done, give it
    back to whoever had it before (or "" if nobody did).
    `checklist` REPLACES the whole list, so send every item back, not just the one you
    ticked: get_card first, flip the `done` you want, send the full list. `labels` likewise
    replaces the whole list. `archived=True` takes the card off the board without deleting
    it; `archived=False` puts it back. `auto_advance` switches the board agent's hands-off
    plan -> develop -> test -> verify run on or off.
    `attention=True` puts a dot on the card meaning "waiting for your input": set it when
    you hand the card back to a person. Any later change or comment clears it.
    `merged` (the card's branch is on the base branch on origin) and `deployed` (deployed
    to the local test backend or a device) are the board agent's to set after a deploy.

    Where text goes — each field REPLACES what is there: `description` is the request,
    and rewriting it on a verified card asks for the work again (see `lane`);
    leave it to the person who asked, do not put a plan in it. `plan` is the
    implementation plan (markdown), the planning agent's to write. `questions` holds only
    what a person must decide before the work can go on, as a list numbered from 1
    (1. 2. 3.; bullets are renumbered so); pass ""
    once none are open. `answers` is the person's reply to `questions`: read it, do not
    write it. Results of development or testing go in a comment, not in these fields.
    """
    fields = {k: v for k, v in dict(
        title=title, description=description, lane=lane, assignee=assignee,
        labels=labels, checklist=checklist, project=project, archived=archived,
        auto_advance=auto_advance, attention=attention, plan=plan, questions=questions,
        answers=answers, merged=merged, deployed=deployed).items()
        if v is not None}
    if fields.get("assignee") == "":
        fields["assignee"] = None   # None already means "not passed", so "" is how you unassign
    return core.update_card(id, actor, **fields)


@tool
def list_projects() -> list[dict]:
    """List the configured projects. Every card belongs to one of these.

    Each has a `name` (what cards and the `project` filters use, matched ignoring case),
    a `path` — the project's folder on this machine, "" if not set — and free-text
    `instructions`: how the humans want agents to work on that project. Read them before
    working a card, and follow them.

    "No Project" holds the cards of deleted projects: do not work on cards in it.
    """
    return core.list_projects()


@tool
def create_user(name: str) -> str:
    """Register a person or agent by name so they appear on the board before writing anything.

    You do not need this to work: any `actor` you pass to another tool registers itself on
    its first write. Use it to put a name on the board ahead of time — your own agent name
    so a human can assign you work, or a teammate you are about to assign a card to.
    Assigning a card to a new name (update_card) registers it too.
    Registering a name that already exists does nothing. Returns the name as stored.
    """
    return core.ensure_user(name)


@tool
def set_activity(actor: str, card_id: int | None = None, doing: str = "") -> list[dict]:
    """Show on the board's status bar what you are doing right now, e.g. card_id=12,
    doing="planning". `actor` is you; you have one entry, and setting it replaces it.
    Call it again with no card_id when you stop, so the board does not show you working
    on something you finished. Writes nothing to the card's activity log. Returns every
    agent's current entry.
    """
    return core.set_activity(actor, card_id, doing)


@tool
def comment(id: int, actor: str, text: str) -> dict:
    """Add a comment to a card's activity log. `actor` is you — it is who the comment is from.

    A person's comment on a card in verify asks for the work again: the card goes back to
    plan, assigned to the agent. The agent's own comments (its deploy result) do not."""
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
