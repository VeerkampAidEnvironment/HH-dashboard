"""ARFSA training database and monitoring dashboard."""

from __future__ import annotations

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
from datetime import date
from functools import wraps
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

from flask import (
    Flask,
    abort,
    flash,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from db import get_db, get_setting, init_db, log_audit, register_db, set_setting, utc_now
from questionnaire import NEXT_ACTION_OPTIONS, QUESTIONNAIRE_VERSION, questionnaire_for_topic
from training import (
    TRAINING_TOPICS,
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


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.getenv("ARFSA_SECRET_KEY", "development-only-change-before-deployment"),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.getenv("ARFSA_SECURE_COOKIES", "0") == "1",
        MAX_CONTENT_LENGTH=8 * 1024 * 1024,
    )
    if test_config:
        app.config.update(test_config)
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    register_db(app)
    with app.app_context():
        connection = get_db()
        init_db(connection)
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
        }

    @app.before_request
    def protect_application():
        allowed = {"login", "static", "health"}
        if request.endpoint not in allowed and not session.get("authenticated"):
            return redirect(url_for("login", next=request.full_path.rstrip("?")))
        if request.endpoint not in allowed and session.get("authenticated"):
            user = get_db().execute(
                "SELECT id, username, display_name, is_active, is_admin, access_scope FROM users WHERE id=?",
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
            fh_allowed = {"dashboard", "record_list", "record_detail", "logout"}
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
            return render_template("fh_dashboard.html", dataset=dataset, filters=filters, **data)
        data = build_dashboard_data(get_db(), dataset, filters)
        return render_template("dashboard.html", dataset=dataset, filters=filters, **data)

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

    @app.get("/cbfs")
    def cbf_list():
        connection = get_db()
        rows = connection.execute(
            """SELECT cbf_name, COUNT(*) AS participant_count, COUNT(DISTINCT farmer_id) AS farmer_count,
                      COUNT(DISTINCT group_name) AS group_count
               FROM records WHERE archived_at IS NULL AND dataset='training' AND TRIM(cbf_name)<>''
               GROUP BY cbf_name ORDER BY cbf_name"""
        ).fetchall()
        return render_template("cbfs.html", cbfs=rows)

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
        if record["dataset"] == "training":
            next_action_labels = {item["value"]: item["label"] for item in NEXT_ACTION_OPTIONS}
            response_rows = connection.execute(
                """SELECT fr.*, e.event_date, e.created_by
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
        return render_template(
            "record_detail.html", record=record, statuses=statuses, related=related,
            raw=raw, sections=sections, followup_history=followup_history,
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
        where = "" if match_status == "all" else f"WHERE ({effective_status})=?"
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
        if selected_user:
            rows = connection.execute(
                """SELECT a.*, COALESCE(u.display_name, a.username) AS display_name
                   FROM audit_log a LEFT JOIN users u ON u.username=a.username COLLATE NOCASE
                   WHERE a.username=? COLLATE NOCASE ORDER BY a.id DESC LIMIT 500""",
                (selected_user,),
            ).fetchall()
        else:
            rows = connection.execute(
                """SELECT a.*, COALESCE(u.display_name, a.username) AS display_name
                   FROM audit_log a LEFT JOIN users u ON u.username=a.username COLLATE NOCASE
                   ORDER BY a.id DESC LIMIT 500"""
            ).fetchall()
        usernames = connection.execute(
            """SELECT DISTINCT a.username, COALESCE(u.display_name, a.username) AS display_name
               FROM audit_log a LEFT JOIN users u ON u.username=a.username COLLATE NOCASE
               ORDER BY display_name COLLATE NOCASE"""
        ).fetchall()
        return render_template("audit.html", entries=rows, usernames=usernames, selected_user=selected_user)

    @app.route("/users", methods=["GET", "POST"])
    @admin_required
    def users():
        connection = get_db()
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
                    is_admin = 1 if request.form.get("is_admin") and access_scope == "full" else 0
                    cursor = connection.execute(
                        """INSERT INTO users(username, display_name, password_hash, is_active, is_admin, access_scope, created_at, updated_at)
                           VALUES(?, ?, ?, 1, ?, ?, ?, ?)""",
                        (username, display_name, generate_password_hash(password),
                         is_admin, access_scope, now, now),
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
        return render_template("users.html", users=rows)

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
        is_admin = 1 if request.form.get("is_admin") and access_scope == "full" else 0
        connection.execute(
            "UPDATE users SET access_scope=?, is_admin=?, updated_at=? WHERE id=?",
            (access_scope, is_admin, utc_now(), user_id),
        )
        label = {"full": "Full system access", "ae_user": "AE User", "fh_dashboard": "FH dashboard only"}[access_scope]
        log_audit(connection, session["username"], "change_access", "user", user_id,
                  f"Changed access for {user['username']} to {label}")
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
        venue_values.extend(row[0] for row in connection.execute(
            f"SELECT DISTINCT TRIM(village) AS venue FROM records WHERE {' AND '.join(village_where)}",
            village_params,
        ).fetchall())
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
                    return redirect(url_for("data_entry", cbf=selected_cbf, mode=mode,
                                            group=request.form.get("group", "")))

        central_eligibility = {topic: [] for topic in TRAINING_TOPICS}
        groups = []
        followup_farmers = []
        followup_topics = []
        selected_group = request.values.get("group", "").strip()
        selected_record_id = request.values.get("record_id", type=int)
        selected_event_date = valid_iso_date(request.values.get("event_date", "")) or ""

        if selected_cbf:
            groups = [row[0] for row in connection.execute(
                """SELECT DISTINCT group_name FROM records
                   WHERE dataset='training' AND archived_at IS NULL AND cbf_name=?
                   AND TRIM(COALESCE(group_name,''))<>'' ORDER BY group_name""",
                (selected_cbf,),
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
                "r.dataset='training'", "r.archived_at IS NULL", "r.cbf_name=?",
                "EXISTS (SELECT 1 FROM topic_statuses ts WHERE ts.record_id=r.id AND ts.status_code='FU')",
            ]
            followup_params = [selected_cbf]
            if selected_group:
                followup_where.append("r.group_name=?")
                followup_params.append(selected_group)
            followup_farmers = connection.execute(
                f"""SELECT r.id, r.name, r.group_name, f.uid,
                            (SELECT COUNT(*) FROM topic_statuses ts
                             WHERE ts.record_id=r.id AND ts.status_code='FU') AS due_count
                     FROM records r JOIN farmers f ON f.id=r.farmer_id
                     WHERE {' AND '.join(followup_where)} ORDER BY r.group_name, r.name""",
                followup_params,
            ).fetchall()
            eligible_followup_ids = {row["id"] for row in followup_farmers}
            if selected_record_id in eligible_followup_ids:
                followup_rows = connection.execute(
                    """SELECT topic, status_label, last_activity_date FROM topic_statuses
                       WHERE record_id=? AND status_code='FU' ORDER BY topic""",
                    (selected_record_id,),
                ).fetchall()
                followup_topics = [
                    {**dict(row), "questionnaire": questionnaire_for_topic(row["topic"])}
                    for row in followup_rows
                ]
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
            history=history, today=date.today().isoformat(), selected_event_date=selected_event_date,
            venues=venues, selected_venue=selected_venue, new_venue=new_venue,
        )

    @app.get("/bulk-upload")
    def bulk_upload():
        return render_template("bulk_upload.html")

    @app.errorhandler(404)
    def not_found(_error):
        return render_template("404.html"), 404

    return app


def valid_iso_date(value: str) -> str | None:
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        return None


def save_centralized_training(connection, cbf_name: str, values, username: str):
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
        """INSERT INTO field_events(event_type, cbf_name, event_date, location, created_by, created_at)
           VALUES('centralized', ?, ?, ?, ?, ?)""",
        (cbf_name, event_date, location, username, utc_now()),
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


def save_followup(connection, cbf_name: str, values, username: str):
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
        """INSERT INTO field_events(event_type, cbf_name, event_date, location, created_by, created_at)
           VALUES('followup', ?, ?, NULL, ?, ?)""",
        (cbf_name, event_date, username, utc_now()),
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
         "next_actions": {entry["topic"]: entry["next_action"] for entry in response_entries}},
    )
    connection.commit()
    return True, f"Follow-up saved for {record['name']}."


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
    return {
        key: values.get(key, "").strip()
        for key in ("gender", "age", "cbf", "group", "category", "date_from", "date_to", "status")
    }


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
    for filter_key, column in (("gender", "sex"), ("age", "age_group"), ("cbf", "cbf_name"), ("group", "group_name")):
        if not filters.get(filter_key):
            continue
        if dataset == "combined":
            where.append(
                f"EXISTS (SELECT 1 FROM records rf WHERE rf.farmer_id=r.farmer_id "
                f"AND rf.archived_at IS NULL AND rf.{column}=?)"
            )
        else:
            where.append(f"{column}=?")
        params.append(filters[filter_key])
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
        if filters.get(key):
            where.append(f"{column}=?")
            params.append(filters[key])
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
        for topic in TRAINING_TOPICS:
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
    activity_filter_options["topics"] = list(TRAINING_TOPICS)

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
        "status_distribution": status_distribution,
        "action_topics": action_topics,
        "attention_total": len(attention_ids),
        "waiting_total": len(waiting_ids),
        "options": options,
    }


def build_fh_dashboard_data(connection, filters: dict[str, str]):
    """Build attendance-focused FH analytics with track-specific denominators."""
    where = ["r.dataset='care'", "r.archived_at IS NULL"]
    params = []
    for key, column in (("gender", "r.sex"), ("age", "r.age_group"), ("group", "r.group_name")):
        if filters.get(key):
            where.append(f"{column}=?")
            params.append(filters[key])
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
    options = {
        "genders": [row[0] for row in connection.execute(
            f"SELECT DISTINCT sex FROM records WHERE {base_where} AND TRIM(COALESCE(sex,''))<>'' ORDER BY sex"
        )],
        "ages": [row[0] for row in connection.execute(
            f"SELECT DISTINCT age_group FROM records WHERE {base_where} AND TRIM(COALESCE(age_group,''))<>'' ORDER BY age_group"
        )],
        "groups": [row[0] for row in connection.execute(
            f"SELECT DISTINCT group_name FROM records WHERE {base_where} AND TRIM(COALESCE(group_name,''))<>'' ORDER BY group_name"
        )],
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
    labels = {"gender": "Gender", "age": "Age group", "cbf": "CBF", "group": "Group", "category": "Category", "date_from": "From", "date_to": "To", "status": "Status"}
    descriptions.extend(f"{labels[key]}: {value}" for key, value in filters.items() if value)
    return descriptions


def build_priorities(records, status_by_record):
    priorities = []
    for record in records:
        if "drop" in (record["record_status"] or "").lower():
            continue
        statuses = status_by_record.get(record["id"], [])
        followup = sum(item["followup_needed"] for item in statuses)
        retraining = sum(item["retraining_needed"] for item in statuses)
        if followup or retraining:
            priorities.append({
                "id": record["id"], "name": record["name"], "uid": record["uid"],
                "group_name": record["group_name"], "followup": followup, "retraining": retraining,
                "priority": followup * 2 + retraining,
            })
    return sorted(priorities, key=lambda item: (-item["priority"], item["name"]))


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
        "gender", "age", "cbf", "group", "category", "date_from", "date_to", "status"
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
