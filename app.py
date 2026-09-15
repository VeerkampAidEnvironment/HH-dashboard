"""ARFSA training database and monitoring dashboard."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import zipfile
from collections import Counter, defaultdict
from datetime import date, timedelta
from functools import wraps
from io import BytesIO
from itertools import combinations
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from flask import (
    Flask,
    abort,
    current_app,
    flash,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.datastructures import MultiDict
from werkzeug.security import check_password_hash, generate_password_hash

from bulk_import import BulkImportError, append_care_database, append_farmer_database
from db import (
    AuditRevertError,
    close_db,
    ensure_test_database,
    get_db,
    get_setting,
    log_audit,
    register_db,
    revert_audit_action,
    set_setting,
    utc_now,
)
from questionnaire import NEXT_ACTION_OPTIONS, QUESTIONNAIRE_VERSION, questionnaire_for_topic
from followup_survey import (
    CARRY_FORWARD_QUESTION_IDS,
    SURVEY_VERSION,
    age_from_birth_year,
    age_group_from_birth_year,
    all_questions_for_topics,
    build_survey,
    offline_scoring_rules,
    question_is_active,
    received_training_topics,
    resolve_locked_answers,
    score_survey,
    validate_answers,
)
from training import (
    TRAINING_TOPICS,
    adoption_threshold,
    care_module_fields,
    care_module_status,
    parse_date,
    parse_number,
    training_topic_status,
)


DATASET_LABELS = {
    "combined": "Combined",
    "training": "AE",
    "care": "FH",
}

COORDINATOR_CBF_NAMES = {
    "dismas cheptoek",
    "ramula chebet",
    "eliakim kibet",
    "ephraim kibet festo",
    "chesang ben samuel",
}


def is_coordinator_cbf(cbf_name: str) -> bool:
    normalized = " ".join(str(cbf_name or "").split()).casefold()
    return normalized in COORDINATOR_CBF_NAMES

MAX_FOLLOWUP_PHOTOS = 6
MAX_FOLLOWUP_PHOTO_BYTES = 8 * 1024 * 1024


def _photo_extension(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    if len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in {b"heic", b"heix", b"hevc", b"hevx", b"mif1"}:
        return ".heic"
    return None


def prepare_followup_photos(field_name: str, encoded_value: str = "") -> list[dict[str, Any]]:
    candidates: list[tuple[str, bytes]] = []
    for upload in request.files.getlist(field_name):
        if upload and upload.filename:
            candidates.append((Path(upload.filename).name, upload.read(MAX_FOLLOWUP_PHOTO_BYTES + 1)))
    if encoded_value:
        try:
            encoded_items = json.loads(encoded_value)
        except (TypeError, ValueError):
            raise ValueError("The offline photo data is invalid.") from None
        if not isinstance(encoded_items, list):
            raise ValueError("The offline photo data is invalid.")
        for item in encoded_items:
            try:
                header, payload = str(item["data"]).split(",", 1)
                if not header.startswith("data:image/") or ";base64" not in header:
                    raise ValueError
                data = base64.b64decode(payload, validate=True)
                candidates.append((Path(str(item.get("name") or "photo")).name, data))
            except (KeyError, TypeError, ValueError, binascii.Error):
                raise ValueError("One of the offline photos is invalid.") from None
    if len(candidates) > MAX_FOLLOWUP_PHOTOS:
        raise ValueError(f"Upload no more than {MAX_FOLLOWUP_PHOTOS} photos for one photo question.")
    prepared = []
    for original_name, data in candidates:
        if not data or len(data) > MAX_FOLLOWUP_PHOTO_BYTES:
            raise ValueError("Each follow-up photo must be smaller than 8 MB.")
        extension = _photo_extension(data)
        if not extension:
            raise ValueError("Photo questions accept JPG, PNG, WebP or HEIC photos only.")
        prepared.append({"name": original_name or f"photo{extension}", "extension": extension, "data": data})
    return prepared


def persist_followup_photos(prepared: list[dict[str, Any]]) -> list[dict[str, str]]:
    if not prepared:
        return []
    folder = Path(current_app.config["FOLLOWUP_PHOTO_FOLDER"])
    folder.mkdir(parents=True, exist_ok=True)
    stored = []
    for item in prepared:
        filename = f"{secrets.token_hex(20)}{item['extension']}"
        (folder / filename).write_bytes(item["data"])
        stored.append({"file": filename, "name": item["name"]})
    return stored


def ensure_bootstrap_user(connection):
    """Create the first administrator once, using deployment environment values."""
    if connection.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        return
    username = os.getenv("ARFSA_USERNAME", "admin").strip() or "admin"
    password = os.getenv("ARFSA_PASSWORD", "change-me-now")
    now = utc_now()
    connection.execute(
        """INSERT INTO users(username, display_name, password_hash, is_active, is_admin, created_at, updated_at)
           VALUES(?, ?, ?, 1, 1, ?, ?)""",
        (username, "Administrator", generate_password_hash(password), now, now),
    )
    connection.commit()


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def followup_photo_files(connection, event_id: int) -> set[str]:
    """Collect validated photo filenames belonging only to one follow-up event."""
    rows = connection.execute(
        """SELECT fr.answers FROM followup_responses fr
           JOIN field_event_entries fee ON fee.id=fr.event_entry_id
           WHERE fee.event_id=?""",
        (event_id,),
    ).fetchall()
    filenames = set()

    def visit(value):
        if isinstance(value, dict):
            filename = value.get("file")
            if isinstance(filename, str) and re.fullmatch(r"[a-f0-9]{40}\.(?:jpg|png|webp|heic)", filename):
                filenames.add(filename)
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    for row in rows:
        try:
            visit(json.loads(row["answers"]))
        except (json.JSONDecodeError, TypeError):
            continue
    return filenames


def delete_historical_field_event(connection, audit_id: int, username: str) -> tuple[int, set[str]]:
    """Remove a pre-snapshot field event and clear its derived training/follow-up values."""
    connection.execute("SAVEPOINT delete_historical_field_event")
    try:
        original = connection.execute(
            """SELECT * FROM audit_log WHERE id=? AND action='field_entry'
               AND entity_type='field_event'""",
            (audit_id,),
        ).fetchone()
        if not original:
            raise AuditRevertError("This historical entry is not a field event that can be deleted.")
        event_id = original["entity_id"]
        event = connection.execute("SELECT * FROM field_events WHERE id=?", (event_id,)).fetchone()
        if not event:
            raise AuditRevertError("This field event has already been removed.")
        if connection.execute(
            "SELECT 1 FROM audit_reverts WHERE original_audit_id=?", (audit_id,)
        ).fetchone():
            raise AuditRevertError("This action has already been reverted.")

        photos = followup_photo_files(connection, event_id)
        entries = connection.execute(
            """SELECT fee.record_id, fee.topic, fr.training_cycle, fr.adoption_rate,
                      fr.next_action
               FROM field_event_entries fee
               LEFT JOIN followup_responses fr ON fr.event_entry_id=fee.id
               WHERE fee.event_id=? ORDER BY fee.id""",
            (event_id,),
        ).fetchall()
        if not entries:
            raise AuditRevertError("No data remains for this historical field event.")

        records_to_update = {}
        for entry in entries:
            record_id = entry["record_id"]
            if record_id not in records_to_update:
                record = connection.execute(
                    "SELECT dataset, raw_data FROM records WHERE id=?", (record_id,)
                ).fetchone()
                if not record:
                    raise AuditRevertError("An affected beneficiary record no longer exists.")
                records_to_update[record_id] = {
                    "dataset": record["dataset"], "raw": json.loads(record["raw_data"]),
                }
            raw = records_to_update[record_id]["raw"]
            topic = entry["topic"]
            if event["event_type"] == "followup":
                cycle = entry["training_cycle"]
                if cycle is None:
                    cycle = next(
                        (
                            number for number in range(1, 4)
                            if str(raw.get(f"Follow up {number} - {topic}") or "")[:10] == event["event_date"]
                        ),
                        None,
                    )
                if cycle is None:
                    raise AuditRevertError(
                        f"The original follow-up cycle for {topic} can no longer be identified."
                    )
                recorded_date = str(raw.get(f"Follow up {cycle} - {topic}") or "")[:10]
                if recorded_date and recorded_date != event["event_date"]:
                    raise AuditRevertError(
                        f"A newer change affected the {topic} follow-up. Revert that newer work first."
                    )
                raw[f"Follow up {cycle} - {topic}"] = ""
                raw[f"Follow up {cycle} score - {topic}"] = ""
                raw[f"Follow up {cycle} next action - {topic}"] = ""
            else:
                cycle = next(
                    (
                        number for number in range(1, 4)
                        if str(raw.get(f"Training {number} - {topic}") or "")[:10] == event["event_date"]
                    ),
                    None,
                )
                if cycle is None:
                    raise AuditRevertError(
                        f"The original training cycle for {topic} can no longer be identified."
                    )
                raw[f"Training {cycle} - {topic}"] = ""

        connection.execute("DELETE FROM field_events WHERE id=?", (event_id,))
        for record_id, update in records_to_update.items():
            connection.execute(
                "UPDATE records SET raw_data=?, updated_at=? WHERE id=?",
                (json.dumps(update["raw"], ensure_ascii=False), utc_now(), record_id),
            )
            rebuild_statuses(connection, record_id, update["dataset"], update["raw"])

        related_audit_ids = [
            row[0] for row in connection.execute(
                """SELECT id FROM audit_log
                   WHERE entity_type='field_event' AND entity_id=?
                   AND action IN ('field_entry', 'followup_actions')""",
                (event_id,),
            ).fetchall()
        ]
        revert_id = log_audit(
            connection, username, "revert", "audit_log", audit_id,
            f"Deleted historical field event #{event_id}: {original['summary']}",
            {"reverted_audit_ids": related_audit_ids, "deleted_field_event_id": event_id},
        )
        for related_id in related_audit_ids:
            connection.execute(
                """INSERT OR IGNORE INTO audit_reverts(
                       original_audit_id, revert_audit_id, reverted_at, reverted_by
                   ) VALUES(?, ?, ?, ?)""",
                (related_id, revert_id, utc_now(), username),
            )
        connection.execute("RELEASE SAVEPOINT delete_historical_field_event")
        return revert_id, photos
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT delete_historical_field_event")
        connection.execute("RELEASE SAVEPOINT delete_historical_field_event")
        raise


def field_app_version(root: Path) -> str:
    """Change the offline release whenever any part of the app shell changes."""
    digest = hashlib.sha256()
    for name in (
        "app.py", "followup_survey.py", "templates/base.html", "templates/field_app.html",
        "static/css/app.css", "static/js/app.js", "static/js/field-app.js",
        "static/js/field-updates.js", "static/js/field-service-worker.js",
        "static/js/field-scoring.js",
        "static/vendor/html2canvas.min.js", "static/img/field-app-icon-192.png",
        "static/img/field-app-icon-512.png",
    ):
        digest.update(name.encode())
        digest.update((root / name).read_bytes())
    return digest.hexdigest()[:12]


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.getenv("ARFSA_SECRET_KEY", "development-only-change-before-deployment"),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.getenv("ARFSA_SECURE_COOKIES", "0") == "1",
        MAX_CONTENT_LENGTH=64 * 1024 * 1024,
        FOLLOWUP_PHOTO_FOLDER=str(Path(app.instance_path) / "followup_photos"),
    )
    if test_config:
        app.config.update(test_config)
    static_files = [Path(app.static_folder) / folder / filename for folder, filename in (
        ("css", "app.css"), ("js", "app.js"), ("css", "identity-review.css"), ("js", "identity-review.js"),
    )]
    app.config["ASSET_VERSION"] = str(max(
        int(path.stat().st_mtime_ns) for path in static_files if path.exists()
    ))
    app.config["FIELD_APP_VERSION"] = field_app_version(Path(app.root_path))
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    register_db(app)
    with app.app_context():
        connection = get_db()
        ensure_bootstrap_user(connection)
        ensure_care_structure(connection)

    @app.template_filter("datefmt")
    def datefmt(value):
        if not value:
            return "-"
        return str(value)[:10]

    @app.template_filter("pct")
    def pct(value):
        return f"{float(value or 0):.0f}%"

    @app.template_filter("beneficiaryterm")
    def beneficiaryterm(value):
        text = str(value or "")
        text = re.sub(r"\bFarmers\b", "Beneficiaries", text)
        text = re.sub(r"\bfarmers\b", "beneficiaries", text)
        text = re.sub(r"\bFarmer\b", "Beneficiary", text)
        return re.sub(r"\bfarmer\b", "beneficiary", text)

    @app.context_processor
    def inject_globals():
        return {
            "csrf_token": csrf_token,
            "dataset_labels": DATASET_LABELS,
            "current_year": date.today().year,
            "asset_version": app.config["ASSET_VERSION"],
        }

    @app.before_request
    def protect_application():
        allowed = {"login", "static", "health"}
        if request.endpoint not in allowed and not session.get("authenticated"):
            if request.path.startswith("/field-app/api/"):
                return jsonify({"ok": False, "error": "Sign in online before synchronizing."}), 401
            return redirect(url_for("login", next=request.full_path.rstrip("?")))
        if request.endpoint not in allowed and session.get("authenticated"):
            user = get_db().execute(
                "SELECT id, username, display_name, is_active, is_admin, access_scope, cbf_name FROM users WHERE id=?",
                (session.get("user_id"),),
            ).fetchone()
            if not user or not user["is_active"]:
                session.clear()
                flash("Your account is no longer active.", "error")
                return redirect(url_for("login"))
            session["username"] = user["username"]
            session["display_name"] = user["display_name"]
            session["is_admin"] = bool(user["is_admin"])
            session["access_scope"] = user["access_scope"]
            session["cbf_name"] = user["cbf_name"] or ""
            fh_allowed = {"dashboard", "record_list", "record_detail", "logout", "fh_settings", "fh_export", "fh_report"}
            if user["access_scope"] == "fh_dashboard" and request.endpoint not in fh_allowed:
                abort(403)
            ae_user_denied = {"data_entry", "bulk_upload", "users", "user_password", "user_status", "user_access"}
            if user["access_scope"] == "ae_user" and request.endpoint in ae_user_denied:
                abort(403)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.endpoint != "login":
            supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
            if not supplied or not hmac.compare_digest(str(supplied), str(csrf_token())):
                abort(400, "Invalid CSRF token")

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/followup-photos/<filename>")
    def followup_photo(filename):
        if not re.fullmatch(r"[a-f0-9]{40}\.(?:jpg|png|webp|heic)", filename):
            abort(404)
        return send_from_directory(app.config["FOLLOWUP_PHOTO_FOLDER"], filename)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            user = get_db().execute(
                "SELECT * FROM users WHERE username=? COLLATE NOCASE", (username,)
            ).fetchone()
            valid = bool(user and user["is_active"] and check_password_hash(user["password_hash"], password))
            if valid:
                session.clear()
                session["authenticated"] = True
                session["user_id"] = user["id"]
                session["username"] = user["username"]
                session["display_name"] = user["display_name"]
                session["is_admin"] = bool(user["is_admin"])
                session["access_scope"] = user["access_scope"]
                session["cbf_name"] = user["cbf_name"] or ""
                session["csrf_token"] = secrets.token_urlsafe(32)
                get_db().execute("UPDATE users SET last_login_at=? WHERE id=?", (utc_now(), user["id"]))
                get_db().commit()
                destination = request.args.get("next", "")
                if user["access_scope"] == "fh_dashboard":
                    destination = url_for("dashboard", dataset="care")
                if not is_safe_redirect(destination):
                    destination = url_for("dashboard")
                return redirect(destination)
            flash("The username or password is incorrect.", "error")
        default_user = get_db().execute("SELECT password_hash FROM users WHERE username='admin'").fetchone()
        default_credentials = bool(
            not os.getenv("ARFSA_USERNAME") and not os.getenv("ARFSA_PASSWORD") and default_user
            and check_password_hash(default_user["password_hash"], "change-me-now")
        )
        return render_template("login.html", default_credentials=default_credentials)

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.post("/test-environment")
    def test_environment():
        action = request.form.get("action", "enter")
        destination = request.form.get("next", "")
        if not is_safe_redirect(destination):
            destination = url_for("data_entry")
        username = str(session.get("username") or session.get("user_id") or "user")
        if action == "enter":
            ensure_test_database(username)
            session["test_environment"] = True
            flash("Testing environment active. All changes now go to your separate test database.", "success")
        elif action == "reset":
            close_db()
            ensure_test_database(username, reset=True)
            session["test_environment"] = True
            flash("Your testing database was reset from the current live database.", "success")
        elif action == "exit":
            close_db()
            session.pop("test_environment", None)
            flash("Testing environment closed. You are back in the live database.", "success")
        else:
            abort(400, "Invalid testing-environment action")
        return redirect(destination)

    @app.get("/")
    @app.get("/dashboard")
    def dashboard():
        refresh_time_sensitive_statuses(get_db())
        dataset = "care" if session.get("access_scope") == "fh_dashboard" else valid_dataset(request.args.get("dataset", "combined"))
        filters = read_dashboard_filters(request.args)
        if dataset == "combined":
            data = build_interaction_data(get_db(), filters)
            return render_template("combined_dashboard.html", dataset=dataset, filters=filters, **data)
        if dataset == "care":
            data = build_fh_dashboard_data(get_db(), filters)
            from fh_monitoring import build_monitoring
            data['monitoring'] = build_monitoring(get_db(), request.args)
            return render_template("fh_dashboard.html", dataset=dataset, filters=filters, **data)
        data = build_dashboard_data(get_db(), dataset, filters)
        return render_template("dashboard.html", dataset=dataset, filters=filters, **data)

    @app.post('/fh/settings')
    def fh_settings():
        from fh_monitoring import ALL_METRICS, LOCATIONS, reports
        if session.get('access_scope') == 'ae_user':
            abort(403)
        connection = get_db()
        try:
            metric = request.form.get('metric')
            if metric not in ALL_METRICS:
                raise ValueError('Choose an indicator.')
            if request.form.get('action') == 'threshold':
                values = {key: float(request.form.get(key, '')) for key in ('excellent', 'good', 'critical')}
                if any(not math.isfinite(v) or v < 0 for v in values.values()):
                    raise ValueError('Thresholds must be finite, non-negative numbers.')
                direction = request.form.get('direction')
                if direction not in ('higher', 'lower'):
                    raise ValueError('Choose a performance direction.')
                ordered = [values[k] for k in ('critical', 'good', 'excellent')]
                if ordered != sorted(ordered, reverse=direction == 'lower'):
                    raise ValueError('For higher-is-better use critical ≤ on-track ≤ exceeding; reverse the order for lower-is-better.')
                basis = request.form.get('basis', 'unit')
                if basis not in ('unit', 'target'):
                    raise ValueError('Choose unit counts or target achievement thresholds.')
                setting_key = 'fh_rules' if basis == 'unit' else 'fh_target_rules'
                rules = get_setting(connection, setting_key, {})
                rules[metric] = {**values, 'direction': direction}
                set_setting(connection, setting_key, rules)
            elif request.form.get('action') in ('target', 'delete_target'):
                month = request.form.get('month', '')
                date.fromisoformat(month + '-01')
                scope = {k: request.form.get('scope_' + k, '').strip() for k in LOCATIONS}
                if any(scope.values()) and not any(all(not v or r[k] == v for k,v in scope.items()) for r in reports(connection)):
                    raise ValueError('Choose an existing reporting location or unit.')
                targets = get_setting(connection, 'fh_targets', [])
                targets = [t for t in targets if (t['metric'],t['month'],t['scope']) != (metric,month,scope)]
                if request.form.get('action') == 'target':
                    target = float(request.form.get('target', ''))
                    if not math.isfinite(target) or target < 0:
                        raise ValueError('Target must be a finite, non-negative number.')
                    targets.append(dict(metric=metric, month=month, scope=scope, target=target))
                set_setting(connection, 'fh_targets', targets)
            else:
                raise ValueError('Unknown settings action.')
            log_audit(connection, session['username'], 'fh_settings', 'settings', None,
                      'Updated FH targets or thresholds', {'metric': metric, 'action': request.form.get('action')})
            connection.commit()
            flash('FH settings saved.', 'success')
        except (ValueError, TypeError) as exc:
            connection.rollback()
            flash(str(exc) or 'Please check the settings values.', 'error')
        return redirect(url_for('dashboard', dataset='care', month=request.form.get('month', '')) + '#fh-settings')

    @app.get('/fh/export.csv')
    def fh_export():
        from fh_monitoring import build_monitoring, export_csv
        response = make_response(export_csv(build_monitoring(get_db(), request.args)))
        response.headers['Content-Type'] = 'text/csv; charset=utf-8'
        response.headers['Content-Disposition'] = 'attachment; filename=fh-monthly-data.csv'
        return response

    @app.get('/fh/report')
    def fh_report():
        from fh_monitoring import build_monitoring
        return render_template('fh_report.html', m=build_monitoring(get_db(), request.args))

    @app.get("/dashboard.pdf")
    def dashboard_pdf():
        from pdf_reports import build_dashboard_pdf, build_fh_dashboard_pdf

        refresh_time_sensitive_statuses(get_db())
        dataset = valid_dataset(request.args.get("dataset", "combined"))
        filters = read_dashboard_filters(request.args)
        if dataset == "care":
            data = build_fh_dashboard_data(get_db(), filters)
            pdf = build_fh_dashboard_pdf(data, describe_filters(dataset, filters))
        else:
            data = build_dashboard_data(get_db(), dataset, filters)
            pdf = build_dashboard_pdf(
                f"{DATASET_LABELS[dataset]} dashboard",
                data["summary"], data["topics"], describe_filters(dataset, filters),
            )
        response = make_response(pdf)
        response.headers["Content-Type"] = "application/pdf"
        response.headers["Content-Disposition"] = f'attachment; filename="arfsa-{dataset}-dashboard.pdf"'
        return response

    @app.route("/cbfs", methods=["GET", "POST"])
    def cbf_list():
        connection = get_db()
        group_rows = connection.execute(
            """SELECT group_name, COALESCE(NULLIF(TRIM(cbf_name), ''), '') AS cbf_name, COUNT(*) AS participant_count
               FROM records WHERE dataset='training' AND archived_at IS NULL AND TRIM(group_name)<>''
               GROUP BY group_name, COALESCE(NULLIF(TRIM(cbf_name), ''), '')
               ORDER BY group_name COLLATE NOCASE, participant_count DESC"""
        ).fetchall()
        groups_by_name = {}
        for row in group_rows:
            group = groups_by_name.setdefault(row["group_name"], {"name": row["group_name"], "cbfs": [], "participant_count": 0})
            group["participant_count"] += row["participant_count"]
            if row["cbf_name"]:
                group["cbfs"].append(row["cbf_name"])
        group_assignments = []
        for group in groups_by_name.values():
            unique_cbfs = sorted(set(group["cbfs"]), key=str.casefold)
            group["cbf_name"] = unique_cbfs[0] if len(unique_cbfs) == 1 else ("Multiple CBFs" if unique_cbfs else "")
            group_assignments.append(group)
        mapped_cbfs = set(get_setting(connection, "cbf_group_map", {}).values())
        cbf_names = sorted(mapped_cbfs | {
            row[0] for row in connection.execute(
                """SELECT cbf_name FROM records WHERE dataset='training' AND TRIM(COALESCE(cbf_name,''))<>''
                   UNION SELECT cbf_name FROM users WHERE TRIM(COALESCE(cbf_name,''))<>''"""
            ).fetchall()
        }, key=str.casefold)
        if request.method == "POST":
            if session.get("access_scope") == "ae_user":
                abort(403)
            group_name = request.form.get("group_name", "").strip()
            cbf_name = request.form.get("cbf_name", "").strip()
            if group_name not in groups_by_name or cbf_name not in cbf_names:
                flash("Choose a valid farmer group and CBF.", "error")
                return redirect(url_for("cbf_list"))
            previous_cbfs = sorted(set(groups_by_name[group_name]["cbfs"]), key=str.casefold)
            cursor = connection.execute(
                "UPDATE records SET cbf_name=?, updated_at=? WHERE dataset='training' AND group_name=?",
                (cbf_name, utc_now(), group_name),
            )
            cbf_map = get_setting(connection, "cbf_group_map", {})
            normalized_group = " ".join(
                "".join(char.lower() if char.isalnum() else " " for char in group_name).split()
            )
            cbf_map[normalized_group] = cbf_name
            set_setting(connection, "cbf_group_map", cbf_map)
            log_audit(
                connection, session["username"], "assign_group", "cbf_group", None,
                f"Assigned {group_name} to {cbf_name}",
                {"group": group_name, "from": previous_cbfs, "to": cbf_name, "beneficiaries_updated": cursor.rowcount},
            )
            connection.commit()
            flash(f"Assigned {group_name} to {cbf_name} and updated {cursor.rowcount} beneficiaries.", "success")
            return redirect(url_for("cbf_list"))
        rows = connection.execute(
            """SELECT r.cbf_name, COUNT(DISTINCT r.id) AS participant_count,
                      COUNT(DISTINCT r.farmer_id) AS farmer_count, COUNT(DISTINCT r.group_name) AS group_count,
                      COUNT(ts.id) AS topic_count,
                      COALESCE(SUM(ts.training_received), 0) AS topics_received,
                      COALESCE(SUM(ts.confirmed_trained), 0) AS topics_confirmed,
                      COUNT(DISTINCT CASE WHEN ts.training_received=1 THEN r.farmer_id END) AS trained_people,
                      COUNT(DISTINCT CASE WHEN ts.confirmed_trained=1 THEN r.farmer_id END) AS adoption_people,
                      COUNT(DISTINCT CASE WHEN NOT EXISTS (
                          SELECT 1 FROM topic_statuses missing
                          WHERE missing.record_id=r.id AND missing.training_received=0
                      ) AND EXISTS (
                          SELECT 1 FROM topic_statuses present WHERE present.record_id=r.id
                      ) THEN r.farmer_id END) AS fully_trained_people,
                      COUNT(DISTINCT CASE WHEN ts.followup_needed=1 THEN r.id END) AS followup_people,
                      COALESCE(SUM(ts.followup_needed), 0) AS followup_actions,
                      COUNT(DISTINCT CASE WHEN ts.retraining_needed=1 THEN r.id END) AS training_need_people
               FROM records r LEFT JOIN topic_statuses ts ON ts.record_id=r.id
               WHERE r.archived_at IS NULL AND r.dataset='training' AND TRIM(r.cbf_name)<>''
               GROUP BY r.cbf_name ORDER BY r.cbf_name"""
        ).fetchall()
        cbfs = []
        for row in rows:
            cbf = dict(row)
            denominator = cbf["farmer_count"]
            cbf["progress_rate"] = round(100 * cbf["trained_people"] / denominator) if denominator else 0
            cbf["fully_trained_rate"] = round(100 * cbf["fully_trained_people"] / denominator) if denominator else 0
            cbf["adoption_rate"] = round(100 * cbf["adoption_people"] / denominator) if denominator else 0
            cbf["urgency_score"] = cbf["followup_actions"] / cbf["participant_count"] if cbf["participant_count"] else 0
            if cbf["followup_people"] == 0:
                cbf.update(urgency="clear", urgency_label="No follow-ups due")
            elif cbf["urgency_score"] >= 4:
                cbf.update(urgency="high", urgency_label="High follow-up load")
            elif cbf["urgency_score"] >= 3:
                cbf.update(urgency="attention", urgency_label="Needs attention")
            else:
                cbf.update(urgency="due", urgency_label="Follow-up due")
            cbfs.append(cbf)
        cbfs.sort(key=lambda item: (-item["urgency_score"], item["cbf_name"].casefold()))
        return render_template(
            "cbfs.html", cbfs=cbfs, cbf_names=cbf_names,
            group_assignments=group_assignments,
        )

    @app.get("/cbfs/<path:cbf_name>")
    def cbf_detail(cbf_name):
        connection = get_db()
        refresh_time_sensitive_statuses(connection)
        exists = connection.execute(
            "SELECT 1 FROM records WHERE dataset='training' AND cbf_name=? LIMIT 1", (cbf_name,)
        ).fetchone()
        if not exists:
            abort(404)
        filters = read_dashboard_filters(request.args)
        filters["cbf"] = cbf_name
        data = build_dashboard_data(connection, "training", filters)
        groups = [row[0] for row in connection.execute(
            "SELECT DISTINCT group_name FROM records WHERE archived_at IS NULL AND cbf_name=? "
            "AND TRIM(group_name)<>'' ORDER BY group_name", (cbf_name,)
        ).fetchall()]
        priorities = build_priorities(data["records"], data["status_by_record"])
        return render_template(
            "cbf_detail.html", cbf_name=cbf_name, filters=filters, groups=groups,
            priorities=priorities, **data,
        )

    @app.get("/cbfs/<path:cbf_name>.pdf")
    def cbf_pdf(cbf_name):
        from pdf_reports import build_cbf_report_pdf

        connection = get_db()
        refresh_time_sensitive_statuses(connection)
        filters = read_dashboard_filters(request.args)
        group_reports = build_cbf_group_reports(connection, cbf_name, filters)
        if not group_reports:
            abort(404)
        pdf = build_cbf_report_pdf(cbf_name, group_reports)
        response = make_response(pdf)
        response.headers["Content-Type"] = "application/pdf"
        safe_name = "-".join(cbf_name.lower().split())[:60]
        response.headers["Content-Disposition"] = f'attachment; filename="cbf-{safe_name}.pdf"'
        return response

    @app.get("/cbf-reports/<path:cbf_name>.pdf")
    def cbf_all_pdf(cbf_name):
        from pdf_reports import build_cbf_report_pdf

        connection = get_db()
        refresh_time_sensitive_statuses(connection)
        group_reports = build_cbf_group_reports(connection, cbf_name, {})
        if not group_reports:
            abort(404)
        response = make_response(build_cbf_report_pdf(cbf_name, group_reports))
        response.headers["Content-Type"] = "application/pdf"
        safe_name = safe_filename(cbf_name)
        response.headers["Content-Disposition"] = f'attachment; filename="cbf-{safe_name}-all-groups.pdf"'
        return response

    @app.get("/cbf-reports/<path:cbf_name>.zip")
    def cbf_all_zip(cbf_name):
        from pdf_reports import build_cbf_report_pdf

        connection = get_db()
        refresh_time_sensitive_statuses(connection)
        group_reports = build_cbf_group_reports(connection, cbf_name, {})
        if not group_reports:
            abort(404)
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for report in group_reports:
                filename = f"{safe_filename(cbf_name)}-{safe_filename(report['group_name'])}.pdf"
                archive.writestr(filename, build_cbf_report_pdf(cbf_name, [report]))
        response = make_response(buffer.getvalue())
        response.headers["Content-Type"] = "application/zip"
        response.headers["Content-Disposition"] = f'attachment; filename="cbf-{safe_filename(cbf_name)}-group-reports.zip"'
        return response

    @app.get("/records")
    def record_list():
        connection = get_db()
        dataset = "care" if session.get("access_scope") == "fh_dashboard" else request.args.get("dataset", "training")
        if dataset not in {"training", "care"}:
            dataset = "training"
        query = request.args.get("q", "").strip()
        archived = request.args.get("archived") == "1"
        page = max(1, request.args.get("page", 1, type=int))
        page_size = 50
        where = ["r.dataset=?", "r.archived_at IS NOT NULL" if archived else "r.archived_at IS NULL"]
        params: list = [dataset]
        if query:
            where.append("(r.name LIKE ? OR f.uid LIKE ? OR r.phone LIKE ? OR r.group_name LIKE ? OR r.cbf_name LIKE ?)")
            search = f"%{query}%"
            params.extend([search] * 5)
        where_sql = " AND ".join(where)
        total = connection.execute(
            f"SELECT COUNT(*) FROM records r JOIN farmers f ON f.id=r.farmer_id WHERE {where_sql}", params
        ).fetchone()[0]
        rows = connection.execute(
            f"""SELECT r.*, f.uid,
                       (SELECT COUNT(*) FROM topic_statuses ts WHERE ts.record_id=r.id AND ts.followup_needed=1) AS followup_count,
                       (SELECT COUNT(*) FROM topic_statuses ts WHERE ts.record_id=r.id AND ts.retraining_needed=1) AS training_count,
                       (SELECT COUNT(*) FROM topic_statuses ts WHERE ts.record_id=r.id AND ts.confirmed_trained=1) AS confirmed_count
                FROM records r JOIN farmers f ON f.id=r.farmer_id
                WHERE {where_sql} ORDER BY r.name COLLATE NOCASE LIMIT ? OFFSET ?""",
            [*params, page_size, (page - 1) * page_size],
        ).fetchall()
        return render_template(
            "records.html", records=rows, dataset=dataset, q=query, archived=archived,
            page=page, pages=max(1, (total + page_size - 1) // page_size), total=total,
        )

    @app.get("/records/<int:record_id>")
    def record_detail(record_id):
        connection = get_db()
        refresh_time_sensitive_statuses(connection)
        record = connection.execute(
            "SELECT r.*, f.uid FROM records r JOIN farmers f ON f.id=r.farmer_id WHERE r.id=?", (record_id,)
        ).fetchone()
        if not record:
            abort(404)
        if session.get("access_scope") == "fh_dashboard" and record["dataset"] != "care":
            abort(403)
        statuses = connection.execute(
            "SELECT * FROM topic_statuses WHERE record_id=? ORDER BY topic", (record_id,)
        ).fetchall()
        related = connection.execute(
            "SELECT id, dataset, name, group_name FROM records WHERE farmer_id=? AND id<>? ORDER BY dataset",
            (record["farmer_id"], record_id),
        ).fetchall()
        if session.get("access_scope") == "fh_dashboard":
            related = []
        raw = json.loads(record["raw_data"])
        schema = get_schema(connection, record["dataset"])
        sections = group_raw_fields(schema, raw)
        followup_history = []
        followup_assessments = []
        if record["dataset"] == "training":
            next_action_labels = {item["value"]: item["label"] for item in NEXT_ACTION_OPTIONS}
            response_rows = connection.execute(
                """SELECT fr.*, e.id AS event_id, e.event_date, e.created_by, e.latitude, e.longitude,
                          e.location_accuracy_m, e.location_captured_at
                   FROM followup_responses fr
                   JOIN field_event_entries fee ON fee.id=fr.event_entry_id
                   JOIN field_events e ON e.id=fee.event_id
                   WHERE fr.record_id=? ORDER BY e.event_date DESC, fr.id DESC""",
                (record_id,),
            ).fetchall()
            for response_row in response_rows:
                response = dict(response_row)
                stored_answers = json.loads(response["answers"])
                response["answers"] = [
                    {
                        **item,
                        "photo_urls": [
                            {"url": url_for("followup_photo", filename=value["file"]), "name": value.get("name") or f"Photo {index + 1}"}
                            for index, value in enumerate(item.get("answer") or [])
                            if isinstance(value, dict) and value.get("file")
                        ] if item.get("type") == "photos" else [],
                        "display_answer": ", ".join(str(value) for value in item["answer"])
                        if isinstance(item["answer"], list)
                        else (str(item["answer"]) if item["answer"] not in {None, ""} else "-"),
                    }
                    for item in stored_answers.values()
                ]
                response["next_action_label"] = next_action_labels.get(
                    response["next_action"], response["next_action"]
                )
                followup_history.append(response)
            assessment_rows = connection.execute(
                """SELECT fa.*, e.event_date, e.created_by
                   FROM followup_assessments fa JOIN field_events e ON e.id=fa.event_id
                   WHERE fa.record_id=? ORDER BY e.event_date DESC, fa.id DESC""",
                (record_id,),
            ).fetchall()
            for assessment_row in assessment_rows:
                assessment = dict(assessment_row)
                assessment["summary"] = json.loads(assessment["summary"])
                assessment["packages"] = [dict(row) for row in connection.execute(
                    """SELECT * FROM followup_package_results
                       WHERE assessment_id=? ORDER BY package_title""",
                    (assessment["id"],),
                ).fetchall()]
                followup_assessments.append(assessment)
        return render_template(
            "record_detail.html", record=record, statuses=statuses, related=related,
            raw=raw, sections=sections, followup_history=followup_history,
            followup_assessments=followup_assessments,
        )

    @app.route("/records/new", methods=["GET", "POST"])
    def record_new():
        dataset = request.values.get("dataset", "training")
        if dataset not in {"training", "care"}:
            dataset = "training"
        connection = get_db()
        schema = get_schema(connection, dataset)
        if request.method == "POST":
            raw = raw_from_form(schema, {})
            core = canonical_fields(connection, dataset, raw, request.form.get("cbf_name", ""))
            if not core["name"]:
                flash("A participant name is required.", "error")
                return render_template("record_form.html", dataset=dataset, schema=schema, raw=raw, record=None)
            cursor = connection.execute("INSERT INTO farmers(uid, created_at) VALUES(NULL, ?)", (utc_now(),))
            farmer_id = cursor.lastrowid
            connection.execute("UPDATE farmers SET uid=? WHERE id=?", (f"ARF-{farmer_id:06d}", farmer_id))
            record_id = insert_application_record(connection, farmer_id, dataset, raw, core)
            rebuild_statuses(connection, record_id, dataset, raw)
            log_audit(connection, session["username"], "create", "record", record_id, f"Created {core['name']}")
            connection.commit()
            flash("The record was added.", "success")
            return redirect(url_for("record_detail", record_id=record_id))
        return render_template("record_form.html", dataset=dataset, schema=schema, raw={}, record=None)

    @app.route("/records/<int:record_id>/edit", methods=["GET", "POST"])
    def record_edit(record_id):
        connection = get_db()
        record = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if not record:
            abort(404)
        schema = get_schema(connection, record["dataset"])
        old_raw = json.loads(record["raw_data"])
        if request.method == "POST":
            raw = raw_from_form(schema, old_raw)
            core = canonical_fields(connection, record["dataset"], raw, request.form.get("cbf_name", ""))
            if not core["name"]:
                flash("A participant name is required.", "error")
                return render_template("record_form.html", dataset=record["dataset"], schema=schema, raw=raw, record=record)
            changes = {key: {"from": old_raw.get(key), "to": raw.get(key)} for key in raw if old_raw.get(key) != raw.get(key)}
            connection.execute(
                """UPDATE records SET name=?, sex=?, age_value=?, age_group=?, phone=?, village=?, parish=?,
                          subcounty=?, district=?, group_name=?, cbf_name=?, record_status=?, raw_data=?, updated_at=?
                   WHERE id=?""",
                (
                    core["name"], core["sex"], core["age_value"], core["age_group"], core["phone"],
                    core["village"], core["parish"], core["subcounty"], core["district"], core["group_name"],
                    core["cbf_name"], core["record_status"], json.dumps(raw, ensure_ascii=False), utc_now(), record_id,
                ),
            )
            rebuild_statuses(connection, record_id, record["dataset"], raw)
            log_audit(
                connection, session["username"], "update", "record", record_id,
                f"Updated {core['name']}", changes,
            )
            connection.commit()
            flash("Changes were saved and dashboard statuses recalculated.", "success")
            return redirect(url_for("record_detail", record_id=record_id))
        return render_template(
            "record_form.html", dataset=record["dataset"], schema=schema, raw=old_raw, record=record,
        )

    @app.post("/records/<int:record_id>/archive")
    def record_archive(record_id):
        connection = get_db()
        record = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if not record:
            abort(404)
        archived = not bool(record["archived_at"])
        connection.execute(
            "UPDATE records SET archived_at=?, updated_at=? WHERE id=?",
            (utc_now() if archived else None, utc_now(), record_id),
        )
        action = "archive" if archived else "restore"
        log_audit(connection, session["username"], action, "record", record_id, f"{action.title()}d {record['name']}")
        connection.commit()
        flash("The record was archived." if archived else "The record was restored.", "success")
        return redirect(url_for("record_detail", record_id=record_id))

    @app.get("/matches")
    def matches():
        connection = get_db()
        if request.args.get("view") == "duplicates":
            return duplicate_review_page(connection)
        match_status = request.args.get("status", "pending")
        if match_status not in {"pending", "confirmed", "rejected", "all"}:
            match_status = "pending"
        page = max(1, request.args.get("page", 1, type=int))
        page_size = 20
        manual_confirmation = (
            "EXISTS (SELECT 1 FROM audit_log ma WHERE ma.entity_type='match' "
            "AND ma.entity_id=m.id AND ma.action='confirm')"
        )
        effective_status = (
            f"CASE WHEN m.status='confirmed' AND NOT {manual_confirmation} "
            "THEN 'pending' ELSE m.status END"
        )
        active_records = (
            "EXISTS (SELECT 1 FROM records ar WHERE ar.id=m.training_record_id AND ar.archived_at IS NULL) "
            "AND EXISTS (SELECT 1 FROM records ar WHERE ar.id=m.care_record_id AND ar.archived_at IS NULL)"
        )
        where = f"WHERE {active_records}" if match_status == "all" else f"WHERE {active_records} AND ({effective_status})=?"
        params = [] if match_status == "all" else [match_status]
        total = connection.execute(f"SELECT COUNT(*) FROM matches m {where}", params).fetchone()[0]
        rows = connection.execute(
            f"""SELECT m.*, ({effective_status}) AS effective_status,
                      t.name AS training_name, t.phone AS training_phone, t.group_name AS training_group,
                      t.raw_data AS training_raw_data, t.source_row AS training_source_row,
                      c.name AS care_name, c.phone AS care_phone, c.group_name AS care_group,
                      c.raw_data AS care_raw_data, c.source_row AS care_source_row,
                      ft.uid AS training_uid, fc.uid AS care_uid
               FROM matches m
               JOIN records t ON t.id=m.training_record_id JOIN records c ON c.id=m.care_record_id
               JOIN farmers ft ON ft.id=t.farmer_id JOIN farmers fc ON fc.id=c.farmer_id
               {where}
               ORDER BY CASE ({effective_status}) WHEN 'pending' THEN 0 WHEN 'confirmed' THEN 1 ELSE 2 END,
                        m.confidence DESC, t.name LIMIT ? OFFSET ?""",
            [*params, page_size, (page - 1) * page_size],
        ).fetchall()
        match_rows = []
        training_schema = get_schema(connection, "training")
        care_schema = get_schema(connection, "care")
        for row in rows:
            item = dict(row)
            try:
                item["reason_list"] = json.loads(item["reasons"])
            except (json.JSONDecodeError, TypeError):
                item["reason_list"] = [item["reasons"]]
            item["training_sections"] = populated_raw_sections(
                training_schema, item.pop("training_raw_data")
            )
            item["care_sections"] = populated_raw_sections(
                care_schema, item.pop("care_raw_data")
            )
            item["training_field_count"] = sum(len(fields) for fields in item["training_sections"].values())
            item["care_field_count"] = sum(len(fields) for fields in item["care_sections"].values())
            match_rows.append(item)
        return render_template(
            "matches.html", matches=match_rows, match_status=match_status, total=total,
            page=page, pages=max(1, (total + page_size - 1) // page_size),
        )

    @app.post("/duplicates/<int:record_a_id>/<int:record_b_id>/<decision>")
    def duplicate_decision(record_a_id, record_b_id, decision):
        if decision not in {"merge", "reject"}:
            abort(404)
        connection = get_db()
        record_ids = sorted((record_a_id, record_b_id))
        rows = connection.execute(
            "SELECT r.*, f.uid FROM records r JOIN farmers f ON f.id=r.farmer_id WHERE r.id IN (?, ?)",
            record_ids,
        ).fetchall()
        if len(rows) != 2:
            abort(404)
        by_id = {row["id"]: row for row in rows}
        left, right = by_id[record_ids[0]], by_id[record_ids[1]]
        if left["dataset"] != right["dataset"] or normalized_person_name(left["name"]) != normalized_person_name(right["name"]):
            abort(400, "These records are not a valid duplicate-name pair.")
        existing_review = connection.execute(
            "SELECT 1 FROM duplicate_reviews WHERE record_a_id=? AND record_b_id=?", record_ids
        ).fetchone()
        if existing_review:
            abort(400, "This duplicate pair has already been reviewed.")
        if decision == "reject":
            connection.execute(
                "INSERT INTO duplicate_reviews(record_a_id, record_b_id, status, reviewed_at) VALUES(?, ?, 'rejected', ?)",
                (*record_ids, utc_now()),
            )
            log_audit(
                connection, session["username"], "reject_duplicate", "duplicate", None,
                f"Kept same-name records separate: {left['name']}",
                {"record_ids": record_ids},
            )
            connection.commit()
            flash("The records will remain separate and this pair will no longer be suggested.", "success")
            return redirect(url_for("matches", view="duplicates"))

        survivor_id = request.form.get("survivor_id", type=int)
        if survivor_id not in record_ids:
            flash("Choose which record should remain active.", "error")
            return redirect(url_for("matches", view="duplicates"))
        donor_id = record_ids[1] if survivor_id == record_ids[0] else record_ids[0]
        survivor, donor = by_id[survivor_id], by_id[donor_id]
        survivor_raw = json.loads(survivor["raw_data"])
        donor_raw = json.loads(donor["raw_data"])
        conflicts = duplicate_conflicts(connection, survivor, donor)
        merged_raw = dict(survivor_raw)
        for key, value in donor_raw.items():
            if not populated_value(merged_raw.get(key)) and populated_value(value):
                merged_raw[key] = value
        for index, conflict in enumerate(conflicts):
            selected_id = request.form.get(f"choice__{index}", type=int)
            if conflict["key"] == "__cbf_name__":
                continue
            if selected_id == donor_id:
                merged_raw[conflict["key"]] = conflict["right"]
            elif selected_id == survivor_id:
                merged_raw[conflict["key"]] = conflict["left"]

        activity_added = 0
        if survivor["dataset"] == "training":
            merged_raw, activity_added, overflow_topics = merge_training_histories(survivor_raw, donor_raw, merged_raw)
            if overflow_topics:
                flash(
                    "These records contain more than three distinct training cycles for: "
                    + ", ".join(overflow_topics) + ". Review the records manually before merging.",
                    "error",
                )
                return redirect(url_for("matches", view="duplicates"))

        selected_cbf = survivor["cbf_name"] or donor["cbf_name"] or ""
        cbf_conflict = next((item for item in conflicts if item["key"] == "__cbf_name__"), None)
        if cbf_conflict:
            cbf_index = conflicts.index(cbf_conflict)
            selected_cbf_id = request.form.get(f"choice__{cbf_index}", type=int)
            selected_cbf = donor["cbf_name"] if selected_cbf_id == donor_id else survivor["cbf_name"]
        core = canonical_fields(connection, survivor["dataset"], merged_raw, selected_cbf)
        timestamp = utc_now()
        connection.execute(
            """UPDATE records SET name=?, sex=?, age_value=?, age_group=?, phone=?, village=?, parish=?,
                      subcounty=?, district=?, group_name=?, cbf_name=?, record_status=?, raw_data=?, updated_at=?
               WHERE id=?""",
            (
                core["name"], core["sex"], core["age_value"], core["age_group"], core["phone"],
                core["village"], core["parish"], core["subcounty"], core["district"], core["group_name"],
                core["cbf_name"], core["record_status"], json.dumps(merged_raw, ensure_ascii=False), timestamp,
                survivor_id,
            ),
        )
        rebuild_statuses(connection, survivor_id, survivor["dataset"], merged_raw)
        connection.execute(
            "UPDATE records SET farmer_id=?, archived_at=?, updated_at=? WHERE id=?",
            (survivor["farmer_id"], timestamp, timestamp, donor_id),
        )
        connection.execute(
            "UPDATE records SET farmer_id=? WHERE farmer_id=? AND id<>?",
            (survivor["farmer_id"], donor["farmer_id"], donor_id),
        )
        reassign_duplicate_event_history(connection, donor_id, survivor_id)
        connection.execute(
            "DELETE FROM farmers WHERE id=? AND NOT EXISTS(SELECT 1 FROM records WHERE farmer_id=?)",
            (donor["farmer_id"], donor["farmer_id"]),
        )
        connection.execute(
            """INSERT INTO duplicate_reviews(
                   record_a_id, record_b_id, survivor_record_id, status, reviewed_at
               ) VALUES(?, ?, ?, 'merged', ?)""",
            (*record_ids, survivor_id, timestamp),
        )
        log_audit(
            connection, session["username"], "merge_duplicate", "record", survivor_id,
            f"Merged duplicate record for {core['name']}",
            {
                "survivor_record_id": survivor_id,
                "archived_record_id": donor_id,
                "training_or_attendance_values_added": activity_added,
                "resolved_fields": [item["label"] for item in conflicts],
            },
        )
        connection.commit()
        flash("The duplicate was merged. Training history was combined and the other record was archived.", "success")
        return redirect(url_for("matches", view="duplicates", status="merged"))

    @app.post("/duplicates/group/<decision>")
    def duplicate_group_decision(decision):
        if decision not in {"merge", "reject"}:
            abort(404)
        connection = get_db()
        try:
            record_ids = sorted({int(value) for value in request.form.getlist("record_ids")})
        except ValueError:
            record_ids = []
        if len(record_ids) < 2:
            flash("This duplicate-name group no longer contains enough records to review.", "error")
            return redirect(url_for("matches", view="duplicates"))
        placeholders = ",".join("?" for _ in record_ids)
        rows = connection.execute(
            f"SELECT r.*, f.uid FROM records r JOIN farmers f ON f.id=r.farmer_id WHERE r.id IN ({placeholders})",
            record_ids,
        ).fetchall()
        by_id = {row["id"]: row for row in rows}
        if len(rows) != len(record_ids):
            abort(404)
        datasets = {row["dataset"] for row in rows}
        names = {normalized_person_name(row["name"]) for row in rows}
        if len(datasets) != 1 or len(names) != 1:
            abort(400, "These records are not one valid duplicate-name group.")
        if decision == "reject":
            for left_id, right_id in combinations(record_ids, 2):
                connection.execute(
                    """INSERT INTO duplicate_reviews(record_a_id, record_b_id, status, reviewed_at)
                       VALUES(?, ?, 'rejected', ?)
                       ON CONFLICT(record_a_id, record_b_id) DO UPDATE SET
                           survivor_record_id=NULL, status='rejected', reviewed_at=excluded.reviewed_at""",
                    (left_id, right_id, utc_now()),
                )
            log_audit(
                connection, session["username"], "reject_duplicate_group", "duplicate", None,
                f"Kept {len(record_ids)} same-name records separate: {rows[0]['name']}",
                {"record_ids": record_ids},
            )
            connection.commit()
            flash("These same-name records will remain separate and will not be suggested again.", "success")
            return redirect(url_for("matches", view="duplicates"))

        try:
            merge_ids = sorted({int(value) for value in request.form.getlist("merge_ids")})
        except ValueError:
            merge_ids = []
        survivor_id = request.form.get("survivor_id", type=int)
        if len(merge_ids) < 2 or any(record_id not in by_id for record_id in merge_ids) or survivor_id not in merge_ids:
            flash("Select at least two records to merge and choose one of them as the survivor.", "error")
            return redirect(url_for("matches", view="duplicates"))
        selected_rows = [by_id[record_id] for record_id in merge_ids]
        survivor = by_id[survivor_id]
        conflicts = duplicate_group_conflicts(connection, rows)
        merged_raw = json.loads(survivor["raw_data"])
        for record in selected_rows:
            record_raw = json.loads(record["raw_data"])
            for key, value in record_raw.items():
                if not populated_value(merged_raw.get(key)) and populated_value(value):
                    merged_raw[key] = value
        for index, conflict in enumerate(conflicts):
            selected_source_id = request.form.get(f"choice__{index}", type=int)
            if conflict["key"] == "__cbf_name__" or selected_source_id not in merge_ids:
                continue
            selected_value = next(
                (option["value"] for option in conflict["options"] if option["record_id"] == selected_source_id),
                None,
            )
            if selected_value is not None:
                merged_raw[conflict["key"]] = selected_value

        activity_added = 0
        if survivor["dataset"] == "training":
            accumulated_raw = json.loads(survivor["raw_data"])
            overflow_topics = []
            for record in selected_rows:
                if record["id"] == survivor_id:
                    continue
                accumulated_raw, added, overflow = merge_training_histories(
                    accumulated_raw, json.loads(record["raw_data"]), merged_raw
                )
                merged_raw = accumulated_raw
                activity_added += added
                overflow_topics.extend(overflow)
            if overflow_topics:
                flash(
                    "These records contain more than three distinct training cycles for: "
                    + ", ".join(sorted(set(overflow_topics))) + ". Review them manually before merging.",
                    "error",
                )
                return redirect(url_for("matches", view="duplicates"))

        selected_cbf = survivor["cbf_name"] or ""
        cbf_conflict = next((item for item in conflicts if item["key"] == "__cbf_name__"), None)
        if cbf_conflict:
            cbf_index = conflicts.index(cbf_conflict)
            selected_cbf_id = request.form.get(f"choice__{cbf_index}", type=int)
            if selected_cbf_id in merge_ids:
                selected_cbf = by_id[selected_cbf_id]["cbf_name"]
        if not selected_cbf:
            selected_cbf = next((record["cbf_name"] for record in selected_rows if record["cbf_name"]), "")
        core = canonical_fields(connection, survivor["dataset"], merged_raw, selected_cbf)
        timestamp = utc_now()
        connection.execute(
            """UPDATE records SET name=?, sex=?, age_value=?, age_group=?, phone=?, village=?, parish=?,
                      subcounty=?, district=?, group_name=?, cbf_name=?, record_status=?, raw_data=?, updated_at=?
               WHERE id=?""",
            (
                core["name"], core["sex"], core["age_value"], core["age_group"], core["phone"],
                core["village"], core["parish"], core["subcounty"], core["district"], core["group_name"],
                core["cbf_name"], core["record_status"], json.dumps(merged_raw, ensure_ascii=False), timestamp,
                survivor_id,
            ),
        )
        rebuild_statuses(connection, survivor_id, survivor["dataset"], merged_raw)
        donor_ids = [record_id for record_id in merge_ids if record_id != survivor_id]
        for donor_id in donor_ids:
            donor = by_id[donor_id]
            connection.execute(
                "UPDATE records SET farmer_id=?, archived_at=?, updated_at=? WHERE id=?",
                (survivor["farmer_id"], timestamp, timestamp, donor_id),
            )
            connection.execute(
                "UPDATE records SET farmer_id=? WHERE farmer_id=? AND id<>?",
                (survivor["farmer_id"], donor["farmer_id"], donor_id),
            )
            reassign_duplicate_event_history(connection, donor_id, survivor_id)
            connection.execute(
                "DELETE FROM farmers WHERE id=? AND NOT EXISTS(SELECT 1 FROM records WHERE farmer_id=?)",
                (donor["farmer_id"], donor["farmer_id"]),
            )
            pair_ids = sorted((survivor_id, donor_id))
            connection.execute(
                """INSERT INTO duplicate_reviews(
                       record_a_id, record_b_id, survivor_record_id, status, reviewed_at
                   ) VALUES(?, ?, ?, 'merged', ?)
                   ON CONFLICT(record_a_id, record_b_id) DO UPDATE SET
                       survivor_record_id=excluded.survivor_record_id, status='merged', reviewed_at=excluded.reviewed_at""",
                (*pair_ids, survivor_id, timestamp),
            )
        log_audit(
            connection, session["username"], "merge_duplicate_group", "record", survivor_id,
            f"Merged {len(merge_ids)} duplicate records for {core['name']}",
            {"survivor_record_id": survivor_id, "archived_record_ids": donor_ids,
             "training_or_attendance_values_added": activity_added},
        )
        connection.commit()
        remaining = len(record_ids) - len(merge_ids)
        message = f"Merged {len(merge_ids)} records and archived {len(donor_ids)} duplicates."
        if remaining:
            message += f" {remaining} same-name record remains for separate review."
        flash(message, "success")
        return redirect(url_for("matches", view="duplicates", status="merged"))

    @app.post("/duplicates/preview")
    def duplicate_split_preview():
        from identity_review import IdentityReviewError, prepare_split

        try:
            _rows, plans = prepare_split(get_db(), request.get_json(silent=True))
        except IdentityReviewError as error:
            return jsonify({"ok": False, "error": str(error)}), 400
        return jsonify({"ok": True, "groups": [
            {key: plan[key] for key in ("record_ids", "survivor_id", "conflicts", "errors", "unresolved")}
            for plan in plans
        ]})

    @app.post("/duplicates/split")
    def duplicate_split_save():
        from identity_review import IdentityReviewError, save_split

        try:
            result = save_split(get_db(), request.get_json(silent=True), session["username"])
        except IdentityReviewError as error:
            return jsonify({"ok": False, "error": str(error)}), 400
        return jsonify({"ok": True, **result})

    @app.post("/matches/<int:match_id>/<decision>")
    def match_decision(match_id, decision):
        if decision not in {"confirm", "reject"}:
            abort(404)
        connection = get_db()
        match = connection.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
        if not match:
            abort(404)
        manually_confirmed = connection.execute(
            "SELECT 1 FROM audit_log WHERE entity_type='match' AND entity_id=? AND action='confirm' LIMIT 1",
            (match_id,),
        ).fetchone()
        if match["status"] != "pending" and not (match["status"] == "confirmed" and not manually_confirmed):
            abort(400, "Only pending matches can be reviewed")
        if decision == "confirm":
            conflict = connection.execute(
                """SELECT m.id FROM matches m WHERE m.id<>? AND m.status='confirmed'
                   AND (m.training_record_id=? OR m.care_record_id=?)
                   AND EXISTS (SELECT 1 FROM audit_log a WHERE a.entity_type='match'
                               AND a.entity_id=m.id AND a.action='confirm') LIMIT 1""",
                (match_id, match["training_record_id"], match["care_record_id"]),
            ).fetchone()
            if conflict:
                flash("This record already belongs to another confirmed identity match.", "error")
                return redirect(url_for("matches"))
            if match["status"] == "pending":
                training = connection.execute("SELECT * FROM records WHERE id=?", (match["training_record_id"],)).fetchone()
                care = connection.execute("SELECT * FROM records WHERE id=?", (match["care_record_id"],)).fetchone()
                old_farmer = care["farmer_id"]
                connection.execute("UPDATE records SET farmer_id=? WHERE id=?", (training["farmer_id"], care["id"]))
                connection.execute(
                    "DELETE FROM farmers WHERE id=? AND NOT EXISTS(SELECT 1 FROM records WHERE farmer_id=?)",
                    (old_farmer, old_farmer),
                )
            status = "confirmed"
        else:
            if match["status"] == "confirmed" and not manually_confirmed:
                cursor = connection.execute("INSERT INTO farmers(uid, created_at) VALUES(NULL, ?)", (utc_now(),))
                new_farmer_id = cursor.lastrowid
                connection.execute("UPDATE farmers SET uid=? WHERE id=?", (f"ARF-{new_farmer_id:06d}", new_farmer_id))
                connection.execute(
                    "UPDATE records SET farmer_id=? WHERE id=?", (new_farmer_id, match["care_record_id"])
                )
            status = "rejected"
        connection.execute("UPDATE matches SET status=?, reviewed_at=? WHERE id=?", (status, utc_now(), match_id))
        log_audit(connection, session["username"], decision, "match", match_id, f"{decision.title()}ed identity match")
        connection.commit()
        flash("The identity match was updated.", "success")
        return redirect(url_for("matches"))

    @app.get("/audit")
    def audit():
        selected_user = request.args.get("user", "").strip()
        connection = get_db()
        audit_select = """SELECT a.*, COALESCE(u.display_name, a.username) AS display_name,
                                  COUNT(aur.id) AS undo_row_count,
                                  ar.revert_audit_id, ar.reverted_at, ar.reverted_by,
                                  CASE WHEN a.action='field_entry' AND a.entity_type='field_event'
                                            AND EXISTS(SELECT 1 FROM field_events fe WHERE fe.id=a.entity_id)
                                       THEN 1 ELSE 0 END AS historical_event_deletable
                           FROM audit_log a
                           LEFT JOIN users u ON u.username=a.username COLLATE NOCASE
                           LEFT JOIN audit_undo_rows aur ON aur.audit_id=a.id
                           LEFT JOIN audit_reverts ar ON ar.original_audit_id=a.id"""
        if selected_user:
            rows = connection.execute(
                audit_select + """ WHERE a.username=? COLLATE NOCASE
                                    GROUP BY a.id ORDER BY a.id DESC LIMIT 500""",
                (selected_user,),
            ).fetchall()
        else:
            rows = connection.execute(
                audit_select + " GROUP BY a.id ORDER BY a.id DESC LIMIT 500"
            ).fetchall()
        entries = []
        for row in rows:
            entry = dict(row)
            try:
                entry["change_details"] = json.loads(entry["changes"]) if entry["changes"] else {}
            except (json.JSONDecodeError, TypeError):
                entry["change_details"] = {}
            entries.append(entry)
        usernames = connection.execute(
            """SELECT DISTINCT a.username, COALESCE(u.display_name, a.username) AS display_name
               FROM audit_log a LEFT JOIN users u ON u.username=a.username COLLATE NOCASE
               ORDER BY display_name COLLATE NOCASE"""
        ).fetchall()
        return render_template("audit.html", entries=entries, usernames=usernames, selected_user=selected_user)

    @app.post("/audit/<int:audit_id>/revert")
    @admin_required
    def audit_revert(audit_id):
        connection = get_db()
        photo_files = set()
        try:
            entry = connection.execute(
                """SELECT a.*, COUNT(aur.id) AS undo_row_count
                   FROM audit_log a LEFT JOIN audit_undo_rows aur ON aur.audit_id=a.id
                   WHERE a.id=? GROUP BY a.id""",
                (audit_id,),
            ).fetchone()
            if not entry:
                raise AuditRevertError("That audit entry no longer exists.")
            if entry["entity_type"] == "field_event" and entry["entity_id"] is not None:
                photo_files = followup_photo_files(connection, entry["entity_id"])
            if not entry["undo_row_count"] and entry["action"] == "field_entry" \
                    and entry["entity_type"] == "field_event":
                _revert_id, photo_files = delete_historical_field_event(
                    connection, audit_id, session["username"]
                )
            else:
                revert_audit_action(
                    connection, audit_id, session["username"], session.get("user_id")
                )
            connection.commit()
            photo_folder = Path(current_app.config["FOLLOWUP_PHOTO_FOLDER"])
            for filename in photo_files:
                try:
                    (photo_folder / filename).unlink(missing_ok=True)
                except OSError:
                    current_app.logger.warning("Could not remove reverted follow-up photo %s", filename)
            flash(f"Audit action #{audit_id} was fully reverted.", "success")
        except AuditRevertError as error:
            connection.rollback()
            flash(str(error), "error")
        except sqlite3.DatabaseError:
            connection.rollback()
            current_app.logger.exception("Could not revert audit action %s", audit_id)
            flash(
                "The action could not be safely reverted. A newer dependent change may need to be reverted first.",
                "error",
            )
        selected_user = request.form.get("user", "").strip()
        return redirect(url_for("audit", user=selected_user) if selected_user else url_for("audit"))

    @app.route("/users", methods=["GET", "POST"])
    @admin_required
    def users():
        connection = get_db()
        cbfs = [row[0] for row in connection.execute(
            """SELECT DISTINCT cbf_name FROM records
               WHERE dataset='training' AND archived_at IS NULL
               AND TRIM(COALESCE(cbf_name,''))<>'' ORDER BY cbf_name"""
        ).fetchall()]
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            display_name = request.form.get("display_name", "").strip()
            password = request.form.get("password", "")
            if not re.fullmatch(r"[A-Za-z0-9._-]{3,50}", username):
                flash("Username must be 3–50 characters and use only letters, numbers, dots, dashes or underscores.", "error")
            elif not display_name or len(display_name) > 80:
                flash("Enter a display name of no more than 80 characters.", "error")
            elif not password:
                flash("Enter a temporary password.", "error")
            else:
                now = utc_now()
                try:
                    access_scope = request.form.get("access_scope", "full")
                    if access_scope not in {"full", "ae_user", "fh_dashboard"}:
                        access_scope = "full"
                    cbf_name = request.form.get("cbf_name", "").strip() if access_scope == "ae_user" else ""
                    if cbf_name not in cbfs:
                        cbf_name = ""
                    is_admin = 1 if request.form.get("is_admin") and access_scope == "full" else 0
                    cursor = connection.execute(
                        """INSERT INTO users(username, display_name, password_hash, is_active, is_admin,
                                             access_scope, cbf_name, created_at, updated_at)
                           VALUES(?, ?, ?, 1, ?, ?, ?, ?, ?)""",
                        (username, display_name, generate_password_hash(password),
                         is_admin, access_scope, cbf_name or None, now, now),
                    )
                    log_audit(connection, session["username"], "create", "user", cursor.lastrowid,
                              f"Created account {username}")
                    connection.commit()
                    flash(f"Account for {display_name} was created.", "success")
                    return redirect(url_for("users"))
                except sqlite3.IntegrityError:
                    connection.rollback()
                    flash("That username is already in use.", "error")
        rows = connection.execute("SELECT * FROM users ORDER BY display_name COLLATE NOCASE").fetchall()
        return render_template("users.html", users=rows, cbfs=cbfs)

    @app.post("/users/<int:user_id>/password")
    @admin_required
    def user_password(user_id):
        password = request.form.get("password", "")
        if not password:
            flash("Enter a new password.", "error")
            return redirect(url_for("users"))
        connection = get_db()
        user = connection.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            abort(404)
        connection.execute(
            "UPDATE users SET password_hash=?, updated_at=? WHERE id=?",
            (generate_password_hash(password), utc_now(), user_id),
        )
        log_audit(connection, session["username"], "reset_password", "user", user_id,
                  f"Reset password for {user['username']}")
        connection.commit()
        flash(f"Password for {user['display_name']} was updated.", "success")
        return redirect(url_for("users"))

    @app.post("/users/<int:user_id>/status")
    @admin_required
    def user_status(user_id):
        connection = get_db()
        user = connection.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            abort(404)
        if user_id == session.get("user_id"):
            flash("You cannot deactivate your own account.", "error")
            return redirect(url_for("users"))
        new_status = 0 if user["is_active"] else 1
        connection.execute("UPDATE users SET is_active=?, updated_at=? WHERE id=?", (new_status, utc_now(), user_id))
        action = "activate" if new_status else "deactivate"
        log_audit(connection, session["username"], action, "user", user_id,
                  f"{action.title()}d account {user['username']}")
        connection.commit()
        flash(f"{user['display_name']} is now {'active' if new_status else 'inactive'}.", "success")
        return redirect(url_for("users"))

    @app.post("/users/<int:user_id>/access")
    @admin_required
    def user_access(user_id):
        connection = get_db()
        user = connection.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            abort(404)
        if user_id == session.get("user_id"):
            flash("For safety, you cannot change your own access level.", "error")
            return redirect(url_for("users"))
        access_scope = request.form.get("access_scope", "full")
        if access_scope not in {"full", "ae_user", "fh_dashboard"}:
            abort(400, "Invalid access level")
        cbfs = {row[0] for row in connection.execute(
            """SELECT DISTINCT cbf_name FROM records
               WHERE dataset='training' AND archived_at IS NULL
               AND TRIM(COALESCE(cbf_name,''))<>''"""
        ).fetchall()}
        cbf_name = request.form.get("cbf_name", "").strip() if access_scope == "ae_user" else ""
        if cbf_name and cbf_name not in cbfs:
            abort(400, "Invalid CBF assignment")
        is_admin = 1 if request.form.get("is_admin") and access_scope == "full" else 0
        connection.execute(
            "UPDATE users SET access_scope=?, is_admin=?, cbf_name=?, updated_at=? WHERE id=?",
            (access_scope, is_admin, cbf_name or None, utc_now(), user_id),
        )
        label = {"full": "Full system access", "ae_user": "AE User", "fh_dashboard": "FH dashboard only"}[access_scope]
        log_audit(connection, session["username"], "change_access", "user", user_id,
                  f"Changed access for {user['username']} to {label}",
                  {"access_scope": access_scope, "cbf_name": cbf_name or None})
        connection.commit()
        flash(f"Access for {user['display_name']} was updated.", "success")
        return redirect(url_for("users"))

    @app.route("/data-entry", methods=["GET", "POST"])
    def data_entry():
        connection = get_db()
        refresh_time_sensitive_statuses(connection)
        cbfs = [row[0] for row in connection.execute(
            """SELECT DISTINCT cbf_name FROM records
               WHERE dataset='training' AND archived_at IS NULL
               AND TRIM(COALESCE(cbf_name,''))<>'' ORDER BY cbf_name"""
        ).fetchall()]
        selected_cbf = request.values.get("cbf", "").strip()
        mode = request.values.get("mode", "").strip()
        if selected_cbf not in cbfs:
            selected_cbf = ""
        if mode not in {"centralized", "followup"}:
            mode = ""

        venue_values = [row[0] for row in connection.execute(
            """SELECT DISTINCT TRIM(location) AS venue FROM field_events
               WHERE event_type='centralized'
               AND TRIM(COALESCE(location,''))<>''"""
        ).fetchall()]
        village_where = [
            "dataset='training'", "archived_at IS NULL",
            "TRIM(COALESCE(village,''))<>''",
        ]
        village_params = []
        if selected_cbf:
            village_where.append("cbf_name=?")
            village_params.append(selected_cbf)
        village_values = [row[0] for row in connection.execute(
            f"SELECT DISTINCT TRIM(village) AS venue FROM records WHERE {' AND '.join(village_where)}",
            village_params,
        ).fetchall()]
        venue_values.extend(village_values)
        # Keep the saved spelling for display while ignoring duplicate casing.
        venues = sorted({venue.casefold(): venue for venue in venue_values}.values(), key=str.casefold)
        selected_venue = request.values.get("location_choice", "").strip()
        new_venue = request.values.get("location_new", "").strip()
        if selected_venue not in venues and selected_venue != "__new__":
            selected_venue = ""

        if request.method == "POST":
            if not selected_cbf or not mode:
                flash("Select a CBF and an update type first.", "error")
            elif mode == "centralized":
                success, message = save_centralized_training(connection, selected_cbf, request.form, session["username"])
                flash(message, "success" if success else "error")
                if success:
                    return redirect(url_for("data_entry", cbf=selected_cbf, mode=mode,
                                            group=request.form.get("group", "")))
            else:
                success, message = save_followup(connection, selected_cbf, request.form, session["username"])
                flash(message, "success" if success else "error")
                if success:
                    result_event_id = session.pop("last_followup_event_id", None)
                    if result_event_id:
                        return redirect(url_for("followup_results", event_id=result_event_id))
                    return redirect(url_for("data_entry", cbf=selected_cbf, mode=mode,
                                            group=request.form.get("group", "")))

        central_eligibility = {topic: [] for topic in TRAINING_TOPICS}
        groups = []
        followup_farmers = []
        followup_topics = []
        followup_survey = None
        selected_followup_record = None
        selected_group = request.values.get("group", "").strip()
        selected_record_id = request.values.get("record_id", type=int)
        selected_event_date = valid_iso_date(request.values.get("event_date", "")) or ""
        not_visited_only = request.values.get("not_visited") == "1"
        selected_is_coordinator = is_coordinator_cbf(selected_cbf)

        if selected_cbf:
            group_where = ["dataset='training'", "archived_at IS NULL", "TRIM(COALESCE(group_name,''))<>''"]
            group_params = []
            if not selected_is_coordinator:
                group_where.append("cbf_name=?")
                group_params.append(selected_cbf)
            groups = [row[0] for row in connection.execute(
                f"SELECT DISTINCT group_name FROM records WHERE {' AND '.join(group_where)} ORDER BY group_name",
                group_params,
            ).fetchall()]
            if selected_group not in groups:
                selected_group = ""
            eligibility_rows = connection.execute(
                """SELECT r.id, r.name, r.group_name, f.uid, ts.topic
                   FROM records r JOIN farmers f ON f.id=r.farmer_id
                   JOIN topic_statuses ts ON ts.record_id=r.id
                   WHERE r.dataset='training' AND r.archived_at IS NULL
                   AND r.cbf_name=? AND ts.status_code IN ('CT', 'RT')
                   ORDER BY ts.topic, r.group_name, r.name""",
                (selected_cbf,),
            ).fetchall()
            for row in eligibility_rows:
                if row["topic"] in central_eligibility and (
                    not selected_group or row["group_name"] == selected_group
                ):
                    central_eligibility[row["topic"]].append(row)

            followup_where = [
                "r.dataset='training'", "r.archived_at IS NULL",
                "EXISTS (SELECT 1 FROM topic_statuses ts WHERE ts.record_id=r.id AND ts.status_code='FU')",
            ]
            followup_params = []
            if not selected_is_coordinator:
                followup_where.append("r.cbf_name=?")
                followup_params.append(selected_cbf)
            if selected_group:
                followup_where.append("r.group_name=?")
                followup_params.append(selected_group)
            if not_visited_only:
                followup_where.append(
                    "NOT EXISTS (SELECT 1 FROM followup_assessments fa "
                    "WHERE fa.record_id=r.id AND fa.questionnaire_version=?)"
                )
                followup_params.append(SURVEY_VERSION)
            followup_farmers = connection.execute(
                f"""SELECT r.id, r.name, r.group_name, f.uid,
                            (SELECT COUNT(*) FROM topic_statuses ts
                             WHERE ts.record_id=r.id AND ts.status_code='FU') AS due_count,
                            EXISTS(SELECT 1 FROM followup_assessments fa
                                   WHERE fa.record_id=r.id AND fa.questionnaire_version=?) AS visited_this_round
                     FROM records r JOIN farmers f ON f.id=r.farmer_id
                     WHERE {' AND '.join(followup_where)} ORDER BY r.group_name, r.name""",
                [SURVEY_VERSION, *followup_params],
            ).fetchall()
            eligible_followup_ids = {row["id"] for row in followup_farmers}
            if selected_record_id in eligible_followup_ids:
                selected_followup_record = connection.execute(
                    """SELECT r.*, f.uid FROM records r JOIN farmers f ON f.id=r.farmer_id
                       WHERE r.id=? AND r.dataset='training' AND r.archived_at IS NULL""",
                    (selected_record_id,),
                ).fetchone()
                followup_rows = connection.execute(
                    """SELECT topic, status_label, last_activity_date FROM topic_statuses
                       WHERE record_id=? AND status_code='FU' ORDER BY topic""",
                    (selected_record_id,),
                ).fetchall()
                followup_topics = [
                    dict(row)
                    for row in followup_rows
                ]
                raw = json.loads(selected_followup_record["raw_data"])
                due_topics = [row["topic"] for row in followup_rows]
                shared_rows = connection.execute(
                    """SELECT r.id, r.name, r.group_name, f.uid,
                              GROUP_CONCAT(ts.topic, char(31)) AS due_topics
                       FROM records r JOIN farmers f ON f.id=r.farmer_id
                       JOIN topic_statuses ts ON ts.record_id=r.id AND ts.status_code='FU'
                       WHERE r.dataset='training' AND r.archived_at IS NULL AND r.id<>?
                         AND LOWER(TRIM(COALESCE(r.village,'')))=LOWER(TRIM(?))
                       GROUP BY r.id HAVING COUNT(*)>0 ORDER BY r.name COLLATE NOCASE""",
                    (selected_record_id, selected_followup_record["village"] or ""),
                ).fetchall() if str(selected_followup_record["village"] or "").strip() else []
                shared_options = [
                    {"value": str(row["id"]), "label": f"{row['name']} — {row['group_name'] or 'No group'}"}
                    for row in shared_rows
                    if set(str(row["due_topics"] or "").split(chr(31))).intersection(due_topics)
                ]
                followup_survey = build_survey(
                    due_topics,
                    followup_profile(selected_followup_record, raw),
                    training_history=received_training_topics(raw),
                    previous_answers=carry_forward_answers_by_record(connection, [selected_record_id]).get(selected_record_id, {}),
                    editable_options={"a6_village": village_values, "a8_group": groups},
                    question_options={"a0_shared_person": shared_options},
                )
            else:
                selected_record_id = None

        history = connection.execute(
            """SELECT e.*, COUNT(i.id) AS entry_count, COUNT(DISTINCT i.record_id) AS farmer_count
               FROM field_events e LEFT JOIN field_event_entries i ON i.event_id=e.id
               GROUP BY e.id ORDER BY e.id DESC LIMIT 20"""
        ).fetchall()
        return render_template(
            "data_entry.html", cbfs=cbfs, selected_cbf=selected_cbf, mode=mode,
            central_eligibility=central_eligibility, topics=TRAINING_TOPICS,
            groups=groups, selected_group=selected_group, followup_farmers=followup_farmers,
            selected_record_id=selected_record_id, followup_topics=followup_topics,
            followup_survey=followup_survey, selected_followup_record=selected_followup_record,
            history=history, today=date.today().isoformat(), selected_event_date=selected_event_date,
            venues=venues, selected_venue=selected_venue, new_venue=new_venue,
            not_visited_only=not_visited_only, selected_is_coordinator=selected_is_coordinator,
        )

    @app.route("/data-entry/followup-results/<int:event_id>", methods=["GET", "POST"])
    def followup_results(event_id):
        connection = get_db()
        assessment_row = connection.execute(
            """SELECT fa.*, e.event_date, e.cbf_name, e.created_by, r.name AS beneficiary_name
               FROM followup_assessments fa
               JOIN field_events e ON e.id=fa.event_id
               JOIN records r ON r.id=fa.record_id
               WHERE fa.event_id=? AND e.event_type='followup'""",
            (event_id,),
        ).fetchone()
        if not assessment_row:
            abort(404)
        if session.get("access_scope") == "ae_user" and assessment_row["cbf_name"] != session.get("cbf_name"):
            abort(403)
        responses = [dict(row) for row in connection.execute(
            """SELECT fr.* FROM followup_responses fr
               JOIN field_event_entries fee ON fee.id=fr.event_entry_id
               WHERE fee.event_id=? AND fr.record_id=? ORDER BY fr.topic""",
            (event_id, assessment_row["record_id"]),
        ).fetchall()]
        if request.method == "POST":
            allowed_actions = {item["value"] for item in NEXT_ACTION_OPTIONS}
            selected_actions = {}
            for response in responses:
                action = request.form.get(f"action__{response['id']}", "")
                if action not in allowed_actions:
                    flash(f"Choose a valid next action for {response['topic']}.", "error")
                    break
                selected_actions[response["id"]] = action
            else:
                record = connection.execute(
                    "SELECT raw_data FROM records WHERE id=?", (assessment_row["record_id"],)
                ).fetchone()
                raw = json.loads(record["raw_data"])
                for response in responses:
                    action = selected_actions[response["id"]]
                    connection.execute(
                        "UPDATE followup_responses SET next_action=? WHERE id=?",
                        (action, response["id"]),
                    )
                    raw[f"Follow up {response['training_cycle']} next action - {response['topic']}"] = action
                connection.execute(
                    "UPDATE records SET raw_data=?, updated_at=? WHERE id=?",
                    (json.dumps(raw, ensure_ascii=False), utc_now(), assessment_row["record_id"]),
                )
                rebuild_statuses(connection, assessment_row["record_id"], "training", raw)
                log_audit(
                    connection, session["username"], "followup_actions", "field_event", event_id,
                    f"Confirmed next actions for {assessment_row['beneficiary_name']}",
                    {response["topic"]: selected_actions[response["id"]] for response in responses},
                )
                connection.commit()
                flash("The CBF decisions were saved and the training statuses were updated.", "success")
                return redirect(url_for("followup_results", event_id=event_id, finalized=1))

        assessment = dict(assessment_row)
        assessment["summary"] = json.loads(assessment["summary"])
        assessment["packages"] = [dict(row) for row in connection.execute(
            "SELECT * FROM followup_package_results WHERE assessment_id=? ORDER BY id",
            (assessment["id"],),
        ).fetchall()]
        action_labels = {item["value"]: item["label"] for item in NEXT_ACTION_OPTIONS}
        for response in responses:
            response["recommended_action"] = assessment["summary"].get("trainings", {}).get(response["topic"], {}).get("next_action", response["next_action"])
            response["recommended_action_label"] = action_labels.get(response["recommended_action"], response["recommended_action"])
        return render_template(
            "followup_results.html", assessment=assessment, responses=responses,
            action_options=NEXT_ACTION_OPTIONS, finalized=request.args.get("finalized") == "1",
        )

    @app.get("/field-app/")
    def field_app():
        connection = get_db()
        cbfs = available_ae_cbfs(connection)
        assigned_cbf = session.get("cbf_name", "") if session.get("access_scope") == "ae_user" else ""
        coordinator = is_coordinator_cbf(assigned_cbf)
        return render_template(
            "field_app.html",
            cbfs=cbfs,
            assigned_cbf=assigned_cbf,
            can_choose_cbf=session.get("access_scope") != "ae_user",
            asset_version=app.config["FIELD_APP_VERSION"],
            field_app_config={
                "appVersion": app.config["FIELD_APP_VERSION"],
                "bootstrapUrl": url_for("field_app_bootstrap"),
                "syncUrl": url_for("field_app_sync"),
                "csrfToken": csrf_token(),
                "assignedCbf": assigned_cbf,
                "canChooseCbf": session.get("access_scope") != "ae_user",
                "isCoordinator": coordinator,
                "username": session.get("username", ""),
                "environment": "test" if session.get("test_environment") else "live",
            },
        )

    @app.get("/field-app/manifest.webmanifest")
    def field_app_manifest():
        response = make_response(json.dumps({
            "name": "ARFSA AE Field App",
            "short_name": "ARFSA Field",
            "description": "Offline centralized training and beneficiary follow-up entry.",
            "start_url": url_for("field_app"),
            "scope": "/field-app/",
            "display": "standalone",
            "background_color": "#f8f8f8",
            "theme_color": "#087880",
            "icons": [
                {
                    "src": url_for("static", filename="img/field-app-icon-192.png"),
                    "sizes": "192x192", "type": "image/png", "purpose": "any maskable",
                },
                {
                    "src": url_for("static", filename="img/field-app-icon-512.png"),
                    "sizes": "512x512", "type": "image/png", "purpose": "any maskable",
                },
            ],
        }))
        response.headers["Content-Type"] = "application/manifest+json"
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/field-app/service-worker.js")
    def field_app_service_worker():
        source = (Path(app.static_folder) / "js" / "field-service-worker.js").read_text(encoding="utf-8")
        response = make_response(source.replace("__FIELD_APP_VERSION__", app.config["FIELD_APP_VERSION"]))
        response.headers["Content-Type"] = "application/javascript; charset=utf-8"
        response.headers["Cache-Control"] = "no-cache"
        response.headers["Service-Worker-Allowed"] = "/field-app/"
        return response

    @app.get("/field-app/scoring-rules.js")
    def field_app_scoring_rules():
        response = make_response("window.ARFSA_FOLLOWUP_RULES = " + json.dumps(offline_scoring_rules()) + ";\n")
        response.headers["Content-Type"] = "application/javascript; charset=utf-8"
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/field-app/api/bootstrap")
    def field_app_bootstrap():
        connection = get_db()
        cbf_name = field_app_cbf(connection, request.args.get("cbf", ""))
        refresh_time_sensitive_statuses(connection)
        return jsonify({"ok": True, "package": build_field_app_package(connection, cbf_name)})

    @app.post("/field-app/api/sync")
    def field_app_sync():
        connection = get_db()
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"ok": False, "error": "The synchronization data is invalid."}), 400
        requested_cbf = str(payload.get("cbf") or "").strip()
        cbf_name = field_app_cbf(connection, requested_cbf)
        device_id = str(payload.get("deviceId") or "").strip()
        submissions = payload.get("submissions")
        if not device_id or len(device_id) > 100:
            return jsonify({"ok": False, "error": "This tablet could not be identified."}), 400
        if not isinstance(submissions, list) or len(submissions) > 100:
            return jsonify({"ok": False, "error": "Synchronize no more than 100 submissions at once."}), 400
        results = [
            synchronize_field_submission(
                connection, cbf_name, submission, session["username"], device_id
            )
            for submission in submissions
        ]
        for result in results:
            if result["status"] in {"accepted", "duplicate"} and result.get("eventId"):
                assessment = connection.execute(
                    """SELECT fa.summary FROM followup_assessments fa
                       JOIN field_events e ON e.id=fa.event_id
                       WHERE fa.event_id=? AND e.cbf_name=?""",
                    (result["eventId"], cbf_name),
                ).fetchone()
                if assessment:
                    result["outcome"] = json.loads(assessment["summary"])
        return jsonify({
            "ok": all(item["status"] in {"accepted", "duplicate"} for item in results),
            "results": results,
            "serverTime": utc_now(),
        })

    @app.route("/bulk-upload", methods=["GET", "POST"])
    def bulk_upload():
        result = None
        if request.method == "POST":
            dataset = request.form.get("dataset", "")
            if dataset not in {"training", "care"}:
                flash("Choose whether this is AE or FH data.", "error")
                return render_template("bulk_upload.html", result=result)
            upload = request.files.get("workbook")
            if not upload or not upload.filename:
                flash("Choose the updated Excel file.", "error")
                return render_template("bulk_upload.html", result=result)
            if Path(upload.filename).suffix.lower() not in {".xlsx", ".xlsm"}:
                flash("Upload an .xlsx or .xlsm Excel workbook.", "error")
                return render_template("bulk_upload.html", result=result)
            connection = get_db()
            try:
                result = (append_farmer_database if dataset == "training" else append_care_database)(connection, upload.stream, upload.filename)
                dataset_label = "AE" if dataset == "training" else "FH"
                log_audit(connection, session["username"], "bulk_upload", "records", None,
                          f"{dataset_label} bulk upload added {result['added']} beneficiaries and updated {result['updated']} existing records",
                          {"dataset": dataset, "filename": Path(upload.filename).name, **result})
                connection.commit()
                if result.get('monthly_updated'):
                    flash(f"Updated {result['monthly_updated']} FH monthly reports from the same workbook.", 'success')
                if result["added"] or result["updated"]:
                    flash(f"Safely added {result['added']} new beneficiaries and updated {result['updated']} existing beneficiaries.", "success")
                else:
                    if not result.get('monthly_updated'):
                        flash("No new beneficiaries or training entries were found; the database was not changed.", "success")
            except BulkImportError as exc:
                connection.rollback()
                flash(str(exc), "error")
            except Exception as exc:
                connection.rollback()
                app.logger.exception("Bulk Farmer Database upload failed")
                flash(f"The workbook could not be imported ({type(exc).__name__}). No data was added.", "error")
        return render_template("bulk_upload.html", result=result)

    @app.errorhandler(404)
    def not_found(_error):
        return render_template("404.html"), 404

    return app


def valid_iso_date(value: str) -> str | None:
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        return None


def followup_profile(record, raw: dict) -> dict[str, str]:
    """Build the editable Section A snapshot from canonical and workbook fields."""
    pwd_value = str(raw.get("PWD (Y/N)") or "").strip().casefold()
    pwd = "yes" if pwd_value in {"y", "yes", "1", "true"} else "no" if pwd_value else ""
    recorded_birth = str(
        raw.get("Year of birth") or raw.get("Date of birth")
        or raw.get("Date of Birth") or raw.get("DOB") or ""
    ).strip()
    birth_year = recorded_birth[:4] if re.fullmatch(r"\d{4}(?:-\d{2}-\d{2})?", recorded_birth) else ""
    return {
        "district": record["district"] or "",
        "subcounty": record["subcounty"] or "",
        "village": record["village"] or "",
        "beneficiary_type": str(raw.get("Benefiary Type") or raw.get("Beneficiary Type") or "").strip(),
        "phone": record["phone"] or "",
        "sex": record["sex"] or "",
        "pwd": pwd,
        "birth_year": birth_year,
        "age": record["age_value"] or "",
        "age_group": record["age_group"] or record["age_value"] or "",
        "group": record["group_name"] or "",
        "education": str(raw.get("Level of education completed") or "").strip(),
    }


def carry_forward_answers_by_record(connection, record_ids: list[int]) -> dict[int, dict[str, Any]]:
    """Return the latest saved carry-forward answers for each requested beneficiary."""
    if not record_ids:
        return {}
    placeholders = ",".join("?" for _ in record_ids)
    rows = connection.execute(
        f"""SELECT record_id, answers FROM followup_responses
            WHERE record_id IN ({placeholders}) ORDER BY id DESC""",
        record_ids,
    ).fetchall()
    carried: dict[int, dict[str, Any]] = {}
    for row in rows:
        record_id = row["record_id"]
        try:
            payload = json.loads(row["answers"])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        record_answers = carried.setdefault(record_id, {})
        for question_id in CARRY_FORWARD_QUESTION_IDS:
            if question_id not in record_answers and question_id in payload:
                record_answers[question_id] = payload[question_id].get("answer", "")
    carried = {record_id: answers for record_id, answers in carried.items() if answers}
    return carried


def available_ae_cbfs(connection) -> list[str]:
    return [row[0] for row in connection.execute(
        """SELECT DISTINCT cbf_name FROM records
           WHERE dataset='training' AND archived_at IS NULL
           AND TRIM(COALESCE(cbf_name,''))<>'' ORDER BY cbf_name"""
    ).fetchall()]


def field_app_cbf(connection, requested_cbf: str) -> str:
    requested_cbf = str(requested_cbf or "").strip()
    if session.get("access_scope") == "ae_user":
        assigned_cbf = str(session.get("cbf_name") or "").strip()
        if not assigned_cbf:
            abort(403, "An administrator must assign this account to a CBF before field data can be prepared.")
        if requested_cbf and requested_cbf != assigned_cbf:
            abort(403, "This account cannot access another CBF's field data.")
        return assigned_cbf
    if requested_cbf not in available_ae_cbfs(connection):
        abort(400, "Select a valid CBF before preparing field data.")
    return requested_cbf


def build_field_app_package(connection, cbf_name: str) -> dict:
    coordinator = is_coordinator_cbf(cbf_name)
    ownership_clause = "" if coordinator else "AND r.cbf_name=?"
    record_params = [SURVEY_VERSION] + ([] if coordinator else [cbf_name])
    records = connection.execute(
        f"""SELECT r.*, f.uid,
                  EXISTS(SELECT 1 FROM followup_assessments fa
                         WHERE fa.record_id=r.id AND fa.questionnaire_version=?) AS visited_this_round
           FROM records r JOIN farmers f ON f.id=r.farmer_id
           WHERE r.dataset='training' AND r.archived_at IS NULL {ownership_clause}
           ORDER BY r.group_name COLLATE NOCASE, r.name COLLATE NOCASE""",
        record_params,
    ).fetchall()
    record_ids = [row["id"] for row in records]
    statuses_by_record = defaultdict(list)
    if record_ids:
        placeholders = ",".join("?" for _ in record_ids)
        status_rows = connection.execute(
            f"""SELECT record_id, topic, status_code, status_label, last_activity_date
                FROM topic_statuses WHERE record_id IN ({placeholders})
                ORDER BY topic""",
            record_ids,
        ).fetchall()
        for row in status_rows:
            statuses_by_record[row["record_id"]].append({
                "topic": row["topic"],
                "code": row["status_code"],
                "label": row["status_label"],
                "lastActivityDate": row["last_activity_date"],
            })
    farmers = []
    previous_answers_by_record = carry_forward_answers_by_record(connection, record_ids)
    ct_entries = 0
    followup_entries = 0
    for row in records:
        statuses = statuses_by_record[row["id"]]
        raw = json.loads(row["raw_data"])
        ct_topics = [
            item["topic"] for item in statuses if item["code"] in {"CT", "RT"}
            and (not coordinator or row["cbf_name"] == cbf_name)
        ]
        followup_topics = [item for item in statuses if item["code"] == "FU"]
        ct_entries += len(ct_topics)
        followup_entries += len(followup_topics)
        farmers.append({
            "id": row["id"],
            "uid": row["uid"],
            "name": row["name"],
            "group": row["group_name"] or "",
            "village": row["village"] or "",
            "assignedCbf": row["cbf_name"] or "",
            "visitedThisRound": bool(row["visited_this_round"]),
            "updatedAt": row["updated_at"],
            "ctTopics": ct_topics,
            "followupTopics": followup_topics,
            "trainingHistory": received_training_topics(raw),
            "profile": followup_profile(row, raw),
            "previousAnswers": previous_answers_by_record.get(row["id"], {}),
        })
    venue_sql = """SELECT location AS venue FROM field_events
           WHERE event_type='centralized' AND TRIM(COALESCE(location,''))<>''
           UNION
           SELECT village AS venue FROM records
           WHERE dataset='training' AND archived_at IS NULL
           AND TRIM(COALESCE(village,''))<>''"""
    venue_params = []
    if not coordinator:
        venue_sql += " AND cbf_name=?"
        venue_params.append(cbf_name)
    venue_sql += " ORDER BY venue COLLATE NOCASE"
    venue_rows = connection.execute(venue_sql, venue_params).fetchall()
    return {
        "version": 2,
        "questionnaireVersion": SURVEY_VERSION,
        "preparedAt": utc_now(),
        "cbf": cbf_name,
        "coordinator": coordinator,
        "topics": list(TRAINING_TOPICS),
        "questionnaires": {
            topic: questionnaire_for_topic(topic) for topic in TRAINING_TOPICS
        },
        "survey": build_survey(
            TRAINING_TOPICS,
            editable_options={
                "a6_village": [row["village"] for row in records if row["village"]],
                "a8_group": [row["group_name"] for row in records if row["group_name"]],
            },
        ),
        "groups": sorted(
            {row["group_name"] for row in records if row["group_name"]}, key=str.casefold
        ),
        "venues": [row["venue"] for row in venue_rows],
        "farmers": farmers,
        "summary": {
            "farmers": len(farmers),
            "centralizedTrainingDue": ct_entries,
            "followupsDue": followup_entries,
        },
    }


def synchronize_field_submission(connection, cbf_name: str, submission, username: str, device_id: str) -> dict:
    if not isinstance(submission, dict):
        return {"id": "", "status": "rejected", "message": "A submission is not valid."}
    submission_id = str(submission.get("id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._:-]{8,100}", submission_id):
        return {"id": submission_id[:100], "status": "rejected", "message": "The submission ID is invalid."}
    existing = connection.execute(
        "SELECT id FROM field_events WHERE client_submission_id=?", (submission_id,)
    ).fetchone()
    if existing:
        return {
            "id": submission_id, "status": "duplicate", "eventId": existing["id"],
            "message": "This submission was already synchronized.",
        }
    submission_type = str(submission.get("type") or "").strip()
    client_created_at = str(submission.get("createdAt") or "")[:40] or None
    pairs = [("event_date", str(submission.get("eventDate") or ""))]
    save_kwargs = {}
    if submission_type == "centralized":
        location = str(submission.get("location") or "").strip()
        entries = submission.get("entries")
        if not isinstance(entries, list) or not entries or len(entries) > 2000:
            return {"id": submission_id, "status": "rejected", "message": "Select at least one valid attendee."}
        grouped_entries = defaultdict(list)
        try:
            for entry in entries:
                record_id = int(entry.get("recordId"))
                topic = str(entry.get("topic") or "")
                if record_id <= 0 or topic not in TRAINING_TOPICS:
                    raise ValueError
                grouped_entries[topic].append(str(record_id))
        except (AttributeError, TypeError, ValueError):
            return {"id": submission_id, "status": "rejected", "message": "One or more attendance entries are invalid."}
        pairs.append(("location_choice", location))
        pairs.append(("topic_count", str(len(grouped_entries))))
        for index, (topic, record_ids) in enumerate(grouped_entries.items()):
            pairs.append((f"topic__{index}", topic))
            pairs.extend((f"attendee__{index}", record_id) for record_id in record_ids)
        save_function = save_centralized_training
    elif submission_type == "followup":
        try:
            record_id = int(submission.get("recordId"))
        except (TypeError, ValueError):
            record_id = 0
        survey_answers = submission.get("answers")
        survey_topics = submission.get("topics")
        if isinstance(survey_answers, dict):
            if (
                record_id <= 0 or not isinstance(survey_topics, list) or not survey_topics
                or len(survey_topics) > len(TRAINING_TOPICS)
                or any(topic not in TRAINING_TOPICS for topic in survey_topics)
            ):
                return {"id": submission_id, "status": "rejected", "message": "Complete at least one valid follow-up training type."}
            pairs.extend((("record_id", str(record_id)), ("topic_count", str(len(survey_topics))),
                          ("survey_version", SURVEY_VERSION)))
            for index, topic in enumerate(survey_topics):
                pairs.append((f"topic__{index}", topic))
            for question in all_questions_for_topics(survey_topics):
                value = survey_answers.get(question["id"], "")
                if question["id"] == "b12_macrofauna":
                    value = {"zero": 0, "one_four": 1, "five_plus": 5}.get(value, value)
                elif question["id"] == "f2_visible":
                    value = {"five_plus": 5, "under_five": survey_answers.get("f2_1_total", 1)}.get(value, value)
                field_name = f"survey_answer__{question['id']}"
                if question["type"] == "gps" and isinstance(value, (dict, list)):
                    pairs.append((field_name, json.dumps(value)))
                elif question["type"] == "photos" and isinstance(value, list):
                    pairs.append((field_name, json.dumps(value)))
                elif isinstance(value, list):
                    pairs.extend((field_name, str(item)) for item in value)
                elif value is not None:
                    pairs.append((field_name, str(value)))
        else:
            # Backward compatibility for tablets prepared with the former topic forms.
            responses = submission.get("responses")
            if record_id <= 0 or not isinstance(responses, list) or not responses or len(responses) > len(TRAINING_TOPICS):
                return {"id": submission_id, "status": "rejected", "message": "Complete at least one valid follow-up topic."}
            pairs.extend((("record_id", str(record_id)), ("topic_count", str(len(responses)))))
            for index, response_entry in enumerate(responses):
                if not isinstance(response_entry, dict):
                    return {"id": submission_id, "status": "rejected", "message": "A follow-up response is invalid."}
                topic = str(response_entry.get("topic") or "")
                answers = response_entry.get("answers")
                if topic not in TRAINING_TOPICS or not isinstance(answers, dict):
                    return {"id": submission_id, "status": "rejected", "message": "A follow-up topic or answer is invalid."}
                pairs.append((f"topic__{index}", topic))
                for question in questionnaire_for_topic(topic)["questions"]:
                    value = answers.get(question["id"], "")
                    field_name = f"answer__{index}__{question['id']}"
                    if isinstance(value, list):
                        pairs.extend((field_name, str(item)) for item in value)
                    elif value is not None:
                        pairs.append((field_name, str(value)))
        geo_location = submission.get("geoLocation")
        if geo_location is not None:
            if not isinstance(geo_location, dict):
                return {"id": submission_id, "status": "rejected", "message": "The captured location is invalid."}
            try:
                latitude = float(geo_location.get("latitude"))
                longitude = float(geo_location.get("longitude"))
                accuracy = float(geo_location.get("accuracy"))
            except (TypeError, ValueError):
                return {"id": submission_id, "status": "rejected", "message": "The captured location is invalid."}
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180 and 0 <= accuracy <= 100000):
                return {"id": submission_id, "status": "rejected", "message": "The captured location is outside the valid range."}
            save_kwargs = {
                "latitude": latitude,
                "longitude": longitude,
                "location_accuracy_m": accuracy,
                "location_captured_at": str(geo_location.get("capturedAt") or "")[:40] or None,
            }
        save_function = save_followup
    else:
        return {"id": submission_id, "status": "rejected", "message": "The submission type is not supported."}

    try:
        success, message = save_function(
            connection, cbf_name, MultiDict(pairs), username,
            client_submission_id=submission_id,
            device_id=device_id,
            client_created_at=client_created_at,
            **save_kwargs,
        )
    except sqlite3.IntegrityError:
        connection.rollback()
        existing = connection.execute(
            "SELECT id FROM field_events WHERE client_submission_id=?", (submission_id,)
        ).fetchone()
        if existing:
            return {
                "id": submission_id, "status": "duplicate", "eventId": existing["id"],
                "message": "This submission was already synchronized.",
            }
        return {"id": submission_id, "status": "rejected", "message": "The server could not store this submission."}
    if not success:
        return {"id": submission_id, "status": "rejected", "message": message}
    event = connection.execute(
        "SELECT id FROM field_events WHERE client_submission_id=?", (submission_id,)
    ).fetchone()
    return {
        "id": submission_id, "status": "accepted", "eventId": event["id"] if event else None,
        "message": message,
    }


def save_centralized_training(
    connection, cbf_name: str, values, username: str, *,
    client_submission_id: str | None = None, device_id: str | None = None,
    client_created_at: str | None = None,
):
    event_date = valid_iso_date(values.get("event_date", ""))
    location_choice = values.get("location_choice", "").strip()
    if location_choice == "__new__":
        location = values.get("location_new", "").strip()
    elif location_choice:
        location = location_choice
    else:
        # Keep accepting the original field name for older clients and saved forms.
        location = values.get("location", "").strip()
    if not event_date:
        return False, "Enter a valid training date."
    if not location:
        return False, "Select an existing meeting venue or enter a new one."

    requested_entries = set()
    topic_count = min(max(values.get("topic_count", 0, type=int), 0), len(TRAINING_TOPICS))
    for index in range(topic_count):
        topic = values.get(f"topic__{index}", "")
        if topic not in TRAINING_TOPICS:
            continue
        for raw_record_id in values.getlist(f"attendee__{index}"):
            try:
                requested_entries.add((int(raw_record_id), topic))
            except (TypeError, ValueError):
                return False, "One of the selected participants is invalid. Reload the page and try again."
    if not requested_entries:
        return False, "Select at least one CT-eligible participant under a training topic."

    records_to_update = {}
    for record_id, topic in requested_entries:
        record = connection.execute(
            """SELECT r.* FROM records r JOIN topic_statuses ts ON ts.record_id=r.id
               WHERE r.id=? AND r.dataset='training' AND r.archived_at IS NULL
               AND r.cbf_name=? AND ts.topic=? AND ts.status_code IN ('CT', 'RT')""",
            (record_id, cbf_name, topic),
        ).fetchone()
        if not record:
            return False, "A selected beneficiary is no longer eligible for centralized training. Reload the page."
        if record_id not in records_to_update:
            records_to_update[record_id] = {"record": record, "raw": json.loads(record["raw_data"])}
        raw = records_to_update[record_id]["raw"]
        training_cycle = next(
            (
                number for number in range(1, 4)
                if raw.get(f"Training {number} - {topic}") in {None, ""}
            ),
            None,
        )
        if training_cycle is None:
            return False, f"No additional training cycle is available for {topic}."
        raw[f"Training {training_cycle} - {topic}"] = event_date

    cursor = connection.execute(
        """INSERT INTO field_events(
               event_type, cbf_name, event_date, location, created_by, created_at,
               client_submission_id, device_id, client_created_at
           ) VALUES('centralized', ?, ?, ?, ?, ?, ?, ?, ?)""",
        (cbf_name, event_date, location, username, utc_now(),
         client_submission_id, device_id, client_created_at),
    )
    event_id = cursor.lastrowid
    for record_id, topic in sorted(requested_entries):
        connection.execute(
            "INSERT INTO field_event_entries(event_id, record_id, topic, score) VALUES(?, ?, ?, NULL)",
            (event_id, record_id, topic),
        )
    for record_id, update in records_to_update.items():
        connection.execute(
            "UPDATE records SET raw_data=?, updated_at=? WHERE id=?",
            (json.dumps(update["raw"], ensure_ascii=False), utc_now(), record_id),
        )
        rebuild_statuses(connection, record_id, "training", update["raw"])
    selected_topics = sorted({topic for _, topic in requested_entries})
    log_audit(
        connection, username, "field_entry", "field_event", event_id,
        f"Recorded centralized training for {len(records_to_update)} beneficiaries",
        {"cbf": cbf_name, "date": event_date, "location": location, "topics": selected_topics,
         "attendance_entries": len(requested_entries)},
    )
    connection.commit()
    return True, f"Centralized training saved for {len(records_to_update)} beneficiaries."


def save_followup(
    connection, cbf_name: str, values, username: str, *,
    client_submission_id: str | None = None, device_id: str | None = None,
    client_created_at: str | None = None,
    latitude: float | None = None, longitude: float | None = None,
    location_accuracy_m: float | None = None, location_captured_at: str | None = None,
):
    if values.get("survey_version", "").strip():
        return save_scored_followup(
            connection, cbf_name, values, username,
            client_submission_id=client_submission_id,
            device_id=device_id,
            client_created_at=client_created_at,
            latitude=latitude,
            longitude=longitude,
            location_accuracy_m=location_accuracy_m,
            location_captured_at=location_captured_at,
        )
    event_date = valid_iso_date(values.get("event_date", ""))
    record_id = values.get("record_id", type=int)
    if not event_date:
        return False, "Enter a valid follow-up date."
    record = connection.execute(
        """SELECT * FROM records WHERE id=? AND dataset='training'
           AND archived_at IS NULL AND cbf_name=?""",
        (record_id, cbf_name),
    ).fetchone() if record_id else None
    if not record:
        return False, "Select a beneficiary belonging to this CBF."

    raw = json.loads(record["raw_data"])
    response_entries = []
    topic_count = min(max(values.get("topic_count", 0, type=int), 0), len(TRAINING_TOPICS))
    for index in range(topic_count):
        topic = values.get(f"topic__{index}", "")
        if topic not in TRAINING_TOPICS:
            return False, "One of the submitted follow-up topics is invalid."
        questionnaire = questionnaire_for_topic(topic)
        answers = {}
        has_any_answer = False
        adoption_rate = None
        next_action = ""
        for question in questionnaire["questions"]:
            field_name = f"answer__{index}__{question['id']}"
            if question["type"] == "multi":
                answer = [item.strip() for item in values.getlist(field_name) if item.strip()]
                has_any_answer = has_any_answer or bool(answer)
            else:
                answer = values.get(field_name, "").strip()
                has_any_answer = has_any_answer or bool(answer)

            allowed_options = question.get("options", [])
            allowed_values = {
                option["value"] if isinstance(option, dict) else option
                for option in allowed_options
            }
            submitted_values = answer if isinstance(answer, list) else ([answer] if answer else [])
            if allowed_values and any(item not in allowed_values for item in submitted_values):
                return False, f"An answer for {topic} is not valid. Reload the page and try again."
            if question["type"] == "number" and answer:
                parsed = parse_number(answer)
                if parsed is None or parsed < 0:
                    return False, f"Enter a valid non-negative number for {question['label']}."
                answer = parsed
            if question["type"] == "adoption_rate" and answer:
                adoption_rate = parse_number(answer)
                if adoption_rate is None or not 0 <= adoption_rate <= 100:
                    return False, f"Enter an adoption rate between 0 and 100 for {topic}."
                answer = adoption_rate
            if question["type"] == "next_action":
                next_action = answer
            answers[question["id"]] = {
                "source_id": question["source_id"],
                "question": question["label"],
                "answer": answer,
            }

        if not has_any_answer:
            continue
        if adoption_rate is None:
            return False, f"Enter the adoption rate for {topic}."
        if not next_action:
            return False, f"Choose what should happen next for {topic}."
        due = connection.execute(
            "SELECT 1 FROM topic_statuses WHERE record_id=? AND topic=? AND status_code='FU'",
            (record_id, topic),
        ).fetchone()
        if not due:
            return False, f"{topic} is no longer due for follow-up. Reload the page."
        cycle = next((number for number in range(1, 4)
                      if raw.get(f"Training {number} - {topic}") not in {None, ""}
                      and raw.get(f"Follow up {number} - {topic}") in {None, ""}), None)
        if cycle is None:
            cycle = next((number for number in range(3, 0, -1)
                          if raw.get(f"Training {number} - {topic}") not in {None, ""}), None)
        if cycle is None:
            return False, f"No training cycle was found for {topic}."
        response_entries.append({
            "topic": topic,
            "score": adoption_rate,
            "cycle": cycle,
            "answers": answers,
            "next_action": next_action,
        })
    if not response_entries:
        return False, "Complete at least one follow-up questionnaire."

    for entry in response_entries:
        topic, score, cycle = entry["topic"], entry["score"], entry["cycle"]
        raw[f"Follow up {cycle} - {topic}"] = event_date
        raw[f"Follow up {cycle} score - {topic}"] = score
        raw[f"Follow up {cycle} next action - {topic}"] = entry["next_action"]
    cursor = connection.execute(
        """INSERT INTO field_events(
               event_type, cbf_name, event_date, location, created_by, created_at,
               client_submission_id, device_id, client_created_at,
               latitude, longitude, location_accuracy_m, location_captured_at
           ) VALUES('followup', ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (cbf_name, event_date, username, utc_now(),
         client_submission_id, device_id, client_created_at,
         latitude, longitude, location_accuracy_m, location_captured_at),
    )
    event_id = cursor.lastrowid
    for entry in response_entries:
        entry_cursor = connection.execute(
            "INSERT INTO field_event_entries(event_id, record_id, topic, score) VALUES(?, ?, ?, ?)",
            (event_id, record_id, entry["topic"], entry["score"]),
        )
        connection.execute(
            """INSERT INTO followup_responses(
                   event_entry_id, record_id, topic, training_cycle, questionnaire_version,
                   answers, adoption_rate, next_action, created_at
               ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                entry_cursor.lastrowid, record_id, entry["topic"], entry["cycle"],
                QUESTIONNAIRE_VERSION, json.dumps(entry["answers"], ensure_ascii=False),
                entry["score"], entry["next_action"], utc_now(),
            ),
        )
    connection.execute(
        "UPDATE records SET raw_data=?, updated_at=? WHERE id=?",
        (json.dumps(raw, ensure_ascii=False), utc_now(), record_id),
    )
    rebuild_statuses(connection, record_id, "training", raw)
    log_audit(
        connection, username, "field_entry", "field_event", event_id,
        f"Recorded follow-up for {record['name']}",
        {"cbf": cbf_name, "date": event_date,
         "scores": {entry["topic"]: entry["score"] for entry in response_entries},
         "next_actions": {entry["topic"]: entry["next_action"] for entry in response_entries},
         "location": {
             "latitude": latitude, "longitude": longitude,
             "accuracy_m": location_accuracy_m, "captured_at": location_captured_at,
         } if latitude is not None and longitude is not None else None},
    )
    connection.commit()
    return True, f"Follow-up saved for {record['name']}."


def parse_gps_track(value: Any) -> dict[str, list[dict[str, Any]]] | str:
    """Return a bounded, normalized A7 GPS track, or an empty string."""
    if value is None or value == "":
        return ""
    try:
        payload = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("A7 contains an invalid GPS track.") from error
    points = payload.get("points") if isinstance(payload, dict) else payload
    if not isinstance(points, list) or not points or len(points) > 5000:
        raise ValueError("A7 must contain between 1 and 5,000 GPS points.")
    normalized = []
    for point in points:
        if not isinstance(point, dict):
            raise ValueError("A7 contains an invalid GPS point.")
        try:
            latitude = float(point.get("latitude"))
            longitude = float(point.get("longitude"))
            accuracy = float(point.get("accuracy", 0))
        except (TypeError, ValueError) as error:
            raise ValueError("A7 contains an invalid GPS point.") from error
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180 and 0 <= accuracy <= 100000):
            raise ValueError("A7 contains a GPS point outside the valid range.")
        captured_at = str(point.get("capturedAt") or "")[:40]
        normalized.append({
            "latitude": latitude, "longitude": longitude,
            "accuracy": accuracy, "capturedAt": captured_at,
        })
    return {"points": normalized}


def save_scored_followup(
    connection, cbf_name: str, values, username: str, *,
    client_submission_id: str | None = None, device_id: str | None = None,
    client_created_at: str | None = None,
    latitude: float | None = None, longitude: float | None = None,
    location_accuracy_m: float | None = None, location_captured_at: str | None = None,
):
    """Validate, score and persist the adaptive follow-up survey."""
    event_date = valid_iso_date(values.get("event_date", ""))
    record_id = values.get("record_id", type=int)
    if not event_date:
        return False, "Enter a valid follow-up date."
    record = connection.execute(
        """SELECT r.*, f.uid FROM records r JOIN farmers f ON f.id=r.farmer_id
           WHERE r.id=? AND r.dataset='training' AND r.archived_at IS NULL
             AND (r.cbf_name=? OR ?=1)""",
        (record_id, cbf_name, int(is_coordinator_cbf(cbf_name))),
    ).fetchone() if record_id else None
    if not record:
        return False, "Select a beneficiary available to this CBF."

    topic_count = min(max(values.get("topic_count", 0, type=int), 0), len(TRAINING_TOPICS))
    topics = []
    for index in range(topic_count):
        topic = values.get(f"topic__{index}", "")
        if topic not in TRAINING_TOPICS or topic in topics:
            return False, "One of the submitted training types is invalid."
        topics.append(topic)
    if not topics:
        return False, "No applicable training type was included in this follow-up."

    due_topics = {
        row["topic"] for row in connection.execute(
            "SELECT topic FROM topic_statuses WHERE record_id=? AND status_code='FU'",
            (record_id,),
        ).fetchall()
    }
    if any(topic not in due_topics for topic in topics):
        return False, "One or more training types are no longer due. Reload the survey and try again."

    raw = json.loads(record["raw_data"])
    training_history = received_training_topics(raw)
    answers: dict[str, Any] = {}
    answer_records: dict[str, dict[str, Any]] = {}
    pending_photos: dict[str, list[dict[str, Any]]] = {}
    seen_questions = set()
    for item in all_questions_for_topics(topics):
        if item["id"] in seen_questions or item["type"] == "training_list" \
                or (item.get("training_topic") and item["training_topic"] not in training_history):
            continue
        seen_questions.add(item["id"])
        field_name = f"survey_answer__{item['id']}"
        if item["type"] in {"multi", "ranking"}:
            answer: Any = [value.strip() for value in values.getlist(field_name) if value.strip()]
        elif item["type"] == "photos":
            try:
                pending_photos[item["id"]] = prepare_followup_photos(field_name, values.get(field_name, ""))
            except ValueError as error:
                return False, str(error)
            answer = [{"name": photo["name"]} for photo in pending_photos[item["id"]]]
        elif item["type"] == "gps":
            try:
                answer = parse_gps_track(values.get(field_name, "").strip())
            except ValueError as error:
                return False, str(error)
        else:
            answer = values.get(field_name, "").strip()
        if item["id"] == "b12_macrofauna":
            answer = {"zero": 0, "one_four": 1, "five_plus": 5}.get(answer, answer)
        elif item["id"] == "f2_visible":
            answer = {"five_plus": 5, "under_five": values.get("survey_answer__f2_1_total", 1)}.get(answer, answer)
        if item["type"] == "number" and answer:
            answer = parse_number(answer)
        answers[item["id"]] = answer
        if question_is_active(item, answers):
            answer_records[item["id"]] = {
                "source_id": item["source_id"],
                "question": item["label"],
                "answer": answer,
                "type": item["type"],
            }

    answers = resolve_locked_answers(answers, topics)
    for item in all_questions_for_topics(topics):
        if item.get("locked_to") and question_is_active(item, answers):
            answer_records[item["id"]] = {
                "source_id": item["source_id"],
                "question": item["label"],
                "answer": answers[item["id"]],
                "type": item["type"],
            }

    household_male = answers.get("a10_male")
    household_female = answers.get("a10_female")
    if household_male not in {None, ""} or household_female not in {None, ""}:
        household_total = (household_male or 0) + (household_female or 0)
        answers["a10_total"] = household_total
        answer_records["a10_total"] = {
            "source_id": "A10.1",
            "question": "People living in the household - total (calculated)",
            "answer": household_total,
            "type": "calculated",
        }

    birth_year = answers.get("a8_birth_year")
    if birth_year:
        visit_date = date.fromisoformat(event_date)
        calculated_age = age_from_birth_year(birth_year, visit_date)
        calculated_age_group = age_group_from_birth_year(birth_year, visit_date)
        if not calculated_age_group:
            return False, "A8.5 year of birth must be valid and cannot be after the follow-up year."
        answers["a8_age"] = calculated_age
        answer_records["a8_age"] = {
            "source_id": "A8.5",
            "question": "Age (calculated from year of birth)",
            "answer": calculated_age,
            "type": "calculated",
        }
        answers["a8_age_group"] = calculated_age_group
        answer_records["a8_age_group"] = {
            "source_id": "A8.5",
            "question": "Age group (calculated from date of birth)",
            "answer": calculated_age_group,
            "type": "calculated",
        }

    validation_error = validate_answers(
        answers, topics, training_history=training_history,
    )
    if validation_error:
        return False, validation_error
    results = score_survey(answers, topics)
    if any(topic not in results["trainings"] for topic in topics):
        return False, "A result could not be calculated for one of the applicable training types."

    gps_track = answers.get("a7_gps")
    if isinstance(gps_track, dict) and gps_track.get("points") and latitude is None:
        final_point = gps_track["points"][-1]
        latitude = final_point["latitude"]
        longitude = final_point["longitude"]
        location_accuracy_m = final_point["accuracy"]
        location_captured_at = final_point["capturedAt"] or None

    for question_id, prepared in pending_photos.items():
        stored_photos = persist_followup_photos(prepared)
        answers[question_id] = stored_photos
        if question_id in answer_records:
            answer_records[question_id]["answer"] = stored_photos

    cycles: dict[str, int] = {}
    for topic in topics:
        cycle = next((number for number in range(1, 4)
                      if raw.get(f"Training {number} - {topic}") not in {None, ""}
                      and raw.get(f"Follow up {number} - {topic}") in {None, ""}), None)
        if cycle is None:
            cycle = next((number for number in range(3, 0, -1)
                          if raw.get(f"Training {number} - {topic}") not in {None, ""}), None)
        if cycle is None:
            return False, f"No training cycle was found for {topic}."
        cycles[topic] = cycle

    shared_record = None
    shared_raw = None
    shared_topics: list[str] = []
    shared_cycles: dict[str, int] = {}
    if answers.get("a0_shared_plot") == "yes":
        try:
            shared_record_id = int(answers.get("a0_shared_person") or 0)
        except (TypeError, ValueError):
            shared_record_id = 0
        shared_record = connection.execute(
            """SELECT r.*, f.uid FROM records r JOIN farmers f ON f.id=r.farmer_id
               WHERE r.id=? AND r.id<>? AND r.dataset='training' AND r.archived_at IS NULL
                 AND LOWER(TRIM(COALESCE(r.village,'')))=LOWER(TRIM(COALESCE(?,'')))""",
            (shared_record_id, record_id, record["village"]),
        ).fetchone() if shared_record_id and str(record["village"] or "").strip() else None
        if not shared_record:
            return False, "Select another registered person from the same village."
        shared_due = {
            row["topic"] for row in connection.execute(
                "SELECT topic FROM topic_statuses WHERE record_id=? AND status_code='FU'",
                (shared_record_id,),
            ).fetchall()
        }
        shared_topics = [topic for topic in topics if topic in shared_due]
        if not shared_topics:
            return False, "The other registered person has no matching training due for follow-up."
        shared_raw = json.loads(shared_record["raw_data"])
        for topic in shared_topics:
            cycle = next((number for number in range(1, 4)
                          if shared_raw.get(f"Training {number} - {topic}") not in {None, ""}
                          and shared_raw.get(f"Follow up {number} - {topic}") in {None, ""}), None)
            if cycle is None:
                cycle = next((number for number in range(3, 0, -1)
                              if shared_raw.get(f"Training {number} - {topic}") not in {None, ""}), None)
            if cycle is None:
                return False, f"No training cycle was found for {shared_record['name']} and {topic}."
            shared_cycles[topic] = cycle
        results["shared_followup"] = {
            "record_id": shared_record_id,
            "name": shared_record["name"],
            "topics": shared_topics,
        }

    cursor = connection.execute(
        """INSERT INTO field_events(
               event_type, cbf_name, event_date, location, created_by, created_at,
               client_submission_id, device_id, client_created_at,
               latitude, longitude, location_accuracy_m, location_captured_at
           ) VALUES('followup', ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (cbf_name, event_date, username, utc_now(), client_submission_id, device_id,
         client_created_at, latitude, longitude, location_accuracy_m, location_captured_at),
    )
    event_id = cursor.lastrowid
    created_at = utc_now()
    profile = followup_profile(record, raw)
    for key, answer_id in {
        "district": "a4_district", "subcounty": "a5_subcounty", "village": "a6_village",
        "beneficiary_type": "a8_beneficiary_type", "phone": "a8_contact", "sex": "a8_gender",
        "pwd": "a8_pwd", "birth_year": "a8_birth_year", "group": "a8_group",
        "education": "a9_education",
    }.items():
        if answers.get(answer_id) not in {None, ""}:
            profile[key] = str(answers[answer_id])
    if answers.get("a8_age_group"):
        profile["age_group"] = answers["a8_age_group"]
        profile["age"] = answers["a8_age"]
    profile.update({
        "household_total": answers.get("a10_total"),
        "household_male": answers.get("a10_male"),
        "household_female": answers.get("a10_female"),
        "training_history": training_history,
    })

    household = results.get("household_outcome") or {}
    breadth = results.get("breadth") or {}
    rvo = results.get("rvo") or {}
    assessment_cursor = connection.execute(
        """INSERT INTO followup_assessments(
               event_id, record_id, questionnaire_version, rvo_passed, rvo_practice_count,
               project_passed, household_outcome, breadth_achieved, breadth_total, depth,
               profile_snapshot, summary, consent, created_at
           ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            event_id, record_id, SURVEY_VERSION,
            int(rvo["passed"]) if rvo else None, rvo.get("count"),
            int(results["project_passed"]) if results.get("project_passed") is not None else None,
            household.get("code"), breadth.get("achieved"), breadth.get("total"), results.get("depth"),
            json.dumps(profile, ensure_ascii=False), json.dumps(results, ensure_ascii=False),
            int(answers.get("consent") == "yes"), created_at,
        ),
    )
    assessment_id = assessment_cursor.lastrowid

    for package in results["packages"].values():
        connection.execute(
            """INSERT INTO followup_package_results(
                   assessment_id, package_key, package_title, points_earned, points_available,
                   score, result_status, critical_failed, recommendation
               ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                assessment_id, package["key"], package["title"], package["points_earned"],
                package["points_available"], package["score"], package["status"],
                int(package["critical_failed"]), package["recommendation"],
            ),
        )

    answers_payload = json.dumps(answer_records, ensure_ascii=False)
    for topic in topics:
        result = results["trainings"][topic]
        cycle = cycles[topic]
        raw[f"Follow up {cycle} - {topic}"] = event_date
        raw[f"Follow up {cycle} score - {topic}"] = result["score"]
        raw[f"Follow up {cycle} next action - {topic}"] = result["next_action"]
        entry_cursor = connection.execute(
            "INSERT INTO field_event_entries(event_id, record_id, topic, score) VALUES(?, ?, ?, ?)",
            (event_id, record_id, topic, result["score"]),
        )
        connection.execute(
            """INSERT INTO followup_responses(
                   event_entry_id, record_id, topic, training_cycle, questionnaire_version,
                   answers, adoption_rate, next_action, created_at, result_status,
                   recommendation, critical_failed, points_earned, points_available
               ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                entry_cursor.lastrowid, record_id, topic, cycle, SURVEY_VERSION,
                answers_payload, result["score"], result["next_action"], created_at,
                result["status"], result["recommendation"], int(result["critical_failed"]),
                result["points_earned"], result["points_available"],
            ),
        )

    if shared_record and shared_raw is not None:
        for topic in shared_topics:
            result = results["trainings"][topic]
            cycle = shared_cycles[topic]
            shared_raw[f"Follow up {cycle} - {topic}"] = event_date
            shared_raw[f"Follow up {cycle} score - {topic}"] = result["score"]
            shared_raw[f"Follow up {cycle} next action - {topic}"] = result["next_action"]
            entry_cursor = connection.execute(
                "INSERT INTO field_event_entries(event_id, record_id, topic, score) VALUES(?, ?, ?, ?)",
                (event_id, shared_record["id"], topic, result["score"]),
            )
            connection.execute(
                """INSERT INTO followup_responses(
                       event_entry_id, record_id, topic, training_cycle, questionnaire_version,
                       answers, adoption_rate, next_action, created_at, result_status,
                       recommendation, critical_failed, points_earned, points_available
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    entry_cursor.lastrowid, shared_record["id"], topic, cycle, SURVEY_VERSION,
                    answers_payload, result["score"], result["next_action"], created_at,
                    result["status"], result["recommendation"], int(result["critical_failed"]),
                    result["points_earned"], result["points_available"],
                ),
            )
        connection.execute(
            "UPDATE records SET raw_data=?, updated_at=? WHERE id=?",
            (json.dumps(shared_raw, ensure_ascii=False), utc_now(), shared_record["id"]),
        )
        rebuild_statuses(connection, shared_record["id"], "training", shared_raw)

    raw.update({
        "DISTRICT": profile["district"], "Sub County": profile["subcounty"],
        "Village": profile["village"], "Phone Number": profile["phone"],
        "Sex": profile["sex"], "PWD (Y/N)": profile["pwd"],
        "Year of birth": profile["birth_year"],
        "Age": profile["age"], "Age group": profile["age_group"], "GROUP NAME": profile["group"],
        "Benefiary Type": profile["beneficiary_type"],
        "Level of education completed": profile["education"],
        "Household members - total": profile["household_total"],
        "Household members - male": profile["household_male"],
        "Household members - female": profile["household_female"],
    })
    connection.execute(
        """UPDATE records SET district=?, subcounty=?, village=?, phone=?, sex=?, age_value=?, age_group=?,
                  group_name=?, raw_data=?, updated_at=? WHERE id=?""",
        (
            profile["district"], profile["subcounty"], profile["village"], profile["phone"],
            profile["sex"], profile["age"], profile["age_group"], profile["group"],
            json.dumps(raw, ensure_ascii=False), utc_now(), record_id,
        ),
    )
    rebuild_statuses(connection, record_id, "training", raw)
    log_audit(
        connection, username, "field_entry", "field_event", event_id,
        f"Recorded scored follow-up for {record['name']}"
        + (f" and {shared_record['name']}" if shared_record else ""),
        {
            "cbf": cbf_name, "date": event_date,
            "household_outcome": household.get("code"),
            "breadth": breadth, "depth": results.get("depth"),
            "training_results": results["trainings"],
            "shared_followup": results.get("shared_followup"),
            "location": {
                "latitude": latitude, "longitude": longitude,
                "accuracy_m": location_accuracy_m, "captured_at": location_captured_at,
            } if latitude is not None and longitude is not None else None,
        },
    )
    connection.commit()
    if client_submission_id is None:
        session["last_followup_event_id"] = event_id
    outcome_text = f" Outcome {household.get('code')} - {household.get('label')}." if household else ""
    shared_text = f" The same plot follow-up was also registered for {shared_record['name']}." if shared_record else ""
    return True, f"Follow-up scored and saved for {record['name']}.{shared_text}{outcome_text}"


def csrf_token() -> str:
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


def is_safe_redirect(target: str) -> bool:
    if not target:
        return False
    parsed = urlparse(target)
    return not parsed.netloc and parsed.path.startswith("/")


def valid_dataset(value: str) -> str:
    return value if value in DATASET_LABELS else "combined"


def read_dashboard_filters(values) -> dict[str, str]:
    filters = {
        key: values.get(key, "").strip()
        for key in ("gender", "age", "cbf", "group", "category", "topic", "date_from", "date_to", "status")
    }
    filters["dropouts"] = "include" if values.get("dropouts") == "include" else "exclude"
    return filters


def selected_filter_values(filters: dict[str, str], key: str) -> list[str]:
    return [value.strip() for value in str(filters.get(key, "")).split("|") if value.strip()]


def _record_query(dataset: str, filters: dict[str, str]):
    where = ["archived_at IS NULL"]
    params: list = []
    if dataset in {"training", "care"}:
        where.append("dataset=?")
        params.append(dataset)
    else:
        where.append(
            "EXISTS (SELECT 1 FROM matches m WHERE m.status='confirmed' "
            "AND (m.training_record_id=r.id OR m.care_record_id=r.id) "
            "AND EXISTS (SELECT 1 FROM audit_log a WHERE a.entity_type='match' "
            "AND a.entity_id=m.id AND a.action='confirm'))"
        )
    if dataset == "training" and filters.get("dropouts", "exclude") != "include":
        where.append("LOWER(COALESCE(record_status,'')) NOT LIKE '%drop%'")
    for filter_key, column in (("gender", "sex"), ("age", "age_group"), ("cbf", "cbf_name"), ("group", "group_name")):
        selected = selected_filter_values(filters, filter_key)
        if not selected:
            continue
        placeholders = ",".join("?" for _ in selected)
        if dataset == "combined":
            where.append(
                f"EXISTS (SELECT 1 FROM records rf WHERE rf.farmer_id=r.farmer_id "
                f"AND rf.archived_at IS NULL AND rf.{column} IN ({placeholders}))"
            )
        else:
            where.append(f"{column} IN ({placeholders})")
        params.extend(selected)
    return " AND ".join(where), params


def _average(values):
    return round(sum(values) / len(values), 1) if values else 0


def _pearson(left, right):
    if len(left) < 2 or len(left) != len(right):
        return None
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    numerator = sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right))
    denominator = math.sqrt(
        sum((x - left_mean) ** 2 for x in left) * sum((y - right_mean) ** 2 for y in right)
    )
    return round(numerator / denominator, 2) if denominator else None


