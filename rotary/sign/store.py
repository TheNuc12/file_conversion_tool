"""SQLite storage for the signing server.

Documents themselves live as files under ``<data>/documents/<envelope id>/``;
the database holds everything else. Each audit event stores a hash chained to
the previous one, so an edited or deleted event is detectable (``verify_chain``).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import secrets
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS envelopes (
    id TEXT PRIMARY KEY,
    owner_id INTEGER NOT NULL REFERENCES users(id),
    title TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    filename TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',  -- draft, sent, completed, declined, voided
    sequential INTEGER NOT NULL DEFAULT 1,
    pages TEXT NOT NULL,                     -- JSON list of [width, height] in points
    original_sha256 TEXT NOT NULL,
    final_sha256 TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS recipients (
    id INTEGER PRIMARY KEY,
    envelope_id TEXT NOT NULL REFERENCES envelopes(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'waiting',  -- waiting, sent, viewed, signed, declined
    token_hash TEXT UNIQUE,
    token_expires TEXT,
    consented_at TEXT,
    viewed_at TEXT,
    signed_at TEXT,
    declined_at TEXT,
    decline_reason TEXT,
    ip TEXT,
    user_agent TEXT
);
CREATE TABLE IF NOT EXISTS fields (
    id TEXT NOT NULL,
    envelope_id TEXT NOT NULL REFERENCES envelopes(id) ON DELETE CASCADE,
    recipient_position INTEGER NOT NULL,
    kind TEXT NOT NULL,
    page INTEGER NOT NULL,
    x REAL NOT NULL, y REAL NOT NULL, w REAL NOT NULL, h REAL NOT NULL,
    required INTEGER NOT NULL DEFAULT 1,
    value TEXT,
    PRIMARY KEY (envelope_id, id)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    envelope_id TEXT NOT NULL,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    ip TEXT,
    user_agent TEXT,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_by_envelope ON events(envelope_id, id);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)  # autocommit; explicit transactions
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# --------------------------------------------------------------------------- settings


def get_setting(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


# --------------------------------------------------------------------------- users


def create_user(conn, email: str, name: str, password_hash: str, is_admin: bool = False) -> int:
    cur = conn.execute(
        "INSERT INTO users (email, name, password_hash, is_admin, created_at) VALUES (?, ?, ?, ?, ?)",
        (email.strip(), name.strip(), password_hash, int(is_admin), now()),
    )
    return cur.lastrowid


def get_user(conn, user_id: int):
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_user_by_email(conn, email: str):
    return conn.execute("SELECT * FROM users WHERE email = ?", (email.strip(),)).fetchone()


def list_users(conn):
    return conn.execute("SELECT * FROM users ORDER BY active DESC, name").fetchall()


def count_users(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def update_user(conn, user_id: int, **values) -> None:
    allowed = {"name", "password_hash", "is_admin", "active"}
    assert set(values) <= allowed
    sets = ", ".join(f"{k} = ?" for k in values)
    conn.execute(f"UPDATE users SET {sets} WHERE id = ?", (*values.values(), user_id))


# --------------------------------------------------------------------------- envelopes


def create_envelope(conn, owner_id: int, title: str, filename: str, pages: list, original_sha256: str) -> str:
    envelope_id = secrets.token_hex(10)
    conn.execute(
        "INSERT INTO envelopes (id, owner_id, title, filename, pages, original_sha256, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (envelope_id, owner_id, title, filename, json.dumps(pages), original_sha256, now()),
    )
    return envelope_id


def get_envelope(conn, envelope_id: str):
    return conn.execute("SELECT * FROM envelopes WHERE id = ?", (envelope_id,)).fetchone()


def list_envelopes(conn, owner_id: int | None = None):
    sql = """SELECT e.*, u.name AS owner_name,
                    (SELECT COUNT(*) FROM recipients r WHERE r.envelope_id = e.id) AS recipient_count,
                    (SELECT COUNT(*) FROM recipients r WHERE r.envelope_id = e.id AND r.status = 'signed') AS signed_count
             FROM envelopes e JOIN users u ON u.id = e.owner_id"""
    if owner_id is None:
        return conn.execute(sql + " ORDER BY e.created_at DESC").fetchall()
    return conn.execute(sql + " WHERE e.owner_id = ? ORDER BY e.created_at DESC", (owner_id,)).fetchall()


def update_envelope(conn, envelope_id: str, **values) -> None:
    allowed = {"title", "message", "status", "sequential", "final_sha256", "sent_at", "completed_at"}
    assert set(values) <= allowed
    sets = ", ".join(f"{k} = ?" for k in values)
    conn.execute(f"UPDATE envelopes SET {sets} WHERE id = ?", (*values.values(), envelope_id))


def transition(conn, envelope_id: str, old: str, new: str, **values) -> bool:
    """Move an envelope from status ``old`` to ``new`` atomically; False if it wasn't in ``old``.

    This is what stops a double-clicked Send from emailing everyone twice."""
    allowed = {"sent_at", "completed_at", "final_sha256"}
    assert set(values) <= allowed
    sets = "".join(f", {k} = ?" for k in values)
    cur = conn.execute(f"UPDATE envelopes SET status = ?{sets} WHERE id = ? AND status = ?", (new, *values.values(), envelope_id, old))
    return cur.rowcount == 1


def delete_envelope(conn, envelope_id: str) -> None:
    conn.execute("DELETE FROM envelopes WHERE id = ?", (envelope_id,))


# --------------------------------------------------------------------------- recipients


def replace_recipients(conn, envelope_id: str, recipients: list[dict]) -> None:
    conn.execute("DELETE FROM recipients WHERE envelope_id = ?", (envelope_id,))
    for position, r in enumerate(recipients):
        conn.execute(
            "INSERT INTO recipients (envelope_id, position, name, email) VALUES (?, ?, ?, ?)",
            (envelope_id, position, r["name"].strip(), r["email"].strip()),
        )


def list_recipients(conn, envelope_id: str):
    return conn.execute("SELECT * FROM recipients WHERE envelope_id = ? ORDER BY position", (envelope_id,)).fetchall()


def get_recipient(conn, recipient_id: int):
    return conn.execute("SELECT * FROM recipients WHERE id = ?", (recipient_id,)).fetchone()


def recipient_for_token(conn, token: str):
    return conn.execute("SELECT * FROM recipients WHERE token_hash = ?", (hash_token(token),)).fetchone()


def issue_token(conn, recipient_id: int, days: int) -> str:
    """A fresh signing link secret. Only its hash is stored, so the database can't leak live links."""
    token = secrets.token_urlsafe(32)
    expires = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("UPDATE recipients SET token_hash = ?, token_expires = ? WHERE id = ?", (hash_token(token), expires, recipient_id))
    return token


