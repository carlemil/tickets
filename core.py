"""Tickets core: schema plus every operation. The only module that touches SQL."""

import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

LANES = ["todo", "plan", "develop", "test", "verify", "done"]
LINK_KINDS = ["parent", "blocks"]

DB_PATH = "tickets.db"  # reassign core.DB_PATH to point elsewhere (tests, alt board)

CARD_FIELDS = ("project", "title", "description", "lane", "assignee", "pos", "labels", "checklist",
               "archived", "plan", "questions", "answers", "merged", "deployed", "session",
               "pr")
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
# session: the id of the Claude Code session that last worked the card (`claude --resume`);
# pr: the card's pull request URL, when its project lands by PR
TEXT_FIELDS = PLAN_FIELDS + ("session", "pr")   # free text, '' when unset
PROJECT_FIELDS = ("name", "path", "instructions", "color", "land")
# how a card that passed its tests lands: merged locally into the base, or as a pull request
LANDS = ("merge", "pr")
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
    answers TEXT NOT NULL DEFAULT '',
    session TEXT NOT NULL DEFAULT '',
    pr TEXT NOT NULL DEFAULT ''
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
    color TEXT NOT NULL DEFAULT '',
    land TEXT NOT NULL DEFAULT 'merge'
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
    for col in TEXT_FIELDS:
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
    # Only live cards count: a deleted project's cards are all archived and stay under its
    # name without a row (#437).
    have = {r["name"] for r in db.execute("PRAGMA table_info(projects)")}
    if "color" not in have:
        db.execute("ALTER TABLE projects ADD COLUMN color TEXT NOT NULL DEFAULT ''")
    if "land" not in have:
        db.execute("ALTER TABLE projects ADD COLUMN land TEXT NOT NULL DEFAULT 'merge'")
    # #437: the "No Project" parking project is gone. Once per database (user_version 0 -> 1):
    # its cards are archived, into Tickets when that project exists, else left under the
    # old name, and the row is removed. A project of that name made later is left alone.
    # Until then every card's project had a row, archived cards' too: a database from
    # before projects gets those rows first, so its archived-only projects are not taken
    # for deleted ones.
    if db.execute("PRAGMA user_version").fetchone()[0] < 1:
        with db:
            db.execute("INSERT OR IGNORE INTO projects (name) SELECT DISTINCT project FROM cards"
                       " WHERE project <> 'No Project' COLLATE NOCASE")
            if db.execute("SELECT 1 FROM projects WHERE name='No Project' COLLATE NOCASE").fetchone():
                home = db.execute("SELECT name FROM projects WHERE name='Tickets' COLLATE NOCASE"
                                  ).fetchone()
                db.execute("UPDATE cards SET archived=1 WHERE project='No Project' COLLATE NOCASE")
                if home:
                    db.execute("UPDATE cards SET project=? WHERE project='No Project' COLLATE NOCASE",
                               (home["name"],))
                db.execute("DELETE FROM projects WHERE name='No Project' COLLATE NOCASE")
            db.execute("PRAGMA user_version=1")
    orphan = db.execute("SELECT 1 FROM cards c WHERE c.archived=0 AND NOT EXISTS"
                        " (SELECT 1 FROM projects p WHERE p.name = c.project)").fetchone()
    if orphan:
        with db:
            db.execute("INSERT OR IGNORE INTO projects (name) SELECT DISTINCT project FROM cards"
                       " WHERE archived=0")
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


# #493: a card in a lane the run works, assigned to the run's agent, quiet this long is stalled
STALL_MINUTES = 30
STALL_LANES = ("plan", "develop", "test")
STALL_ASSIGNEE = "claude-agent"


def _ago(minutes, now=None):
    return ((now or datetime.now(timezone.utc)) - timedelta(minutes=minutes)).isoformat()


def _expire_activity(db):
    """Activity is live state: a row older than STALL_MINUTES belongs to a dead agent. Only a
    write (set_activity) deletes; reads filter by the same cutoff, so a read takes no write lock."""
    db.execute("DELETE FROM activity WHERE since < ?", (_ago(STALL_MINUTES),))


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


