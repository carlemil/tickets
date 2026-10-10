"""MCP tools and HTTP routes. Both are thin wrappers over core — no SQL lives here."""

import functools
import os
import socket
import subprocess
import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse

import core

mcp = MCPServer("tickets")
BOARD = Path(__file__).parent / "board.html"
DOCS = Path(__file__).parent / "docs.html"
DOCS_CARD = Path(__file__).parent / "docs-card.png"


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


@route("/docs/card.png", methods=["GET"])
async def docs_card(request):   # the annotated sheet in docs.html, made by docs_card.py
    return FileResponse(DOCS_CARD, media_type="image/png")


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


# the board polls this and reloads when it changes
@route("/api/version", methods=["GET"])
async def api_version(request):
    return JSONResponse({"event": core.last_event()})


# the set_activity tool over HTTP, for an agent whose MCP connection failed
@route("/api/activity", methods=["POST"])
async def api_set_activity(request):
    return JSONResponse(core.set_activity(**await body(request)))


@route("/api/projects", methods=["GET"])
async def api_projects(request):
    return JSONResponse(core.list_projects())


# Project config is not card activity, so these writes carry no `actor` and log no event.
@route("/api/projects", methods=["POST"])
async def api_create_project(request):
    p = core.create_project(**await request.json())
    core.setup_cards(p["name"])   # what it still needs, as cards for a person
    return JSONResponse(p, status_code=201)


@route("/api/projects/{name:path}", methods=["DELETE"])
async def api_delete_project(request):
    return JSONResponse(core.delete_project(request.path_params["name"]))


@route("/api/projects/{name:path}", methods=["PATCH"])
async def api_update_project(request):
    return JSONResponse(core.update_project(request.path_params["name"],
                                            **await request.json()))


# ---------- board settings ----------
# Hosting on the LAN means a different bind address, which only a backend restart can
# change: uvicorn binds once. The board backend is always started by restart-backend.ps1,
# which reads settings.json, so the restart is that same script, detached and delayed so
# this response gets out before the kill.
RESTART_SCRIPT = Path(__file__).parent / "restart-backend.ps1"
LOOPBACK = ("127.0.0.1", "localhost", "::1")


RESTART_PORT = 8123   # the port restart-backend.ps1 kills and starts ($PORT there)


def _cli_option(name, argv):
    argv = sys.argv if argv is None else argv
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


def bound_host(argv=None):
    """The --host this process was started with by uvicorn's command line, or None when it
    was not started that way (the tests' in-thread server, a script): then it is unknown."""
    return _cli_option("--host", argv)


def bound_port(argv=None):
    """The --port from uvicorn's command line: uvicorn's 8000 when it is left out, None when
    it is not a number."""
    p = _cli_option("--port", argv)
    try:
        return int(p) if p is not None else 8000
    except ValueError:
        return None


def lan_addresses():
    """This machine's IPv4 addresses another device on the LAN could use, loopback and
    link-local (169.254.*, no DHCP answer) left out. The one the default route leaves from
    comes first: virtual adapters (WSL, Hyper-V) add addresses no other device can reach."""
    primary = None
    try:   # a UDP connect sends nothing, it only picks the route
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            primary = s.getsockname()[0]
    except OSError:
        pass
    found = set()
    try:
        found.update(i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None,
                                                          socket.AF_INET))
    except OSError:
        pass
    usable = lambda a: a and not a.startswith(("127.", "169.254.", "0."))
    first = [primary] if usable(primary) else []
    return first + sorted(a for a in found if usable(a) and a not in first)


def can_restart():
    """Only the backend restart-backend.ps1 manages restarts itself: started by uvicorn's CLI
    (so we know its bind) on the script's port, on Windows, with the script beside it, and
    with its settings.json the one the script reads. Anything else (a worktree's trial
    server on another port, a DB_PATH elsewhere) would have the script kill and replace the
    live board, or bind from a file this backend never wrote."""
    return (bound_host() is not None and bound_port() == RESTART_PORT and os.name == "nt"
            and RESTART_SCRIPT.exists()
            and core.settings_path().resolve().parent == RESTART_SCRIPT.resolve().parent)


def restart_backend():
    """restart-backend.ps1 -Delay 2: it returns at once and restarts the backend 2 s later
    in a detached process, which outlives the one it kills. The suite replaces this."""
    flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
             | subprocess.CREATE_NO_WINDOW)
    subprocess.Popen(["powershell", "-NoProfile", "-File", str(RESTART_SCRIPT), "-Delay", "2"],
                     cwd=RESTART_SCRIPT.parent, creationflags=flags, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)


def settings_view(request):
    """The settings plus what the running backend is doing: `bound` is its --host (None if
    unknown), `listening_lan` whether it answers on the LAN now, `urls` the addresses to open
    from another device, `pending` the setting differs from what is running."""
    s = core.get_settings()
    bound = bound_host()
    port = request.url.port or 8123
    listening_lan = bound is not None and bound not in LOOPBACK
    return {**s, "bound": bound, "port": port, "listening_lan": listening_lan,
            "urls": [f"http://{a}:{port}/" for a in lan_addresses()],
            "pending": bound is not None and listening_lan != s["lan"],
            "can_restart": can_restart()}


