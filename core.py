"""Tickets core: schema plus every operation. The only module that touches SQL."""

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

LANES = ["todo", "plan", "develop", "test", "verify", "done"]
PRIORITIES = ["low", "med", "high"]
LINK_KINDS = ["parent", "blocks"]
DEFAULT_PROJECT = "inbox"

DB_PATH = "tickets.db"  # reassign core.DB_PATH to point elsewhere (tests, alt board)

CARD_FIELDS = ("project", "title", "description", "lane", "assignee", "priority", "labels", "checklist")
JSON_FIELDS = ("labels", "checklist")
# lane/assignee get their own event kind; everything else is an `edited`
EVENT_KIND = {"lane": "moved", "assignee": "assigned"}

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
    priority TEXT NOT NULL,
    labels TEXT NOT NULL DEFAULT '[]',
    checklist TEXT NOT NULL DEFAULT '[]'
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
"""


class NotFound(Exception):
    """Unknown card id. Distinct from ValueError so HTTP maps it to 404."""


def connect():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA)
    return db


def _now():
    return datetime.now(timezone.utc).isoformat()


def _card(row):
    card = dict(row)
    for f in JSON_FIELDS:
        card[f] = json.loads(card[f])
    return card


def _load(db, id):
    row = db.execute("SELECT * FROM cards WHERE id=?", (id,)).fetchone()
    if row is None:
        raise NotFound(f"no card {id}")
    return _card(row)


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
    if "priority" in fields and fields["priority"] not in PRIORITIES:
        raise ValueError(f"priority must be one of {PRIORITIES}")


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


def list_projects():
    """Distinct projects in use, one entry per name ignoring case. No projects table."""
    with closing(connect()) as db:
        return [r["project"] for r in db.execute(
            "SELECT project FROM cards GROUP BY project COLLATE NOCASE"
            " ORDER BY project COLLATE NOCASE"
        )]


def create_card(
    title,
    actor,
    description="",
    lane="todo",
    assignee=None,
    priority="med",
    labels=None,
    checklist=None,
    project=DEFAULT_PROJECT,
):
    _validate({"lane": lane, "priority": priority})
    now = _now()
    with closing(connect()) as db, db:
        id = db.execute(
            "INSERT INTO cards (project, title, description, lane, assignee, created_by,"
            " created_at, updated_at, priority, labels, checklist) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                project, title, description, lane, assignee, actor, now, now, priority,
                json.dumps(labels or []), json.dumps(checklist or []),
            ),
        ).lastrowid
        _event(db, id, actor, "created", {"title": title, "lane": lane})
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


def list_cards(lane=None, assignee=None, label=None, project=None):
    where, args = [], []
    if lane is not None:
        where.append("lane=?")
        args.append(lane)
    if assignee is not None:
        where.append("assignee=?")
        args.append(assignee)
    if project is not None:
        where.append("project=? COLLATE NOCASE")  # stored casing displays, matching ignores it
        args.append(project)
    sql = "SELECT * FROM cards" + (" WHERE " + " AND ".join(where) if where else "")
    with closing(connect()) as db:
        cards = [_card(r) for r in db.execute(sql + " ORDER BY updated_at DESC", args)]
    # ponytail: label filter scans in Python — fine to a few thousand cards.
    # Past that, index labels with json1 (json_each) and push it into the WHERE clause.
    return [c for c in cards if label is None or label in c["labels"]]


def update_card(id, actor, **fields):
    """The single write path for card mutations: diff, write, one event per changed field."""
    unknown = set(fields) - set(CARD_FIELDS)
    if unknown:
        raise ValueError(f"unknown field(s): {sorted(unknown)}")
    _validate(fields)
    with closing(connect()) as db, db:
        old = _load(db, id)
        sets, args, evs = [], [], []
        for f, new in fields.items():
            if new == old[f]:
                continue
            sets.append(f"{f}=?")
            args.append(json.dumps(new) if f in JSON_FIELDS else new)
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
    return get_card(id)


def comment(id, actor, text):
    with closing(connect()) as db, db:
        _load(db, id)
        _event(db, id, actor, "comment", {"text": text})
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
