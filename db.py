"""SQLite persistence for the ARFSA dashboard."""

from __future__ import annotations

import json
import os
import sqlite3
from hashlib import sha256
from datetime import datetime, timezone
from pathlib import Path

try:
    from flask import current_app, g, has_request_context, session
except ModuleNotFoundError:  # Import utilities can run in the spreadsheet runtime.
    current_app = None
    g = None
    has_request_context = lambda: False
    session = None


DATABASE_SCHEMA_VERSION = 2


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

CREATE TABLE IF NOT EXISTS duplicate_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_a_id INTEGER NOT NULL REFERENCES records(id),
    record_b_id INTEGER NOT NULL REFERENCES records(id),
    survivor_record_id INTEGER REFERENCES records(id),
    status TEXT NOT NULL CHECK(status IN ('merged', 'rejected')),
    reviewed_at TEXT NOT NULL,
    UNIQUE(record_a_id, record_b_id)
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

CREATE TABLE IF NOT EXISTS audit_undo_rows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    audit_id INTEGER NOT NULL REFERENCES audit_log(id) ON DELETE CASCADE,
    change_order INTEGER NOT NULL,
    table_name TEXT NOT NULL,
    row_key TEXT NOT NULL,
    before_row TEXT,
    after_row TEXT,
    UNIQUE(audit_id, change_order)
);

CREATE TABLE IF NOT EXISTS audit_reverts (
    original_audit_id INTEGER PRIMARY KEY REFERENCES audit_log(id),
    revert_audit_id INTEGER NOT NULL REFERENCES audit_log(id),
    reverted_at TEXT NOT NULL,
    reverted_by TEXT NOT NULL
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
    client_created_at TEXT,
    latitude REAL,
    longitude REAL,
    location_accuracy_m REAL,
    location_captured_at TEXT
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

CREATE TABLE IF NOT EXISTS followup_assessments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL UNIQUE REFERENCES field_events(id) ON DELETE CASCADE,
    record_id INTEGER NOT NULL REFERENCES records(id),
    questionnaire_version TEXT NOT NULL,
    rvo_passed INTEGER,
    rvo_practice_count INTEGER,
    project_passed INTEGER,
    household_outcome TEXT,
    breadth_achieved INTEGER,
    breadth_total INTEGER,
    depth REAL,
    profile_snapshot TEXT NOT NULL,
    summary TEXT NOT NULL,
    consent INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS followup_package_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    assessment_id INTEGER NOT NULL REFERENCES followup_assessments(id) ON DELETE CASCADE,
    package_key TEXT NOT NULL,
    package_title TEXT NOT NULL,
    points_earned REAL NOT NULL,
    points_available REAL NOT NULL,
    score REAL NOT NULL,
    result_status TEXT NOT NULL,
    critical_failed INTEGER NOT NULL DEFAULT 0,
    recommendation TEXT,
    UNIQUE(assessment_id, package_key)
);

CREATE INDEX IF NOT EXISTS idx_records_dataset ON records(dataset, archived_at);
CREATE INDEX IF NOT EXISTS idx_records_farmer ON records(farmer_id);
CREATE INDEX IF NOT EXISTS idx_records_cbf ON records(cbf_name);
CREATE INDEX IF NOT EXISTS idx_records_filters ON records(sex, age_group, record_status);
CREATE INDEX IF NOT EXISTS idx_topic_record ON topic_statuses(record_id);
CREATE INDEX IF NOT EXISTS idx_topic_status ON topic_statuses(status_code);
CREATE INDEX IF NOT EXISTS idx_duplicate_reviews_status ON duplicate_reviews(status);
CREATE INDEX IF NOT EXISTS idx_field_events_cbf ON field_events(cbf_name, event_date);
CREATE INDEX IF NOT EXISTS idx_field_entries_event ON field_event_entries(event_id);
CREATE INDEX IF NOT EXISTS idx_followup_responses_record ON followup_responses(record_id, created_at);
CREATE INDEX IF NOT EXISTS idx_followup_assessments_record ON followup_assessments(record_id, created_at);
CREATE INDEX IF NOT EXISTS idx_followup_packages_assessment ON followup_package_results(assessment_id);
CREATE INDEX IF NOT EXISTS idx_users_active ON users(is_active, username);
CREATE INDEX IF NOT EXISTS idx_audit_undo_audit ON audit_undo_rows(audit_id, change_order);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def primary_database_path() -> Path:
    configured = os.getenv("ARFSA_DATABASE")
    if configured:
        return Path(configured).expanduser().resolve()
    root = Path(current_app.root_path if current_app else Path(__file__).parent)
    return root / "instance" / "arfsa.db"


