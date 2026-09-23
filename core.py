"""Tickets core: schema plus every operation. The only module that touches SQL."""

import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

LANES = ["todo", "plan", "develop", "test", "verify", "done"]
LINK_KINDS = ["parent", "blocks"]

DB_PATH = "tickets.db"  # reassign core.DB_PATH to point elsewhere (tests, alt board)

CARD_FIELDS = ("project", "title", "description", "lane", "assignee", "pos", "labels", "checklist",
               "archived", "plan", "questions", "answers", "merged", "deployed")
# merged: on the base branch on origin, set by the agent's deploy step from git.
# deployed: on the local test backend or a device, set here from the deploy comment.
BOOL_FIELDS = ("archived", "merged", "deployed")   # stored as 0/1, surfaced as true/false
# columns of the retired board agent: still created and added to an older tickets.db so it
# opens, but never read, written or returned
LEGACY_COLUMNS = ("auto_advance", "attention")
JSON_FIELDS = ("labels", "checklist")
# the planning round, kept apart from the request in `description`: the agent's plan, its
# open questions, a person's answers
PLAN_FIELDS = ("plan", "questions", "answers")
PROJECT_FIELDS = ("name", "path", "instructions", "color")
# Where a deleted project's cards go. Agents never work a card in it; matched ignoring case.
NO_PROJECT = "No Project"
NO_PROJECT_COLOR = "#6b778c"
# a new project takes the least-used of these; any #rrggbb can be set instead
PALETTE = ["#0052cc", "#00875a", "#ff991f", "#6554c0", "#de350b", "#00a3bf", "#c9372c",
           "#5e4db2", "#b65c02", "#216e4e"]
