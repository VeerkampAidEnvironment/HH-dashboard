"""Follow-up scoring summaries using saved outcomes and the dashboard's scope."""

import json
from collections import Counter

from followup_survey import (
    PACKAGE_TITLES, TRAINING_PACKAGES, TRAINING_TOPICS,
    collect_critical_failure_comments,
)


def _bucket(key, title):
    return {"key": key, "title": title, "count": 0, "passed": 0,
            "critical": 0, "score_total": 0, "reasons": Counter()}


def _add(bucket, score, status, critical, reasons, titles):
    bucket["count"] += 1
    bucket["passed"] += status == "Achieved" and not critical
    bucket["score_total"] += score
    if critical:
        bucket["critical"] += 1
        matched = {
            (item["source_id"], item["comment"])
            for item in reasons if titles.intersection(item["packages"])
        }
        if not matched:
            matched.add(("", "Critical failure recorded; specific reason unavailable."))
        bucket["reasons"].update(matched)


def _finish(bucket):
    total = bucket["count"]
    unsuccessful = total - bucket["passed"]
    return {
        **{key: value for key, value in bucket.items() if key not in {"reasons", "score_total"}},
        "unsuccessful": unsuccessful,
        "pass_rate": round(bucket["passed"] / total * 100, 1) if total else None,
        "average": round(bucket["score_total"] / total, 1) if total else None,
        "critical_rate": round(bucket["critical"] / total * 100, 1) if total else None,
        "reasons": [
            {"source_id": source, "label": label, "count": count,
             "percent": round(count / unsuccessful * 100, 1)}
            for (source, label), count in bucket["reasons"].most_common()
        ],
    }


def build_scoring_overview(connection, record_ids, topics=None, date_from=None, date_to=None):
    """Count beneficiary/topic results and distinct plot visits for packages.

    Shared household responses retain their beneficiary scope. Package results
    count once per visit even when multiple selected household members attended.
    Legacy questionnaires without a saved outcome are excluded.
    """
    selected = list(TRAINING_TOPICS if topics is None else topics)
    package_keys = set(PACKAGE_TITLES) if topics is None else {
        key for topic in selected for key in TRAINING_PACKAGES.get(topic, [])
    }
    trainings = {topic: _bucket(topic, topic) for topic in selected}
    packages = {key: _bucket(key, title) for key, title in PACKAGE_TITLES.items() if key in package_keys}
    visits = {}
    excluded = 0
    ids = sorted(record_ids)
    for offset in range(0, len(ids), 400):
        batch = ids[offset:offset + 400]
        placeholders = ",".join("?" for _ in batch)
        rows = connection.execute(
            f"""SELECT fr.*, fee.event_id, fe.event_date
                FROM followup_responses fr
                JOIN field_event_entries fee ON fee.id=fr.event_entry_id
                JOIN field_events fe ON fe.id=fee.event_id
                WHERE fr.record_id IN ({placeholders}) ORDER BY fr.id""", batch,
        )
        for row in rows:
            if row["topic"] not in trainings:
                continue
            day = row["event_date"]
            if (date_from and day < date_from) or (date_to and day > date_to):
                continue
            if row["result_status"] not in {"Achieved", "Partial", "Failed"}:
                excluded += 1
                continue
            event_id = row["event_id"]
            # Each response stores the complete questionnaire, including
            # responses copied to other members of the same household.
            if event_id not in visits:
                payload = json.loads(row["answers"])
                answers = {
                    key: value.get("answer") if isinstance(value, dict) else value
                    for key, value in payload.items()
                }
                visits[event_id] = collect_critical_failure_comments(answers)
            reasons = visits[event_id]
            titles = {PACKAGE_TITLES[key] for key in TRAINING_PACKAGES.get(row["topic"], [])}
            titles.add(row["topic"])
            _add(trainings[row["topic"]], row["adoption_rate"], row["result_status"],
                 row["critical_failed"], reasons, titles)

    event_ids = sorted(visits)
    for offset in range(0, len(event_ids), 400):
        batch = event_ids[offset:offset + 400]
        placeholders = ",".join("?" for _ in batch)
        for row in connection.execute(
            f"""SELECT p.*, a.event_id FROM followup_package_results p
                JOIN followup_assessments a ON a.id=p.assessment_id
                WHERE a.event_id IN ({placeholders})""", batch,
        ):
            if row["package_key"] in packages:
                bucket = packages[row["package_key"]]
                _add(bucket, row["score"], row["result_status"], row["critical_failed"],
                     visits[row["event_id"]], {bucket["title"]})
    return {"training": [_finish(item) for item in trainings.values()],
            "package": [_finish(item) for item in packages.values()], "excluded": excluded}