def _order_check(db, id, old, placed):
    """A card must not sit ahead of a live blocker in its lane (#449), nor a blocker
    behind a card it blocks: the lane order is (pos, id). Run on the written row, inside
    the write's transaction, so liveness is the one after the move (`_blocks`: a blocker
    dragged into verify may be finished, one dragged back out of done blocks again).
    Only cards side by side on one board count: same lane, project and archived view. A
    pair blocking both ways has no order to keep, and a pair already in the wrong order
    before the write (`old`) is left alone. Refused with a ValueError, which rolls the write
    back and which the board answers by putting the card back."""
    me = _load(db, id)
    by, blocking = _blocks(db).get(id, ([], []))
    for other in by + blocking:
        if other in by and other in blocking:
            continue
        blocked = other in by
        if not blocked and not placed:   # a blocker arriving unplaced goes to the bottom
            continue
        o = _load(db, other)
        view = (o["lane"], o["project"], o["archived"])

        def wrong(c):
            ahead = (c["pos"], id) < (o["pos"], other)
            return (c["lane"], c["project"], c["archived"]) == view and ahead == blocked

        if wrong(me) and not wrong(old):
            raise ValueError(f"#{id} is blocked by #{other} and cannot go ahead of it" if blocked
                             else f"#{id} blocks #{other} and cannot go behind it")


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
    for f in TEXT_FIELDS:
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
        _expire_activity(db)
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
    """What the board's dots and status bar show (#498). A live row counts only while its card
    is not archived and not in verify/done (a card the run handed on keeps no dot). A card the run
    holds (claude-agent, work lane, not stalled, not waiting for answers) with no live row of
    its own gets a derived one: the run's single row names one card, so a second card in
    flight, or a stage longer than the row's life, would have no dot. A read, like `_quiet`."""
    lanes = ",".join("?" * len(STALL_LANES))
    with closing(connect()) as db:
        rows = [dict(r) for r in db.execute(
            "SELECT a.actor, a.card_id, a.doing, a.since, c.title, c.project, c.lane"
            " FROM activity a JOIN cards c ON c.id = a.card_id WHERE a.since >= ?"
            " AND c.archived = 0 AND c.lane NOT IN ('verify', 'done') ORDER BY a.since",
            (_ago(STALL_MINUTES),))]
        held = [dict(c) for c in db.execute(
            "SELECT id, title, project, lane, assignee, archived, updated_at, questions, answers"
            f" FROM cards WHERE archived = 0 AND assignee = ? AND lane IN ({lanes}) ORDER BY id",
            (STALL_ASSIGNEE, *STALL_LANES))]
        quiet = _quiet(db) if held else {}   # the whole-events scan only when a card is held
    working = [c for c in held if not _stalled(c, quiet)
               and not ((c["questions"] or "").strip() and not (c["answers"] or "").strip())]
    have = {r["card_id"] for r in rows}
    rows += [{"actor": c["assignee"], "card_id": c["id"], "doing": "working",
              "since": max(c["updated_at"], quiet.get(c["id"], "")),
              "title": c["title"], "project": c["project"], "lane": c["lane"]}
             for c in working if c["id"] not in have]
    return rows