# lane/assignee get their own event kind; everything else is an `edited`
EVENT_KIND = {"lane": "moved", "assignee": "assigned", "archived": "archived"}
AGENT = "claude-agent"   # the agent's name: only its deploy comment moves the deployed dot
# how the agent's deploy comment starts: the deployed dot follows it, see `comment`
DEPLOYED_HEAD, SKIPPED_HEAD, FAILED_HEAD = "deployed: ", "deploy skipped: ", "deploy failed: "
DEPLOY_HEADS = (DEPLOYED_HEAD, SKIPPED_HEAD, FAILED_HEAD)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY,
    project TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    lane TEXT NOT NULL,
    assignee TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    pos REAL NOT NULL DEFAULT 0,
    labels TEXT NOT NULL DEFAULT '[]',
    checklist TEXT NOT NULL DEFAULT '[]',
    archived INTEGER NOT NULL DEFAULT 0,
    auto_advance INTEGER NOT NULL DEFAULT 0,
    attention INTEGER NOT NULL DEFAULT 0,
    merged INTEGER NOT NULL DEFAULT 0,
    deployed INTEGER NOT NULL DEFAULT 0,
    plan TEXT NOT NULL DEFAULT '',
    questions TEXT NOT NULL DEFAULT '',
    answers TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    card_id INTEGER NOT NULL REFERENCES cards(id),
    actor TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS links (
    from_id INTEGER NOT NULL REFERENCES cards(id),
    to_id INTEGER NOT NULL REFERENCES cards(id),
    kind TEXT NOT NULL,
    PRIMARY KEY (from_id, to_id, kind)
);
CREATE TABLE IF NOT EXISTS projects (
    name TEXT PRIMARY KEY COLLATE NOCASE,
    path TEXT NOT NULL DEFAULT '',
    instructions TEXT NOT NULL DEFAULT '',
    color TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS activity (
    actor TEXT PRIMARY KEY,
    card_id INTEGER NOT NULL REFERENCES cards(id),
    doing TEXT NOT NULL,
    since TEXT NOT NULL
);
"""


class NotFound(Exception):
    """Unknown card id or project name. Distinct from ValueError so HTTP maps it to 404."""


def connect():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA)
    # columns added after the live board already held real cards: add them to an older
    # tickets.db rather than make the user delete the file
    have = {r["name"] for r in db.execute("PRAGMA table_info(cards)")}
    for col in BOOL_FIELDS + LEGACY_COLUMNS:
        if col not in have:
            db.execute(f"ALTER TABLE cards ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
    for col in PLAN_FIELDS:
        if col not in have:
            db.execute(f"ALTER TABLE cards ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
    # free ordering replaced `priority` (#58): rank the existing cards by what the board
    # used to show them in, so nothing jumps the first time it loads
    if "pos" not in have:
        db.execute("ALTER TABLE cards ADD COLUMN pos REAL NOT NULL DEFAULT 0")
        db.execute("UPDATE cards SET pos = (SELECT COUNT(*) FROM cards x"
                   " WHERE x.updated_at > cards.updated_at"
                   " OR (x.updated_at = cards.updated_at AND x.id > cards.id))")
    if "priority" in have:
        db.execute("ALTER TABLE cards DROP COLUMN priority")
    # Every card names a configured project. A database from before projects has cards
    # naming projects that were only strings, so give each one a row. A fresh database has
    # no projects at all: a card cannot be made until someone configures one. Checked
    # first so a plain read does not take the write lock.
    orphan = db.execute("SELECT 1 FROM cards c WHERE NOT EXISTS"
                        " (SELECT 1 FROM projects p WHERE p.name = c.project)").fetchone()
    if "color" not in {r["name"] for r in db.execute("PRAGMA table_info(projects)")}:
        db.execute("ALTER TABLE projects ADD COLUMN color TEXT NOT NULL DEFAULT ''")
    if orphan:
        with db:
            db.execute("INSERT OR IGNORE INTO projects (name) SELECT DISTINCT project FROM cards")
    # every project has a color: one from before colors, or backfilled above, gets one
    uncolored = [r["name"] for r in
                 db.execute("SELECT name FROM projects WHERE color='' ORDER BY name")]
    if uncolored:
        with db:
            for name in uncolored:
                db.execute("UPDATE projects SET color=? WHERE name=?", (_next_color(db), name))
    return db


def _next_color(db):
    """The palette color fewest projects use, earliest in the palette on a tie."""
    used = [r["color"] for r in db.execute("SELECT color FROM projects")]
    return min(PALETTE, key=lambda c: (used.count(c), PALETTE.index(c)))


def _now():
    return datetime.now(timezone.utc).isoformat()


def _card(row):
    card = dict(row)
    for f in LEGACY_COLUMNS:
        del card[f]
    for f in JSON_FIELDS:
        card[f] = json.loads(card[f])
    for f in BOOL_FIELDS:
        card[f] = bool(card[f])
    return card


def _load(db, id):
    row = db.execute("SELECT * FROM cards WHERE id=?", (id,)).fetchone()
    if row is None:
        raise NotFound(f"no card {id}")
    return _card(row)


def _bottom(db, lane):
    """The free slot under a lane's last card: where a card arriving in the lane lands.
    Lane-wide, not per project — it must be below every card there, whatever the board
    is filtered to."""
    return (db.execute("SELECT MAX(pos) FROM cards WHERE lane=?", (lane,)).fetchone()[0] or 0) + 1


def _event(db, card_id, actor, kind, detail):
    # every mutation funnels through here, so actors self-register: no auth, a user is
    # just a name that has done something
    db.execute("INSERT OR IGNORE INTO users (name) VALUES (?)", (actor,))
    db.execute(
        "INSERT INTO events (card_id, actor, kind, detail, at) VALUES (?,?,?,?,?)",
        (card_id, actor, kind, json.dumps(detail), _now()),
    )


def _validate(fields):
    if "lane" in fields and fields["lane"] not in LANES:
        raise ValueError(f"lane must be one of {LANES}")
    # a position arrives from an HTTP body, so it may be anything; a bool is an int in
    # Python and would slip through
    if "pos" in fields and (isinstance(fields["pos"], bool)
                            or not isinstance(fields["pos"], (int, float))):
        raise ValueError("pos must be a number")
    for f in BOOL_FIELDS:
        if f in fields and not isinstance(fields[f], bool):
            raise ValueError(f"{f} must be true or false")
    for f in PLAN_FIELDS:
        if f in fields and not isinstance(fields[f], str):
            raise ValueError(f"{f} must be text")


def _checklist_events(old, new):
    """One `checked` event per item whose done flipped, plus one `edited` if items moved in or out."""
    was = {i["text"]: i["done"] for i in old}
    now = {i["text"]: i["done"] for i in new}
    evs = [
        ("checked", {"text": t, "done": d}) for t, d in now.items() if t in was and was[t] != d
    ]
    if was.keys() != now.keys():
        evs.append(("edited", {"field": "checklist", "from": list(was), "to": list(now)}))
    return evs


def ensure_user(name):
    """The explicit door into `users`, for a name that has not written anything yet.
    Idempotent; a blank name is bad input, not a silent no-op."""
    name = (name or "").strip()
    if not name:
        raise ValueError("name is required")
    with closing(connect()) as db, db:
        db.execute("INSERT OR IGNORE INTO users (name) VALUES (?)", (name,))
    return name


def list_users():
    with closing(connect()) as db:
        return [r["name"] for r in db.execute("SELECT name FROM users ORDER BY name")]


def rename_user(old, new):
    """One-off: move every trace of `old` to `new` (history, cards, activity, users).
    Idempotent. Not run by connect(): call it by hand on the live tickets.db."""
    old, new = (old or "").strip(), (new or "").strip()
    if not old or not new:
        raise ValueError("both names are required")
    if old == new:
        return
    with closing(connect()) as db, db:
        db.execute("UPDATE events SET actor=? WHERE actor=?", (new, old))
        for f in ("assignee", "created_by"):
            db.execute(f"UPDATE cards SET {f}=? WHERE {f}=?", (new, old))
        for k in ("$.to", "$.from"):
            db.execute("UPDATE events SET detail=json_set(detail, ?, ?)"
                       " WHERE kind='assigned' AND json_extract(detail, ?)=?", (k, new, k, old))
        # activity is one row per actor: if `new` already has one, it is the current one
        db.execute("UPDATE OR IGNORE activity SET actor=? WHERE actor=?", (new, old))
        db.execute("DELETE FROM activity WHERE actor=?", (old,))
        db.execute("INSERT OR IGNORE INTO users (name) VALUES (?)", (new,))
        db.execute("DELETE FROM users WHERE name=?", (old,))


# ---------- activity: what agents are doing right now ----------

def set_activity(actor, card_id=None, doing=""):
    """One row per actor: setting replaces it, card_id=None clears it. Live state for the
    board's status bar, not history, so it writes no event."""
    actor = (actor or "").strip()
    if not actor:
        raise ValueError("actor is required")
    with closing(connect()) as db, db:
        if card_id is None:
            db.execute("DELETE FROM activity WHERE actor=?", (actor,))
        else:
            _load(db, card_id)
            if not (doing or "").strip():
                raise ValueError("doing is required: say what you are doing to the card")
            db.execute("INSERT OR REPLACE INTO activity VALUES (?,?,?,?)",
                       (actor, card_id, doing.strip(), _now()))
    return list_activity()


def list_activity():
    with closing(connect()) as db:
        return [dict(r) for r in db.execute(
            "SELECT a.actor, a.card_id, a.doing, a.since, c.title, c.project, c.lane"
            " FROM activity a JOIN cards c ON c.id = a.card_id ORDER BY a.since")]


# ---------- projects ----------

def _project(db, name):
    """The configured project's own spelling of `name`. Cards may only name a real project."""
    if not name:
        raise ValueError("a card needs a project: create one first (see list_projects)")
    row = db.execute("SELECT name FROM projects WHERE name=?", (name,)).fetchone()
    if row is None:
        raise ValueError(f"unknown project {name!r}: configure it first (see list_projects)")
    return row["name"]


def _load_project(db, name):
    row = db.execute("SELECT name, path, instructions, color FROM projects WHERE name=?",
                     (name,)).fetchone()
    if row is None:
        raise NotFound(f"no project {name!r}")
    return dict(row)


def _clean_name(name):
    name = (name or "").strip()
    if not name:
        raise ValueError("a project needs a name")
    return name


def _clean_path(path):
    """"" means not configured. Anything else must be an existing folder, given absolutely:
    the agent runs in it, and a relative path would depend on where the server started."""
    path = (path or "").strip()
    if path and not Path(path).is_absolute():
        raise ValueError(f"path must be absolute: {path}")
    if path and not Path(path).is_dir():
        raise ValueError(f"no folder at {path}")
    return path


def _clean_color(color):
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", color or ""):
        raise ValueError(f"color must be #rrggbb, not {color!r}")
    return color.lower()


def list_projects():
    """Configured projects: name, path on disk ("" if unset), free-text agent instructions,
    and the #rrggbb color their cards are tagged with."""
    with closing(connect()) as db:
        return [dict(r) for r in db.execute(
            "SELECT name, path, instructions, color FROM projects ORDER BY name")]


def get_project(name):
    with closing(connect()) as db:
        return _load_project(db, name)


def create_project(name, path="", instructions="", color=""):
    """`color` "" picks the least-used palette color."""
    name, path = _clean_name(name), _clean_path(path)
    color = color and _clean_color(color)
    with closing(connect()) as db, db:
        if db.execute("SELECT 1 FROM projects WHERE name=?", (name,)).fetchone():
            raise ValueError(f"project {name!r} already exists")
        db.execute("INSERT INTO projects (name, path, instructions, color) VALUES (?,?,?,?)",
                   (name, path, instructions or "", color or _next_color(db)))
    return get_project(name)


def update_project(name, /, **fields):   # positional-only, so name= in fields is a rename
    """Change a project's name, path, instructions or color. A rename carries every card with it
    (archived ones too) and writes no card events: the cards did not change, the name did."""
    unknown = set(fields) - set(PROJECT_FIELDS)
    if unknown:
        raise ValueError(f"unknown field(s): {sorted(unknown)}")
    if "path" in fields:
        fields["path"] = _clean_path(fields["path"])
    if "instructions" in fields:
        fields["instructions"] = fields["instructions"] or ""
    if "color" in fields:
        fields["color"] = _clean_color(fields["color"])
    with closing(connect()) as db, db:
        old = _load_project(db, name)
        if "name" in fields:
            new = fields["name"] = _clean_name(fields["name"])
            clash = db.execute("SELECT name FROM projects WHERE name=?", (new,)).fetchone()
            if clash and clash["name"] != old["name"]:
                raise ValueError(f"project {new!r} already exists")
            db.execute("UPDATE cards SET project=? WHERE project=? COLLATE NOCASE",
                       (new, old["name"]))
        if fields:
            db.execute(f"UPDATE projects SET {', '.join(f'{f}=?' for f in fields)} WHERE name=?",
                       [*fields.values(), old["name"]])
    return get_project(fields.get("name", old["name"]))


def delete_project(name):
    """Delete a project. Its cards, archived ones too, move to NO_PROJECT (created on first
    use), where agents leave them alone. Like a rename, the move writes no card events.
    NO_PROJECT itself cannot be deleted: its cards would have nowhere to go."""
    with closing(connect()) as db, db:
        old = _load_project(db, name)["name"]
        if old.lower() == NO_PROJECT.lower():
            raise ValueError(f"{NO_PROJECT!r} holds the cards of deleted projects and cannot be deleted")
        n = db.execute("SELECT COUNT(*) FROM cards WHERE project=? COLLATE NOCASE",
                       (old,)).fetchone()[0]
        if n:
            db.execute("INSERT OR IGNORE INTO projects (name, color) VALUES (?,?)",
                       (NO_PROJECT, NO_PROJECT_COLOR))
            home = db.execute("SELECT name FROM projects WHERE name=?", (NO_PROJECT,)).fetchone()
            db.execute("UPDATE cards SET project=? WHERE project=? COLLATE NOCASE",
                       (home["name"], old))
        db.execute("DELETE FROM projects WHERE name=?", (old,))
    return {"deleted": old, "moved": n}


BOARD = "board"   # author of the cards the board opens by itself
FOLDER = """The project has no folder set, so an agent cannot check out a card's branch.

Set its path under "projects…" on the board: the absolute path of a git checkout that has
an `origin` remote (the test lane pushes the card's branch there). Archive this card once
it is set."""
TESTS = """The project's instructions do not say how to test it.

An agent must run the project's tests before it may pass a card, so write the test command
into the instructions under "projects…" on the board. Archive this card once they do."""
DEPLOY = """The project's instructions do not say how to deploy it.

The agent is told to follow the project instructions on how to deploy, so write the deploy
steps into the instructions under "projects…" on the board. Archive this card once they do."""


def setup_cards(name):
    """Cards for what a new project still needs before an agent can work a card in it: a
    folder, and instructions covering tests and deploying. Unassigned, in `todo` -- a
    person has to fix them: agents cannot configure a project.
    ponytail: "the instructions cover tests/deploying" is a word match. It asks once, on a
    new project, and a wrong guess costs one archived card."""
    p = get_project(name)          # NotFound for an unknown name; gives the stored spelling
    said = p["instructions"].lower()
    missing = []
    if not p["path"]:
        missing.append((f"Set {p['name']}'s folder", FOLDER))
    if "test" not in said:
        missing.append((f"Tell the agent how to test {p['name']}", TESTS))
    if "deploy" not in said:
        missing.append((f"Tell the agent how to deploy {p['name']}", DEPLOY))
    return [create_card(t, actor=BOARD, description=d, project=p["name"])
            for t, d in missing]


def create_card(
    title,
    actor,
    description="",
    lane="todo",
    assignee=None,
    labels=None,
    checklist=None,
    project=None,   # required in practice: _project refuses a missing one with a reason
):
    _validate({"lane": lane})
    now = _now()
    with closing(connect()) as db, db:
        project = _project(db, project)
        id = db.execute(
            "INSERT INTO cards (project, title, description, lane, assignee, created_by,"
            " created_at, updated_at, pos, labels, checklist)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                project, title, description, lane, assignee, actor, now, now,
                _bottom(db, lane),
                json.dumps(labels or []), json.dumps(checklist or []),
            ),
        ).lastrowid
        _event(db, id, actor, "created", {"title": title, "lane": lane})
        if assignee:
            db.execute("INSERT OR IGNORE INTO users (name) VALUES (?)", (assignee,))
    return get_card(id)


