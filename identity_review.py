"""Plan and save a complete same-name identity split without partial merges."""

import hashlib
import json
from itertools import combinations

from db import log_audit, utc_now
from training import TRAINING_TOPICS


class IdentityReviewError(ValueError):
    pass


def source_token(record_id, key):
    return json.dumps([record_id, key], separators=(",", ":"), ensure_ascii=False)


def revision_for(connection, records):
    ids = sorted(record["id"] for record in records)
    placeholders = ",".join("?" for _ in ids)
    reviews = connection.execute(
        f"SELECT * FROM duplicate_reviews WHERE record_a_id IN ({placeholders}) "
        f"OR record_b_id IN ({placeholders}) ORDER BY id", ids + ids,
    ).fetchall()
    snapshot = {"records": [dict(row) for row in sorted(records, key=lambda row: row["id"])],
                "reviews": [dict(row) for row in reviews]}
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()


def comparison_fields(connection, records):
    from app import get_schema, populated_value

    schema = get_schema(connection, records[0]["dataset"])
    metadata = {field["key"]: field for field in schema}
    raws = {row["id"]: json.loads(row["raw_data"]) for row in records}
    keys = set().union(*(raw.keys() for raw in raws.values()))
    keys.add("__cbf_name__")
    priority = ["Name", "Sex", "Age", "Age group", "Phone Number", "Village", "Parish",
                "Sub County", "DISTRICT", "GROUP NAME", "__cbf_name__"]
    fields = []
    for key in sorted(keys, key=lambda key: (priority.index(key) if key in priority else 100, key.casefold())):
        values = {str(row["id"]): (row["cbf_name"] if key == "__cbf_name__" else raws[row["id"]].get(key))
                  for row in records}
        if not any(populated_value(value) for value in values.values()):
            continue
        history = records[0]["dataset"] == "training" and any(
            key == f"{prefix} {cycle} - {topic}"
            for topic in TRAINING_TOPICS for cycle in range(1, 4)
            for prefix in ("Training", "Follow up", "Follow up score", "Follow up next action")
        )
        fields.append({
            "key": key, "label": "Current CBF assignment" if key == "__cbf_name__" else metadata.get(key, {}).get("label", key),
            "history": history, "values": values,
            "normalized": {record_id: str(value).strip().casefold() if populated_value(value) else ""
                           for record_id, value in values.items()},
        })
    return fields


def review_payload(connection, records):
    from app import duplicate_similarity
    from flask import url_for

    pairs = []
    for left, right in combinations(records, 2):
        score, reasons, warnings = duplicate_similarity(left, right)
        pairs.append({"record_ids": [left["id"], right["id"]], "score": score, "reasons": reasons, "warnings": warnings})
    pairs.sort(key=lambda pair: (-pair["score"], -len(pair["reasons"]), pair["record_ids"]))
    return {"revision": revision_for(connection, records), "fields": comparison_fields(connection, records), "pairs": pairs,
            "records": [{**{key: row[key] for key in ("id", "uid", "source_row", "group_name", "cbf_name", "village", "phone")},
                         "url": url_for("record_detail", record_id=row["id"])}
                        for row in records]}


