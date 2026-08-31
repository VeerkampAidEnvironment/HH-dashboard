"""Adaptive CBF follow-up survey and authoritative scoring rules.

The survey is deliberately data-driven: the web form and offline field app use
the same question identifiers and the server always recalculates submitted
scores. Client-provided totals are never trusted.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from statistics import mean
from typing import Any, Iterable


SURVEY_VERSION = "2026-08-27"

PIP = "Household Resource Mapping (PIP)"
SUSTAINABLE = "Sustainable/Regenerative Agriculture"
SWC = "SWC"
AGROFORESTRY = "Tree planting/Agroforestry"
BIO_INPUTS = "Bio-inputs training"
KITCHEN = "Kitchen garden Establishment and Vegetable growing"
FINANCIAL = "Financial Literacy"
POULTRY = "Poultry Mgt and Vaccination"

TRAINING_TOPICS = [PIP, SWC, KITCHEN, BIO_INPUTS, POULTRY, FINANCIAL, AGROFORESTRY, SUSTAINABLE]
SECTION_B_TOPICS = {SUSTAINABLE, SWC, AGROFORESTRY, BIO_INPUTS}


def options(*items: tuple[str, str] | str) -> list[dict[str, str]]:
    return [
        {"value": item[0], "label": item[1]} if isinstance(item, tuple)
        else {"value": item, "label": item}
        for item in items
    ]


YES_NO = options(("yes", "Yes"), ("no", "No"))
FOLLOWUP_LABELS = {
    "followup_1": "Follow up after 1 month",
    "followup_3": "Follow up after 3 months",
    "followup_6": "Follow up after 6 months",
    "none": "No follow-up needed",
}


def question(
    source_id: str,
    question_id: str,
    label: str,
    question_type: str = "choice",
    question_options: list[dict[str, str]] | None = None,
    *,
    help_text: str = "",
    condition: dict[str, Any] | None = None,
    required: bool = True,
    profile_key: str | None = None,
    unit: str = "",
    max_selections: int | None = None,
    exclusive_values: Iterable[str] | None = None,
    max_value: float | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "source_id": source_id,
        "id": question_id,
        "label": label,
        "type": question_type,
        "required": required,
    }
    if question_options:
        item["options"] = question_options
    if help_text:
        item["help"] = help_text
    if condition:
        item["condition"] = condition
    if profile_key:
        item["profile_key"] = profile_key
    if unit:
        item["unit"] = unit
    if max_selections is not None:
        item["max_selections"] = max_selections
    if exclusive_values:
        item["exclusive_values"] = list(exclusive_values)
    if max_value is not None:
        item["max_value"] = max_value
    return item


def eq(question_id: str, value: Any) -> dict[str, Any]:
    return {"question": question_id, "operator": "equals", "value": value}


def one_of(question_id: str, values: Iterable[Any]) -> dict[str, Any]:
    return {"question": question_id, "operator": "in", "values": list(values)}


def contains(question_id: str, value: Any) -> dict[str, Any]:
    return {"question": question_id, "operator": "contains", "value": value}


FOOD_GROUPS = options(
    ("dark_leafy", "Dark green leafy vegetables"),
    ("other_vegetables", "Other vegetables"),
    ("beans_pulses", "Beans and pulses"),
    ("roots_tubers", "Roots and tubers"),
    ("cereals", "Cereals and grains"),
    ("bananas", "Bananas or plantain"),
    ("fruit", "Fruit"),
    ("coffee", "Coffee (does not count as a food group)"),
    ("cash_crop", "Other cash crop (does not count as a food group)"),
)


SURVEY_SECTIONS: list[dict[str, Any]] = [
    {
        "id": "A", "title": "Visit, location and household profile", "always": True,
        "intro": "Confirm the pre-filled beneficiary information. Training history is read-only.",
        "questions": [
            question("A4", "a4_district", "District", "text", profile_key="district", required=False),
            question("A5", "a5_subcounty", "Sub-county / Division", "text", profile_key="subcounty", required=False),
            question("A6", "a6_village", "Village / Cell", "text", profile_key="village", required=False),
            question("A8.1", "a8_beneficiary_type", "Type of beneficiary", "text", profile_key="beneficiary_type", required=False),
            question("A8.2", "a8_contact", "Contact of the beneficiary", "text", profile_key="phone", required=False),
            question("A8.3", "a8_gender", "Gender", "text", profile_key="sex", required=False),
            question("A8.4", "a8_pwd", "Person with a disability (PWD)", "choice", YES_NO, profile_key="pwd", required=False),
            question("A8.5", "a8_age_group", "Age group", "text", profile_key="age_group", required=False),
            question("A8.6", "a8_group", "Farmer group membership", "text", profile_key="group", required=False),
            question("A9", "a9_education", "Level of education completed", "choice", options(
                ("none", "Not gone to school"), ("pre_primary", "Pre-primary"), ("primary", "Primary"),
                ("secondary_o", "Secondary O-level"), ("secondary_a", "Secondary A-level"),
                ("certificate", "Certificate"), ("diploma", "Diploma"), ("degree", "Degree"),
                ("other", "Other")), required=False, profile_key="education"),
            question("A10.1", "a10_total", "People living in the household - total", "number", required=False),
            question("A10.2", "a10_male", "People living in the household - male", "number", required=False),
            question("A10.3", "a10_female", "People living in the household - female", "number", required=False),
            question("A11", "a11_trainings", "Trainings received", "training_list", required=False,
                     help_text="Filled from the AE training history and cannot be changed during follow-up."),
        ],
    },
    {
        "id": "B", "title": "Regenerative / sustainable farming practices", "topics": sorted(SECTION_B_TOPICS),
        "intro": "Conduct this as one continuous walking interview. Physical observation takes precedence over self-report.",
        "questions": [
            question("B1", "b1_food_groups", "Walking the plot, which crops or species are planted?", "multi", FOOD_GROUPS),
            question("B2", "b2_cover", "What is mostly covering the soil surface between plants?", "choice", options(
                ("bare", "Bare soil"), ("mulch", "Crop residue or mulch"),
                ("living", "Living ground cover"), ("other", "Other"))),
            question("B2.1", "b2_1_mulch", "What do you observe about the mulch?", "choice", options(
                ("full", "Full cover, at least 3 cm thick"), ("over_80", "More than 80% cover and at least 1 cm thick"),
                ("over_50", "Scattered, above 50% covered"), ("under_50", "Less than 50% covered")), condition=eq("b2_cover", "mulch")),
            question("B2.1", "b2_1_living", "What do you observe about the living cover?", "choice", options(
                ("dense", "Dense deliberate cover crop with little bare ground"),
                ("patchy", "Patchy cover or a mix of planted and volunteer growth"),
                ("sparse", "Sparse or mostly volunteer weeds")), condition=eq("b2_cover", "living")),
            question("B3", "b3_features", "Which erosion features are visible?", "multi", options(
                ("rills", "Rills or small channels"), ("gully", "Gully too wide or deep to step across"),
                ("roots", "Exposed plant or tree roots"), ("loose_soil", "Loose soil deposited at a slope or plot edge"),
                ("compacted", "Bare compacted patches"), ("none", "None")), required=False,
                help_text="Two or more severe features override and fail the Erosion package.", exclusive_values=["none"]),
            question("B3.1", "b3_1_rills", "What is the depth of the rills?", "choice", options(
                ("shallow", "Ankle-height or less"), ("deep", "Deeper than an ankle")), condition=contains("b3_features", "rills")),
            question("B3.2", "b3_2_roots", "How widespread are the exposed roots?", "choice", options(
                ("isolated", "Isolated - 1 or 2 plants"), ("widespread", "Widespread")), condition=contains("b3_features", "roots")),
            question("B3.3", "b3_3_soil", "What is the occurrence of the loose soil?", "choice", options(
                ("scatter", "Thin scatter"), ("ridge", "Built-up ridge")), condition=contains("b3_features", "loose_soil")),
            question("B4", "b4_structures", "Which soil and water conservation structures are visible?", "multi", options(
                ("grass", "Grass strips"), ("trash", "Trash lines"), ("stone", "Stone bunds"),
                ("trenches", "Trenches"), ("terracing", "Bench terracing"), ("none", "None")), exclusive_values=["none"]),
            question("B4.1", "b4_1_width", "Do the structures run across the full width of the plot?", "choice", options(
                ("full", "Full width"), ("partial", "Partial"), ("single", "Single structure only")),
                condition={"question": "b4_structures", "operator": "has_any_except", "value": "none"}),
            question("B4.2", "b4_2_contour", "Do the structures follow the contour of the land?", "choice", options(
                ("yes", "Yes"), ("no", "No"), ("unsure", "Unsure")),
                condition={"question": "b4_structures", "operator": "has_any_except", "value": "none"}),
            question("B5", "b5_preparation", "Looking at the soil surface, how was this plot prepared?", "choice", options(
                ("fully_tilled", "Fully tilled"), ("minimum", "Minimally tilled - 1 or 2 times per year"),
                ("holes", "Planting holes or strips"), ("undisturbed", "Undisturbed between rows"))),
            question("B5.1", "b5_1_old_marks", "Are old furrow or ridge marks visible?", "choice", options(
                ("none", "No old marks"), ("visible", "Old furrows visible - recent transition"),
                ("new_plot", "Newly opened plot; no previous cultivation to compare")), condition=one_of("b5_preparation", ["minimum", "holes", "undisturbed"])),
            question("B5.2", "b5_2_extent", "Is this preparation method used across the whole plot?", "choice", options(
                ("whole", "Whole plot"), ("part", "Part of the plot only"), ("varies", "Varies across sections")), condition=one_of("b5_preparation", ["minimum", "holes", "undisturbed"])),
            question("B6", "b6_weeds", "How are weeds typically managed during the season?", "choice", options(
                ("spot", "Hand-pulling or spot-hoeing"), ("few_full", "Full-bed hoeing a few times per year"),
                ("frequent_full", "Frequent full-bed hoeing"), ("herbicide", "Herbicide"))),
            question("B7", "b7_arrangement", "How are crops arranged on this plot?", "choice", options(
                ("single", "Single crop in uniform rows"), ("mixed", "Mixed crops interplanted"), ("other", "Other"))),
            question("B8", "b8_trees", "Are trees or shrubs within or bordering this plot?", "choice", YES_NO),
            question("B8.1", "b8_1_under", "Is anything planted directly next to or underneath the trees?", "choice", YES_NO, condition=eq("b8_trees", "yes")),
            question("B8.2", "b8_2_arrangement", "How are the trees arranged?", "choice", options(
                ("boundary", "Boundary"), ("compound", "Compound"), ("intercropping", "Intercropping"),
                ("woodlot", "Woodlots"), ("other", "Other")), condition=eq("b8_trees", "yes")),
            question("B9", "b9_traces", "Can a previous crop be identified from residue, stubble or physical traces?", "choice", options(
                ("identified", "Yes - previous crop identified"), ("unidentified", "Traces visible, crop not identifiable"),
                ("none", "No traces of a previous crop"))),
            question("B9.a", "b9_previous_crop", "Name the previous crop", "text", condition=eq("b9_traces", "identified")),
            question("B9.b", "b9_differs", "Is the identified crop different from the current crop?", "choice", YES_NO, condition=eq("b9_traces", "identified")),
            question("B9.1", "b9_1_farmer", "Was a different crop grown here last season?", "choice", options(
                ("yes", "Yes"), ("no", "No"), ("unsure", "Unsure"))),
            question("B10", "b10_pest_method", "Do you see pest damage or a pest-management method in use?", "choice", options(
                ("biological", "Biological or manual method"), ("both", "Both synthetic and biological/manual"),
                ("synthetic", "Synthetic pesticide only"), ("none", "No evidence"))),
            question("B10.1", "b10_1_damage", "Of 10 plants inspected, how many show pest or disease damage?", "number", condition=one_of("b10_pest_method", ["biological", "both"]), unit="/ 10", max_value=10),
            question("B10.2", "b10_2_severe", "How many have damage affecting over half the leaves or growing point?", "number", condition=one_of("b10_pest_method", ["biological", "both"]), unit="/ 10", max_value=10),
            question("B10.3", "b10_3_chemical_change", "Since using bio-pesticide, how has chemical pesticide use changed?", "choice", options(
                ("increased", "Increased"), ("same", "Stayed the same"), ("reduced", "Reduced"),
                ("never_used", "Did not use chemical pesticide before")), condition=one_of("b10_pest_method", ["biological", "both"])),
            question("B11", "b11_inputs", "Which soil-fertility inputs have physical evidence?", "multi", options(
                ("manure", "Manure or compost visible"), ("heap", "Compost heap or manure pile present"),
                ("bio", "Bio-fertilizer preparation materials present"), ("synthetic", "Empty synthetic-fertilizer bags or containers"),
                ("none", "None")), exclusive_values=["none"]),
            question("B11.1", "b11_1_location", "Where is the manure or compost applied?", "choice", options(
                ("whole", "Across the whole plot evenly"), ("holes", "In planting holes or around plants"),
                ("section", "One section only")), condition=contains("b11_inputs", "manure")),
            question("B11.2", "b11_2_method", "How is the fertility input applied?", "choice", options(
                ("surface", "Spread on the surface"), ("mixed", "Dug or mixed into soil"), ("holes", "Placed in planting holes"),
                ("liquid_base", "Liquid poured at plant base"), ("liquid_leaf", "Liquid sprayed on leaves"),
                ("none", "None of these")), condition={"question": "b11_inputs", "operator": "contains_any", "values": ["manure", "bio"]}),
            question("B12", "b12_macrofauna", "In a 30 cm x 30 cm x 30 cm quadrant, how many earthworms or other macrofauna are found?", "choice", options(
                ("zero", "0"), ("one_four", "1-4"), ("five_plus", "5+"))),
            question("B13", "b13_synthetic_fertilizer", "Times synthetic fertilizer was applied last season", "number", required=False, help_text="Comparison value only; not scored."),
            question("B14", "b14_manure_loads", "Wheelbarrow-loads of manure or compost applied last season", "number", required=False, help_text="Comparison value only; not scored."),
            question("B15", "b15_gap", "Main gap observed", "textarea", required=False),
            question("B16", "b16_advice", "Immediate recommendation or advice given", "textarea", required=False),
            question("B18", "b18_photo_reference", "Photo reference for the best or weakest practice", "text", required=False),
        ],
    },
    {
        "id": "C", "title": "Household Resource Mapping / PIP", "topics": [PIP],
        "questions": [
            question("C1", "c1_map_drawn", "Did the household draw a Household Resource Map?", "choice", YES_NO),
            question("C2", "c2_current_map", "Is a map showing the current household situation present?", "choice", YES_NO, condition=eq("c1_map_drawn", "yes")),
            question("C3", "c3_storage", "How is the map or plan kept?", "choice", options(
                ("displayed", "Displayed or kept in a dedicated book"), ("loose", "Exists but is loose or hard to locate"),
                ("not_shown", "Not shown")), condition=eq("c1_map_drawn", "yes")),
            question("C4", "c4_use", "Does the plan show signs of use since it was made?", "choice", options(
                ("clear", "Clear signs of use"), ("some", "Some signs, unclear"), ("none", "No signs of use")), condition=eq("c1_map_drawn", "yes")),
            question("C5", "c5_gap", "Main gap observed", "textarea", required=False),
            question("C6", "c6_advice", "Immediate recommendation or advice", "textarea", required=False),
            question("C8", "c8_photo_reference", "Photo reference for the map", "text", required=False),
        ],
    },
    {
        "id": "D", "title": "Kitchen Garden and Vegetable Growing", "topics": [KITCHEN],
        "questions": [
            question("D1", "d1_food_sources", "For each food group, record whether it is produced, bought, both or not consumed", "textarea", required=False, help_text="Repeated comparison inventory; not scored."),
            question("D2", "d2_garden_cover", "How much of the kitchen garden is covered by growing vegetables?", "choice", options(
                ("mostly_veg", "Vegetables cover most of the garden"), ("equal", "Vegetables and bare ground/weeds are roughly equal"),
                ("mostly_bare", "Mostly bare ground or weeds"))),
            question("D3", "d3_weeds", "Are weeds taller than the vegetable plants?", "choice", options(
                ("no", "No"), ("patches", "Yes, in patches"), ("most", "Yes, across most of the garden"))),
            question("D4", "d4_unhealthy", "Of 10 vegetable plants, how many are wilted, yellowing or dead?", "number", unit="/ 10", max_value=10),
            question("D5", "d5_structures", "Which kitchen-garden structures are visible?", "multi", options("Sack", "Keyhole wall", "Raised bed edge", "Fencing"), required=False),
            question("D6", "d6_seed_storage", "Is seed stored for next season, and can it be seen?", "choice", options(
                ("visible", "Yes, storage container or hanging bundle visible"), ("claimed", "Beneficiary says yes, nothing visible"), ("no", "No"))),
            question("D7", "d7_gap", "Main gap observed", "textarea", required=False),
            question("D8", "d8_advice", "Immediate recommendation or advice", "textarea", required=False),
            question("D10", "d10_photo_reference", "Photo reference for the best or weakest practice", "text", required=False),
        ],
    },
    {
        "id": "E", "title": "Financial Literacy", "topics": [FINANCIAL],
        "questions": [
            question("E1", "e1_budget", "Does the household have a budget, plan or financial record?", "choice", options(
                ("shown", "Yes, shown"), ("not_shown", "Says yes, not shown"), ("no", "No"))),
            question("E2", "e2_decisions", "Who makes financial decisions in the household?", "choice", options(
                ("respondent", "Respondent alone"), ("spouse", "Spouse alone"), ("both", "Both"),
                ("single", "Single-adult household - not applicable"))),
            question("E3", "e3_within_means", "Does the household live within its means?", "choice", YES_NO),
            question("E4", "e4_invested", "Were investments made in the last 12 months?", "choice", YES_NO),
            question("E4.1", "e4_1_investments", "What was invested in since training?", "multi", options("Farming", "Land", "Business", "Other"), condition=eq("e4_invested", "yes")),
            question("E5", "e5_savings", "Average amount saved per month", "number", unit="UGX"),
            question("E6", "e6_saving_place", "Where is part of the income saved?", "choice", options(
                ("bank", "Bank / SACCO"), ("group", "Saving group"), ("mobile", "Mobile money"),
                ("home", "At home"), ("other", "Other"))),
            question("E7", "e7_records", "Are income and expense records kept?", "choice", YES_NO),
            question("E8", "e8_book", "Can a record-keeping book be shown?", "choice", options(
                ("shown", "Yes, shown"), ("not_shown", "Yes, not shown"), ("no", "No"))),
            question("E8.1", "e8_1_frequency", "How often are the records updated?", "choice", options(
                ("never", "Never"), ("daily", "Daily"), ("weekly", "Weekly"), ("monthly", "Monthly"),
                ("annually", "Annually"), ("other", "Other")), condition=one_of("e8_book", ["shown", "not_shown"])),
            question("E8.2", "e8_2_help", "How has record keeping helped?", "multi", options(
                "Track income and expenses", "Improve planning and budgeting", "Make better investment decisions", "Calculate profit or loss", "Other"), required=False, condition=one_of("e8_book", ["shown", "not_shown"])),
            question("E9", "e9_gap", "Main gap observed", "textarea", required=False),
            question("E10", "e10_advice", "Immediate recommendation or advice", "textarea", required=False),
            question("E12", "e12_photo_reference", "Photo reference for the record or practice", "text", required=False),
        ],
    },
    {
        "id": "F", "title": "Poultry Production and Management", "topics": [POULTRY],
        "questions": [
            question("F1", "f1_location", "Where are the birds at this moment?", "choice", options(
                ("house", "In a house or enclosed structure"), ("bounded", "In a fenced or bounded area"),
                ("free", "Roaming freely with no boundary"), ("none", "No birds can be seen"))),
            question("F1.1", "f1_1_structure", "Which features are present in the structure?", "multi", options(
                ("roof", "Roof intact"), ("walls", "Walls or mesh on all sides"), ("door", "Door that closes"),
                ("air", "Openings above bird height"), ("litter", "Litter or bedding")), condition=one_of("f1_location", ["house", "bounded"])),
            question("F2", "f2_visible", "How many birds are visible?", "choice", options(
                ("five_plus", "5 or more"), ("under_five", "Fewer than 5")), condition=one_of("f1_location", ["house", "bounded"])),
            question("F2.1", "f2_1_unhealthy", "How many inspected birds show signs of illness?", "number", condition=one_of("f1_location", ["house", "bounded"])),
            question("F2.1b", "f2_1_total", "How many birds were inspected in total?", "number", condition=eq("f2_visible", "under_five")),
            question("F3", "f3_isolation", "Are sick birds kept apart from the rest?", "choice", options(
                ("yes", "Yes, separate space in use"), ("none_seen", "No sick birds seen"),
                ("mixed", "Sick birds mixed with the flock")), condition=one_of("f1_location", ["house", "bounded"])),
            question("F4", "f4_records", "Can a poultry record book be shown?", "choice", options(
                ("recent", "Shown, with entries in the last month"), ("stale", "Shown, no recent entries"),
                ("none", "Not shown or does not exist")), condition=one_of("f1_location", ["house", "bounded"])),
            question("F5", "f5_vaccination", "Can a vaccination record or last vaccine container be shown?", "choice", options(
                ("recent", "Shown, dated within the last 6 months"), ("stale", "Shown, older or undated"),
                ("none", "Nothing shown")), condition=one_of("f1_location", ["house", "bounded"])),
            question("F6", "f6_gap", "Main gap observed or shared", "textarea", required=False),
            question("F7", "f7_advice", "Immediate advice", "textarea", required=False),
            question("F9", "f9_photo_reference", "Photo reference for poultry-management practices", "text", required=False),
        ],
    },
    {
        "id": "H", "title": "Radio Talk Show Impact", "always": True,
        "questions": [
            question("H1", "h1_radio", "Do you listen to the radio?", "choice", YES_NO),
            question("H1.1", "h1_1_hh_show", "Have you listened to a Harvesting Health radio talk show?", "choice", YES_NO, condition=eq("h1_radio", "yes")),
            question("H1.2", "h1_2_practised", "Have you practised something learned from it?", "choice", YES_NO, condition=eq("h1_1_hh_show", "yes")),
            question("H1.2a", "h1_2_example", "What did you practise?", "textarea", condition=eq("h1_2_practised", "yes"), required=False),
        ],
    },
    {
        "id": "I", "title": "Beneficiary Feedback on Trainings", "always": True,
        "questions": [
            question("I1", "i1_helpful", "Which training topics helped the most? Select up to two.", "multi", options(
                "Bio-inputs", "Regenerative / Sustainable Agriculture", "Tree Planting & Agroforestry", "SWC",
                "Kitchen Garden", "Financial Literacy", "Household Resource Mapping", "Poultry Management & Vaccination"), max_selections=2),
            question("I2", "i2_change", "What is the biggest change noticed since starting these practices?", "choice", options(
                "More harvest / yield", "More food variety at home", "Spending less on inputs", "Less soil erosion / land damage",
                "Better animal / poultry health", "Saving more money", "No noticeable change yet", "Other")),
            question("I3", "i3_improve", "What would make the trainings more useful?", "choice", options(
                "More hands-on demonstrations", "More frequent follow-up visits", "Materials in local language",
                "Cover new or different topics", "Nothing - satisfied as is", "Other")),
            question("Consent", "consent", "Permission received to document and share practices for learning and communication", "consent", options(("yes", "Permission received"))),
        ],
    },
]


def received_training_topics(raw: dict[str, Any]) -> list[str]:
    """Return the pre-filled A11 topics in the platform's stable display order."""
    return [
        topic for topic in TRAINING_TOPICS
        if any(str(raw.get(f"Training {cycle} - {topic}") or "").strip() for cycle in range(1, 4))
    ]


