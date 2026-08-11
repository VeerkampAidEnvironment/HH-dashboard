"""Training and participation rules shared by imports, CRUD, and dashboards."""

from __future__ import annotations

import calendar
import os
from datetime import date, datetime
from typing import Any


TRAINING_TOPICS = [
    "Household Resource Mapping (PIP)",
    "SWC",
    "Kitchen garden Establishment and Vegetable growing",
    "Bio-inputs training",
    "Poultry Mgt and Vaccination",
    "Financial Literacy",
    "Tree planting/Agroforestry",
    "Sustainable/Regenerative Agriculture",
]

CARE_MODULE_SESSIONS = {
    "Care groups - Module 1": 4,
    "Care groups - Module 2": 11,
    "Care groups - Module 3": 4,
    "Care groups - Module 4": 3,
    "Care groups - Module 5": 7,
    "School clubs - Nutrition comic book topics": 5,
    "School clubs - Agronomic practices": 6,
    "School clubs - Soil and water conservation": 7,
    "School clubs - Theme 4": 6,
    "School clubs - Weeding, pest and disease control": 5,
    "School clubs - Theme 7": 3,
}


def care_module_fields() -> dict[str, list[str]]:
    return {
        module: [f"{module} - Session {session}" for session in range(1, count + 1)]
        for module, count in CARE_MODULE_SESSIONS.items()
    }

STATUS_LABELS = {
    "CT": "Centralized training needed",
    "RT": "Refresher training needed",
    "WAIT": "Follow-up not yet due",
    "FU": "Follow-up needed",
    "COMPLETED": "Completed",
    "REVIEW": "Review needed",
    "NOT_STARTED": "Not started",
    "IN_PROGRESS": "In progress",
}


def followup_months() -> int:
    return max(1, int(os.getenv("ARFSA_FOLLOWUP_MONTHS", "3")))


def adoption_threshold() -> float:
    return float(os.getenv("ARFSA_ADOPTION_THRESHOLD", "40"))


def parse_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None


def add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def training_topic_status(raw: dict[str, Any], topic: str, as_of: date | None = None) -> dict[str, Any]:
    """Apply the workbook lifecycle with the user-selected follow-up interval.

    Unlike the workbook's final-cycle formula, a low third score remains a
    refresher-training need instead of being incorrectly shown as complete.
    """
    as_of = as_of or date.today()
    threshold = adoption_threshold()
    months = followup_months()
    cycles = []
    activity_dates: list[date] = []
    for cycle in range(1, 4):
        training_value = raw.get(f"Training {cycle} - {topic}")
        followup_value = raw.get(f"Follow up {cycle} - {topic}")
        training_present = training_value is not None and str(training_value).strip() != ""
        followup_present = followup_value is not None and str(followup_value).strip() != ""
        training = parse_date(training_value)
        followup = parse_date(followup_value)
        score = parse_number(raw.get(f"Follow up {cycle} score - {topic}"))
        next_action = str(raw.get(f"Follow up {cycle} next action - {topic}", "")).strip()
        if training:
            activity_dates.append(training)
        if followup:
            activity_dates.append(followup)
        cycles.append((training, followup, score, training_present, followup_present, next_action))

    received = any(training_present for _, _, _, training_present, _, _ in cycles)
    confirmed = any(score is not None and score >= threshold for _, _, score, _, _, _ in cycles)
    last_activity = max(activity_dates).isoformat() if activity_dates else None
    next_followup_date = None

    if not received:
        code = "CT"
    else:
        code = "REVIEW"
        for index, (training, followup, score, training_present, followup_present, next_action) in enumerate(cycles):
            if not training_present:
                code = "CT" if index == 0 else "RT"
                break
            if not followup_present:
                if not training:
                    code = "REVIEW"
                else:
                    eligible_on = add_months(training, months)
                    next_followup_date = eligible_on.isoformat()
                    code = "WAIT" if as_of < eligible_on else "FU"
                break
            if score is None:
                code = "REVIEW"
                break
            if next_action == "ct":
                if index == len(cycles) - 1 or not cycles[index + 1][3]:
                    code = "RT"
                    break
                continue
            if next_action in {"followup_1", "followup_3", "followup_6"}:
                months_until_followup = int(next_action.rsplit("_", 1)[1])
                if not followup:
                    code = "REVIEW"
                else:
                    eligible_on = add_months(followup, months_until_followup)
                    next_followup_date = eligible_on.isoformat()
                    code = "WAIT" if as_of < eligible_on else "FU"
                break
            if next_action == "none":
                code = "COMPLETED"
                break
            if score >= threshold:
                code = "COMPLETED"
                break
            if index == len(cycles) - 1 or not cycles[index + 1][3]:
                code = "RT"
                break

    return {
        "status_code": code,
        "status_label": STATUS_LABELS[code],
        "training_received": int(received),
        "confirmed_trained": int(confirmed),
        "followup_needed": int(code == "FU"),
        "retraining_needed": int(code in {"CT", "RT"}),
        "last_activity_date": last_activity,
        "next_followup_date": next_followup_date,
    }


def care_module_status(raw: dict[str, Any], field_keys: list[str]) -> dict[str, Any]:
    values = [str(raw.get(key, "")).strip().upper() for key in field_keys]
    attended = sum(value in {"A", "ATTENDED", "YES", "Y", "1"} for value in values)
    marked = sum(bool(value) for value in values)
    expected = len(field_keys)
    if attended == 0:
        code = "NOT_STARTED"
    elif attended == expected:
        code = "COMPLETED"
    else:
        code = "IN_PROGRESS"
    return {
        "status_code": code,
        "status_label": STATUS_LABELS[code],
        "training_received": int(attended > 0),
        "confirmed_trained": int(attended == expected and expected > 0),
        "followup_needed": 0,
        "retraining_needed": 0,
        "last_activity_date": None,
        "attended": attended,
        "marked": marked,
        "expected": expected,
    }