def build_plan(connection, records, group):
    from app import comparable_activity_value, populated_value

    by_id = {row["id"]: row for row in records}
    survivor_id = group.get("survivor_id")
    if type(survivor_id) is not int or survivor_id not in by_id:
        raise IdentityReviewError("Choose a retained record from each person group.")
    choices = group.get("choices", {})
    if not isinstance(choices, dict):
        raise IdentityReviewError("The conflict choices are invalid.")
    fields = comparison_fields(connection, records)
    raws = {row["id"]: json.loads(row["raw_data"]) for row in records}
    merged = dict(raws[survivor_id])
    conflicts = []
    errors = []

    def resolve(key, label, options, history=False):
        distinct = {option["normalized"] for option in options}
        selected = next((option for option in options if option["token"] == choices.get(key)), None)
        if len(distinct) > 1:
            conflicts.append({"key": key, "label": label, "options": options,
                              "selected": selected["token"] if selected else None, "history": history})
        if selected is None:
            selected = next((option for option in options if option["record_id"] == survivor_id), options[0])
        return selected["value"]

    def option(row, key, value, history=False):
        return {"record_id": row["id"], "uid": row["uid"], "value": value,
                "token": source_token(row["id"], key),
                "normalized": comparable_activity_value(value) if history else str(value).strip().casefold()}

    cbf = ""
    for field in fields:
        if field["history"]:
            continue
        options = [option(row, field["key"], field["values"][str(row["id"])]) for row in records
                   if populated_value(field["values"][str(row["id"])])]
        value = resolve(field["key"], field["label"], options)
        if field["key"] == "__cbf_name__":
            cbf = value
        else:
            merged[field["key"]] = value

    if records[0]["dataset"] == "training" and len(records) > 1:
        prefixes = ("Training", "Follow up", "Follow up score", "Follow up next action")
        for topic in TRAINING_TOPICS:
            cycles = []
            for row in sorted(records, key=lambda row: row["id"]):
                for cycle in range(1, 4):
                    values = {prefix: raws[row["id"]].get(f"{prefix} {cycle} - {topic}") for prefix in prefixes}
                    if any(populated_value(value) for value in values.values()):
                        cycles.append({"row": row, "cycle": cycle, "values": values})
            # Connected components also handle a record bridging two copies of one event.
            clusters = []
            while cycles:
                cluster = [cycles.pop(0)]
                while True:
                    linked = [candidate for candidate in cycles if any(
                        populated_value(candidate["values"][prefix]) and populated_value(existing["values"][prefix])
                        and comparable_activity_value(candidate["values"][prefix]) == comparable_activity_value(existing["values"][prefix])
                        for existing in cluster for prefix in ("Training", "Follow up")
                    )]
                    if not linked:
                        break
                    cluster.extend(linked)
                    cycles = [candidate for candidate in cycles if candidate not in linked]
                clusters.append(cluster)
            if len(clusters) > 3:
                errors.append(f"{topic}: {len(clusters)} distinct training cycles exceed the three-cycle limit. Keep these records separate or review their history first.")
            for cycle in range(1, 4):
                for prefix in prefixes:
                    merged.pop(f"{prefix} {cycle} - {topic}", None)
            for index, cluster in enumerate(clusters, 1):
                for prefix in prefixes:
                    options = [option(item["row"], f"{prefix} {item['cycle']} - {topic}", item["values"][prefix], True)
                               for item in cluster if populated_value(item["values"][prefix])]
                    if options:
                        key = f"__history__:{topic}:{index}:{prefix}"
                        merged[f"{prefix} {index} - {topic}"] = resolve(
                            key, f"{topic} · Event {index} · {prefix}", options, history=True,
                        )
    return {"record_ids": sorted(by_id), "survivor_id": survivor_id, "raw": merged, "cbf": cbf,
            "conflicts": conflicts, "errors": errors,
            "unresolved": sum(conflict["selected"] is None for conflict in conflicts)}


def prepare_split(connection, payload, complete=False):
    from app import normalized_person_name

    if not isinstance(payload, dict):
        raise IdentityReviewError("The identity split is invalid.")
    record_ids = payload.get("record_ids")
    if (not isinstance(record_ids, list) or len(record_ids) < 2
            or any(type(value) is not int for value in record_ids) or len(set(record_ids)) != len(record_ids)):
        raise IdentityReviewError("Select one complete same-name set to review.")
    placeholders = ",".join("?" for _ in record_ids)
    rows = connection.execute(
        f"SELECT r.*, f.uid FROM records r JOIN farmers f ON f.id=r.farmer_id WHERE r.id IN ({placeholders}) ORDER BY r.id",
        record_ids,
    ).fetchall()
    if len(rows) != len(record_ids) or any(row["archived_at"] for row in rows):
        raise IdentityReviewError("Some records were archived or removed. Reload this name before saving.")
    names = {normalized_person_name(row["name"]) for row in rows}
    if len(names) != 1 or not next(iter(names)) or len({row["dataset"] for row in rows}) != 1:
        raise IdentityReviewError("These records do not share one name and dataset.")
    current_ids = {row["id"] for row in connection.execute(
        "SELECT id, name FROM records WHERE dataset=? AND archived_at IS NULL", (rows[0]["dataset"],)
    ) if normalized_person_name(row["name"]) in names}
    if current_ids != set(record_ids) or payload.get("revision") != revision_for(connection, rows):
        raise IdentityReviewError("This name has changed since you opened it. Reload the page and review the latest records.")
    groups = payload.get("groups")
    if not isinstance(groups, list) or (complete and not groups):
        raise IdentityReviewError("Assign every record to a person group before saving.")
    by_id = {row["id"]: row for row in rows}
    used = set()
    plans = []
    farmer_group = {}
    for index, group in enumerate(groups):
        ids = group.get("record_ids") if isinstance(group, dict) else None
        if (not isinstance(ids, list) or not ids or any(type(value) is not int for value in ids)
                or len(ids) != len(set(ids)) or not set(ids) <= by_id.keys() or used.intersection(ids)):
            raise IdentityReviewError("Each record must belong to exactly one person group.")
        used.update(ids)
        for record_id in ids:
            farmer_id = by_id[record_id]["farmer_id"]
            if farmer_id in farmer_group and farmer_group[farmer_id] != index:
                raise IdentityReviewError("Some records already share a confirmed beneficiary ID. Revert the earlier identity decision before separating them.")
            farmer_group[farmer_id] = index
        plans.append(build_plan(connection, [by_id[record_id] for record_id in sorted(ids)], group))
    if complete and used != set(record_ids):
        raise IdentityReviewError("Assign every record, including unique people, before saving.")
    if complete and any(plan["errors"] or plan["unresolved"] for plan in plans):
        raise IdentityReviewError("Resolve the highlighted conflicts and training-history issues in every group before saving.")
    return rows, plans