def survey_sections_for_topics(topics: Iterable[str]) -> list[dict[str, Any]]:
    selected = set(topics)
    return deepcopy([
        section for section in SURVEY_SECTIONS
        if section.get("always") or selected.intersection(section.get("topics", []))
    ])


def build_survey(
    topics: Iterable[str],
    profile: dict[str, Any] | None = None,
    *,
    training_history: Iterable[str] | None = None,
) -> dict[str, Any]:
    topics = [topic for topic in TRAINING_TOPICS if topic in set(topics)]
    profile = profile or {}
    history_topics = [topic for topic in TRAINING_TOPICS if topic in set(training_history or topics)]
    sections = survey_sections_for_topics(topics)
    for section in sections:
        for item in section["questions"]:
            if item["type"] == "training_list":
                item["display_values"] = history_topics
            elif item.get("profile_key"):
                item["prefill"] = profile.get(item["profile_key"], "")
    return {"version": SURVEY_VERSION, "topics": topics, "training_history": history_topics, "sections": sections}


def all_questions_for_topics(topics: Iterable[str]) -> list[dict[str, Any]]:
    return [item for section in survey_sections_for_topics(topics) for item in section["questions"]]


def _answer(answers: dict[str, Any], key: str, default: Any = "") -> Any:
    value = answers.get(key, default)
    if isinstance(value, str):
        return value.strip()
    return value