def get_card(id):
    with closing(connect()) as db:
        card = _load(db, id)
        card["events"] = [
            {**dict(r), "detail": json.loads(r["detail"])}
            for r in db.execute("SELECT * FROM events WHERE card_id=? ORDER BY id", (id,))
        ]
        card["links"] = [
            dict(r)
            for r in db.execute("SELECT * FROM links WHERE from_id=? OR to_id=?", (id, id))
        ]
    return card


def list_cards(lane=None, assignee=None, label=None, project=None, archived=False):
    """In the board's own order — each lane top card first. Archived cards are off the
    board: listed only when asked for with archived=True."""
    where, args = ["archived=?"], [int(archived)]
    if lane is not None:
        where.append("lane=?")
        args.append(lane)
    if assignee is not None:
        where.append("assignee=?")
        args.append(assignee)
    if project is not None:
        where.append("project=? COLLATE NOCASE")  # stored casing displays, matching ignores it
        args.append(project)
    sql = "SELECT * FROM cards WHERE " + " AND ".join(where)
    with closing(connect()) as db:
        cards = [_card(r) for r in db.execute(sql + " ORDER BY pos, id", args)]
    # ponytail: label filter scans in Python — fine to a few thousand cards.
    # Past that, index labels with json1 (json_each) and push it into the WHERE clause.
    return [c for c in cards if label is None or label in c["labels"]]