def save_split(connection, payload, username):
    from app import canonical_fields, rebuild_statuses, reassign_duplicate_event_history

    # Lock before checking the revision; a second reviewer cannot save a stale split.
    connection.execute("BEGIN IMMEDIATE")
    try:
        rows, plans = prepare_split(connection, payload, complete=True)
        by_id = {row["id"]: row for row in rows}
        timestamp = utc_now()
        membership = {}
        for index, plan in enumerate(plans):
            survivor_id = plan["survivor_id"]
            survivor = by_id[survivor_id]
            for record_id in plan["record_ids"]:
                membership[record_id] = (index, survivor_id)
            if len(plan["record_ids"]) == 1:
                continue
            core = canonical_fields(connection, survivor["dataset"], plan["raw"], plan["cbf"])
            columns = ("name", "sex", "age_value", "age_group", "phone", "village", "parish", "subcounty",
                       "district", "group_name", "cbf_name", "record_status")
            connection.execute(
                f"UPDATE records SET {', '.join(column + '=?' for column in columns)}, raw_data=?, updated_at=? WHERE id=?",
                [core[column] for column in columns] + [json.dumps(plan["raw"], ensure_ascii=False), timestamp, survivor_id],
            )
            rebuild_statuses(connection, survivor_id, survivor["dataset"], plan["raw"])
            for donor_id in plan["record_ids"]:
                if donor_id == survivor_id:
                    continue
                donor = by_id[donor_id]
                connection.execute("UPDATE records SET farmer_id=?, archived_at=?, updated_at=? WHERE id=?",
                                   (survivor["farmer_id"], timestamp, timestamp, donor_id))
                connection.execute("UPDATE records SET farmer_id=? WHERE farmer_id=? AND id<>?",
                                   (survivor["farmer_id"], donor["farmer_id"], donor_id))
                reassign_duplicate_event_history(connection, donor_id, survivor_id)
                connection.execute("DELETE FROM farmers WHERE id=? AND NOT EXISTS(SELECT 1 FROM records WHERE farmer_id=?)",
                                   (donor["farmer_id"], donor["farmer_id"]))
        for left_id, right_id in combinations(sorted(by_id), 2):
            same = membership[left_id][0] == membership[right_id][0]
            connection.execute(
                """INSERT INTO duplicate_reviews(record_a_id, record_b_id, survivor_record_id, status, reviewed_at)
                   VALUES(?, ?, ?, ?, ?) ON CONFLICT(record_a_id, record_b_id) DO UPDATE SET
                   survivor_record_id=excluded.survivor_record_id, status=excluded.status, reviewed_at=excluded.reviewed_at""",
                (left_id, right_id, membership[left_id][1] if same else None, "merged" if same else "rejected", timestamp),
            )
        audit_id = log_audit(connection, username, "split_duplicate_identities", "duplicate", None,
                            f"Split {len(rows)} records for {rows[0]['name']} into {len(plans)} people",
                            {"groups": [{"record_ids": plan["record_ids"], "survivor_id": plan["survivor_id"]}
                                        for plan in plans]})
        connection.commit()
        return {"people": len(plans), "archived": len(rows) - len(plans), "audit_id": audit_id}
    except Exception:
        connection.rollback()
        raise
