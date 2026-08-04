"""Compact reconciliation checks for the imported application database."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from openpyxl import load_workbook


ROOT = Path(__file__).resolve().parents[1]
DATABASE = ROOT / "instance" / "arfsa.db"
WORKBOOK = ROOT / "data" / "M_E_Dasboard_DONT_EDIT.xlsm"


def main() -> None:
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    for dataset in ("training", "care"):
        result = connection.execute(
            "SELECT COUNT(*) AS records, COUNT(DISTINCT farmer_id) AS farmers "
            "FROM records WHERE dataset=?",
            (dataset,),
        ).fetchone()
        print(dataset, dict(result))

    print("database training topics")
    rows = connection.execute(
        """SELECT topic, SUM(training_received) AS received, SUM(confirmed_trained) AS confirmed,
                  SUM(followup_needed) AS followup, SUM(retraining_needed) AS retraining,
                  SUM(status_code='WAIT') AS waiting
           FROM topic_statuses ts JOIN records r ON r.id=ts.record_id
           WHERE r.dataset='training' GROUP BY topic ORDER BY topic"""
    ).fetchall()
    for row in rows:
        print(dict(row))

    workbook = load_workbook(WORKBOOK, read_only=True, data_only=True, keep_links=False)
    dashboard = workbook["Total Dashboard"]
    print("excel total/dropouts", dashboard["I2"].value, dashboard["I5"].value)
    print("excel dashboard topics")
    for index in range(3, 11):
        print({
            "topic": dashboard[f"A{index}"].value,
            "followup": dashboard[f"B{index}"].value,
            "retraining": dashboard[f"C{index}"].value,
            "received": dashboard[f"D{index}"].value,
            "confirmed": dashboard[f"F{index}"].value,
        })
    workbook.close()
    connection.close()


if __name__ == "__main__":
    main()