def number(questions):
    """Questions for a person, as a list numbered from 1 whoever wrote them: top-level
    bullets and numbers are renumbered in order; indented lines and other text are kept."""
    n, out = 0, []
    for ln in questions.splitlines():
        m = re.match(r"(?:[-*+]|\d+[.)])\s+(.*)", ln)
        if m:
            n += 1
            ln = f"{n}. {m[1]}"
        out.append(ln)
    return "\n".join(out)


def update_card(id, actor, **fields):
    """The single write path for card mutations: diff, write, one event per changed field."""
    unknown = set(fields) - set(CARD_FIELDS)
    if unknown:
        raise ValueError(f"unknown field(s): {sorted(unknown)}")
    _validate(fields)
    with closing(connect()) as db, db:
        old = _load(db, id)
        if "project" in fields:
            fields["project"] = _project(db, fields["project"])
        if isinstance(fields.get("questions"), str):   # every writer's questions: 1. 2. 3.
            fields["questions"] = number(fields["questions"])
        frm, to = LANES.index(old["lane"]), LANES.index(fields.get("lane", old["lane"]))
        # a card arriving in a lane queues behind the ones already there, unless the
        # writer said where it goes (the board's drag does; an agent's move does not)
        if to != frm:
            fields.setdefault("pos", _bottom(db, LANES[to]))
        # back into plan, develop or test is rework: the new work is neither on master
        # nor deployed
        work = ("plan", "develop", "test")
        if to != frm and LANES[to] in work and LANES[frm] not in work:
            fields.setdefault("merged", False)
            fields.setdefault("deployed", False)
        sets, args, evs = [], [], []
        for f, new in fields.items():
            if new == old[f]:
                continue
            sets.append(f"{f}=?")
            args.append(json.dumps(new) if f in JSON_FIELDS else new)
            if f == "pos":
                continue   # a place in the queue is not history
            if f == "checklist":
                evs += _checklist_events(old[f], new)
            else:
                evs.append((EVENT_KIND.get(f, "edited"), {"field": f, "from": old[f], "to": new}))
        if sets:
            db.execute(
                f"UPDATE cards SET {', '.join(sets)}, updated_at=? WHERE id=?", args + [_now(), id]
            )
            for kind, detail in evs:
                _event(db, id, actor, kind, detail)
            if fields.get("assignee"):   # an agent assigning itself needs no create_user first
                db.execute("INSERT OR IGNORE INTO users (name) VALUES (?)", (fields["assignee"],))
    return get_card(id)