def build_interaction_data(connection, filters: dict[str, str]):
    """Build cross-project analysis from manually confirmed AE/FH pairs only."""
    where = [
        "m.status='confirmed'",
        "t.archived_at IS NULL",
        "c.archived_at IS NULL",
        "EXISTS (SELECT 1 FROM audit_log a WHERE a.entity_type='match' "
        "AND a.entity_id=m.id AND a.action='confirm')",
    ]
    params = []
    for key, column in (("gender", "t.sex"), ("age", "t.age_group"), ("cbf", "t.cbf_name"), ("group", "t.group_name")):
        selected = selected_filter_values(filters, key)
        if selected:
            placeholders = ",".join("?" for _ in selected)
            where.append(f"{column} IN ({placeholders})")
            params.extend(selected)
    pairs = [dict(row) for row in connection.execute(
        f"""SELECT m.id AS match_id, m.confidence,
                   t.id AS training_id, t.name, t.sex, t.age_group, t.group_name, t.cbf_name,
                   c.id AS care_id, c.group_name AS care_group
            FROM matches m
            JOIN records t ON t.id=m.training_record_id
            JOIN records c ON c.id=m.care_record_id
            WHERE {' AND '.join(where)} ORDER BY t.name COLLATE NOCASE""",
        params,
    ).fetchall()]

    status_by_record = defaultdict(list)
    record_ids = [record_id for pair in pairs for record_id in (pair["training_id"], pair["care_id"])]
    if record_ids:
        placeholders = ",".join("?" for _ in record_ids)
        for row in connection.execute(
            f"SELECT * FROM topic_statuses WHERE record_id IN ({placeholders}) ORDER BY topic", record_ids
        ).fetchall():
            status_by_record[row["record_id"]].append(row)

    for pair in pairs:
        training_statuses = status_by_record[pair["training_id"]]
        care_statuses = status_by_record[pair["care_id"]]
        pair["ae_received"] = sum(item["training_received"] for item in training_statuses)
        pair["ae_adopted"] = sum(item["confirmed_trained"] for item in training_statuses)
        pair["ae_topics"] = {item["topic"]: bool(item["training_received"]) for item in training_statuses}
        pair["fh_modules"] = sum(item["confirmed_trained"] for item in care_statuses)
        attended = expected = 0
        for item in care_statuses:
            try:
                details = json.loads(item["details"] or "{}")
            except (json.JSONDecodeError, TypeError):
                details = {}
            attended += int(details.get("attended") or 0)
            expected += int(details.get("expected") or 0)
        pair["fh_attended"] = attended
        pair["fh_expected"] = expected
        pair["fh_rate"] = round(attended / expected * 100, 1) if expected else 0

    exposure_buckets = [
        {"label": "No AE topics", "minimum": 0, "maximum": 0},
        {"label": "1-2 AE topics", "minimum": 1, "maximum": 2},
        {"label": "3-4 AE topics", "minimum": 3, "maximum": 4},
        {"label": "5+ AE topics", "minimum": 5, "maximum": 99},
    ]
    for bucket in exposure_buckets:
        members = [pair for pair in pairs if bucket["minimum"] <= pair["ae_received"] <= bucket["maximum"]]
        bucket["count"] = len(members)
        bucket["fh_rate"] = _average([pair["fh_rate"] for pair in members])
        bucket["fh_modules"] = _average([pair["fh_modules"] for pair in members])
        bucket["ae_adopted"] = _average([pair["ae_adopted"] for pair in members])

    topic_relationships = []
    for topic in TRAINING_TOPICS:
        exposed = [pair for pair in pairs if pair["ae_topics"].get(topic)]
        unexposed = [pair for pair in pairs if not pair["ae_topics"].get(topic)]
        exposed_rate = _average([pair["fh_rate"] for pair in exposed])
        unexposed_rate = _average([pair["fh_rate"] for pair in unexposed])
        topic_relationships.append({
            "topic": topic,
            "exposed_count": len(exposed),
            "unexposed_count": len(unexposed),
            "exposed_rate": exposed_rate,
            "unexposed_rate": unexposed_rate,
            "difference": round(exposed_rate - unexposed_rate, 1),
        })
    topic_relationships.sort(key=lambda item: abs(item["difference"]), reverse=True)

    correlations = [
        {"label": "AE topics received vs FH attendance", "value": _pearson(
            [pair["ae_received"] for pair in pairs], [pair["fh_rate"] for pair in pairs]
        )},
        {"label": "AE adoption vs FH attendance", "value": _pearson(
            [pair["ae_adopted"] for pair in pairs], [pair["fh_rate"] for pair in pairs]
        )},
        {"label": "AE topics received vs FH modules", "value": _pearson(
            [pair["ae_received"] for pair in pairs], [pair["fh_modules"] for pair in pairs]
        )},
    ]
    for item in correlations:
        value = item["value"]
        item["strength"] = (
            "Not enough variation" if value is None else
            "Strong" if abs(value) >= 0.7 else
            "Moderate" if abs(value) >= 0.4 else
            "Weak" if abs(value) >= 0.2 else "Very weak"
        )

    care_schema = get_schema(connection, "care")
    nutrition_score_available = any(
        "nutrition" in str(field.get("label", "")).lower() and "score" in str(field.get("label", "")).lower()
        for field in care_schema
    )
    reviewable_count = connection.execute(
        """SELECT COUNT(*) FROM matches m WHERE m.status='pending' OR (
               m.status='confirmed' AND NOT EXISTS (
                   SELECT 1 FROM audit_log a WHERE a.entity_type='match'
                   AND a.entity_id=m.id AND a.action='confirm'))"""
    ).fetchone()[0]
    options = {
        "genders": [row[0] for row in connection.execute(
            "SELECT DISTINCT sex FROM records WHERE dataset='training' AND archived_at IS NULL "
            "AND TRIM(COALESCE(sex,''))<>'' ORDER BY sex"
        )],
        "ages": [row[0] for row in connection.execute(
            "SELECT DISTINCT age_group FROM records WHERE dataset='training' AND archived_at IS NULL "
            "AND TRIM(COALESCE(age_group,''))<>'' ORDER BY age_group"
        )],
        "cbfs": [row[0] for row in connection.execute(
            "SELECT DISTINCT cbf_name FROM records WHERE dataset='training' AND archived_at IS NULL "
            "AND TRIM(COALESCE(cbf_name,''))<>'' ORDER BY cbf_name"
        )],
    }
    return {
        "pairs": pairs,
        "pair_count": len(pairs),
        "reviewable_count": reviewable_count,
        "exposure_buckets": exposure_buckets,
        "topic_relationships": topic_relationships,
        "correlations": correlations,
        "nutrition_score_available": nutrition_score_available,
        "summary": {
            "ae_received": _average([pair["ae_received"] for pair in pairs]),
            "ae_adopted": _average([pair["ae_adopted"] for pair in pairs]),
            "fh_rate": _average([pair["fh_rate"] for pair in pairs]),
            "fh_modules": _average([pair["fh_modules"] for pair in pairs]),
        },
        "options": options,
    }