def last_event():
    """The newest event id, 0 on an empty board: the board polls it as its change marker."""
    with closing(connect()) as db:
        return db.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]


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
    row = db.execute("SELECT name, path, instructions, color, land FROM projects WHERE name=?",
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


def _clean_land(land):
    if land not in LANDS:
        raise ValueError(f"land must be 'merge' or 'pr', not {land!r}")
    return land


def list_projects():
    """Configured projects: name, path on disk ("" if unset), free-text agent instructions,
    the #rrggbb color their cards are tagged with, and how a passed card lands (`land`)."""
    with closing(connect()) as db:
        return [dict(r) for r in db.execute(
            "SELECT name, path, instructions, color, land FROM projects ORDER BY name")]


def get_project(name):
    with closing(connect()) as db:
        return _load_project(db, name)


def create_project(name, path="", instructions="", color="", land="merge"):
    """`color` "" picks the least-used palette color."""
    name, path, land = _clean_name(name), _clean_path(path), _clean_land(land)
    color = color and _clean_color(color)
    with closing(connect()) as db, db:
        if db.execute("SELECT 1 FROM projects WHERE name=?", (name,)).fetchone():
            raise ValueError(f"project {name!r} already exists")
        db.execute("INSERT INTO projects (name, path, instructions, color, land)"
                   " VALUES (?,?,?,?,?)",
                   (name, path, instructions or "", color or _next_color(db), land))
    return get_project(name)


def update_project(name, /, **fields):   # positional-only, so name= in fields is a rename
    """Change a project's name, path, instructions, color or land. A rename carries every card
    with it (archived ones too) and writes no card events: the cards did not change, the name
    did."""
    unknown = set(fields) - set(PROJECT_FIELDS)
    if unknown:
        raise ValueError(f"unknown field(s): {sorted(unknown)}")
    if "path" in fields:
        fields["path"] = _clean_path(fields["path"])
    if "instructions" in fields:
        fields["instructions"] = fields["instructions"] or ""
    if "color" in fields:
        fields["color"] = _clean_color(fields["color"])
    if "land" in fields:
        fields["land"] = _clean_land(fields["land"])
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
    """Delete a project. Its cards are all archived and keep the project's name as plain
    text, hidden until a project of that name is made again (#437); `archived` counts the
    ones this archived. Like a rename, this writes no card events."""
    with closing(connect()) as db, db:
        old = _load_project(db, name)["name"]
        n = db.execute("UPDATE cards SET archived=1 WHERE project=? COLLATE NOCASE"
                       " AND archived=0", (old,)).rowcount
        db.execute("DELETE FROM projects WHERE name=?", (old,))
    return {"deleted": old, "archived": n}


BOARD = "board"   # author of the cards the board opens by itself
FOLDER = """The project has no folder set, so an agent cannot check out a card's branch.

Set its path under "projects…" on the board: the absolute path of a git checkout that has
an `origin` remote (the test lane pushes the card's branch there). Archive this card once
it is set."""
TESTS = """Neither the repo's CLAUDE.md nor the project's instructions say how to test it.

An agent must run the project's tests before it may pass a card, so write the test command
into CLAUDE.md at the root of the project's folder: every Claude Code session reads it. The
instructions under "projects…" on the board count too. Archive this card once one does."""
DEPLOY = """Neither the repo's CLAUDE.md nor the project's instructions say how to deploy it.

The agent is told to follow the project's rules on how to deploy, so write the deploy steps
into CLAUDE.md at the root of the project's folder: every Claude Code session reads it. The
instructions under "projects…" on the board count too. Archive this card once one does."""


def setup_cards(name):
    """Cards for what a new project still needs before an agent can work a card in it: a
    folder, and rules covering tests and deploying -- in the repo's CLAUDE.md (which every
    Claude Code session reads anyway) or the board's instructions. Unassigned, in `todo` --
    a person has to fix them: agents cannot configure a project.
    ponytail: "CLAUDE.md + instructions cover tests/deploying" is a word match. It asks
    once, on a new project, and a wrong guess costs one archived card."""
    p = get_project(name)          # NotFound for an unknown name; gives the stored spelling
    said = p["instructions"]
    if p["path"]:
        try:
            said += "\n" + (Path(p["path"]) / "CLAUDE.md").read_text("utf-8", errors="ignore")
        except OSError:            # no CLAUDE.md, or unreadable: only the instructions count
            pass
    said = said.lower()
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


def _blocks(db):
    """{id: (blocked_by, blocking)} over live `blocks` links: the blocker not finished
    and the blocked card not done, any project, archived or not. Finished (#62, #87) is
    done, or verify with `merged` set, or verify with no PR in a project that lands by
    merge (its verify is merged already; a person's todo blocker dragged there counts
    too). In a `pr` project an empty `pr` is not enough: rework clears it (#72), so a
    card dragged back to verify by hand without a ship would unblock early. Computed,
    never deleted: a blocker moved back from verify blocks again. A card whose project
    row is missing counts as merge, the column default. The board marks cards with
    these and #37 draws arrows."""
    out = {}
    for f, t in db.execute(
        "SELECT l.from_id, l.to_id FROM links l JOIN cards a ON a.id=l.from_id"
        " JOIN cards b ON b.id=l.to_id LEFT JOIN projects p ON p.name=a.project"
        " WHERE l.kind='blocks' AND a.lane!='done'"
        " AND NOT (a.lane='verify' AND (a.merged=1"
        " OR (COALESCE(a.pr,'')='' AND COALESCE(p.land,'merge')='merge')))"
        " AND b.lane!='done' ORDER BY l.from_id, l.to_id"
    ):
        out.setdefault(t, ([], []))[0].append(f)
        out.setdefault(f, ([], []))[1].append(t)
    return out


def _mark(card, blocks, quiet=None):
    card["blocked_by"], card["blocking"] = blocks.get(card["id"], ([], []))   # sorted by the ORDER BY
    card["stalled"] = _stalled(card, quiet or {})
    return card


def _quiet(db, id=None):
    """{card_id: newest sign of life}: the later of the card's last event and its activity row
    (#493), for every card or just card `id`. A read: an activity row older than STALL_MINUTES
    is older than the cutoff anyway, so it counts for nothing without being deleted here; a
    card's own `updated_at` is added by `_stalled`."""
    one, args = ("", ()) if id is None else (" WHERE card_id=?", (id,))
    out = {}
    for cid, at in db.execute("SELECT card_id, MAX(at) FROM events" + one + " GROUP BY card_id", args):
        out[cid] = at or ""
    for cid, since in db.execute("SELECT card_id, since FROM activity" + one, args):
        out[cid] = max(out.get(cid, ""), since)
    return out


def _stalled(card, quiet, now=None):
    """#493: a card the run works (plan/develop/test), assigned to claude-agent, not archived,
    with no update, event or activity row in STALL_MINUTES: its agent is gone and nothing will
    free it. The board marks it; the run takes it over."""
    if card["lane"] not in STALL_LANES or card["archived"] or card["assignee"] != STALL_ASSIGNEE:
        return False
    last = max(card["updated_at"], quiet.get(card["id"], ""))
    return last < _ago(STALL_MINUTES, now)


def get_card(id):
    with closing(connect()) as db:
        card = _mark(_load(db, id), _blocks(db), _quiet(db, id))
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
        blocks, quiet = _blocks(db), _quiet(db)
        cards = [_mark(_card(r), blocks, quiet) for r in db.execute(sql + " ORDER BY pos, id", args)]
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
            # #505: an old-format choice ("15 (recommended): ...") right after a question
            # is that question's indented bullet, not a question of its own
            c = n and re.match(r"([^:?]{1,30}?)(\s*\(recommended\))?:\s+(.*)", m[1], re.I)
            if c and (c[2] or re.search(r"\d", c[1])):
                out.append(f"   - {c[1]}: {c[3]}" + (" (recommended)" if c[2] else ""))
                continue
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
        if fields.get("archived") is False and old["archived"]:   # #437: a deleted project's cards
            proj = fields.get("project", old["project"])
            if db.execute("SELECT 1 FROM projects WHERE name=?", (proj,)).fetchone() is None:
                raise ValueError("its project was deleted: recreate it or move the card to a project")
        if fields.get("assignee") == "":   # unassigned is NULL, whether "" or null was sent
            fields["assignee"] = None
        if isinstance(fields.get("questions"), str):   # every writer's questions: 1. 2. 3.
            fields["questions"] = number(fields["questions"])
        frm, to = LANES.index(old["lane"]), LANES.index(fields.get("lane", old["lane"]))
        # a card arriving in a lane queues behind the ones already there, unless the
        # writer said where it goes (the board's drag does; an agent's move does not)
        placed = "pos" in fields
        if to != frm:
            fields.setdefault("pos", _bottom(db, LANES[to]))
        order = "pos" in fields and (to != frm or fields["pos"] != old["pos"])
        # back into plan, develop or test is rework: the new work is neither on master
        # nor deployed. The old PR goes too (#72): a merged one would vouch for code
        # that is not on the base, and the next ship opens a new one.
        work = ("plan", "develop", "test")
        if to != frm and LANES[to] in work and LANES[frm] not in work:
            fields.setdefault("merged", False)
            fields.setdefault("deployed", False)
            fields.setdefault("pr", "")
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
            if order:   # on the written row: a refusal rolls the whole write back (#449)
                _order_check(db, id, old, placed)
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


# ---------- board settings ----------
# Kept in a JSON file beside the database rather than in it, so restart-backend.ps1 can
# read them with ConvertFrom-Json before any Python runs. Like project config, a change is
# not card activity: no actor, no event.
SETTINGS_DEFAULTS = {"lan": False}   # lan: listen on every address (0.0.0.0), not 127.0.0.1


def settings_path():
    """settings.json in the database's folder, so a redirected DB_PATH (tests, an alt board)
    takes its settings along."""
    return Path(DB_PATH).with_name("settings.json")


def get_settings():
    """The board settings, defaults filled in. A missing or unreadable file is the defaults."""
    try:
        stored = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = {}
    if not isinstance(stored, dict):
        stored = {}
    return {k: stored.get(k, v) if type(stored.get(k, v)) is type(v) else v
            for k, v in SETTINGS_DEFAULTS.items()}


def update_settings(**fields):
    """Change board settings; pass only what changes. `lan` must be a bool."""
    unknown = set(fields) - set(SETTINGS_DEFAULTS)
    if unknown:
        raise ValueError(f"unknown setting(s): {', '.join(sorted(unknown))}")
    for k, v in fields.items():
        if type(v) is not type(SETTINGS_DEFAULTS[k]):
            raise ValueError(f"{k} must be true or false, not {v!r}")
    settings = {**get_settings(), **fields}
    settings_path().write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return settings
