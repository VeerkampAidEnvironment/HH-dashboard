"""SQLite persistence for the ARFSA dashboard."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

try:
    from flask import current_app, g
except ModuleNotFoundError:  # Import utilities can run in the spreadsheet runtime.
    current_app = None
    g = None


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    is_admin INTEGER NOT NULL DEFAULT 0,
    access_scope TEXT NOT NULL DEFAULT 'full' CHECK(access_scope IN ('full', 'ae_user', 'fh_dashboard')),
    cbf_name TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS farmers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    farmer_id INTEGER NOT NULL REFERENCES farmers(id),
    dataset TEXT NOT NULL CHECK(dataset IN ('training', 'care')),
    source_row INTEGER,
    name TEXT NOT NULL,
    sex TEXT,
    age_value TEXT,
    age_group TEXT,
    phone TEXT,
    village TEXT,
    parish TEXT,
    subcounty TEXT,
    district TEXT,
    group_name TEXT,
    cbf_name TEXT,
    record_status TEXT,
    raw_data TEXT NOT NULL,
    archived_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(dataset, source_row)
);

CREATE TABLE IF NOT EXISTS topic_statuses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
    topic TEXT NOT NULL,
    status_code TEXT NOT NULL,
    status_label TEXT NOT NULL,
    training_received INTEGER NOT NULL DEFAULT 0,
    confirmed_trained INTEGER NOT NULL DEFAULT 0,
    followup_needed INTEGER NOT NULL DEFAULT 0,
    retraining_needed INTEGER NOT NULL DEFAULT 0,
    last_activity_date TEXT,
    details TEXT,
    UNIQUE(record_id, topic)
);

CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    training_record_id INTEGER NOT NULL REFERENCES records(id),
    care_record_id INTEGER NOT NULL REFERENCES records(id),
    confidence INTEGER NOT NULL,
    reasons TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('confirmed', 'pending', 'rejected')),
    reviewed_at TEXT,
    UNIQUE(training_record_id, care_record_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    username TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id INTEGER,
    summary TEXT NOT NULL,
    changes TEXT
);

CREATE TABLE IF NOT EXISTS field_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL CHECK(event_type IN ('centralized', 'followup')),
    cbf_name TEXT NOT NULL,
    event_date TEXT NOT NULL,
    location TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    client_submission_id TEXT,
    device_id TEXT,
    client_created_at TEXT
);

CREATE TABLE IF NOT EXISTS field_event_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES field_events(id) ON DELETE CASCADE,
    record_id INTEGER NOT NULL REFERENCES records(id),
    topic TEXT NOT NULL,
    score REAL,
    UNIQUE(event_id, record_id, topic)
);

CREATE TABLE IF NOT EXISTS followup_responses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_entry_id INTEGER NOT NULL UNIQUE REFERENCES field_event_entries(id) ON DELETE CASCADE,
    record_id INTEGER NOT NULL REFERENCES records(id),
    topic TEXT NOT NULL,
    training_cycle INTEGER NOT NULL,
    questionnaire_version TEXT NOT NULL,
    answers TEXT NOT NULL,
    adoption_rate REAL NOT NULL,
    next_action TEXT NOT NULL CHECK(next_action IN ('followup_1', 'followup_3', 'followup_6', 'ct', 'none')),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_records_dataset ON records(dataset, archived_at);
CREATE INDEX IF NOT EXISTS idx_records_farmer ON records(farmer_id);
CREATE INDEX IF NOT EXISTS idx_records_cbf ON records(cbf_name);
CREATE INDEX IF NOT EXISTS idx_records_filters ON records(sex, age_group, record_status);
CREATE INDEX IF NOT EXISTS idx_topic_record ON topic_statuses(record_id);
CREATE INDEX IF NOT EXISTS idx_topic_status ON topic_statuses(status_code);
CREATE INDEX IF NOT EXISTS idx_field_events_cbf ON field_events(cbf_name, event_date);
CREATE INDEX IF NOT EXISTS idx_field_entries_event ON field_event_entries(event_id);
CREATE INDEX IF NOT EXISTS idx_followup_responses_record ON followup_responses(record_id, created_at);
CREATE INDEX IF NOT EXISTS idx_users_active ON users(is_active, username);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def database_path() -> Path:
    configured = os.getenv("ARFSA_DATABASE")
    if configured:
        return Path(configured).expanduser().resolve()
    root = Path(current_app.root_path if current_app else Path(__file__).parent)
    return root / "instance" / "arfsa.db"


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    target = Path(path) if path else database_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(target)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect()
    return g.db


def close_db(_error=None) -> None:
    connection = g.pop("db", None)
    if connection is not None:
        connection.close()


def init_db(connection: sqlite3.Connection | None = None) -> None:
    owns_connection = connection is None
    connection = connection or connect()
    connection.executescript(SCHEMA)
    user_columns = {row["name"] for row in connection.execute("PRAGMA table_info(users)").fetchall()}
    if "access_scope" not in user_columns:
        connection.execute("ALTER TABLE users ADD COLUMN access_scope TEXT NOT NULL DEFAULT 'full'")
    if "cbf_name" not in user_columns:
        connection.execute("ALTER TABLE users ADD COLUMN cbf_name TEXT")
    user_schema = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone()["sql"]
    if "ae_user" not in user_schema:
        connection.execute("ALTER TABLE users RENAME TO users_legacy")
        connection.execute(
            """CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                display_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                is_admin INTEGER NOT NULL DEFAULT 0,
                access_scope TEXT NOT NULL DEFAULT 'full' CHECK(access_scope IN ('full', 'ae_user', 'fh_dashboard')),
                cbf_name TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_login_at TEXT
            )"""
        )
        connection.execute(
            """INSERT INTO users(id, username, display_name, password_hash, is_active, is_admin, access_scope, cbf_name,
                                 created_at, updated_at, last_login_at)
               SELECT id, username, display_name, password_hash, is_active, is_admin,
                      COALESCE(access_scope, 'full'), cbf_name, created_at, updated_at, last_login_at
               FROM users_legacy"""
        )
        connection.execute("DROP TABLE users_legacy")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_users_active ON users(is_active, username)")
    field_event_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(field_events)").fetchall()
    }
    for column_name in ("client_submission_id", "device_id", "client_created_at"):
        if column_name not in field_event_columns:
            connection.execute(f"ALTER TABLE field_events ADD COLUMN {column_name} TEXT")
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_field_events_client_submission "
        "ON field_events(client_submission_id) WHERE client_submission_id IS NOT NULL"
    )
    connection.commit()
    if owns_connection:
        connection.close()


def set_setting(connection: sqlite3.Connection, key: str, value) -> None:
    payload = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    connection.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, payload),
    )


def get_setting(connection: sqlite3.Connection, key: str, default=None):
    row = connection.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return row["value"]


def log_audit(
    connection: sqlite3.Connection,
    username: str,
    action: str,
    entity_type: str,
    entity_id: int | None,
    summary: str,
    changes=None,
) -> None:
    connection.execute(
        "INSERT INTO audit_log(occurred_at, username, action, entity_type, entity_id, summary, changes) "
        "VALUES(?, ?, ?, ?, ?, ?, ?)",
        (
            utc_now(),
            username,
            action,
            entity_type,
            entity_id,
            summary,
            json.dumps(changes, ensure_ascii=False, default=str) if changes is not None else None,
        ),
    )


def register_db(app) -> None:
    app.teardown_appcontext(close_db)