def question_is_active(item: dict[str, Any], answers: dict[str, Any]) -> bool:
    condition = item.get("condition")
    if not condition:
        return True
    value = _answer(answers, condition["question"])
    operator = condition.get("operator", "equals")
    if operator == "equals":
        return value == condition.get("value")
    if operator == "in":
        return value in condition.get("values", [])
    if operator == "contains":
        return isinstance(value, list) and condition.get("value") in value
    if operator == "contains_any":
        return isinstance(value, list) and bool(set(value).intersection(condition.get("values", [])))
    if operator == "has_any_except":
        return isinstance(value, list) and any(item != condition.get("value") for item in value)
    return False


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == []


def validate_answers(answers: dict[str, Any], topics: Iterable[str]) -> str | None:
    """Validate active questions. Hidden conditional answers are ignored."""
    for item in all_questions_for_topics(topics):
        if item["type"] == "training_list" or not question_is_active(item, answers):
            continue
        value = _answer(answers, item["id"])
        if item.get("required") and _is_empty(value):
            return f"Answer {item['source_id']}: {item['label']}."
        if _is_empty(value):
            continue
        if item["type"] == "number":
            try:
                number = float(value)
            except (TypeError, ValueError):
                return f"Enter a valid number for {item['source_id']}."
            if number < 0:
                return f"Enter a non-negative number for {item['source_id']}."
            if item.get("max_value") is not None and number > item["max_value"]:
                return f"{item['source_id']} cannot be greater than {item['max_value']:g}."
        allowed = {option["value"] for option in item.get("options", [])}
        submitted = value if isinstance(value, list) else [value]
        if allowed and any(option not in allowed for option in submitted):
            return f"An answer for {item['source_id']} is invalid."
        if item.get("max_selections") is not None and isinstance(value, list) and len(value) > item["max_selections"]:
            return f"Select no more than {item['max_selections']} answers for {item['source_id']}."
        exclusive = set(item.get("exclusive_values", []))
        if isinstance(value, list) and exclusive.intersection(value) and len(value) > 1:
            return f"{item['source_id']}: 'None' cannot be combined with another answer."
    damage = _answer(answers, "b10_1_damage")
    severe = _answer(answers, "b10_2_severe")
    if not _is_empty(damage) and not _is_empty(severe):
        try:
            if float(severe) > float(damage):
                return "B10.2 cannot be greater than the number of damaged plants in B10.1."
        except (TypeError, ValueError):
            pass  # Active numeric questions already return their more specific error above.
    unhealthy = _answer(answers, "f2_1_unhealthy")
    inspected = _answer(answers, "f2_1_total")
    if not _is_empty(unhealthy) and not _is_empty(inspected):
        try:
            if float(unhealthy) > float(inspected):
                return "F2.1 cannot be greater than the total number of birds inspected."
        except (TypeError, ValueError):
            pass
    return None