def test_database_path(username: str) -> Path:
    safe_name = "".join(character if character.isalnum() else "-" for character in username.lower()).strip("-")
    suffix = sha256(username.encode("utf-8")).hexdigest()[:12]
    return primary_database_path().parent / "test-environments" / f"{(safe_name or 'user')[:30]}-{suffix}.db"


def _test_database_contains_user(path: Path, username: str) -> bool:
    """Return whether an existing test snapshot can authenticate this user."""
    connection = None
    try:
        connection = sqlite3.connect(path)
        row = connection.execute(
            "SELECT 1 FROM users WHERE username = ? COLLATE NOCASE",
            (username,),
        ).fetchone()
        return row is not None
    except sqlite3.DatabaseError:
        return False
    finally:
        if connection is not None:
            connection.close()


def ensure_test_database(username: str, *, reset: bool = False) -> Path:
    """Create a consistent per-user SQLite snapshot without writing to live data."""
    target = test_database_path(username)
    if reset and target.exists():
        target.unlink()
    if target.exists() and _test_database_contains_user(target, username):
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(primary_database_path())
    target_connection = sqlite3.connect(target)
    try:
        source_connection.backup(target_connection)
    finally:
        target_connection.close()
        source_connection.close()
    return target


def database_path() -> Path:
    if has_request_context() and session and session.get("test_environment"):
        return test_database_path(str(session.get("username") or session.get("user_id") or "user"))
    return primary_database_path()


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
        target = None
        if has_request_context() and session and session.get("test_environment"):
            username = str(session.get("username") or session.get("user_id") or "user")
            target = ensure_test_database(username)
        g.db = connect(target)
        schema_version = g.db.execute("PRAGMA user_version").fetchone()[0]
        if schema_version < DATABASE_SCHEMA_VERSION:
            init_db(g.db)
        prepare_audit_capture(g.db)
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
    field_event_migrations = {
        "client_submission_id": "TEXT",
        "device_id": "TEXT",
        "client_created_at": "TEXT",
        "latitude": "REAL",
        "longitude": "REAL",
        "location_accuracy_m": "REAL",
        "location_captured_at": "TEXT",
    }
    for column_name, column_type in field_event_migrations.items():
        if column_name not in field_event_columns:
            connection.execute(f"ALTER TABLE field_events ADD COLUMN {column_name} {column_type}")
    followup_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(followup_responses)").fetchall()
    }
    followup_migrations = {
        "result_status": "TEXT",
        "recommendation": "TEXT",
        "critical_failed": "INTEGER NOT NULL DEFAULT 0",
        "points_earned": "REAL",
        "points_available": "REAL",
    }
    for column_name, column_type in followup_migrations.items():
        if column_name not in followup_columns:
            connection.execute(f"ALTER TABLE followup_responses ADD COLUMN {column_name} {column_type}")
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_field_events_client_submission "
        "ON field_events(client_submission_id) WHERE client_submission_id IS NOT NULL"
    )
    connection.execute(f"PRAGMA user_version = {DATABASE_SCHEMA_VERSION}")
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


AUDIT_CAPTURE_EXCLUDED_TABLES = {"audit_log", "audit_undo_rows", "audit_reverts"}


class AuditRevertError(ValueError):
    """Raised when an audited action cannot be safely reversed."""


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _audit_row_json(*values) -> str:
    return json.dumps(
        {str(values[index]): values[index + 1] for index in range(0, len(values), 2)},
        ensure_ascii=False,
    )