def build_dashboard_data(connection, dataset: str, filters: dict[str, str]):
    where, params = _record_query(dataset, filters)
    records = list(connection.execute(
        f"SELECT r.*, f.uid FROM records r JOIN farmers f ON f.id=r.farmer_id WHERE {where} ORDER BY r.name",
        params,
    ).fetchall())
    statuses = list(connection.execute(
        f"""SELECT ts.* FROM topic_statuses ts JOIN records r ON r.id=ts.record_id
            WHERE {where} ORDER BY ts.topic""",
        params,
    ).fetchall())
    topic_filter = filters.get("topic", "") if dataset == "training" else ""
    selected_topics = [topic for topic in selected_filter_values(filters, "topic") if topic in TRAINING_TOPICS]
    if topic_filter == "__none__":
        selected_topics = []
        statuses = []
    elif selected_topics:
        statuses = [status for status in statuses if status["topic"] in selected_topics]
    elif topic_filter:
        topic_filter = ""
    status_by_record = defaultdict(list)
    for status in statuses:
        status_by_record[status["record_id"]].append(status)

    eligible_ids = {record["id"] for record in records}
    farmer_by_record = {record["id"]: record["farmer_id"] for record in records}

    def include_complete_pairs(candidate_ids):
        if dataset != "combined":
            return set(candidate_ids)
        farmer_ids = {farmer_by_record[record_id] for record_id in candidate_ids}
        return {record_id for record_id, farmer_id in farmer_by_record.items() if farmer_id in farmer_ids}

    date_from, date_to = filters.get("date_from"), filters.get("date_to")
    if date_from or date_to:
        date_ids = set()
        for status in statuses:
            activity = status["last_activity_date"] or ""
            if activity and (not date_from or activity >= date_from) and (not date_to or activity <= date_to):
                date_ids.add(status["record_id"])
        eligible_ids &= include_complete_pairs(date_ids)

    status_filter = filters.get("status")
    if status_filter:
        matching = set()
        for record in records:
            row_statuses = status_by_record.get(record["id"], [])
            dropped = "drop" in (record["record_status"] or "").lower()
            conditions = {
                "trained": any(item["training_received"] for item in row_statuses),
                "confirmed": any(item["confirmed_trained"] for item in row_statuses),
                "followup": not dropped and any(item["followup_needed"] for item in row_statuses),
                "training": not dropped and any(item["retraining_needed"] for item in row_statuses),
                "waiting": any(item["status_code"] == "WAIT" for item in row_statuses),
                "untrained": not any(item["training_received"] for item in row_statuses),
                "dropped": dropped,
                "active": not dropped,
            }
            if conditions.get(status_filter, True):
                matching.add(record["id"])
        eligible_ids &= include_complete_pairs(matching)

    records = [record for record in records if record["id"] in eligible_ids]
    statuses = [status for status in statuses if status["record_id"] in eligible_ids]
    status_by_record = defaultdict(list)
    for status in statuses:
        status_by_record[status["record_id"]].append(status)

    dropped_ids = {
        record["id"] for record in records if "drop" in (record["record_status"] or "").lower()
    }

    def dashboard_unit(record_id):
        return farmer_by_record[record_id] if dataset == "combined" else record_id

    def ids_for(predicate, exclude_dropouts=False):
        return {
            dashboard_unit(status["record_id"]) for status in statuses
            if predicate(status) and (not exclude_dropouts or status["record_id"] not in dropped_ids)
        }

    trained_ids = ids_for(lambda item: item["training_received"])
    confirmed_ids = ids_for(lambda item: item["confirmed_trained"])
    followup_ids = ids_for(lambda item: item["followup_needed"], exclude_dropouts=True)
    retraining_ids = ids_for(lambda item: item["retraining_needed"], exclude_dropouts=True)
    waiting_ids = ids_for(lambda item: item["status_code"] == "WAIT", exclude_dropouts=True)
    followup_actions = sum(
        1 for status in statuses
        if status["followup_needed"] and status["record_id"] not in dropped_ids
    )
    retraining_actions = sum(
        1 for status in statuses
        if status["retraining_needed"] and status["record_id"] not in dropped_ids
    )

    summary = {
        "total_records": len(records),
        "unique_farmers": len({record["farmer_id"] for record in records}),
        "trained": len(trained_ids),
        "confirmed": len(confirmed_ids),
        "followup": len(followup_ids),
        "retraining": len(retraining_ids),
        "followup_actions": followup_actions,
        "retraining_actions": retraining_actions,
        "dropped": len({
            dashboard_unit(record["id"]) for record in records
            if "drop" in (record["record_status"] or "").lower()
        }),
    }

    # Each person appears once in this operational distribution, even when
    # they have activity in multiple topics.
    record_ids = {dashboard_unit(record["id"]) for record in records}
    attention_ids = followup_ids | retraining_ids
    verified_ids = confirmed_ids - attention_ids
    learning_ids = trained_ids - verified_ids - attention_ids
    not_started_ids = record_ids - verified_ids - learning_ids - attention_ids
    status_distribution = [
        {"label": "Verified", "count": len(verified_ids), "tone": "verified"},
        {"label": "In learning", "count": len(learning_ids), "tone": "learning"},
        {"label": "Needs attention", "count": len(attention_ids), "tone": "attention"},
        {"label": "Not started", "count": len(not_started_ids), "tone": "not-started"},
    ]
    for item in status_distribution:
        item["percent"] = round(item["count"] / len(record_ids) * 100) if record_ids else 0
    distribution_cursor = 0
    for item in status_distribution:
        distribution_cursor += item["percent"]
        item["end"] = min(distribution_cursor, 100)

    topic_groups = defaultdict(list)
    for status in statuses:
        if date_from or date_to:
            activity = status["last_activity_date"] or ""
            if not activity or (date_from and activity < date_from) or (date_to and activity > date_to):
                continue
        topic_groups[status["topic"]].append(status)
    topics = []
    for topic, items in sorted(topic_groups.items()):
        participants = len({item["record_id"] for item in items})
        trained = len({item["record_id"] for item in items if item["training_received"]})
        topics.append({
            "topic": topic,
            "participants": participants,
            "trained": trained,
            "confirmed": len({item["record_id"] for item in items if item["confirmed_trained"]}),
            "followup": len({item["record_id"] for item in items if item["followup_needed"] and item["record_id"] not in dropped_ids}),
            "retraining": len({item["record_id"] for item in items if item["retraining_needed"] and item["record_id"] not in dropped_ids}),
            "waiting": len({item["record_id"] for item in items if item["status_code"] == "WAIT"}),
            "rate": round((trained / participants * 100) if participants else 0),
        })

    action_topics = sorted(
        topics, key=lambda item: (item["followup"] + item["retraining"], item["participants"]), reverse=True
    )[:3]

    pathway_topics = selected_topics if topic_filter else list(TRAINING_TOPICS)
    pathway_labels = {
        "Household Resource Mapping (PIP)": "PIP",
        "SWC": "SWC",
        "Kitchen garden Establishment and Vegetable growing": "Kitchen",
        "Bio-inputs training": "Bio",
        "Poultry Mgt and Vaccination": "Poultry",
        "Financial Literacy": "Finance",
        "Tree planting/Agroforestry": "Trees",
        "Sustainable/Regenerative Agriculture": "Sust. ag",
    }
    combination_counts = Counter()
    training_count_distribution = Counter()
    if dataset == "training" and pathway_topics:
        for record in records:
            received_by_topic = {
                status["topic"]: bool(status["training_received"])
                for status in status_by_record.get(record["id"], [])
            }
            combination = tuple(bool(received_by_topic.get(topic)) for topic in pathway_topics)
            combination_counts[combination] += 1
            training_count_distribution[sum(combination)] += 1
    pathway_total = sum(combination_counts.values())
    all_combinations = []
    for combination, count in sorted(
        combination_counts.items(), key=lambda item: (-item[1], -sum(item[0]), item[0])
    ):
        all_combinations.append({
            "cells": combination,
            "count": count,
            "percent": round(count / pathway_total * 100, 1) if pathway_total else 0,
            "training_count": sum(combination),
        })
    initial_combination_count = 12
    shown_combinations = sum(item["count"] for item in all_combinations[:initial_combination_count])
    max_training_count = max(training_count_distribution.values(), default=1)
    training_pathways = {
        "topics": [{"name": topic, "short": pathway_labels.get(topic, topic)} for topic in pathway_topics],
        "combinations": all_combinations,
        "initial_combination_count": initial_combination_count,
        "hidden_combination_count": max(0, len(all_combinations) - initial_combination_count),
        "other_count": max(0, pathway_total - shown_combinations),
        "total": pathway_total,
        "distribution": [
            {
                "number": number,
                "count": training_count_distribution[number],
                "percent": round(training_count_distribution[number] / pathway_total * 100, 1) if pathway_total else 0,
                "width": round(training_count_distribution[number] / max_training_count * 100),
                "cumulative_count": sum(
                    count for training_count, count in training_count_distribution.items()
                    if training_count >= number
                ),
                "cumulative_percent": round(
                    sum(
                        count for training_count, count in training_count_distribution.items()
                        if training_count >= number
                    ) / pathway_total * 100, 1
                ) if pathway_total else 0,
            }
            for number in range(len(pathway_topics) + 1)
        ],
    }

    demographic_records = records
    if dataset == "combined":
        records_by_farmer = {}
        for record in sorted(records, key=lambda item: item["dataset"] != "training"):
            records_by_farmer.setdefault(record["farmer_id"], record)
        demographic_records = list(records_by_farmer.values())
    gender_counts = Counter(record["sex"] or "Not recorded" for record in demographic_records)
    age_counts = Counter(record["age_group"] or "Not recorded" for record in demographic_records)
    gender_labels = list(gender_counts)
    age_labels = list(age_counts)
    age_by_gender = {
        gender: {
            age: sum(
                1
                for record in demographic_records
                if (record["sex"] or "Not recorded") == gender
                and (record["age_group"] or "Not recorded") == age
            )
            for age in age_labels
        }
        for gender in gender_labels
    }
    gender_by_age = {
        age: {gender: age_by_gender[gender][age] for gender in gender_labels}
        for age in age_labels
    }
    month_records = defaultdict(set)
    for status in statuses:
        if status["last_activity_date"]:
            month_records[status["last_activity_date"][:7]].add(dashboard_unit(status["record_id"]))
    trend = [{"month": month, "count": len(ids)} for month, ids in sorted(month_records.items())[-12:]]
    max_trend = max((item["count"] for item in trend), default=1)
    for item in trend:
        item["width"] = round(item["count"] / max_trend * 100)

    activity_events = []
    timeline_month_values = set()
    activity_filter_options = {"ages": set(), "genders": set(), "cbfs": set()}
    for record in records:
        raw = json.loads(record["raw_data"])
        unit_id = dashboard_unit(record["id"])
        age = record["age_group"] or "Not recorded"
        gender = record["sex"] or "Not recorded"
        cbf = record["cbf_name"] or "Not recorded"
        activity_filter_options["ages"].add(age)
        activity_filter_options["genders"].add(gender)
        activity_filter_options["cbfs"].add(cbf)
        for topic in (selected_topics if topic_filter else TRAINING_TOPICS):
            for cycle in range(1, 4):
                for event_type, field_prefix in (("ct", "Training"), ("fu", "Follow up")):
                    event_date = parse_date(raw.get(f"{field_prefix} {cycle} - {topic}"))
                    if not event_date:
                        continue
                    event_iso = event_date.isoformat()
                    if (date_from and event_iso < date_from) or (date_to and event_iso > date_to):
                        continue
                    month = event_iso[:7]
                    timeline_month_values.add(month)
                    activity_events.append({
                        "month": month, "farmer": unit_id, "activity": event_type,
                        "topic": topic, "age": age, "gender": gender, "cbf": cbf,
                    })

    timeline_months = sorted(timeline_month_values)[-12:]
    visible_months = set(timeline_months)
    activity_events = [event for event in activity_events if event["month"] in visible_months]
    activity_filter_options = {
        key: sorted(values) for key, values in activity_filter_options.items()
    }
    activity_filter_options["topics"] = selected_topics if topic_filter else list(TRAINING_TOPICS)

    momentum_chart = {"months": [], "series": []}
    if dataset == "training":
        momentum_topics = selected_topics if topic_filter else list(TRAINING_TOPICS)
        momentum_counts = {
            "active": Counter(),
            "first_training": Counter(),
            "training_attendances": Counter(),
            "followups": Counter(),
            "adoptions": Counter(),
        }
        momentum_month_values = set()
        momentum_adoption_threshold = adoption_threshold()

        def in_momentum_window(event_date):
            event_iso = event_date.isoformat()
            return (not date_from or event_iso >= date_from) and (not date_to or event_iso <= date_to)

        for record in records:
            raw = json.loads(record["raw_data"])
            training_dates = []
            adoption_dates = []
            active_months = set()
            for topic in momentum_topics:
                for cycle in range(1, 4):
                    training_date = parse_date(raw.get(f"Training {cycle} - {topic}"))
                    if training_date:
                        training_dates.append(training_date)
                        if in_momentum_window(training_date):
                            month = training_date.isoformat()[:7]
                            momentum_counts["training_attendances"][month] += 1
                            active_months.add(month)
                            momentum_month_values.add(month)
                    followup_date = parse_date(raw.get(f"Follow up {cycle} - {topic}"))
                    if followup_date:
                        if in_momentum_window(followup_date):
                            month = followup_date.isoformat()[:7]
                            momentum_counts["followups"][month] += 1
                            active_months.add(month)
                            momentum_month_values.add(month)
                        score = parse_number(raw.get(f"Follow up {cycle} score - {topic}"))
                        if score is not None and score >= momentum_adoption_threshold:
                            adoption_dates.append(followup_date)
            for month in active_months:
                momentum_counts["active"][month] += 1
            if training_dates:
                first_training = min(training_dates)
                if in_momentum_window(first_training):
                    month = first_training.isoformat()[:7]
                    momentum_counts["first_training"][month] += 1
                    momentum_month_values.add(month)
            if adoption_dates:
                first_adoption = min(adoption_dates)
                if in_momentum_window(first_adoption):
                    month = first_adoption.isoformat()[:7]
                    momentum_counts["adoptions"][month] += 1
                    momentum_month_values.add(month)

        def shift_month(month, offset):
            year, month_number = (int(part) for part in month.split("-"))
            month_index = year * 12 + month_number - 1 + offset
            return f"{month_index // 12:04d}-{month_index % 12 + 1:02d}"

        if momentum_month_values:
            first_month = min(momentum_month_values)
            last_month = max(momentum_month_values)
            limited_first_month = shift_month(last_month, -11)
            first_month = max(first_month, limited_first_month)
            momentum_months = []
            month = first_month
            while month <= last_month:
                momentum_months.append(month)
                month = shift_month(month, 1)

            series_definitions = [
                ("active", "Active beneficiaries", "Beneficiaries with any dated training or follow-up", "#087880"),
                ("first_training", "First recorded training", "Beneficiaries in the month of their earliest recorded training", "#77aa2a"),
                ("training_attendances", "Training attendances", "All recorded training activities", "#1f6fb2"),
                ("followups", "Follow-ups completed", "All recorded follow-up activities", "#EFB417"),
                ("adoptions", "New confirmed adoptions", "Beneficiaries first reaching the adoption threshold", "#b04b63"),
            ]
            series_values = {
                key: [momentum_counts[key][month] for month in momentum_months]
                for key, _label, _description, _color in series_definitions
            }
            plot_left, plot_top, plot_width, plot_height = 40, 8, 706, 52
            month_count = len(momentum_months)
            momentum_series = []
            for key, label, description, color in series_definitions:
                values = series_values[key]
                peak = max(values, default=0)
                if peak:
                    raw_step = peak / 2
                    magnitude = 10 ** math.floor(math.log10(raw_step)) if raw_step else 1
                    normalized_step = raw_step / magnitude
                    step_factor = 1 if normalized_step <= 1 else 2 if normalized_step <= 2 else 5 if normalized_step <= 5 else 10
                    tick_step = max(1, math.ceil(step_factor * magnitude))
                    scale_max = tick_step * 2
                    tick_values = [0, tick_step, scale_max]
                else:
                    scale_max, tick_values = 1, [0]
                points = []
                markers = []
                for index, value in enumerate(values):
                    x = plot_left + (plot_width / (month_count - 1) * index if month_count > 1 else plot_width / 2)
                    y = plot_top + plot_height - (value / scale_max * plot_height)
                    points.append(f"{x:.1f},{y:.1f}")
                    markers.append({"x": round(x, 1), "y": round(y, 1), "value": value})
                momentum_series.append({
                    "key": key, "label": label, "description": description, "color": color,
                    "values": values, "points": " ".join(points), "markers": markers,
                    "total": sum(values), "peak": peak, "scale_max": scale_max,
                    "ticks": [
                        {"value": value, "y": round(plot_top + plot_height - value / scale_max * plot_height, 1)}
                        for value in tick_values
                    ],
                })
            momentum_chart = {
                "months": [
                    {"key": month, "label": date(int(month[:4]), int(month[5:]), 1).strftime("%b %y")}
                    for month in momentum_months
                ],
                "series": momentum_series,
            }

    options = {
        "genders": [row[0] for row in connection.execute(
            "SELECT DISTINCT sex FROM records WHERE archived_at IS NULL AND TRIM(COALESCE(sex,''))<>'' ORDER BY sex"
        )],
        "ages": [row[0] for row in connection.execute(
            "SELECT DISTINCT age_group FROM records WHERE archived_at IS NULL AND TRIM(COALESCE(age_group,''))<>'' ORDER BY age_group"
        )],
        "cbfs": [row[0] for row in connection.execute(
            "SELECT DISTINCT cbf_name FROM records WHERE archived_at IS NULL AND TRIM(COALESCE(cbf_name,''))<>'' ORDER BY cbf_name"
        )],
        "topics": list(TRAINING_TOPICS),
    }
    return {
        "summary": summary,
        "topics": topics,
        "records": records,
        "status_by_record": status_by_record,
        "gender_counts": gender_counts,
        "age_counts": age_counts,
        "age_by_gender": age_by_gender,
        "gender_by_age": gender_by_age,
        "trend": trend,
        "activity_timeline": {"months": timeline_months, "events": activity_events},
        "activity_filter_options": activity_filter_options,
        "momentum_chart": momentum_chart,
        "status_distribution": status_distribution,
        "action_topics": action_topics,
        "attention_total": len(attention_ids),
        "waiting_total": len(waiting_ids),
        "training_pathways": training_pathways,
        "options": options,
    }