@dataclass
class ItemResult:
    points: float | None
    critical: bool = False


PACKAGE_ITEMS = {
    "crop_diversification": ["b1_food_groups", "b2_cover", "b7_arrangement", "b8_trees", "b8_1_under", "b8_2_arrangement", "b9_traces", "b9_1_farmer"],
    "erosion_water": ["b2_cover", "b2_detail", "b4_structures", "b4_1_width", "b4_2_contour", "b5_preparation", "b8_trees", "b8_1_under", "b8_2_arrangement"],
    "soil_cover_fertility": ["b2_cover", "b2_detail", "b5_preparation", "b8_trees", "b8_1_under", "b9_traces", "b9_1_farmer", "b11_inputs", "b11_1_location", "b11_2_method", "b12_macrofauna"],
    "pest_management": ["b7_arrangement", "b9_traces", "b9_1_farmer", "b10_pest_method", "b10_1_damage", "b10_2_severe", "b10_3_chemical_change"],
    "reduced_disturbance": ["b5_preparation", "b5_1_old_marks", "b5_2_extent", "b6_weeds"],
}

PACKAGE_TITLES = {
    "crop_diversification": "Crop diversification",
    "erosion_water": "Erosion & water structures",
    "soil_cover_fertility": "Soil cover & fertility",
    "pest_management": "Pest management",
    "reduced_disturbance": "Reduced disturbance",
}