def prepare_audit_capture(connection: sqlite3.Connection) -> None:
    """Track every row changed during this request until it is attached to an audit entry."""
    connection.create_function("_arfsa_audit_row_json", -1, _audit_row_json)
    connection.execute(
        """CREATE TEMP TABLE IF NOT EXISTS arfsa_audit_capture (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               table_name TEXT NOT NULL,
               row_key TEXT NOT NULL,
               before_row TEXT,
               after_row TEXT
           )"""
    )
    table_names = [
        row[0] for row in connection.execute(
            "SELECT name FROM main.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        if row[0] not in AUDIT_CAPTURE_EXCLUDED_TABLES
    ]
    for table_name in table_names:
        quoted_table = _quote_identifier(table_name)
        columns = connection.execute(f"PRAGMA main.table_info({quoted_table})").fetchall()
        primary_keys = [row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5]]
        if not primary_keys:
            continue
        column_names = [row[1] for row in columns]

        def packed(alias: str, names: list[str]) -> str:
            arguments = ", ".join(
                f"'{name.replace(chr(39), chr(39) * 2)}', {alias}.{_quote_identifier(name)}"
                for name in names
            )
            return f"_arfsa_audit_row_json({arguments})"

        table_literal = table_name.replace("'", "''")
        trigger_base = "arfsa_audit_" + "".join(
            character if character.isalnum() else "_" for character in table_name
        )
        trigger_definitions = (
            (
                "insert", "AFTER INSERT", packed("NEW", primary_keys), "NULL",
                packed("NEW", column_names),
            ),
            (
                "update", "AFTER UPDATE", packed("NEW", primary_keys), packed("OLD", column_names),
                packed("NEW", column_names),
            ),
            (
                "delete", "AFTER DELETE", packed("OLD", primary_keys), packed("OLD", column_names),
                "NULL",
            ),
        )
        for suffix, timing, row_key, before_row, after_row in trigger_definitions:
            trigger_name = _quote_identifier(f"{trigger_base}_{suffix}")
            connection.execute(
                f"""CREATE TEMP TRIGGER IF NOT EXISTS {trigger_name}
                       {timing} ON main.{quoted_table}
                       BEGIN
                         INSERT INTO arfsa_audit_capture(table_name, row_key, before_row, after_row)
                         VALUES('{table_literal}', {row_key}, {before_row}, {after_row});
                       END"""
            )


def _audit_capture_is_available(connection: sqlite3.Connection) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_temp_master WHERE type='table' AND name='arfsa_audit_capture'"
    ).fetchone() is not None


def log_audit(
    connection: sqlite3.Connection,
    username: str,
    action: str,
    entity_type: str,
    entity_id: int | None,
    summary: str,
    changes=None,
) -> int:
    cursor = connection.execute(
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
    audit_id = cursor.lastrowid
    if _audit_capture_is_available(connection):
        captures = connection.execute(
            """SELECT id, table_name, row_key, before_row, after_row
               FROM arfsa_audit_capture ORDER BY id"""
        ).fetchall()
        connection.executemany(
            """INSERT INTO audit_undo_rows(
                   audit_id, change_order, table_name, row_key, before_row, after_row
               ) VALUES(?, ?, ?, ?, ?, ?)""",
            [
                (
                    audit_id, capture["id"], capture["table_name"], capture["row_key"],
                    capture["before_row"], capture["after_row"],
                )
                for capture in captures
            ],
        )
        connection.execute("DELETE FROM arfsa_audit_capture")
    return audit_id


def _table_definition(connection: sqlite3.Connection, table_name: str) -> tuple[list[str], list[str]]:
    if table_name in AUDIT_CAPTURE_EXCLUDED_TABLES:
        raise AuditRevertError("This audit entry contains a protected internal change.")
    available = connection.execute(
        "SELECT 1 FROM main.sqlite_master WHERE type='table' AND name=?", (table_name,)
    ).fetchone()
    if not available:
        raise AuditRevertError(f"The affected table {table_name!r} no longer exists.")
    quoted_table = _quote_identifier(table_name)
    columns = connection.execute(f"PRAGMA main.table_info({quoted_table})").fetchall()
    column_names = [row[1] for row in columns]
    primary_keys = [row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5]]
    if not primary_keys:
        raise AuditRevertError(f"The affected table {table_name!r} has no primary key.")
    return column_names, primary_keys


def _primary_key_values(row: dict, primary_keys: list[str]) -> list:
    if any(key not in row for key in primary_keys):
        raise AuditRevertError("The stored reversal data is incomplete.")
    return [row[key] for key in primary_keys]


