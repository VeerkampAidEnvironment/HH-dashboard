"""Verify imported database integrity and relationship invariants."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATABASE = ROOT / "instance" / "arfsa.db"


def scalar(connection, sql, parameters=()):
    return connection.execute(sql, parameters).fetchone()[0]


def main() -> None:
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    checks = {
        "integrity": scalar(connection, "PRAGMA integrity_check"),
        "records": scalar(connection, "SELECT COUNT(*) FROM records"),
        "training_records": scalar(connection, "SELECT COUNT(*) FROM records WHERE dataset='training'"),
        "care_records": scalar(connection, "SELECT COUNT(*) FROM records WHERE dataset='care'"),
        "farmers": scalar(connection, "SELECT COUNT(*) FROM farmers"),
        "training_statuses": scalar(connection, """SELECT COUNT(*) FROM topic_statuses ts
            JOIN records r ON r.id=ts.record_id WHERE r.dataset='training'"""),
        "care_statuses": scalar(connection, """SELECT COUNT(*) FROM topic_statuses ts
            JOIN records r ON r.id=ts.record_id WHERE r.dataset='care'"""),
        "confirmed_matches": scalar(connection, "SELECT COUNT(*) FROM matches WHERE status='confirmed'"),
        "pending_matches": scalar(connection, "SELECT COUNT(*) FROM matches WHERE status='pending'"),
        "confirmed_match_id_errors": scalar(connection, """SELECT COUNT(*) FROM matches m
            JOIN records t ON t.id=m.training_record_id JOIN records c ON c.id=m.care_record_id
            WHERE m.status='confirmed' AND t.farmer_id<>c.farmer_id"""),
        "orphan_statuses": scalar(connection, """SELECT COUNT(*) FROM topic_statuses ts
            LEFT JOIN records r ON r.id=ts.record_id WHERE r.id IS NULL"""),
        "blank_names": scalar(connection, "SELECT COUNT(*) FROM records WHERE TRIM(name)=''"),
    }
    expected = {
        "integrity": "ok",
        "records": 4449,
        "training_records": 2908,
        "care_records": 1541,
        "farmers": 4322,
        "training_statuses": 2908 * 8,
        "care_statuses": 1541 * 12,
        "confirmed_matches": 127,
        "pending_matches": 280,
        "confirmed_match_id_errors": 0,
        "orphan_statuses": 0,
        "blank_names": 0,
    }
    failures = {key: {"actual": checks[key], "expected": value} for key, value in expected.items() if checks[key] != value}
    print(json.dumps({"checks": checks, "failures": failures}, indent=2))
    connection.close()
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