def build_fh_dashboard_data(connection, filters: dict[str, str]):
    """Build attendance-focused FH analytics with track-specific denominators."""
    where = ["r.dataset='care'", "r.archived_at IS NULL"]
    params = []
    for key, column in (("gender", "r.sex"), ("age", "r.age_group"), ("group", "r.group_name")):
        selected = selected_filter_values(filters, key)
        if selected:
            placeholders = ",".join("?" for _ in selected)
            where.append(f"{column} IN ({placeholders})")
            params.extend(selected)
    if filters.get("category"):
        where.append("json_extract(r.raw_data, '$.Category')=?")
        params.append(filters["category"])

    records = [dict(row) for row in connection.execute(
        f"""SELECT r.*, f.uid FROM records r JOIN farmers f ON f.id=r.farmer_id
             WHERE {' AND '.join(where)} ORDER BY r.group_name, r.name""",
        params,
    ).fetchall()]
    record_ids = [record["id"] for record in records]
    statuses = []
    if record_ids:
        placeholders = ",".join("?" for _ in record_ids)
        statuses = [dict(row) for row in connection.execute(
            f"SELECT * FROM topic_statuses WHERE record_id IN ({placeholders}) ORDER BY topic",
            record_ids,
        ).fetchall()]
    status_by_record = defaultdict(list)
    for status in statuses:
        status["details_data"] = json.loads(status.get("details") or "{}")
        status_by_record[status["record_id"]].append(status)

    def track_for(record):
        category = str(record["raw"].get("Category", "")).strip()
        return "school" if category.lower() in {"learner", "teacher"} else "care"

    def is_dropped(record):
        return "drop" in (record.get("record_status") or "").lower()

    def applicable(record, status):
        return status["topic"].startswith("School clubs") == (record["track"] == "school")

    for record in records:
        record["raw"] = json.loads(record["raw_data"])
        record["category"] = str(record["raw"].get("Category", "")).strip() or "Not recorded"
        record["track"] = track_for(record)
        record["applicable_statuses"] = [
            status for status in status_by_record[record["id"]] if applicable(record, status)
        ]
        record["attended"] = sum(item["details_data"].get("attended", 0) for item in record["applicable_statuses"])
        record["marked"] = sum(item["details_data"].get("marked", 0) for item in record["applicable_statuses"])
        record["possible"] = sum(item["details_data"].get("expected", 0) for item in record["applicable_statuses"])
        record["completed_modules"] = sum(item["status_code"] == "COMPLETED" for item in record["applicable_statuses"])
        record["in_progress_modules"] = sum(item["status_code"] == "IN_PROGRESS" for item in record["applicable_statuses"])

    status_filter = filters.get("status")
    if status_filter:
        def matches(record):
            checks = {
                "active": not is_dropped(record),
                "dropped": is_dropped(record),
                "reached": record["attended"] > 0,
                "not_started": record["attended"] == 0,
                "module_completed": record["completed_modules"] > 0,
                "in_progress": record["in_progress_modules"] > 0,
            }
            return checks.get(status_filter, True)
        records = [record for record in records if matches(record)]

    def totals(items):
        attended = sum(item["attended"] for item in items)
        marked = sum(item["marked"] for item in items)
        possible = sum(item["possible"] for item in items)
        return {
            "participants": len(items),
            "reached": sum(item["attended"] > 0 for item in items),
            "attended": attended,
            "marked": marked,
            "possible": possible,
            "absent": max(0, marked - attended),
            "unrecorded": max(0, possible - marked),
            "attendance_rate": round(attended / marked * 100) if marked else 0,
            "recording_rate": round(marked / possible * 100) if possible else 0,
        }

    overall = totals(records)
    summary = {
        "total_records": len(records),
        "unique_farmers": len({record["farmer_id"] for record in records}),
        "active": sum(not is_dropped(record) for record in records),
        "dropped": sum(is_dropped(record) for record in records),
        "groups": len({record["group_name"] for record in records if record["group_name"]}),
        "care_participants": sum(record["track"] == "care" for record in records),
        "school_participants": sum(record["track"] == "school" for record in records),
        "completed_modules": sum(record["completed_modules"] for record in records),
        "in_progress_modules": sum(record["in_progress_modules"] for record in records),
        **overall,
    }

    expected_modules = care_module_fields()
    modules = []
    for topic, fields in expected_modules.items():
        track = "school" if topic.startswith("School clubs") else "care"
        relevant = [record for record in records if record["track"] == track]
        module_statuses = [
            next((item for item in record["applicable_statuses"] if item["topic"] == topic), None)
            for record in relevant
        ]
        module_statuses = [item for item in module_statuses if item]
        attended = sum(item["details_data"].get("attended", 0) for item in module_statuses)
        marked = sum(item["details_data"].get("marked", 0) for item in module_statuses)
        possible = len(relevant) * len(fields)
        completed = sum(item["status_code"] == "COMPLETED" for item in module_statuses)
        started = sum(item["training_received"] for item in module_statuses)
        modules.append({
            "topic": topic,
            "label": topic.replace("Care groups - ", "").replace("School clubs - ", ""),
            "track": track,
            "track_label": "School clubs" if track == "school" else "Care groups",
            "sessions": len(fields),
            "participants": len(relevant),
            "started": started,
            "completed": completed,
            "attended": attended,
            "absent": max(0, marked - attended),
            "marked": marked,
            "unrecorded": max(0, possible - marked),
            "possible": possible,
            "attendance_rate": round(attended / marked * 100) if marked else 0,
            "recording_rate": round(marked / possible * 100) if possible else 0,
            "completion_rate": round(completed / len(relevant) * 100) if relevant else 0,
            "reach_rate": round(started / len(relevant) * 100) if relevant else 0,
        })

    track_stats = []
    for track, label in (("care", "Care groups"), ("school", "School clubs")):
        members = [record for record in records if record["track"] == track]
        track_stats.append({
            "track": track,
            "label": label,
            "groups": len({record["group_name"] for record in members if record["group_name"]}),
            **totals(members),
        })

    group_stats = []
    grouped = defaultdict(list)
    for record in records:
        grouped[record["group_name"] or "Not recorded"].append(record)
    for group_name, members in grouped.items():
        group_stats.append({
            "group_name": group_name,
            "track": "School club" if members[0]["track"] == "school" else "Care group",
            **totals(members),
        })
    group_stats.sort(key=lambda item: (-item["participants"], item["group_name"]))

    engagement_counts = Counter()
    for record in records:
        if is_dropped(record):
            engagement_counts["Dropped"] += 1
        elif record["completed_modules"]:
            engagement_counts["Completed a module"] += 1
        elif record["attended"]:
            engagement_counts["Participating"] += 1
        else:
            engagement_counts["No attendance recorded"] += 1

    category_counts = Counter(record["category"] for record in records)
    gender_counts = Counter(record["sex"] or "Not recorded" for record in records)
    base_where = "dataset='care' AND archived_at IS NULL"
    group_rows = connection.execute(
        f"""SELECT group_name,
                    MAX(CASE WHEN LOWER(TRIM(COALESCE(json_extract(raw_data, '$.Category'), '')))
                                      IN ('learner', 'teacher') THEN 1 ELSE 0 END) AS is_school
             FROM records WHERE {base_where} AND TRIM(COALESCE(group_name,''))<>''
             GROUP BY group_name ORDER BY group_name"""
    ).fetchall()
    options = {
        "genders": [row[0] for row in connection.execute(
            f"SELECT DISTINCT sex FROM records WHERE {base_where} AND TRIM(COALESCE(sex,''))<>'' ORDER BY sex"
        )],
        "ages": [row[0] for row in connection.execute(
            f"SELECT DISTINCT age_group FROM records WHERE {base_where} AND TRIM(COALESCE(age_group,''))<>'' ORDER BY age_group"
        )],
        "groups": [row[0] for row in group_rows],
        "care_groups": [row[0] for row in group_rows if not row[1]],
        "schools": [row[0] for row in group_rows if row[1]],
        "categories": [row[0] for row in connection.execute(
            f"SELECT DISTINCT json_extract(raw_data, '$.Category') FROM records WHERE {base_where} "
            "AND TRIM(COALESCE(json_extract(raw_data, '$.Category'),''))<>'' ORDER BY 1"
        )],
    }
    return {
        "summary": summary,
        "modules": modules,
        "track_stats": track_stats,
        "group_stats": group_stats,
        "engagement_counts": engagement_counts,
        "category_counts": category_counts,
        "gender_counts": gender_counts,
        "records": records,
        "options": options,
    }


