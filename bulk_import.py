"""Safe, append-only imports from updated AE Farmer Database workbooks."""
from __future__ import annotations

import json
from typing import Any, BinaryIO
from openpyxl import load_workbook
from db import set_setting, utc_now
from scripts.import_data import care_field_schema, clean, json_value, normalize, normalize_age_group, normalize_phone, normalize_sex
from training import TRAINING_TOPICS, care_module_status, training_topic_status


class BulkImportError(ValueError):
    pass


def _identity_keys(core: dict[str, Any]) -> set[tuple[str, ...]]:
    keys = {(
        "profile", normalize(core.get("name")), normalize(core.get("sex")),
        normalize(core.get("group_name")), normalize(core.get("village")),
    )}
    phone = normalize_phone(core.get("phone"))
    if len(phone) >= 9:
        keys.add(("phone", phone))
    return keys


def _insert_record(connection, dataset: str, source_row: int, raw: dict[str, Any], core: dict[str, Any]) -> tuple[int, str]:
    timestamp = utc_now()
    cursor = connection.execute("INSERT INTO farmers(uid, created_at) VALUES(NULL, ?)", (timestamp,))
    farmer_id = cursor.lastrowid
    uid = f"ARF-{farmer_id:06d}"
    connection.execute("UPDATE farmers SET uid=? WHERE id=?", (uid, farmer_id))
    cursor = connection.execute(
        """INSERT INTO records(farmer_id, dataset, source_row, name, sex, age_value, age_group, phone,
           village, parish, subcounty, district, group_name, cbf_name, record_status, raw_data, created_at, updated_at)
           VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (farmer_id, dataset, source_row, core["name"], core["sex"], core["age_value"], core["age_group"], core["phone"],
         core["village"], core["parish"], core["subcounty"], core["district"], core["group_name"], core["cbf_name"],
         core["record_status"], json.dumps(raw, ensure_ascii=False, default=json_value), timestamp, timestamp))
    return cursor.lastrowid, uid


def _replace_statuses(connection, record_id: int, raw: dict[str, Any]) -> None:
    connection.execute("DELETE FROM topic_statuses WHERE record_id=?", (record_id,))
    for topic in TRAINING_TOPICS:
        status = training_topic_status(raw, topic)
        connection.execute("""INSERT INTO topic_statuses(record_id, topic, status_code, status_label,
            training_received, confirmed_trained, followup_needed, retraining_needed, last_activity_date, details)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
            (record_id, topic, status["status_code"], status["status_label"], status["training_received"],
             status["confirmed_trained"], status["followup_needed"], status["retraining_needed"],
             status.get("last_activity_date")))