PACKAGE_RECOMMENDATIONS = {
    "pest_management": [BIO_INPUTS],
    "soil_cover_fertility": [BIO_INPUTS],
    "erosion_water": [SWC],
    "crop_diversification": [SUSTAINABLE, AGROFORESTRY],
    "reduced_disturbance": [SUSTAINABLE],
}

TRAINING_PACKAGES = {
    BIO_INPUTS: ["pest_management", "soil_cover_fertility"],
    SWC: ["erosion_water"],
    AGROFORESTRY: ["crop_diversification"],
    SUSTAINABLE: ["crop_diversification", "reduced_disturbance"],
}


def _number(answers: dict[str, Any], key: str) -> float:
    try:
        return float(_answer(answers, key, 0))
    except (TypeError, ValueError):
        return 0


def _score_b_item(item_id: str, package: str, answers: dict[str, Any]) -> ItemResult:
    value = _answer(answers, item_id)
    if item_id == "b1_food_groups":
        count = len(set(value or []).difference({"coffee", "cash_crop"}))
        return ItemResult(2 if count > 2 else 0, critical=count <= 2)
    if item_id == "b2_cover":
        return ItemResult(2 if value in {"mulch", "living"} else 0)
    if item_id == "b2_detail":
        if _answer(answers, "b2_cover") == "mulch":
            detail = _answer(answers, "b2_1_mulch")
            return ItemResult({"full": 2, "over_80": 2, "over_50": 1, "under_50": 0}.get(detail))
        if _answer(answers, "b2_cover") == "living":
            detail = _answer(answers, "b2_1_living")
            return ItemResult({"dense": 2, "patchy": 1, "sparse": 0}.get(detail))
        return ItemResult(None)
    if item_id == "b4_structures":
        selected = set(value or [])
        present = bool(selected.difference({"none"}))
        return ItemResult(2 if present else 0, critical=not present)
    if item_id == "b4_1_width":
        if not question_is_active({"condition": {"question": "b4_structures", "operator": "has_any_except", "value": "none"}}, answers):
            return ItemResult(None)
        return ItemResult({"full": 2, "partial": 1, "single": 0}.get(value))
    if item_id == "b4_2_contour":
        if not question_is_active({"condition": {"question": "b4_structures", "operator": "has_any_except", "value": "none"}}, answers):
            return ItemResult(None)
        return ItemResult(2 if value == "yes" else 0)
    if item_id == "b5_preparation":
        points = {"fully_tilled": 0, "minimum": 1, "holes": 2, "undisturbed": 2}.get(value)
        return ItemResult(points, critical=package == "reduced_disturbance" and value == "fully_tilled")
    if item_id == "b5_1_old_marks":
        if _answer(answers, "b5_preparation") not in {"minimum", "holes", "undisturbed"}:
            return ItemResult(None)
        return ItemResult({"none": 2, "visible": 1, "new_plot": None}.get(value))
    if item_id == "b5_2_extent":
        if _answer(answers, "b5_preparation") not in {"minimum", "holes", "undisturbed"}:
            return ItemResult(None)
        return ItemResult(2 if value == "whole" else 0)
    if item_id == "b6_weeds":
        return ItemResult({"spot": 2, "few_full": 1, "frequent_full": 0, "herbicide": 0}.get(value))
    if item_id == "b7_arrangement":
        return ItemResult(2 if value == "mixed" else 0)
    if item_id == "b8_trees":
        return ItemResult(2 if value == "yes" else 0)
    if item_id == "b8_1_under":
        if _answer(answers, "b8_trees") != "yes":
            return ItemResult(None)
        return ItemResult(2 if value == "yes" else 1)
    if item_id == "b8_2_arrangement":
        if _answer(answers, "b8_trees") != "yes":
            return ItemResult(None)
        return ItemResult(2 if value in {"boundary", "intercropping", "woodlot"} else 0)
    if item_id == "b9_traces":
        if package == "soil_cover_fertility":
            return ItemResult(2 if value in {"identified", "unidentified"} else 0)
        if value == "identified":
            return ItemResult(2 if _answer(answers, "b9_differs") == "yes" else 1)
        return ItemResult(1 if value == "unidentified" else 0)
    if item_id == "b9_1_farmer":
        return ItemResult(2 if value == "yes" else 0)
    if item_id == "b10_pest_method":
        passed = value in {"biological", "both"}
        return ItemResult(2 if passed else 0, critical=not passed)
    if item_id == "b10_1_damage":
        if _answer(answers, "b10_pest_method") not in {"biological", "both"}:
            return ItemResult(None)
        number = _number(answers, item_id)
        return ItemResult(2 if number <= 2 else 1 if number <= 5 else 0, critical=number >= 6)
    if item_id == "b10_2_severe":
        if _answer(answers, "b10_pest_method") not in {"biological", "both"}:
            return ItemResult(None)
        number = _number(answers, item_id)
        return ItemResult(2 if number <= 1 else 1 if number == 2 else 0, critical=number >= 3)
    if item_id == "b10_3_chemical_change":
        if _answer(answers, "b10_pest_method") not in {"biological", "both"}:
            return ItemResult(None)
        return ItemResult(2 if value in {"same", "reduced"} else 0)
    if item_id == "b11_inputs":
        selected = set(value or [])
        return ItemResult(2 if selected.intersection({"manure", "heap", "bio"}) else 0)
    if item_id == "b11_1_location":
        if "manure" not in set(_answer(answers, "b11_inputs", []) or []):
            return ItemResult(None)
        return ItemResult(2 if value in {"whole", "holes"} else 1)
    if item_id == "b11_2_method":
        if not set(_answer(answers, "b11_inputs", []) or []).intersection({"manure", "bio"}):
            return ItemResult(None)
        return ItemResult(0 if value == "none" else 2)
    if item_id == "b12_macrofauna":
        return ItemResult({"zero": 0, "one_four": 1, "five_plus": 2}.get(value))
    raise KeyError(item_id)