def describe_filters(dataset: str, filters: dict[str, str]) -> list[str]:
    descriptions = [f"Dataset: {DATASET_LABELS[dataset]}"]
    labels = {"gender": "Gender", "age": "Age group", "cbf": "CBF", "group": "Group", "category": "Category", "topic": "Training type", "date_from": "From", "date_to": "To", "status": "Status"}
    descriptions.extend(
        f"{labels[key]}: {', '.join(selected_filter_values(filters, key))}"
        for key, value in filters.items() if value and key in labels
    )
    if dataset == "training":
        descriptions.append("Dropouts: Included" if filters.get("dropouts") == "include" else "Dropouts: Excluded")
    return descriptions


def normalized_person_name(value: str) -> str:
    return " ".join("".join(character.lower() if character.isalnum() else " " for character in str(value)).split())


def populated_value(value) -> bool:
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def duplicate_activity_field(dataset: str, key: str) -> bool:
    if dataset == "training":
        return key.startswith("Training ") or key.startswith("Follow up ") or key == "Number of Birds Vaccinated"
    return key.startswith("Care groups - ") or key.startswith("School clubs - ")


def duplicate_conflicts(connection, left, right) -> list[dict]:
    left_raw = json.loads(left["raw_data"])
    right_raw = json.loads(right["raw_data"])
    schema = get_schema(connection, left["dataset"])
    labels = {field.get("key", ""): field.get("label") or field.get("key", "") for field in schema}
    conflicts = []
    for key in sorted(set(left_raw) | set(right_raw), key=str.casefold):
        left_value, right_value = left_raw.get(key), right_raw.get(key)
        if not populated_value(left_value) or not populated_value(right_value):
            continue
        if str(left_value).strip().casefold() == str(right_value).strip().casefold():
            continue
        if left["dataset"] == "training" and duplicate_activity_field("training", key):
            continue
        conflicts.append({
            "key": key,
            "label": labels.get(key, key),
            "left": left_value,
            "right": right_value,
        })
    if populated_value(left["cbf_name"]) and populated_value(right["cbf_name"]) \
            and left["cbf_name"].strip().casefold() != right["cbf_name"].strip().casefold():
        conflicts.append({
            "key": "__cbf_name__", "label": "Current CBF assignment",
            "left": left["cbf_name"], "right": right["cbf_name"],
        })
    return conflicts


