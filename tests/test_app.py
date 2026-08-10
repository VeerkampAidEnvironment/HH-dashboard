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

    def test_bulk_upload_requires_an_excel_workbook(self):
        response = self.client.post(
            "/bulk-upload",
            data={"csrf_token": self.csrf()},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Choose the updated Farmer Database Excel file", response.data)

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
        self.assertIn(b'<details class="dashboard-filter-details">', response.data)
        self.assertNotIn(b'<details class="dashboard-filter-details" open>', response.data)
        self.assertIn(b"dashboard-primary-controls", response.data)
        self.assertIn(b'<select name="topic">', response.data)
        self.assertIn(b"All training types", response.data)
        self.assertIn(b"data-demographic-explorer", response.data)
        self.assertIn(b"Age by sex", response.data)
        self.assertIn(b"Sex by age", response.data)
        self.assertIn(b"Age distribution within each sex", response.data)

    def test_testing_environment_isolated_from_live_database(self):
        with self.app.app_context():
            from db import get_db

            live_count = get_db().execute("SELECT COUNT(*) FROM records").fetchone()[0]

        entered = self.client.post(
            "/test-environment",
            data={"csrf_token": self.csrf(), "action": "enter", "next": "/data-entry"},
        )
        self.assertEqual(entered.status_code, 302)
        test_page = self.client.get("/data-entry")
        self.assertIn(b"Testing environment is active", test_page.data)

        created = self.client.post(
            "/records/new",
            data={
                "csrf_token": self.csrf(), "dataset": "training",
                "field__Name": "PRACTICE ONLY FARMER", "field__Sex": "F",
            },
        )
        self.assertEqual(created.status_code, 302)

        exited = self.client.post(
            "/test-environment",
            data={"csrf_token": self.csrf(), "action": "exit", "next": "/data-entry"},
        )
        self.assertEqual(exited.status_code, 302)
        with self.app.app_context():
            from db import get_db

            self.assertEqual(get_db().execute("SELECT COUNT(*) FROM records").fetchone()[0], live_count)
            self.assertIsNone(get_db().execute(
                "SELECT 1 FROM records WHERE name='PRACTICE ONLY FARMER'"
            ).fetchone())

        live_page = self.client.get("/field-app/")
        self.assertIn(b"Open testing environment", live_page.data)
        reentered = self.client.post(
            "/test-environment",
            data={"csrf_token": self.csrf(), "action": "enter", "next": "/field-app/"},
        )
        self.assertEqual(reentered.status_code, 302)
        test_field_page = self.client.get("/field-app/")
        self.assertIn(b'"environment": "test"', test_field_page.data)

    def test_ae_training_activity_chart_has_editable_breakdowns(self):
        response = self.client.get("/dashboard?dataset=training")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"data-activity-explorer", response.data)
        self.assertIn(b"timeline-filter-deck", response.data)
        self.assertIn(b"data-cbf-multiselect", response.data)
        self.assertIn(b"data-activity-topic", response.data)
        self.assertIn(b"data-activity-age", response.data)
        self.assertIn(b"data-activity-stack-mode", response.data)
        self.assertIn(b'data-value="activity">Activity</button>', response.data)
        self.assertIn(b'data-value="topic">Training type</button>', response.data)
        self.assertIn(b'data-value="cbf">CBF</button>', response.data)
        self.assertNotIn(b'data-activity-group="status"', response.data)
        self.assertIn(b'data-value="ct">CT</button>', response.data)
        self.assertIn(b'data-value="fu">Follow-up</button>', response.data)
        self.assertIn(b"over time", response.data)
        self.assertIn(b"Sex distribution within each age group", response.data)
        self.assertIn(b"follow-ups", response.data)
        self.assertIn(b"CTs", response.data)

        with self.app.app_context():
            from app import build_dashboard_data
            from db import get_db

            filters = {key: "" for key in ("gender", "age", "cbf", "group", "topic", "date_from", "date_to", "status")}
            data = build_dashboard_data(get_db(), "training", filters)
            self.assertGreaterEqual(data["summary"]["followup_actions"], data["summary"]["followup"])
            self.assertGreaterEqual(data["summary"]["retraining_actions"], data["summary"]["retraining"])

    def test_ae_dashboard_training_type_filter_applies_to_all_metrics(self):
        from training import TRAINING_TOPICS

        topic = TRAINING_TOPICS[0]
        response = self.client.get(
            "/dashboard", query_string={"dataset": "training", "topic": topic}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(f'<option value="{topic}" selected>'.encode(), response.data)

        with self.app.app_context():
            from app import build_dashboard_data
            from db import get_db

            filters = {key: "" for key in (
                "gender", "age", "cbf", "group", "topic", "date_from", "date_to", "status"
            )}
            filters["topic"] = topic
            data = build_dashboard_data(get_db(), "training", filters)
            self.assertEqual([item["topic"] for item in data["topics"]], [topic])
            self.assertTrue(all(
                status["topic"] == topic
                for statuses in data["status_by_record"].values()
                for status in statuses
            ))
            self.assertTrue(all(
                event["topic"] == topic for event in data["activity_timeline"]["events"]
            ))
            self.assertEqual(data["activity_filter_options"]["topics"], [topic])

        pdf = self.client.get(
            "/dashboard.pdf", query_string={"dataset": "training", "topic": topic}
        )
        self.assertEqual(pdf.status_code, 200)
        self.assertTrue(pdf.data.startswith(b"%PDF"))

    def test_ae_dashboard_dropout_toggle_changes_the_full_scope(self):
        default_page = self.client.get("/dashboard?dataset=training")
        self.assertEqual(default_page.status_code, 200)
        self.assertIn(b"Ignore dropouts", default_page.data)
        self.assertIn(b"Include dropouts", default_page.data)
        self.assertIn(b'name="dropouts" value="exclude" aria-pressed="true"', default_page.data)

        with self.app.app_context():
            from app import build_dashboard_data
            from db import get_db

            connection = get_db()
            expected_active = connection.execute(
                """SELECT COUNT(*) FROM records WHERE dataset='training' AND archived_at IS NULL
                   AND LOWER(COALESCE(record_status,'')) NOT LIKE '%drop%'"""
            ).fetchone()[0]
            expected_all = connection.execute(
                "SELECT COUNT(*) FROM records WHERE dataset='training' AND archived_at IS NULL"
            ).fetchone()[0]
            base_filters = {key: "" for key in (
                "gender", "age", "cbf", "group", "topic", "date_from", "date_to", "status"
            )}
            active_data = build_dashboard_data(connection, "training", {**base_filters, "dropouts": "exclude"})
            all_data = build_dashboard_data(connection, "training", {**base_filters, "dropouts": "include"})
            self.assertEqual(active_data["summary"]["total_records"], expected_active)
            self.assertEqual(all_data["summary"]["total_records"], expected_all)
            self.assertGreater(all_data["summary"]["total_records"], active_data["summary"]["total_records"])

        include_page = self.client.get("/dashboard?dataset=training&dropouts=include")
        self.assertIn(b'name="dropouts" value="include" aria-pressed="true"', include_page.data)

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

    def test_individual_user_login_and_audit_attribution(self):
        response = self.client.post(
            "/users",
            data={
                "csrf_token": self.csrf(),
                "display_name": "Field Officer Test",
                "username": "field.officer.test",
                "password": "secure-test-password",
            },
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            from db import get_db

            user = get_db().execute(
                "SELECT * FROM users WHERE username='field.officer.test'"
            ).fetchone()
            self.assertIsNotNone(user)
            self.assertNotEqual(user["password_hash"], "secure-test-password")

        self.client.post("/logout", data={"csrf_token": self.csrf()})
        login = self.client.post(
            "/login", data={"username": "field.officer.test", "password": "secure-test-password"}
        )
        self.assertEqual(login.status_code, 302)
        self.assertEqual(self.client.get("/users").status_code, 403)
        created = self.client.post(
            "/records/new",
            data={
                "csrf_token": self.csrf(), "dataset": "training",
                "field__Name": "ATTRIBUTION TEST FARMER", "field__Sex": "F",
            },
        )
        self.assertEqual(created.status_code, 302)
        with self.app.app_context():
            from db import get_db

            audit_user = get_db().execute(
                "SELECT username FROM audit_log WHERE summary='Created ATTRIBUTION TEST FARMER' ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
            self.assertEqual(audit_user, "field.officer.test")

    def test_fh_dashboard_account_is_restricted_and_read_only(self):
        created = self.client.post(
            "/users",
            data={
                "csrf_token": self.csrf(), "display_name": "FH Viewer Test",
                "username": "fh.viewer.test", "password": "fh",
                "access_scope": "fh_dashboard",
            },
        )
        self.assertEqual(created.status_code, 302)
        self.client.post("/logout", data={"csrf_token": self.csrf()})
        login = self.client.post("/login", data={"username": "fh.viewer.test", "password": "fh"})
        self.assertEqual(login.status_code, 302)

        dashboard = self.client.get("/dashboard?dataset=training")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn(b"FH attendance dashboard", dashboard.data)
        self.assertIn(b"nav-disabled", dashboard.data)

        farmers = self.client.get("/records?dataset=training")
        self.assertEqual(farmers.status_code, 200)
        self.assertIn(b"FH data", farmers.data)
        self.assertIn(b"Read only", farmers.data)
        with self.app.app_context():
            from db import get_db

            care_id = get_db().execute("SELECT id FROM records WHERE dataset='care' LIMIT 1").fetchone()[0]
            training_id = get_db().execute("SELECT id FROM records WHERE dataset='training' LIMIT 1").fetchone()[0]
        self.assertEqual(self.client.get(f"/records/{care_id}").status_code, 200)
        self.assertEqual(self.client.get(f"/records/{training_id}").status_code, 403)
        self.assertEqual(self.client.get(f"/records/{care_id}/edit").status_code, 403)
        self.assertEqual(self.client.get("/audit").status_code, 403)

    def test_ae_user_cannot_access_data_entry_bulk_upload_or_user_accounts(self):
        created = self.client.post(
            "/users",
            data={
                "csrf_token": self.csrf(), "display_name": "AE User Test",
                "username": "ae.user.test", "password": "ae",
                "access_scope": "ae_user",
            },
        )
        self.assertEqual(created.status_code, 302)
        self.client.post("/logout", data={"csrf_token": self.csrf()})
        login = self.client.post("/login", data={"username": "ae.user.test", "password": "ae"})
        self.assertEqual(login.status_code, 302)
        self.assertEqual(self.client.get("/dashboard?dataset=training").status_code, 200)
        self.assertEqual(self.client.get("/records?dataset=training").status_code, 200)
        self.assertEqual(self.client.get("/cbfs").status_code, 200)
        self.assertEqual(self.client.get("/audit").status_code, 200)
        self.assertEqual(self.client.get("/data-entry").status_code, 403)
        self.assertEqual(self.client.get("/bulk-upload").status_code, 403)
        self.assertEqual(self.client.get("/users").status_code, 403)

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
            filters = {key: "" for key in ("gender", "age", "cbf", "group", "topic", "date_from", "date_to", "status")}
            data = build_interaction_data(connection, filters)
            expected = connection.execute(
                """SELECT COUNT(*) FROM matches m WHERE m.status='confirmed' AND EXISTS (
                       SELECT 1 FROM audit_log a WHERE a.entity_type='match'
                       AND a.entity_id=m.id AND a.action='confirm')"""
            ).fetchone()[0]
            self.assertEqual(data["pair_count"], expected)
            self.assertEqual(data["pair_count"], 0)

        response = self.client.get("/dashboard?dataset=combined")
        self.assertIn(b"No manually confirmed cross-project beneficiaries yet", response.data)

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
        with self.app.app_context():
            from db import get_db

            known_village = get_db().execute(
                """SELECT village FROM records WHERE dataset='training' AND cbf_name=?
                   AND TRIM(COALESCE(village,''))<>'' LIMIT 1""",
                (eligible["cbf_name"],),
            ).fetchone()[0]
        self.assertIn(b'<option value="Test training venue"', venue_page.data)
        self.assertIn(known_village.encode(), venue_page.data)
        self.assertIn(b'+ Add a new meeting venue', venue_page.data)
        self.assertIn(b'data-new-venue-field', venue_page.data)

    def test_field_app_shell_manifest_and_bootstrap_are_ae_only(self):
        with self.app.app_context():
            from db import get_db

            cbf = get_db().execute(
                """SELECT cbf_name FROM records WHERE dataset='training'
                   AND archived_at IS NULL AND TRIM(cbf_name)<>'' LIMIT 1"""
            ).fetchone()[0]
        page = self.client.get("/field-app/")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"AE offline data collection", page.data)
        self.assertIn(b"field-app-config", page.data)
        self.assertIn(b"field-app.js", page.data)
        manifest = self.client.get("/field-app/manifest.webmanifest")
        self.assertEqual(manifest.status_code, 200)
        self.assertEqual(manifest.headers["Content-Type"], "application/manifest+json")
        manifest_data = json.loads(manifest.data)
        self.assertEqual(manifest_data["start_url"], "/field-app/")
        self.assertEqual({icon["sizes"] for icon in manifest_data["icons"]}, {"192x192", "512x512"})
        worker = self.client.get("/field-app/service-worker.js")
        self.assertEqual(worker.status_code, 200)
        self.assertIn(b"arfsa-field-shell", worker.data)
        worker.close()

        bootstrap = self.client.get("/field-app/api/bootstrap", query_string={"cbf": cbf})
        self.assertEqual(bootstrap.status_code, 200)
        package = bootstrap.get_json()["package"]
        self.assertEqual(package["cbf"], cbf)
        self.assertEqual(len(package["topics"]), 8)
        self.assertTrue(package["farmers"])
        self.assertNotIn("raw_data", package["farmers"][0])
        self.assertNotIn("phone", package["farmers"][0])
        with self.app.app_context():
            from db import get_db

            expected = get_db().execute(
                """SELECT COUNT(*) FROM records WHERE dataset='training'
                   AND archived_at IS NULL AND cbf_name=?""",
                (cbf,),
            ).fetchone()[0]
        self.assertEqual(package["summary"]["farmers"], expected)

    def test_field_app_centralized_sync_is_idempotent(self):
        with self.app.app_context():
            from db import get_db

            eligible = get_db().execute(
                """SELECT r.id, r.cbf_name, ts.topic FROM records r
                   JOIN topic_statuses ts ON ts.record_id=r.id
                   WHERE r.dataset='training' AND r.archived_at IS NULL
                   AND TRIM(r.cbf_name)<>'' AND ts.status_code IN ('CT','RT') LIMIT 1"""
            ).fetchone()
        submission_id = "field-test-centralized-0001"
        payload = {
            "cbf": eligible["cbf_name"],
            "deviceId": "test-tablet-0001",
            "submissions": [{
                "id": submission_id,
                "type": "centralized",
                "eventDate": date.today().isoformat(),
                "location": "Offline sync venue",
                "createdAt": "2026-08-06T08:00:00Z",
                "entries": [{"recordId": eligible["id"], "topic": eligible["topic"]}],
            }],
        }
        first = self.client.post(
            "/field-app/api/sync", json=payload, headers={"X-CSRF-Token": self.csrf()}
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.get_json()["results"][0]["status"], "accepted")
        second = self.client.post(
            "/field-app/api/sync", json=payload, headers={"X-CSRF-Token": self.csrf()}
        )
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.get_json()["results"][0]["status"], "duplicate")
        with self.app.app_context():
            from db import get_db

            rows = get_db().execute(
                """SELECT id, device_id, client_created_at FROM field_events
                   WHERE client_submission_id=?""", (submission_id,)
            ).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["device_id"], "test-tablet-0001")
            self.assertEqual(rows[0]["client_created_at"], "2026-08-06T08:00:00Z")

    def test_ae_field_user_is_restricted_to_assigned_cbf_package(self):
        with self.app.app_context():
            from db import get_db

            cbfs = [row[0] for row in get_db().execute(
                """SELECT DISTINCT cbf_name FROM records WHERE dataset='training'
                   AND archived_at IS NULL AND TRIM(cbf_name)<>'' ORDER BY cbf_name LIMIT 2"""
            ).fetchall()]
        self.assertEqual(len(cbfs), 2)
        created = self.client.post(
            "/users",
            data={
                "csrf_token": self.csrf(), "display_name": "Offline CBF Test",
                "username": "offline.cbf.test", "password": "field-password",
                "access_scope": "ae_user", "cbf_name": cbfs[0],
            },
        )
        self.assertEqual(created.status_code, 302)
        self.client.post("/logout", data={"csrf_token": self.csrf()})
        login = self.client.post(
            "/login", data={"username": "offline.cbf.test", "password": "field-password"}
        )
        self.assertEqual(login.status_code, 302)
        self.assertEqual(self.client.get("/field-app/").status_code, 200)
        assigned = self.client.get("/field-app/api/bootstrap")
        self.assertEqual(assigned.status_code, 200)
        self.assertEqual(assigned.get_json()["package"]["cbf"], cbfs[0])
        self.assertEqual(
            self.client.get("/field-app/api/bootstrap", query_string={"cbf": cbfs[1]}).status_code,
            403,
        )

    def test_field_app_followup_sync_uses_existing_questionnaire_rules(self):
        with self.app.app_context():
            from db import get_db

            due = get_db().execute(
                """SELECT r.id, r.cbf_name, ts.topic FROM records r
                   JOIN topic_statuses ts ON ts.record_id=r.id
                   WHERE r.dataset='training' AND r.archived_at IS NULL
                   AND TRIM(r.cbf_name)<>'' AND ts.status_code='FU' LIMIT 1"""
            ).fetchone()
        self.assertIsNotNone(due)
        payload = {
            "cbf": due["cbf_name"],
            "deviceId": "test-tablet-followup",
            "submissions": [{
                "id": "field-test-followup-0001",
                "type": "followup",
                "eventDate": date.today().isoformat(),
                "recordId": due["id"],
                "createdAt": "2026-08-06T09:00:00Z",
                "geoLocation": {
                    "latitude": 1.234567,
                    "longitude": 34.765432,
                    "accuracy": 12.5,
                    "capturedAt": "2026-08-06T08:59:30Z",
                },
                "responses": [{
                    "topic": due["topic"],
                    "answers": {"adoption_rate": "61", "next_action": "followup_1"},
                }],
            }],
        }
        response = self.client.post(
            "/field-app/api/sync", json=payload, headers={"X-CSRF-Token": self.csrf()}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["results"][0]["status"], "accepted")
        with self.app.app_context():
            from db import get_db

            stored = get_db().execute(
                """SELECT fr.adoption_rate, fr.next_action, e.latitude, e.longitude,
                          e.location_accuracy_m, e.location_captured_at
                   FROM followup_responses fr
                   JOIN field_event_entries fee ON fee.id=fr.event_entry_id
                   JOIN field_events e ON e.id=fee.event_id
                   WHERE e.client_submission_id='field-test-followup-0001'"""
            ).fetchone()
            self.assertEqual(stored["adoption_rate"], 61)
            self.assertEqual(stored["next_action"], "followup_1")
            self.assertAlmostEqual(stored["latitude"], 1.234567)
            self.assertAlmostEqual(stored["longitude"], 34.765432)
            self.assertEqual(stored["location_accuracy_m"], 12.5)
            self.assertEqual(stored["location_captured_at"], "2026-08-06T08:59:30Z")
        detail = self.client.get(f"/records/{due['id']}")
        self.assertIn(b"Visit location", detail.data)
        self.assertIn(b"openstreetmap.org", detail.data)

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
        self.assertIn(b"Beneficiary group", response.data)

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
        questionnaire_page = self.client.get(
            "/data-entry",
            query_string={
                "cbf": due["cbf_name"], "mode": "followup",
                "event_date": date.today().isoformat(), "record_id": str(due["id"]),
            },
        )
        self.assertIn(b"Follow-up questionnaire", questionnaire_page.data)
        self.assertIn(b"Adoption rate", questionnaire_page.data)
        self.assertIn(b"Centralized training (CT) needed", questionnaire_page.data)
        response = self.client.post(
            "/data-entry",
            data={
                "csrf_token": self.csrf(), "cbf": due["cbf_name"], "mode": "followup",
                "event_date": date.today().isoformat(), "record_id": str(due["id"]),
                "topic_count": "1", "topic__0": due["topic"],
                "answer__0__adoption_rate": "55",
                "answer__0__next_action": "followup_1",
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
            stored = connection.execute(
                """SELECT adoption_rate, next_action, answers FROM followup_responses
                   WHERE record_id=? AND topic=? ORDER BY id DESC LIMIT 1""",
                (due["id"], due["topic"]),
            ).fetchone()
            self.assertEqual(stored["adoption_rate"], 55)
            self.assertEqual(stored["next_action"], "followup_1")
            self.assertEqual(json.loads(stored["answers"])["adoption_rate"]["answer"], 55)
            status = connection.execute(
                "SELECT status_code FROM topic_statuses WHERE record_id=? AND topic=?",
                (due["id"], due["topic"]),
            ).fetchone()[0]
            self.assertEqual(status, "WAIT")

    def test_followup_can_request_ct_and_new_ct_restarts_waiting_period(self):
        with self.app.app_context():
            from db import get_db

            connection = get_db()
            candidates = connection.execute(
                """SELECT r.id, r.cbf_name, ts.topic, r.raw_data FROM records r
                   JOIN topic_statuses ts ON ts.record_id=r.id
                   WHERE r.dataset='training' AND r.archived_at IS NULL
                   AND TRIM(r.cbf_name)<>'' AND ts.status_code='FU'"""
            ).fetchall()
            due = next(
                row for row in candidates
                if any(
                    json.loads(row["raw_data"]).get(f"Training {cycle} - {row['topic']}") in {None, ""}
                    for cycle in (2, 3)
                )
            )

        followup = self.client.post(
            "/data-entry",
            data={
                "csrf_token": self.csrf(), "cbf": due["cbf_name"], "mode": "followup",
                "event_date": date.today().isoformat(), "record_id": str(due["id"]),
                "topic_count": "1", "topic__0": due["topic"],
                "answer__0__adoption_rate": "20", "answer__0__next_action": "ct",
            },
        )
        self.assertEqual(followup.status_code, 302)
        with self.app.app_context():
            from db import get_db

            status = get_db().execute(
                "SELECT status_code FROM topic_statuses WHERE record_id=? AND topic=?",
                (due["id"], due["topic"]),
            ).fetchone()[0]
            self.assertEqual(status, "RT")

        retraining = self.client.post(
            "/data-entry",
            data={
                "csrf_token": self.csrf(), "cbf": due["cbf_name"], "mode": "centralized",
                "event_date": date.today().isoformat(), "location": "Cycle restart venue",
                "topic_count": "1", "topic__0": due["topic"], "attendee__0": str(due["id"]),
            },
        )
        self.assertEqual(retraining.status_code, 302)
        with self.app.app_context():
            from db import get_db

            status = get_db().execute(
                "SELECT status_code FROM topic_statuses WHERE record_id=? AND topic=?",
                (due["id"], due["topic"]),
            ).fetchone()[0]
            self.assertEqual(status, "WAIT")

    def test_selected_followup_interval_becomes_due_after_that_period(self):
        from training import training_topic_status

        topic = "Financial Literacy"
        raw = {
            f"Training 1 - {topic}": "2026-01-01",
            f"Follow up 1 - {topic}": "2026-04-01",
            f"Follow up 1 score - {topic}": 55,
            f"Follow up 1 next action - {topic}": "followup_1",
        }
        self.assertEqual(training_topic_status(raw, topic, as_of=date(2026, 4, 30))["status_code"], "WAIT")
        self.assertEqual(training_topic_status(raw, topic, as_of=date(2026, 5, 1))["status_code"], "FU")

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
        self.assertNotIn(b"No manually confirmed cross-project beneficiaries yet", dashboard.data)
        self.assertIn(b"Cross-project correlations", dashboard.data)


if __name__ == "__main__":
    unittest.main()