def _result(points: float, available: float, critical: bool) -> dict[str, Any]:
    score = round((points / available * 100) if available else 0, 1)
    status = "Failed" if critical else "Achieved" if score >= 50 else "Partial"
    return {
        "points_earned": points,
        "points_available": available,
        "score": score,
        "status": status,
        "critical_failed": critical,
    }


def score_packages(answers: dict[str, Any]) -> dict[str, dict[str, Any]]:
    severe_count = sum([
        _answer(answers, "b3_1_rills") == "deep",
        _answer(answers, "b3_2_roots") == "widespread",
        _answer(answers, "b3_3_soil") == "ridge",
    ])
    packages: dict[str, dict[str, Any]] = {}
    for package, item_ids in PACKAGE_ITEMS.items():
        points = 0.0
        available = 0.0
        critical = package == "erosion_water" and severe_count >= 2
        for item_id in item_ids:
            item = _score_b_item(item_id, package, answers)
            if item.points is not None:
                points += item.points
                available += 2
            critical = critical or item.critical
        packages[package] = {
            "key": package,
            "title": PACKAGE_TITLES[package],
            **_result(points, available, critical),
            "recommendations": PACKAGE_RECOMMENDATIONS[package],
        }
    return packages


SECTION_ITEMS = {
    PIP: [
        ("c1_map_drawn", {"yes": 2, "no": 0}, {"no"}),
        ("c2_current_map", {"yes": 2, "no": 0}, set()),
        ("c3_storage", {"displayed": 2, "loose": 1, "not_shown": 0}, set()),
        ("c4_use", {"clear": 2, "some": 1, "none": 0}, set()),
    ],
    KITCHEN: [
        ("d2_garden_cover", {"mostly_veg": 2, "equal": 1, "mostly_bare": 0}, set()),
        ("d3_weeds", {"no": 2, "patches": 1, "most": 0}, set()),
        ("d4_unhealthy", "d4_count", set()),
        ("d6_seed_storage", {"visible": 2, "claimed": 1, "no": 0}, set()),
    ],
    FINANCIAL: [
        ("e1_budget", {"shown": 2, "not_shown": 1, "no": 0}, set()),
        ("e2_decisions", {"respondent": 0, "spouse": 0, "both": 2, "single": None}, set()),
        ("e3_within_means", {"yes": 2, "no": 0}, {"no"}),
        ("e4_invested", {"yes": 2, "no": 0}, set()),
        ("e4_1_investments", "multi_any", set()),
        ("e5_savings", "positive", set()),
        ("e6_saving_place", {"bank": 2, "group": 2, "mobile": 2, "home": 1, "other": 1}, set()),
        ("e7_records", {"yes": 2, "no": 0}, set()),
        ("e8_book", {"shown": 2, "not_shown": 1, "no": 0}, set()),
        ("e8_1_frequency", {"daily": 2, "weekly": 2, "monthly": 2, "annually": 1, "other": 1, "never": 0}, set()),
    ],
    POULTRY: [
        ("f1_location", {"house": 2, "bounded": 2, "free": 0, "none": None}, {"free"}),
        ("f1_1_structure", "poultry_structure", set()),
        ("f2_1_unhealthy", "poultry_health", set()),
        ("f3_isolation", {"yes": 2, "none_seen": 1, "mixed": 0}, {"mixed"}),
        ("f4_records", {"recent": 2, "stale": 1, "none": 0}, set()),
        ("f5_vaccination", {"recent": 2, "stale": 1, "none": 0}, set()),
    ],
}