def _current_row(
    connection: sqlite3.Connection, table_name: str, columns: list[str],
    primary_keys: list[str], expected: dict,
) -> dict | None:
    quoted_table = _quote_identifier(table_name)
    where = " AND ".join(f"{_quote_identifier(key)} IS ?" for key in primary_keys)
    selected = ", ".join(_quote_identifier(column) for column in columns)
    row = connection.execute(
        f"SELECT {selected} FROM {quoted_table} WHERE {where}",
        _primary_key_values(expected, primary_keys),
    ).fetchone()
    return dict(row) if row else None


def _restore_audit_row(connection: sqlite3.Connection, capture) -> None:
    table_name = capture["table_name"]
    columns, primary_keys = _table_definition(connection, table_name)
    before = json.loads(capture["before_row"]) if capture["before_row"] else None
    after = json.loads(capture["after_row"]) if capture["after_row"] else None
    identity = after or before
    current = _current_row(connection, table_name, columns, primary_keys, identity)
    if current != after:
        raise AuditRevertError(
            f"A later change affected {table_name}. Revert the newer action first."
        )

    quoted_table = _quote_identifier(table_name)
    if before is None:
        where = " AND ".join(f"{_quote_identifier(key)} IS ?" for key in primary_keys)
        connection.execute(
            f"DELETE FROM {quoted_table} WHERE {where}",
            _primary_key_values(after, primary_keys),
        )
        return
    if after is None:
        names = [column for column in columns if column in before]
        quoted_names = ", ".join(_quote_identifier(name) for name in names)
        placeholders = ", ".join("?" for _ in names)
        connection.execute(
            f"INSERT INTO {quoted_table}({quoted_names}) VALUES({placeholders})",
            [before[name] for name in names],
        )
        return

    assignments = ", ".join(f"{_quote_identifier(column)}=?" for column in columns)
    where = " AND ".join(f"{_quote_identifier(key)} IS ?" for key in primary_keys)
    connection.execute(
        f"UPDATE {quoted_table} SET {assignments} WHERE {where}",
        [before.get(column) for column in columns] + _primary_key_values(after, primary_keys),
    )


def revert_audit_action(
    connection: sqlite3.Connection, audit_id: int, username: str, current_user_id: int | None = None,
) -> int:
    """Atomically restore every database row changed by one audited action."""
    connection.execute("SAVEPOINT revert_audit_action")
    try:
        original = connection.execute(
            "SELECT * FROM audit_log WHERE id=?", (audit_id,)
        ).fetchone()
        if not original:
            raise AuditRevertError("That audit entry no longer exists.")
        if original["action"] == "revert":
            raise AuditRevertError("A reversal entry cannot itself be reverted.")
        if connection.execute(
            "SELECT 1 FROM audit_reverts WHERE original_audit_id=?", (audit_id,)
        ).fetchone():
            raise AuditRevertError("This action has already been reverted.")
        captures = connection.execute(
            """SELECT * FROM audit_undo_rows WHERE audit_id=?
               ORDER BY change_order DESC, id DESC""",
            (audit_id,),
        ).fetchall()
        if not captures:
            raise AuditRevertError(
                "This older action predates complete reversal snapshots and cannot be fully reverted automatically."
            )

        if current_user_id is not None:
            for capture in captures:
                if capture["table_name"] != "users":
                    continue
                before = json.loads(capture["before_row"]) if capture["before_row"] else None
                after = json.loads(capture["after_row"]) if capture["after_row"] else None
                affected_id = (after or before or {}).get("id")
                if affected_id != current_user_id:
                    continue
                if before is None or not before.get("is_active") or not before.get("is_admin"):
                    raise AuditRevertError("For safety, you cannot revert an action that would lock your own account.")

        for capture in captures:
            _restore_audit_row(connection, capture)
        revert_id = log_audit(
            connection, username, "revert", "audit_log", audit_id,
            f"Reverted audit entry #{audit_id}: {original['summary']}",
            {"reverted_audit_id": audit_id, "original_action": original["action"]},
        )
        connection.execute(
            """INSERT INTO audit_reverts(original_audit_id, revert_audit_id, reverted_at, reverted_by)
               VALUES(?, ?, ?, ?)""",
            (audit_id, revert_id, utc_now(), username),
        )
        connection.execute("RELEASE SAVEPOINT revert_audit_action")
        return revert_id
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT revert_audit_action")
        connection.execute("RELEASE SAVEPOINT revert_audit_action")
        raise


def register_db(app) -> None:
    app.teardown_appcontext(close_db)