def comment(id, actor, text, output=""):
    """`output` is the CLI transcript of the run this comment reports, if it has one: it
    rides along on the event, so every run keeps its own, and the board shows it in a box
    that starts closed. Nothing to show -> the key is not there at all."""
    with closing(connect()) as db, db:
        _load(db, id)
        _event(db, id, actor, "comment", {"text": text, **({"output": output} if output else {})})
    # the deployed dot follows the deploy comment, so it is right whoever wrote the deploy
    # step: #41's dots never reached the board through 59 deploys, because the one agent
    # process running them all was older than the code that set them
    if actor == AGENT and text.startswith(DEPLOY_HEADS):
        return update_card(id, actor, deployed=text.startswith(DEPLOYED_HEAD))
    return get_card(id)


def _check_kind(kind):
    if kind not in LINK_KINDS:
        raise ValueError(f"kind must be one of {LINK_KINDS}")


def link_cards(from_id, to_id, kind, actor):
    """Idempotent: re-linking an existing pair is a silent no-op, like a no-op update."""
    if from_id == to_id:
        raise ValueError("a card cannot link to itself")
    _check_kind(kind)
    with closing(connect()) as db, db:
        _load(db, from_id)
        _load(db, to_id)
        added = db.execute("INSERT OR IGNORE INTO links (from_id, to_id, kind) VALUES (?,?,?)",
                           (from_id, to_id, kind)).rowcount
        if added:
            _event(db, from_id, actor, "linked", {"to": to_id, "kind": kind})
    return get_card(from_id)


def unlink_cards(from_id, to_id, kind, actor):
    """Idempotent: a link already gone is a no-op, but an unknown kind is bad input."""
    _check_kind(kind)
    with closing(connect()) as db, db:
        _load(db, from_id)
        _load(db, to_id)
        gone = db.execute(
            "DELETE FROM links WHERE from_id=? AND to_id=? AND kind=?", (from_id, to_id, kind)
        ).rowcount
        if gone:
            _event(db, from_id, actor, "unlinked", {"to": to_id, "kind": kind})
    return get_card(from_id)