def _active_question_lookup(topics: Iterable[str], answers: dict[str, Any]) -> set[str]:
    return {item["id"] for item in all_questions_for_topics(topics) if question_is_active(item, answers)}


def score_single_training(topic: str, answers: dict[str, Any]) -> dict[str, Any]:
    active = _active_question_lookup([topic], answers)
    points = 0.0
    available = 0.0
    critical = False
    for item_id, scoring, critical_values in SECTION_ITEMS[topic]:
        if item_id not in active:
            continue
        value = _answer(answers, item_id)
        if scoring == "d4_count":
            number = _number(answers, item_id)
            item_points = 2 if number <= 3 else 1 if number <= 6 else 0
        elif scoring == "multi_any":
            item_points = 2 if value else 0
        elif scoring == "positive":
            item_points = 2 if _number(answers, item_id) > 0 else 0
        elif scoring == "poultry_structure":
            count = len(value or [])
            item_points = 2 if count > 2 else 1 if count == 1 else 0
        elif scoring == "poultry_health":
            unhealthy = _number(answers, item_id)
            if _answer(answers, "f2_visible") == "five_plus":
                item_points = 2 if unhealthy <= 1 else 1 if unhealthy <= 3 else 0
                critical = critical or unhealthy >= 4
            else:
                inspected = max(1, _number(answers, "f2_1_total"))
                item_points = 0 if unhealthy > inspected / 2 else 2
        else:
            item_points = scoring.get(value)
        if item_points is None:
            continue
        points += item_points
        available += 2
        critical = critical or (not isinstance(value, list) and value in critical_values)
    return _result(points, available, critical)


