from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from datetime import date
import json
from pathlib import Path
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DATABASE = ROOT / "instance" / "arfsa.db"


class ApplicationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.database = Path(cls.temp_dir.name) / "test.db"
        shutil.copyfile(SOURCE_DATABASE, cls.database)
        cls.previous_database = os.environ.get("ARFSA_DATABASE")
        os.environ["ARFSA_DATABASE"] = str(cls.database)
        from app import create_app

        cls.app = create_app({"TESTING": True, "SECRET_KEY": "test-secret"})

    @classmethod
    def tearDownClass(cls):
        if cls.previous_database is None:
            os.environ.pop("ARFSA_DATABASE", None)
        else:
            os.environ["ARFSA_DATABASE"] = cls.previous_database
        cls.temp_dir.cleanup()

    def setUp(self):
        self.client = self.app.test_client()
        response = self.client.post(
            "/login", data={"username": "admin", "password": "change-me-now"}
        )
        self.assertEqual(response.status_code, 302)

    def csrf(self):
        with self.client.session_transaction() as current_session:
            return current_session["csrf_token"]

    def test_main_pages_and_filters_render(self):
        endpoints = [
            "/dashboard",
            "/dashboard?dataset=training&gender=F&status=followup",
            "/dashboard?dataset=care&age=Youth",
            "/records?dataset=training",
            "/records?dataset=care",
            "/cbfs",
            "/matches",
            "/data-entry",
            "/audit",
            "/bulk-upload",
        ]
        for endpoint in endpoints:
            with self.subTest(endpoint=endpoint):
                response = self.client.get(endpoint)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn(b"Internal Server Error", response.data)

    def test_record_cbf_and_pdf_routes(self):
        with self.app.app_context():
            from db import get_db

            record_id = get_db().execute("SELECT MIN(id) FROM records").fetchone()[0]
            cbf_name = get_db().execute(
                "SELECT cbf_name FROM records WHERE TRIM(cbf_name)<>'' LIMIT 1"
            ).fetchone()[0]
        self.assertEqual(self.client.get(f"/records/{record_id}").status_code, 200)
        self.assertEqual(self.client.get(f"/records/{record_id}/edit").status_code, 200)
        cbf_path = quote(cbf_name, safe="")
        self.assertEqual(self.client.get(f"/cbfs/{cbf_path}").status_code, 200)
        dashboard_pdf = self.client.get("/dashboard.pdf?dataset=training")
        self.assertEqual(dashboard_pdf.status_code, 200)
        self.assertTrue(dashboard_pdf.data.startswith(b"%PDF"))
        cbf_pdf = self.client.get(f"/cbfs/{cbf_path}.pdf")
        self.assertEqual(cbf_pdf.status_code, 200)
        self.assertTrue(cbf_pdf.data.startswith(b"%PDF"))
        all_pdf = self.client.get(f"/cbf-reports/{cbf_path}.pdf")
        self.assertEqual(all_pdf.status_code, 200)
        self.assertTrue(all_pdf.data.startswith(b"%PDF"))
        all_zip = self.client.get(f"/cbf-reports/{cbf_path}.zip")
        self.assertEqual(all_zip.status_code, 200)
        self.assertEqual(all_zip.headers["Content-Type"], "application/zip")

    def test_fh_dashboard_uses_attendance_structure(self):
        response = self.client.get("/dashboard?dataset=care")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"FH attendance dashboard", response.data)
        self.assertIn(b"Care groups", response.data)
        self.assertIn(b"School clubs", response.data)
        self.assertIn(b"Absences", response.data)
        self.assertNotIn(b"Recording coverage", response.data)
        self.assertNotIn(b"Training needed", response.data)
        with self.app.app_context():
            from db import get_db, get_setting

            modules = get_setting(get_db(), "care_modules")
            self.assertNotIn("School clubs - Theme 5", modules)
            self.assertEqual(len(modules["School clubs - Theme 4"]), 6)
            self.assertEqual(len(modules["School clubs - Theme 7"]), 3)

    def test_ae_dashboard_has_interactive_age_gender_breakdowns(self):
        response = self.client.get("/dashboard?dataset=training")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"data-demographic-explorer", response.data)
        self.assertIn(b"Age by sex", response.data)
        self.assertIn(b"Sex by age", response.data)
        self.assertIn(b"Age distribution within each sex", response.data)
        self.assertIn(b"Sex distribution within each age group", response.data)

    def test_add_archive_restore_and_audit(self):
        response = self.client.post(
            "/records/new",
            data={
                "csrf_token": self.csrf(),
                "dataset": "training",
                "field__Name": "TEST FARMER",
                "field__Sex": "F",
                "field__Age group": "Y",
            },
        )
        self.assertEqual(response.status_code, 302)
        record_id = int(response.headers["Location"].rstrip("/").split("/")[-1])
        self.assertEqual(self.client.get(f"/records/{record_id}").status_code, 200)
        archive = self.client.post(
            f"/records/{record_id}/archive", data={"csrf_token": self.csrf()}
        )
        self.assertEqual(archive.status_code, 302)
        restore = self.client.post(
            f"/records/{record_id}/archive", data={"csrf_token": self.csrf()}
        )
        self.assertEqual(restore.status_code, 302)
        audit = self.client.get("/audit")
        self.assertIn(b"Created TEST FARMER", audit.data)

    def test_csrf_blocks_mutation(self):
        with self.app.app_context():
            from db import get_db

            record_id = get_db().execute("SELECT MIN(id) FROM records").fetchone()[0]
        response = self.client.post(f"/records/{record_id}/archive", data={})
        self.assertEqual(response.status_code, 400)

    def test_combined_dashboard_contains_only_manually_confirmed_pairs(self):
        with self.app.app_context():
            from app import build_interaction_data
            from db import get_db

            connection = get_db()
            filters = {key: "" for key in ("gender", "age", "cbf", "group", "date_from", "date_to", "status")}
            data = build_interaction_data(connection, filters)
            expected = connection.execute(
                """SELECT COUNT(*) FROM matches m WHERE m.status='confirmed' AND EXISTS (
                       SELECT 1 FROM audit_log a WHERE a.entity_type='match'
                       AND a.entity_id=m.id AND a.action='confirm')"""
            ).fetchone()[0]
            self.assertEqual(data["pair_count"], expected)
            self.assertEqual(data["pair_count"], 0)

        response = self.client.get("/dashboard?dataset=combined")
        self.assertIn(b"No manually confirmed cross-project farmers yet", response.data)

    def test_cbf_centralized_training_entry(self):
        with self.app.app_context():
            from db import get_db

            eligible = get_db().execute(
                """SELECT r.id, r.cbf_name, ts.topic FROM records r
                   JOIN topic_statuses ts ON ts.record_id=r.id
                   WHERE r.dataset='training' AND r.archived_at IS NULL
                   AND TRIM(r.cbf_name)<>'' AND ts.status_code='CT' LIMIT 1"""
            ).fetchone()
        today = date.today().isoformat()
        response = self.client.post(
            "/data-entry",
            data={
                "csrf_token": self.csrf(), "cbf": eligible["cbf_name"], "mode": "centralized",
                "event_date": today, "location_choice": "__new__",
                "location_new": "Test training venue", "topic_count": "1",
                "topic__0": eligible["topic"], "attendee__0": str(eligible["id"]),
            },
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            from db import get_db

            connection = get_db()
            raw = json.loads(connection.execute(
                "SELECT raw_data FROM records WHERE id=?", (eligible["id"],)
            ).fetchone()[0])
            self.assertEqual(raw[f"Training 1 - {eligible['topic']}"], today)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM field_events WHERE location='Test training venue'"
            ).fetchone()[0], 1)
        venue_page = self.client.get(
            "/data-entry", query_string={"cbf": eligible["cbf_name"], "mode": "centralized"}
        )
        self.assertIn(b'<option value="Test training venue"', venue_page.data)
        self.assertIn(b'+ Add a new meeting venue', venue_page.data)
        self.assertIn(b'data-new-venue-field', venue_page.data)

    def test_data_entry_uses_switches_for_short_choices(self):
        response = self.client.get("/data-entry")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b'type="radio" name="mode" value="centralized"', response.data)
        self.assertNotIn(b'<select name="mode"', response.data)
        self.assertIn(b'<select name="cbf"', response.data)
        self.assertIn(b'data-enhanced-select', response.data)
        self.assertIn(b'data-search-placeholder="Search CBF by name"', response.data)
        self.assertIn(b'data-auto-submit="selection"', response.data)
        self.assertIn(b'data-submit-on-change', response.data)
        self.assertNotIn(b"CBF submissions", response.data)

        with self.app.app_context():
            from db import get_db

            cbf = get_db().execute(
                "SELECT cbf_name FROM records WHERE dataset='training' AND TRIM(cbf_name)<>'' LIMIT 1"
            ).fetchone()[0]
        response = self.client.get("/data-entry", query_string={"cbf": cbf})
        self.assertIn(b'type="radio" name="mode" value="centralized"', response.data)
        self.assertIn(b'data-auto-submit="selection"', response.data)
        self.assertIn(b"Farmer group", response.data)

        response = self.client.get(
            "/data-entry", query_string={"cbf": cbf, "mode": "centralized"}
        )
        self.assertIn(b'aria-label="Training date"', response.data)
        self.assertIn(b'value="" required aria-label="Training date"', response.data)
        self.assertIn(b'data-fill-date=', response.data)

    def test_cbf_followup_entry(self):
        with self.app.app_context():
            from db import get_db

            due = get_db().execute(
                """SELECT r.id, r.cbf_name, ts.topic FROM records r
                   JOIN topic_statuses ts ON ts.record_id=r.id
                   WHERE r.dataset='training' AND r.archived_at IS NULL
                   AND TRIM(r.cbf_name)<>'' AND ts.status_code='FU' LIMIT 1"""
            ).fetchone()
        self.assertIsNotNone(due)
        response = self.client.post(
            "/data-entry",
            data={
                "csrf_token": self.csrf(), "cbf": due["cbf_name"], "mode": "followup",
                "event_date": date.today().isoformat(), "record_id": str(due["id"]),
                "topic_count": "1", "topic__0": due["topic"], "score__0": "55",
            },
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            from db import get_db

            connection = get_db()
            latest = connection.execute(
                """SELECT i.score FROM field_event_entries i JOIN field_events e ON e.id=i.event_id
                   WHERE e.event_type='followup' AND i.record_id=? AND i.topic=? ORDER BY i.id DESC LIMIT 1""",
                (due["id"], due["topic"]),
            ).fetchone()
            self.assertEqual(latest["score"], 55)

    def test_identity_review_exposes_full_populated_profiles(self):
        response = self.client.get("/matches?status=pending")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Compare all available information", response.data)
        self.assertIn(b"Open full record", response.data)
        self.assertIn(b"AE fields", response.data)
        self.assertIn(b"FH fields", response.data)

    def test_manual_confirmation_populates_interaction_dashboard(self):
        with self.app.app_context():
            from db import get_db

            match_id = get_db().execute(
                """SELECT m.id FROM matches m WHERE m.status='confirmed' AND NOT EXISTS (
                       SELECT 1 FROM audit_log a WHERE a.entity_type='match'
                       AND a.entity_id=m.id AND a.action='confirm') LIMIT 1"""
            ).fetchone()[0]
        response = self.client.post(
            f"/matches/{match_id}/confirm", data={"csrf_token": self.csrf()}
        )
        self.assertEqual(response.status_code, 302)
        dashboard = self.client.get("/dashboard?dataset=combined")
        self.assertNotIn(b"No manually confirmed cross-project farmers yet", dashboard.data)
        self.assertIn(b"Cross-project correlations", dashboard.data)


if __name__ == "__main__":
    unittest.main()