def update_recipient(conn, recipient_id: int, **values) -> None:
    allowed = {"status", "consented_at", "viewed_at", "signed_at", "declined_at", "decline_reason", "ip", "user_agent", "token_expires"}
    assert set(values) <= allowed
    sets = ", ".join(f"{k} = ?" for k in values)
    conn.execute(f"UPDATE recipients SET {sets} WHERE id = ?", (*values.values(), recipient_id))


# --------------------------------------------------------------------------- fields


def replace_fields(conn, envelope_id: str, fields: list[dict]) -> None:
    conn.execute("DELETE FROM fields WHERE envelope_id = ?", (envelope_id,))
    for f in fields:
        conn.execute(
            "INSERT INTO fields (id, envelope_id, recipient_position, kind, page, x, y, w, h, required) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f["id"], envelope_id, f["signer"], f["kind"], f["page"], f["x"], f["y"], f["w"], f["h"], int(f.get("required", True))),
        )


def list_fields(conn, envelope_id: str):
    return conn.execute("SELECT * FROM fields WHERE envelope_id = ? ORDER BY page, y, x", (envelope_id,)).fetchall()


def set_field_value(conn, envelope_id: str, field_id: str, value: str | None) -> None:
    conn.execute("UPDATE fields SET value = ? WHERE envelope_id = ? AND id = ?", (value, envelope_id, field_id))


# --------------------------------------------------------------------------- audit trail


def _event_hash(prev: str, at: str, actor: str, action: str, ip: str | None, envelope_id: str) -> str:
    payload = json.dumps([prev, envelope_id, at, actor, action, ip or ""], separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def add_event(conn, envelope_id: str, actor: str, action: str, ip: str | None = None, user_agent: str | None = None) -> None:
    row = conn.execute("SELECT hash FROM events WHERE envelope_id = ? ORDER BY id DESC LIMIT 1", (envelope_id,)).fetchone()
    prev = row["hash"] if row else "0" * 64
    at = now()
    conn.execute(
        "INSERT INTO events (envelope_id, at, actor, action, ip, user_agent, prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (envelope_id, at, actor, action, ip, user_agent, prev, _event_hash(prev, at, actor, action, ip, envelope_id)),
    )


def list_events(conn, envelope_id: str):
    return conn.execute("SELECT * FROM events WHERE envelope_id = ? ORDER BY id", (envelope_id,)).fetchall()


def verify_chain(conn, envelope_id: str) -> bool:
    prev = "0" * 64
    for e in list_events(conn, envelope_id):
        if e["prev_hash"] != prev or e["hash"] != _event_hash(prev, e["at"], e["actor"], e["action"], e["ip"], envelope_id):
            return False
        prev = e["hash"]
    return True