def build_duplicate_candidate(connection, left, right) -> dict:
    schema = get_schema(connection, left["dataset"])
    left_raw = json.loads(left["raw_data"])
    right_raw = json.loads(right["raw_data"])
    return {
        "left": dict(left),
        "right": dict(right),
        "conflicts": duplicate_conflicts(connection, left, right),
        "left_sections": populated_raw_sections(schema, left_raw),
        "right_sections": populated_raw_sections(schema, right_raw),
        "left_activity_count": sum(populated_value(value) for key, value in left_raw.items() if duplicate_activity_field(left["dataset"], key)),
        "right_activity_count": sum(populated_value(value) for key, value in right_raw.items() if duplicate_activity_field(right["dataset"], key)),
    }


def duplicate_group_conflicts(connection, records) -> list[dict]:
    schema = get_schema(connection, records[0]["dataset"])
    labels = {field.get("key", ""): field.get("label") or field.get("key", "") for field in schema}
    raw_by_id = {record["id"]: json.loads(record["raw_data"]) for record in records}
    conflicts = []
    all_keys = set().union(*(raw.keys() for raw in raw_by_id.values()))
    for key in sorted(all_keys, key=str.casefold):
        if records[0]["dataset"] == "training" and duplicate_activity_field("training", key):
            continue
        options = []
        seen_values = set()
        for record in records:
            value = raw_by_id[record["id"]].get(key)
            if not populated_value(value):
                continue
            normalized = str(value).strip().casefold()
            if normalized in seen_values:
                continue
            seen_values.add(normalized)
            options.append({"record_id": record["id"], "uid": record["uid"], "value": value})
        if len(options) > 1:
            conflicts.append({"key": key, "label": labels.get(key, key), "options": options})
    cbf_options = []
    seen_cbfs = set()
    for record in records:
        cbf = str(record["cbf_name"] or "").strip()
        if cbf and cbf.casefold() not in seen_cbfs:
            seen_cbfs.add(cbf.casefold())
            cbf_options.append({"record_id": record["id"], "uid": record["uid"], "value": cbf})
    if len(cbf_options) > 1:
        conflicts.append({"key": "__cbf_name__", "label": "Current CBF assignment", "options": cbf_options})
    return conflicts