# settings_view resolves the host name (lan_addresses), which can block for seconds on a
# machine with broken DNS: off the event loop, so the board's polls and MCP calls go on.
@route("/api/settings", methods=["GET"])
async def api_settings(request):
    return JSONResponse(await run_in_threadpool(settings_view, request))


# Board settings are not card activity: an `actor` is accepted (the board sends one on
# every write) but not needed, and nothing is logged. A restart is started only by a PATCH
# that changes the setting: a repeat (a second tab, a retry) never stacks restarts.
@route("/api/settings", methods=["PATCH"])
async def api_update_settings(request):
    fields = await request.json()
    if not isinstance(fields, dict):
        raise ValueError("send a JSON object of settings")
    fields.pop("actor", None)
    before = core.get_settings()
    changed = core.update_settings(**fields) != before
    view = await run_in_threadpool(settings_view, request)
    view["restarting"] = changed and view["pending"] and view["can_restart"]
    if view["restarting"]:
        restart_backend()
    return JSONResponse(view)


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
                labels: list[str] | None = None, checklist: list[dict] | None = None) -> dict:
    """Create a card. `actor` is you: pass your own agent name, it is recorded as the author.

    `lane` is one of todo -> plan -> develop -> test -> verify -> done, and normally starts
    at todo; the card lands at the bottom of it. `project` is required: the project the
    work belongs to. It must be a configured project (list_projects); an unknown name is an
    error, not a new project, and if there are none, ask a person to create one on the
    board — agents do not configure projects.
    `description` is the request: what needs doing and why. A plan, its open questions and
    their answers have fields of their own, set later with update_card.
    `checklist` items are {"text": str, "done": bool}. `assignee` is a person's name.
    """
    return core.create_card(title=title, actor=actor, description=description, lane=lane,
                            assignee=assignee, labels=labels,
                            checklist=checklist, project=project)


@tool
def update_card(id: int, actor: str, title: str | None = None, description: str | None = None,
                lane: str | None = None, assignee: str | None = None,
                labels: list[str] | None = None, checklist: list[dict] | None = None,
                project: str | None = None, archived: bool | None = None,
                plan: str | None = None, questions: str | None = None,
                answers: str | None = None, merged: bool | None = None,
                deployed: bool | None = None, session: str | None = None,
                pr: str | None = None) -> dict:
    """Change a card: this is how you move it between lanes, assign it, and tick checklist items.

    `actor` is you — every change is logged under that name. Pass only the fields you are
    changing; omitted fields are left alone, and a field passed unchanged records nothing.

    `lane` moves the card, in order todo -> plan -> develop -> test -> verify -> done.
    It lands at the bottom of the lane it arrives in, behind the cards already waiting there.
    `assignee` assigns it to a person or an agent, by name; pass "" to unassign. A name
    not yet on the board is registered as a user by assigning it.
    `checklist` REPLACES the whole list, so send every item back, not just the one you
    ticked: get_card first, flip the `done` you want, send the full list. `labels` likewise
    replaces the whole list. `archived=True` takes the card off the board without deleting
    it; `archived=False` puts it back.
    `merged` (the card's branch is on the base branch on origin) and `deployed` (deployed
    to the local test backend or a device) are the board agent's to set after a deploy.
    `session`: the board agent sets it to its Claude Code session id when it takes a card,
    so a person can reopen the run with `claude --resume`.
    `pr`: the board agent sets it to the card's pull request URL when the project lands by PR.
    A move from todo, verify or done into plan, develop or test (rework) clears `pr`,
    `merged` and `deployed`, unless the same call sets them.

    Where text goes — each field REPLACES what is there: `description` is the request;
    leave it to the person who asked, do not put a plan in it. `plan` is the
    implementation plan (markdown), the planning agent's to write. `questions` holds only
    what a person must decide before the work can go on, as a list numbered from 1
    (1. 2. 3.; bullets are renumbered so); a question with sensible choices lists them
    under it as indented `- ` bullets, the recommended one suffixed ` (recommended)`, and
    the board offers them as radio buttons. Pass ""
    once none are open. `answers` is the person's reply to `questions`: read it, do not
    write it. Results of development or testing go in a comment, not in these fields.
    """
    fields = {k: v for k, v in dict(
        title=title, description=description, lane=lane, assignee=assignee,
        labels=labels, checklist=checklist, project=project, archived=archived,
        plan=plan, questions=questions,
        answers=answers, merged=merged, deployed=deployed, session=session,
        pr=pr).items()
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
    working a card, and follow them. `land` is how a card that passed its tests lands:
    "merge" (merged locally into the base branch) or "pr" (as a pull request).

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
def comment(id: int, actor: str, text: str, output: str = "") -> dict:
    """Add a comment to a card's activity log. `actor` is you — it is who the comment is from.

    `output` is optional: the raw CLI transcript of the run this comment reports, which the
    board shows under the comment in a box that starts closed."""
    return core.comment(id, actor, text, output)


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