def _replace_care_statuses(connection, record_id: int, raw: dict[str, Any], module_fields: dict[str, list[str]]) -> None:
    connection.execute("DELETE FROM topic_statuses WHERE record_id=?", (record_id,))
    for topic, fields in module_fields.items():
        status = care_module_status(raw, fields)
        details = {key: value for key, value in status.items() if key in {"attended", "marked", "expected"}}
        connection.execute("""INSERT INTO topic_statuses(record_id, topic, status_code, status_label,
            training_received, confirmed_trained, followup_needed, retraining_needed, last_activity_date, details)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (record_id, topic, status["status_code"], status["status_label"], status["training_received"],
             status["confirmed_trained"], status["followup_needed"], status["retraining_needed"],
             status.get("last_activity_date"), json.dumps(details, ensure_ascii=False)))


def _is_training_field(field: str) -> bool:
    return field.startswith("Training ") or field.startswith("Follow up ") or field == "Number of Birds Vaccinated"


def append_farmer_database(connection, file: BinaryIO, filename: str = "") -> dict[str, int]:
    try:
        workbook = load_workbook(file, read_only=True, data_only=True, keep_links=False)
    except Exception as exc:
        raise BulkImportError("The file is not a readable Excel workbook.") from exc
    try:
        if "Farmer Database" not in workbook.sheetnames:
            raise BulkImportError("The workbook must contain a 'Farmer Database' sheet.")
        sheet = workbook["Farmer Database"]
        headers = [clean(cell.value) for cell in next(sheet.iter_rows(min_row=2, max_row=2, max_col=88))]
        required = {"Name", "Sex", "Age group", "Phone Number", "Village", "GROUP NAME"}
        if not required.issubset(set(headers)):
            raise BulkImportError("This is not the expected Farmer Database format; required columns are missing.")
        cbf_map = {}
        if "CBF_Groups" in workbook.sheetnames:
            cbf_map = {normalize(group): clean(cbf) for cbf, group in
                       workbook["CBF_Groups"].iter_rows(min_row=2, max_col=2, values_only=True)
                       if clean(cbf) and clean(group)}
        existing_by_id = {}
        identity_matches: dict[tuple[str, ...], set[int]] = {}
        for existing_row in connection.execute(
            """SELECT r.id, r.name, r.sex, r.phone, r.village, r.group_name, r.raw_data, f.uid
               FROM records r JOIN farmers f ON f.id=r.farmer_id WHERE r.dataset='training'"""
        ).fetchall():
            existing = dict(existing_row)
            existing["raw"] = json.loads(existing.pop("raw_data"))
            existing_by_id[existing["id"]] = existing
            for identity in _identity_keys(existing):
                identity_matches.setdefault(identity, set()).add(existing["id"])
        next_source_row = connection.execute(
            "SELECT COALESCE(MAX(source_row), 2) + 1 FROM records WHERE dataset='training'"
        ).fetchone()[0]
        uploaded_identities = set()
        new_rows = []
        training_updates = []
        unchanged = duplicates = ambiguous = 0
        for row_number, row in enumerate(sheet.iter_rows(min_row=3, max_row=sheet.max_row, max_col=88, values_only=True), start=3):
            if not clean(row[0]):
                continue
            raw = {header: json_value(value) for header, value in zip(headers, row) if header}
            group_name = clean(raw.get("GROUP NAME"))
            core = {"name": clean(raw.get("Name")), "sex": normalize_sex(raw.get("Sex")),
                    "age_value": clean(raw.get("Age group")), "age_group": normalize_age_group(raw.get("Age group")),
                    "phone": clean(raw.get("Phone Number")), "village": clean(raw.get("Village")),
                    "parish": clean(raw.get("Parish")), "subcounty": clean(raw.get("Sub County")),
                    "district": clean(raw.get("DISTRICT")), "group_name": group_name,
                    "cbf_name": cbf_map.get(normalize(group_name), ""), "record_status": clean(raw.get("Status"))}
            if not core["name"]:
                raise BulkImportError(f"Row {row_number} has no beneficiary name.")
            identities = _identity_keys(core)
            phone = normalize_phone(core.get("phone"))
            phone_matches = identity_matches.get(("phone", phone), set()) if len(phone) >= 9 else set()
            profile = next(identity for identity in identities if identity[0] == "profile")
            matching_ids = set(phone_matches) if phone_matches else set(identity_matches.get(profile, set()))
            if matching_ids:
                if len(matching_ids) != 1:
                    ambiguous += 1
                    continue
                existing = existing_by_id[matching_ids.pop()]
                merged = dict(existing["raw"])
                added_fields = []
                for field, value in raw.items():
                    if _is_training_field(field) and clean(value) and not clean(merged.get(field)):
                        merged[field] = value
                        added_fields.append(field)
                if added_fields:
                    connection.execute(
                        "UPDATE records SET raw_data=?, updated_at=? WHERE id=?",
                        (json.dumps(merged, ensure_ascii=False, default=json_value), utc_now(), existing["id"]),
                    )
                    _replace_statuses(connection, existing["id"], merged)
                    existing["raw"] = merged
                    training_updates.append({
                        "record_id": existing["id"], "uid": existing["uid"], "name": existing["name"],
                        "fields": added_fields, "field_count": len(added_fields),
                    })
                else:
                    unchanged += 1
                continue
            if identities & uploaded_identities:
                duplicates += 1
                continue
            uploaded_identities.update(identities)
            new_rows.append((raw, core))
        for raw, core in new_rows:
            assigned_source_row = next_source_row
            record_id, uid = _insert_record(connection, "training", assigned_source_row, raw, core)
            next_source_row += 1
            core["audit"] = {
                "record_id": record_id, "uid": uid, "name": core["name"], "sex": core["sex"],
                "village": core["village"], "group_name": core["group_name"],
                "source_row": assigned_source_row,
            }
            _replace_statuses(connection, record_id, raw)
        if cbf_map:
            set_setting(connection, "cbf_group_map", cbf_map)
        set_setting(connection, "training_source", filename or "Farmer database upload")
        return {
            "added": len(new_rows), "updated": len(training_updates), "existing": unchanged,
            "duplicates": duplicates, "ambiguous": ambiguous,
            "beneficiaries": [core["audit"] for _raw, core in new_rows],
            "training_updates": training_updates,
        }
    finally:
        workbook.close()


def append_care_database(connection, file: BinaryIO, filename: str = "") -> dict[str, Any]:
    """Append FH beneficiaries and only fill missing attendance values for known beneficiaries."""
    try:
        workbook = load_workbook(file, read_only=True, data_only=True, keep_links=False)
    except Exception as exc:
        raise BulkImportError("The file is not a readable Excel workbook.") from exc
    try:
        if "Sheet1" not in workbook.sheetnames:
            raise BulkImportError("The FH workbook must contain a 'Sheet1' sheet.")
        sheet = workbook["Sheet1"]
        headers, _fields, module_fields = care_field_schema(sheet)
        attendance_fields = {field for fields in module_fields.values() for field in fields}
        existing_by_id, identity_matches = {}, {}
        for row in connection.execute("""SELECT r.id, r.name, r.sex, r.phone, r.village, r.group_name, r.raw_data, f.uid
                                       FROM records r JOIN farmers f ON f.id=r.farmer_id WHERE r.dataset='care'""").fetchall():
            existing = dict(row); existing["raw"] = json.loads(existing.pop("raw_data")); existing_by_id[existing["id"]] = existing
            for identity in _identity_keys(existing): identity_matches.setdefault(identity, set()).add(existing["id"])
        next_source_row = connection.execute("SELECT COALESCE(MAX(source_row), 3) + 1 FROM records WHERE dataset='care'").fetchone()[0]
        uploaded_identities, new_rows, attendance_updates = set(), [], []
        unchanged = duplicates = ambiguous = 0
        for row_number, row in enumerate(sheet.iter_rows(min_row=4, max_row=sheet.max_row, max_col=76, values_only=True), start=4):
            if not clean(row[1]): continue
            raw = {header: json_value(value) for header, value in zip(headers, row)}
            core = {"name": clean(raw.get("Project Participant")), "sex": normalize_sex(raw.get("Sex")),
                    "age_value": clean(raw.get("Age")), "age_group": normalize_age_group("", raw.get("Age")),
                    "phone": clean(raw.get("Contact")), "village": clean(raw.get("Village")), "parish": clean(raw.get("Parish")),
                    "subcounty": clean(raw.get("Subcounty")), "district": clean(raw.get("District")),
                    "group_name": clean(raw.get("Name of group/school")), "cbf_name": "", "record_status": clean(raw.get("Status"))}
            identities = _identity_keys(core); phone = normalize_phone(core["phone"])
            phone_matches = identity_matches.get(("phone", phone), set()) if len(phone) >= 9 else set()
            profile = next(identity for identity in identities if identity[0] == "profile")
            matches = set(phone_matches) if phone_matches else set(identity_matches.get(profile, set()))
            if matches:
                if len(matches) != 1: ambiguous += 1; continue
                existing = existing_by_id[matches.pop()]; merged = dict(existing["raw"])
                fields_added = [field for field in attendance_fields if clean(raw.get(field)) and not clean(merged.get(field))]
                if fields_added:
                    for field in fields_added: merged[field] = raw[field]
                    connection.execute("UPDATE records SET raw_data=?, updated_at=? WHERE id=?", (json.dumps(merged, ensure_ascii=False), utc_now(), existing["id"]))
                    _replace_care_statuses(connection, existing["id"], merged, module_fields); existing["raw"] = merged
                    attendance_updates.append({"record_id": existing["id"], "uid": existing["uid"], "name": existing["name"], "field_count": len(fields_added), "fields": fields_added})
                else: unchanged += 1
                continue
            if identities & uploaded_identities: duplicates += 1; continue
            uploaded_identities.update(identities); new_rows.append((raw, core))
        beneficiaries = []
        for raw, core in new_rows:
            record_id, uid = _insert_record(connection, "care", next_source_row, raw, core)
            _replace_care_statuses(connection, record_id, raw, module_fields)
            beneficiaries.append({"record_id": record_id, "uid": uid, "name": core["name"], "sex": core["sex"], "village": core["village"], "group_name": core["group_name"], "source_row": next_source_row})
            next_source_row += 1
        set_setting(connection, "care_source", filename or "FH database upload")
        return {"added": len(new_rows), "updated": len(attendance_updates), "existing": unchanged, "duplicates": duplicates,
                "ambiguous": ambiguous, "beneficiaries": beneficiaries, "attendance_updates": attendance_updates}
    finally:
        workbook.close()