def duplicate_similarity(left, right) -> tuple[int, list[str], list[str]]:
    weights = {
        "phone": 25, "sex": 15, "age_group": 15, "age_value": 12,
        "village": 15, "parish": 10, "subcounty": 8, "district": 7,
        "cbf_name": 10,
    }
    compared_weight = 0
    matched_weight = 0
    reasons = []
    warnings = []
    reason_labels = {"age_group": "Age group", "age_value": "Age", "cbf_name": "CBF"}
    for field, weight in weights.items():
        left_value = str(left[field] or "").strip().casefold()
        right_value = str(right[field] or "").strip().casefold()
        if not left_value or not right_value:
            continue
        compared_weight += weight
        if left_value == right_value:
            matched_weight += weight
            reasons.append(reason_labels.get(field, field.replace("_", " ").title()))
    if left["dataset"] == "training":
        left_raw = json.loads(left["raw_data"])
        right_raw = json.loads(right["raw_data"])
        def training_dates(raw):
            return {
                topic: {
                    comparable_activity_value(raw.get(f"Training {cycle} - {topic}"))
                    for cycle in range(1, 4)
                    if populated_value(raw.get(f"Training {cycle} - {topic}"))
                }
                for topic in TRAINING_TOPICS
            }

        left_dates = training_dates(left_raw)
        right_dates = training_dates(right_raw)
        shared_topics = [
            topic for topic in TRAINING_TOPICS if left_dates[topic] and right_dates[topic]
        ]
        if shared_topics:
            compared_weight += 25
            exact_topics = [
                topic for topic in shared_topics if left_dates[topic] & right_dates[topic]
            ]
            different_date_topics = [topic for topic in shared_topics if topic not in exact_topics]
            if exact_topics:
                matched_weight += round(25 * len(exact_topics) / len(shared_topics))
                reasons.append("Same training on exact date")
            if different_date_topics:
                label = "training topic" if len(different_date_topics) == 1 else "training topics"
                warnings.append(
                    f"{len(different_date_topics)} shared {label} only recorded on different dates"
                )
    score = round(matched_weight / max(50, compared_weight) * 100)
    return min(score, 100), reasons, warnings