def rvo_result(answers: dict[str, Any]) -> dict[str, Any]:
    practices = {
        "Mulching / soil cover": _answer(answers, "b2_cover") in {"mulch", "living"},
        "Soil & water conservation structures": bool(set(_answer(answers, "b4_structures", []) or []).difference({"none"})),
        "Conservation / minimum tillage": _answer(answers, "b5_preparation") in {"minimum", "holes", "undisturbed"},
        "Intercropping": _answer(answers, "b7_arrangement") == "mixed",
        "Agroforestry": _answer(answers, "b8_trees") == "yes",
        "Crop rotation": _answer(answers, "b9_traces") == "identified" and _answer(answers, "b9_differs") == "yes",
        "Organic manure / bio-fertilizer": bool(set(_answer(answers, "b11_inputs", []) or []).intersection({"manure", "heap", "bio"})),
        "Integrated pest management": _answer(answers, "b10_pest_method") in {"biological", "both"},
    }
    count = sum(practices.values())
    return {"passed": count >= 2, "count": count, "total": 8, "practices": practices}


def _section_b_followup(packages: dict[str, dict[str, Any]], rvo: dict[str, Any]) -> str:
    if not rvo["passed"] or any(item["critical_failed"] for item in packages.values()):
        return "followup_1"
    achieved = sum(item["status"] == "Achieved" for item in packages.values())
    if achieved < 2 or any(item["status"] == "Partial" for item in packages.values()):
        return "followup_3"
    if any(item["score"] < 70 for item in packages.values()):
        return "followup_6"
    return "none"


def _ordinary_followup(result: dict[str, Any]) -> str:
    if result["critical_failed"]:
        return "followup_1"
    if result["status"] == "Partial":
        return "followup_3"
    return "followup_6" if result["score"] < 70 else "none"


def score_survey(answers: dict[str, Any], topics: Iterable[str]) -> dict[str, Any]:
    topics = [topic for topic in TRAINING_TOPICS if topic in set(topics)]
    has_section_b = bool(set(topics).intersection(SECTION_B_TOPICS))
    packages = score_packages(answers) if has_section_b else {}
    rvo = rvo_result(answers) if has_section_b else None
    breadth = None
    depth = None
    household = None
    project_passed = None
    section_b_next = None
    if has_section_b:
        achieved = sum(item["status"] == "Achieved" for item in packages.values())
        survivors = [item["score"] for item in packages.values() if not item["critical_failed"]]
        breadth = {"achieved": achieved, "total": len(packages)}
        depth = round(mean(survivors), 1) if survivors else 0.0
        project_passed = achieved >= 2
        any_weak = any(item["status"] != "Achieved" for item in packages.values())
        if not rvo["passed"]:
            household = {"code": "D", "label": "Below both bars", "action": "Priority household. Full training package. Shortest follow-up interval."}
        elif not project_passed:
            household = {"code": "C", "label": "RVO-Compliant", "action": "Practices present but shallow. Centralized training for all weak packages. Short-interval follow-up."}
        elif any_weak:
            household = {"code": "B", "label": "Compliant, gaps remain", "action": "Centralized training for each weak package. Follow-up to verify uptake."}
        else:
            household = {"code": "A", "label": "Strong adopter", "action": "No training needed."}
        section_b_next = _section_b_followup(packages, rvo)

    trainings: dict[str, dict[str, Any]] = {}
    for topic in topics:
        if topic in TRAINING_PACKAGES:
            components = [packages[key] for key in TRAINING_PACKAGES[topic]]
            result = _result(
                sum(item["points_earned"] for item in components),
                sum(item["points_available"] for item in components),
                any(item["critical_failed"] for item in components),
            )
            weak_packages = [item for item in components if item["status"] in {"Partial", "Failed"}]
            recommendations = sorted({training for item in weak_packages for training in item["recommendations"]})
            result["recommendation"] = ", ".join(recommendations) if recommendations else "No centralized training needed"
            result["next_action"] = section_b_next
        elif topic in SECTION_ITEMS:
            result = score_single_training(topic, answers)
            result["recommendation"] = f"Centralized training: {topic}" if result["status"] in {"Partial", "Failed"} else "No centralized training needed"
            result["next_action"] = _ordinary_followup(result)
        else:
            continue
        result["next_action_label"] = FOLLOWUP_LABELS[result["next_action"]]
        trainings[topic] = result

    package_training_recommendations = sorted({
        training
        for item in packages.values() if item["status"] in {"Partial", "Failed"}
        for training in item["recommendations"]
    })
    return {
        "version": SURVEY_VERSION,
        "rvo": rvo,
        "project_passed": project_passed,
        "household_outcome": household,
        "breadth": breadth,
        "depth": depth,
        "packages": packages,
        "trainings": trainings,
        "centralized_training_recommendations": package_training_recommendations,
    }