def build_duplicate_group_candidate(connection, records) -> dict:
    schema = get_schema(connection, records[0]["dataset"])
    pair_scores = [
        (*duplicate_similarity(left, right), left["id"], right["id"])
        for left, right in combinations(records, 2)
    ]
    best_score, best_reasons, best_warnings, left_id, right_id = max(
        pair_scores, key=lambda item: (item[0], len(item[1]))
    )
    best_pair_ids = {left_id, right_id}
    pair_score_by_ids = {
        frozenset((pair_left_id, pair_right_id)): score
        for score, _reasons, _warnings, pair_left_id, pair_right_id in pair_scores
    }
    records_by_id = {record["id"]: record for record in records}
    ordered_records = [records_by_id[left_id], records_by_id[right_id]]
    ordered_records.extend(sorted(
        (record for record in records if record["id"] not in best_pair_ids),
        key=lambda record: (
            -max(pair_score_by_ids[frozenset((record["id"], best_id))] for best_id in best_pair_ids),
            record["id"],
        ),
    ))
    items = []
    for record in ordered_records:
        raw = json.loads(record["raw_data"])
        items.append({
            "record": dict(record),
            "sections": populated_raw_sections(schema, raw),
            "default_selected": record["id"] in best_pair_ids,
            "strongest_pair": len(records) > 2 and record["id"] in best_pair_ids,
            "activity_count": sum(
                populated_value(value) for key, value in raw.items()
                if duplicate_activity_field(record["dataset"], key)
            ),
        })
    return {
        "name": records[0]["name"],
        "dataset": records[0]["dataset"],
        "records": items,
        "record_ids": [record["id"] for record in ordered_records],
        "conflicts": duplicate_group_conflicts(connection, ordered_records),
        "similarity": best_score,
        "similarity_reasons": best_reasons,
        "similarity_warnings": best_warnings,
    }


def duplicate_review_page(connection):
    review_status = request.args.get("status", "pending")
    if review_status not in {"pending", "merged", "rejected"}:
        review_status = "pending"
    dataset = request.args.get("dataset", "training")
    if dataset not in {"training", "care"}:
        dataset = "training"
    page = max(1, request.args.get("page", 1, type=int))
    query = request.args.get("q", "").strip()
    page_size = 12
    record_groups = []
    if review_status == "pending":
        rows = connection.execute(
            """SELECT r.*, f.uid FROM records r JOIN farmers f ON f.id=r.farmer_id
               WHERE r.dataset=? AND r.archived_at IS NULL ORDER BY r.name COLLATE NOCASE, r.id""",
            (dataset,),
        ).fetchall()
        by_name = defaultdict(list)
        for row in rows:
            normalized = normalized_person_name(row["name"])
            if normalized and (not query or query.casefold() in row["name"].casefold()):
                by_name[normalized].append(row)
        reviewed = {
            (row[0], row[1]) for row in connection.execute(
                "SELECT record_a_id, record_b_id FROM duplicate_reviews"
            ).fetchall()
        }
        for same_name_rows in by_name.values():
            if len(same_name_rows) < 2:
                continue
            if any(tuple(sorted((left["id"], right["id"]))) not in reviewed
                   for left, right in combinations(same_name_rows, 2)):
                record_groups.append(same_name_rows)
    else:
        reviews = connection.execute(
            """SELECT dr.record_a_id, dr.record_b_id FROM duplicate_reviews dr
               JOIN records a ON a.id=dr.record_a_id
               WHERE dr.status=? AND a.dataset=? ORDER BY dr.reviewed_at DESC""",
            (review_status, dataset),
        ).fetchall()
        reviewed_ids = sorted({record_id for review in reviews for record_id in review})
        if reviewed_ids:
            placeholders = ",".join("?" for _ in reviewed_ids)
            reviewed_rows = connection.execute(
                f"""SELECT r.*, f.uid FROM records r JOIN farmers f ON f.id=r.farmer_id
                    WHERE r.id IN ({placeholders}) ORDER BY r.name COLLATE NOCASE, r.id""",
                reviewed_ids,
            ).fetchall()
            by_name = defaultdict(list)
            for row in reviewed_rows:
                if not query or query.casefold() in row["name"].casefold():
                    by_name[normalized_person_name(row["name"])].append(row)
            record_groups.extend(group for group in by_name.values() if len(group) >= 2)
    candidates = [build_duplicate_group_candidate(connection, group) for group in record_groups]
    candidates.sort(key=lambda item: (
        -item["similarity"], -len(item["similarity_reasons"]),
        item["name"].casefold(), item["record_ids"],
    ))
    total = len(candidates)
    candidates = candidates[(page - 1) * page_size:page * page_size]
    if review_status == "pending":
        from identity_review import review_payload

        for candidate in candidates:
            candidate["review_data"] = review_payload(connection, [item["record"] for item in candidate["records"]])
    return render_template(
        "duplicates.html", candidates=candidates, review_status=review_status, dataset=dataset,
        total=total, page=page, pages=max(1, (total + page_size - 1) // page_size), q=query,
    )


def comparable_activity_value(value) -> str:
    parsed = parse_date(value)
    return parsed.isoformat() if parsed else str(value).strip().casefold()


def merge_training_histories(survivor_raw: dict, donor_raw: dict, merged_base: dict):
    merged = dict(merged_base)
    added = 0
    overflow = []
    for topic in TRAINING_TOPICS:
        fields = ("Training", "Follow up", "Follow up score", "Follow up next action")

        def cycles_from(raw):
            cycles = []
            for cycle in range(1, 4):
                payload = {prefix: raw.get(f"{prefix} {cycle} - {topic}") for prefix in fields}
                if any(populated_value(value) for value in payload.values()):
                    cycles.append(payload)
            return cycles

        cycles = cycles_from(survivor_raw)
        for donor_cycle in cycles_from(donor_raw):
            match = None
            for existing_cycle in cycles:
                same_training = populated_value(donor_cycle["Training"]) and populated_value(existing_cycle["Training"]) \
                    and comparable_activity_value(donor_cycle["Training"]) == comparable_activity_value(existing_cycle["Training"])
                same_followup = populated_value(donor_cycle["Follow up"]) and populated_value(existing_cycle["Follow up"]) \
                    and comparable_activity_value(donor_cycle["Follow up"]) == comparable_activity_value(existing_cycle["Follow up"])
                if same_training or same_followup:
                    match = existing_cycle
                    break
            if match is not None:
                for prefix, value in donor_cycle.items():
                    if populated_value(value) and not populated_value(match.get(prefix)):
                        match[prefix] = value
                        added += 1
            elif len(cycles) < 3:
                cycles.append(dict(donor_cycle))
                added += sum(populated_value(value) for value in donor_cycle.values())
            else:
                overflow.append(topic)
        for cycle in range(1, 4):
            for prefix in fields:
                merged.pop(f"{prefix} {cycle} - {topic}", None)
        for cycle, payload in enumerate(cycles, start=1):
            for prefix, value in payload.items():
                if populated_value(value):
                    merged[f"{prefix} {cycle} - {topic}"] = value
    return merged, added, sorted(set(overflow))


def reassign_duplicate_event_history(connection, donor_id: int, survivor_id: int) -> None:
    entries = connection.execute(
        "SELECT id, event_id, topic FROM field_event_entries WHERE record_id=?", (donor_id,)
    ).fetchall()
    for entry in entries:
        conflict = connection.execute(
            "SELECT 1 FROM field_event_entries WHERE event_id=? AND record_id=? AND topic=?",
            (entry["event_id"], survivor_id, entry["topic"]),
        ).fetchone()
        if conflict:
            continue
        connection.execute(
            "UPDATE followup_responses SET record_id=? WHERE event_entry_id=?",
            (survivor_id, entry["id"]),
        )
        connection.execute(
            "UPDATE field_event_entries SET record_id=? WHERE id=?", (survivor_id, entry["id"])
        )


def build_priorities(records, status_by_record, as_of: date | None = None):
    as_of = as_of or date.today()
    soon_cutoff = as_of + timedelta(days=30)
    priorities = []
    for record in records:
        if "drop" in (record["record_status"] or "").lower():
            continue
        statuses = status_by_record.get(record["id"], [])
        followup = sum(item["followup_needed"] for item in statuses)
        retraining = sum(item["retraining_needed"] for item in statuses)
        raw = json.loads(record["raw_data"])
        upcoming_dates = []
        for item in statuses:
            if item["status_code"] != "WAIT":
                continue
            eligible_value = training_topic_status(raw, item["topic"], as_of=as_of).get("next_followup_date")
            eligible_on = parse_date(eligible_value)
            if eligible_on and as_of < eligible_on <= soon_cutoff:
                upcoming_dates.append(eligible_on)
        next_followup = min(upcoming_dates) if upcoming_dates else None
        if followup or retraining or next_followup:
            priorities.append({
                "id": record["id"], "name": record["name"], "uid": record["uid"],
                "group_name": record["group_name"], "followup": followup, "retraining": retraining,
                "upcoming_followups": len(upcoming_dates),
                "next_followup_date": next_followup.isoformat() if next_followup else None,
                "next_followup_display": next_followup.strftime("%d %b %Y") if next_followup else None,
                "days_until_followup": (next_followup - as_of).days if next_followup else None,
                "priority_state": "due" if followup else "upcoming" if next_followup else "training",
            })
    state_order = {"due": 0, "upcoming": 1, "training": 2}
    return sorted(priorities, key=lambda item: (
        state_order[item["priority_state"]],
        -item["followup"],
        item["next_followup_date"] or "9999-12-31",
        -item["retraining"],
        item["name"].casefold(),
    ))


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return cleaned[:70] or "report"


def build_cbf_group_reports(connection, cbf_name: str, supplied_filters: dict):
    exists = connection.execute(
        "SELECT 1 FROM records WHERE dataset='training' AND archived_at IS NULL AND cbf_name=? LIMIT 1",
        (cbf_name,),
    ).fetchone()
    if not exists:
        return []
    filters = {key: supplied_filters.get(key, "") for key in (
        "gender", "age", "cbf", "group", "category", "topic", "date_from", "date_to", "status", "dropouts"
    )}
    selected_group = filters.get("group", "")
    if selected_group:
        groups = [selected_group]
    else:
        groups = [row[0] for row in connection.execute(
            """SELECT DISTINCT group_name FROM records
               WHERE dataset='training' AND archived_at IS NULL AND cbf_name=?
               AND TRIM(COALESCE(group_name,''))<>'' ORDER BY group_name""",
            (cbf_name,),
        ).fetchall()]

    reports = []
    for group_name in groups:
        group_filters = dict(filters, cbf=cbf_name, group=group_name)
        data = build_dashboard_data(connection, "training", group_filters)
        if not data["records"]:
            continue
        farmers = []
        dropped = []
        for record in data["records"]:
            if "drop" in (record["record_status"] or "").lower():
                dropped.append(record["name"])
                continue
            statuses = {item["topic"]: item for item in data["status_by_record"].get(record["id"], [])}
            topic_status = {}
            followup_topics = []
            training_topics = []
            for topic in TRAINING_TOPICS:
                status = statuses.get(topic)
                code = status["status_code"] if status else ""
                if code == "FU":
                    display = "FU"
                    followup_topics.append(topic)
                elif code in {"CT", "RT"}:
                    display = "CT"
                    training_topics.append(topic)
                elif code == "COMPLETED":
                    display = "OK"
                elif code == "WAIT":
                    display = "WAIT"
                elif code == "REVIEW":
                    display = "RV"
                else:
                    display = "-"
                topic_status[topic] = display
            farmers.append({
                "name": record["name"],
                "uid": record["uid"],
                "topic_status": topic_status,
                "followup_topics": followup_topics,
                "training_topics": training_topics,
                "action_count": len(followup_topics) + len(training_topics),
            })
        farmers.sort(key=lambda item: (-item["action_count"], item["name"]))
        active_topics = []
        for topic in TRAINING_TOPICS:
            codes = [farmer["topic_status"][topic] for farmer in farmers]
            active_topics.append({
                "topic": topic,
                "participants": len(farmers),
                "followup": sum(code == "FU" for code in codes),
                "retraining": sum(code == "CT" for code in codes),
                "confirmed": sum(code == "OK" for code in codes),
                "waiting": sum(code == "WAIT" for code in codes),
            })
        reports.append({
            "group_name": group_name,
            "summary": data["summary"],
            "topics": active_topics,
            "farmers": farmers,
            "dropped": sorted(dropped),
            "active_count": len(farmers),
            "fu_people": sum(bool(item["followup_topics"]) for item in farmers),
            "ct_people": sum(bool(item["training_topics"]) for item in farmers),
            "action_entries": sum(item["action_count"] for item in farmers),
            "filters": describe_filters("training", group_filters),
        })
    return reports


def get_schema(connection, dataset: str):
    return get_setting(connection, f"{dataset}_schema", [])


def group_raw_fields(schema, raw):
    sections = defaultdict(list)
    for field in schema:
        sections[field.get("section", "Other")].append({**field, "value": raw.get(field["key"])})
    return sections


def populated_raw_sections(schema, raw_payload):
    """Return every populated source field, grouped in workbook order."""
    try:
        raw = json.loads(raw_payload) if isinstance(raw_payload, str) else dict(raw_payload or {})
    except (json.JSONDecodeError, TypeError, ValueError):
        raw = {}

    sections = defaultdict(list)
    known_keys = set()
    for field in schema:
        key = field.get("key", "")
        known_keys.add(key)
        value = raw.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        sections[field.get("section", "Other")].append({
            "label": field.get("label") or key,
            "value": value,
        })
    for key, value in raw.items():
        if key in known_keys or value is None or (isinstance(value, str) and not value.strip()):
            continue
        sections["Other"].append({"label": key, "value": value})
    return dict(sections)


def raw_from_form(schema, previous):
    raw = dict(previous)
    for field in schema:
        raw[field["key"]] = request.form.get(f"field__{field['key']}", "").strip()
    return raw


def canonical_fields(connection, dataset: str, raw: dict, manual_cbf: str = ""):
    if dataset == "training":
        name = raw.get("Name", "")
        age_value = raw.get("Age group", "")
        age_lookup = {"Y": "Youth", "A": "Adult", "E": "Elder", "YOUTH": "Youth", "ADULT": "Adult", "ELDER": "Elder"}
        age_group = age_lookup.get(str(age_value).strip().upper(), str(age_value).strip())
        group_name = raw.get("GROUP NAME", "")
        cbf_map = get_setting(connection, "cbf_group_map", {})
        normalized_group = " ".join("".join(char.lower() if char.isalnum() else " " for char in group_name).split())
        cbf = manual_cbf.strip() or cbf_map.get(normalized_group, "")
        return {
            "name": name.strip(), "sex": normalize_sex_app(raw.get("Sex", "")),
            "age_value": str(age_value).strip(), "age_group": age_group, "phone": str(raw.get("Phone Number", "")).strip(),
            "village": str(raw.get("Village", "")).strip(), "parish": str(raw.get("Parish", "")).strip(),
            "subcounty": str(raw.get("Sub County", "")).strip(), "district": str(raw.get("DISTRICT", "")).strip(),
            "group_name": str(group_name).strip(), "cbf_name": cbf, "record_status": str(raw.get("Status", "")).strip(),
        }
    age_value = str(raw.get("Age", "")).strip()
    try:
        age_number = int(float(age_value))
        age_group = "Youth" if age_number <= 35 else "Adult" if age_number <= 59 else "Elder"
    except ValueError:
        age_group = age_value
    return {
        "name": str(raw.get("Project Participant", "")).strip(), "sex": normalize_sex_app(raw.get("Sex", "")),
        "age_value": age_value, "age_group": age_group, "phone": str(raw.get("Contact", "")).strip(),
        "village": str(raw.get("Village", "")).strip(), "parish": str(raw.get("Parish", "")).strip(),
        "subcounty": str(raw.get("Subcounty", "")).strip(), "district": str(raw.get("District", "")).strip(),
        "group_name": str(raw.get("Name of group/school", "")).strip(), "cbf_name": manual_cbf.strip(),
        "record_status": str(raw.get("Status", "")).strip(),
    }


def normalize_sex_app(value):
    text = str(value).strip().lower()
    if text in {"f", "female"}:
        return "F"
    if text in {"m", "male"}:
        return "M"
    return str(value).strip().upper()


def insert_application_record(connection, farmer_id, dataset, raw, core):
    cursor = connection.execute(
        """INSERT INTO records(
            farmer_id, dataset, source_row, name, sex, age_value, age_group, phone, village, parish,
            subcounty, district, group_name, cbf_name, record_status, raw_data, created_at, updated_at
        ) VALUES(?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            farmer_id, dataset, core["name"], core["sex"], core["age_value"], core["age_group"], core["phone"],
            core["village"], core["parish"], core["subcounty"], core["district"], core["group_name"],
            core["cbf_name"], core["record_status"], json.dumps(raw, ensure_ascii=False), utc_now(), utc_now(),
        ),
    )
    return cursor.lastrowid


def rebuild_statuses(connection, record_id: int, dataset: str, raw: dict):
    connection.execute("DELETE FROM topic_statuses WHERE record_id=?", (record_id,))
    if dataset == "training":
        statuses = [(topic, training_topic_status(raw, topic)) for topic in TRAINING_TOPICS]
    else:
        modules = get_setting(connection, "care_modules", {})
        statuses = [(topic, care_module_status(raw, fields)) for topic, fields in modules.items()]
    for topic, status in statuses:
        details = {key: value for key, value in status.items() if key in {"attended", "marked", "expected"}}
        connection.execute(
            """INSERT INTO topic_statuses(
                record_id, topic, status_code, status_label, training_received, confirmed_trained,
                followup_needed, retraining_needed, last_activity_date, details
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record_id, topic, status["status_code"], status["status_label"], status["training_received"],
                status["confirmed_trained"], status["followup_needed"], status["retraining_needed"],
                status.get("last_activity_date"), json.dumps(details) if details else None,
            ),
        )


def ensure_care_structure(connection):
    """Normalize FH module definitions and rebuild their derived statuses once."""
    expected_modules = care_module_fields()
    if (
        get_setting(connection, "care_structure_version") == 2
        and get_setting(connection, "care_modules", {}) == expected_modules
    ):
        return

    old_theme = "School clubs - Theme 5 - Session "
    old_extra = "School clubs - Theme 7 - Session 76"
    schema = get_setting(connection, "care_schema", [])
    normalized_schema = []
    for field in schema:
        field = dict(field)
        key = str(field.get("key", ""))
        if key.startswith(old_theme):
            session = int(key.removeprefix(old_theme)) + 2
            key = f"School clubs - Theme 4 - Session {session}"
            field.update({"key": key, "label": key, "section": "School clubs - Theme 4"})
        elif key == old_extra:
            field.update({"key": "Column BX", "label": "Column BX", "section": "Other", "type": "text"})
        normalized_schema.append(field)

    rows = connection.execute(
        "SELECT id, raw_data FROM records WHERE dataset='care'"
    ).fetchall()
    normalized_raw = {}
    for row in rows:
        raw = json.loads(row["raw_data"])
        for old_session in range(1, 5):
            old_key = f"{old_theme}{old_session}"
            new_key = f"School clubs - Theme 4 - Session {old_session + 2}"
            if old_key in raw:
                raw[new_key] = raw.pop(old_key)
        if old_extra in raw:
            raw["Column BX"] = raw.pop(old_extra)
        normalized_raw[row["id"]] = raw
        connection.execute(
            "UPDATE records SET raw_data=? WHERE id=?",
            (json.dumps(raw, ensure_ascii=False), row["id"]),
        )

    connection.execute(
        "DELETE FROM topic_statuses WHERE record_id IN (SELECT id FROM records WHERE dataset='care')"
    )
    for record_id, raw in normalized_raw.items():
        for topic, fields in expected_modules.items():
            status = care_module_status(raw, fields)
            details = {key: value for key, value in status.items() if key not in {
                "status_code", "status_label", "training_received", "confirmed_trained",
                "followup_needed", "retraining_needed", "last_activity_date",
            }}
            connection.execute(
                """INSERT INTO topic_statuses(
                    record_id, topic, status_code, status_label, training_received, confirmed_trained,
                    followup_needed, retraining_needed, last_activity_date, details
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record_id, topic, status["status_code"], status["status_label"],
                    status["training_received"], status["confirmed_trained"],
                    status["followup_needed"], status["retraining_needed"],
                    status.get("last_activity_date"), json.dumps(details, ensure_ascii=False),
                ),
            )
    set_setting(connection, "care_schema", normalized_schema)
    set_setting(connection, "care_modules", expected_modules)
    set_setting(connection, "care_structure_version", 2)
    connection.commit()


def refresh_time_sensitive_statuses(connection):
    today = date.today().isoformat()
    if get_setting(connection, "status_calculated_on") == today:
        return
    rows = connection.execute(
        "SELECT id, raw_data FROM records WHERE dataset='training'"
    ).fetchall()
    for row in rows:
        rebuild_statuses(connection, row["id"], "training", json.loads(row["raw_data"]))
    from db import set_setting

    set_setting(connection, "status_calculated_on", today)
    connection.commit()


app = create_app()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")), debug=os.getenv("FLASK_DEBUG") == "1")
