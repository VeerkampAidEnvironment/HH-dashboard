"""Adaptive CBF follow-up survey and authoritative scoring rules.

The survey is deliberately data-driven: the web form and offline field app use
the same question identifiers and the server always recalculates submitted
scores. Client-provided totals are never trusted.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from math import isfinite
from statistics import mean
from typing import Any, Iterable


SURVEY_VERSION = "2026-09-22-r24"

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
CARRY_FORWARD_QUESTION_IDS = (
    "b13_synthetic_fertilizer", "b13_unit", "b13_unit_other",
    "e5_savings",
)


def options(*items: tuple[str, str] | str) -> list[dict[str, str]]:
    return [
        {"value": item[0], "label": item[1]} if isinstance(item, tuple)
        else {"value": item, "label": item}
        for item in items
    ]


YES_NO = options(("yes", "Yes"), ("no", "No"))
FOLLOWUP_LABELS = {
    "ct": "New centralized training needed",
    "followup_1": "Follow up after 1 month",
    "followup_3": "Follow up after 3 months",
    "followup_6": "Follow up after 6 months",
    "none": "No follow-up needed",
}

FOOD_SOURCE_OPTIONS = options(
    ("own_production", "Own production"),
    ("bought", "Bought"),
    ("both", "Both"),
    ("not_consumed", "Not consumed"),
)

YEAR_OF_BIRTH_OPTIONS = options(*(str(year) for year in range(date.today().year, 1899, -1)))
TRAINING_RANKING_OPTIONS = options(
    (BIO_INPUTS, "Bio-inputs"),
    (SUSTAINABLE, "Regenerative / Sustainable Agriculture"),
    (AGROFORESTRY, "Tree Planting & Agroforestry"),
    (SWC, "SWC"),
    (KITCHEN, "Kitchen Garden"),
    (FINANCIAL, "Financial Literacy"),
    (PIP, "Household Resource Mapping"),
    (POULTRY, "Poultry Mgt & Vaccination"),
)


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
    carry_forward: bool = False,
    unit: str = "",
    max_selections: int | None = None,
    exclusive_values: Iterable[str] | None = None,
    min_value: float | None = None,
    max_value: float | None = None,
    integer: bool = False,
    step_value: float | None = None,
    editable: bool = False,
    skip_condition: dict[str, Any] | None = None,
    guide_parent: str | None = None,
    locked_to: str | None = None,
    training_topic: str | None = None,
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
    if carry_forward:
        item["carry_forward"] = True
    if unit:
        item["unit"] = unit
    if max_selections is not None:
        item["max_selections"] = max_selections
    if exclusive_values:
        item["exclusive_values"] = list(exclusive_values)
    if min_value is not None:
        item["min_value"] = min_value
    if max_value is not None:
        item["max_value"] = max_value
    if integer:
        item["integer"] = True
    if step_value is not None:
        item["step_value"] = step_value
    if editable:
        item["editable"] = True
    if skip_condition:
        item["skip_condition"] = skip_condition
    if guide_parent:
        item["guide_parent"] = guide_parent
    if locked_to:
        item["locked_to"] = locked_to
    if training_topic:
        item["training_topic"] = training_topic
    return item


def eq(question_id: str, value: Any) -> dict[str, Any]:
    return {"question": question_id, "operator": "equals", "value": value}


def one_of(question_id: str, values: Iterable[Any]) -> dict[str, Any]:
    return {"question": question_id, "operator": "in", "values": list(values)}


def contains(question_id: str, value: Any) -> dict[str, Any]:
    return {"question": question_id, "operator": "contains", "value": value}


def age_from_birth_year(value: Any, as_of: date | None = None) -> int | None:
    """Calculate an approximate age when only the birth year is collected."""
    try:
        text = str(value).strip()
        year = int(text[:4])
    except (TypeError, ValueError):
        return None
    reference = as_of or date.today()
    age = reference.year - year
    if len(text) != 4 or age < 0 or age > 130:
        return None
    return age


def age_group_from_birth_year(value: Any, as_of: date | None = None) -> str:
    """Calculate the dashboard age band from a four-digit birth year."""
    age = age_from_birth_year(value, as_of)
    if age is None:
        return ""
    if age <= 35:
        return "Youth"
    if age <= 59:
        return "Adult"
    return "Elder"


def age_group_from_birth_date(value: Any, as_of: date | None = None) -> str:
    """Backward-compatible helper for historical ISO dates of birth."""
    try:
        born = date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return ""
    reference = as_of or date.today()
    if born > reference:
        return ""
    age = reference.year - born.year - ((reference.month, reference.day) < (born.month, born.day))
    return "Youth" if age <= 35 else "Adult" if age <= 59 else "Elder"


CROP_OPTIONS = options(
    ("apples", "Apples"), ("avocado", "Avocado"), ("bananas_matooke", "Bananas/matooke"),
    ("barley", "Barley"), ("beans", "Beans"), ("black_nightshade", "Black nightshade"),
    ("cabbage", "Cabbage"), ("carrots", "Carrots"), ("cassava", "Cassava"),
    ("coffee", "Coffee"), ("cow_peas", "Cow peas"), ("dodo_amaranth", "Dodo/amaranth"),
    ("egg_plants", "Egg plants"), ("garlic", "Garlic"), ("groundnuts", "Groundnuts"),
    ("guavas", "Guavas"), ("irish_potatoes", "Irish potatoes"), ("jackfruits", "Jackfruits"),
    ("lemons", "Lemons"), ("loquat", "Loquat"), ("maize", "Maize"), ("mangoes", "Mangoes"),
    ("millet", "Millet"), ("nakati", "Nakati"), ("onions", "Onions"), ("oranges", "Oranges"),
    ("sorghum", "Sorghum"), ("soya_bean", "Soya bean"), ("spider_plant", "Spider plant"),
    ("spinach", "Spinach"), ("sukuma_wiki", "Sukuma wiki"),
    ("sweet_potatoes", "Sweet potatoes"), ("tomatoes", "Tomatoes"),
    ("watermelon", "Watermelon"), ("yams", "Yams"),
)
CROP_OPTIONS_WITH_OTHER = CROP_OPTIONS + options(("other", "Other (specify)"))
CASH_ONLY_CROPS = {"coffee", "barley"}
CROP_GROUPS = {
    "cereals_grains": {"maize", "sorghum", "millet"},
    "roots_tubers": {"irish_potatoes", "sweet_potatoes", "yams", "cassava"},
    "bananas_plantain": {"bananas_matooke"},
    "beans_pulses": {"beans", "cow_peas", "groundnuts", "soya_bean"},
    "dark_green_leafy": {"sukuma_wiki", "dodo_amaranth", "black_nightshade", "spider_plant", "spinach", "nakati"},
    "other_vegetables": {"cabbage", "onions", "tomatoes", "egg_plants", "carrots", "garlic"},
    "fruit": {"mangoes", "jackfruits", "oranges", "lemons", "apples", "loquat", "guavas", "avocado", "watermelon"},
}


SURVEY_SECTIONS: list[dict[str, Any]] = [
    {
        "id": "A", "title": "Visit, location and household profile", "always": True,
        "intro": "Confirm the pre-filled beneficiary information. Training history is read-only.",
        "questions": [
            question("A0", "a0_shared_plot", "Is another person on this plot of land already registered?", "choice", YES_NO),
            question("A0.1", "a0_shared_person", "Select the other registered person from this village", "choice",
                     condition=eq("a0_shared_plot", "yes")),
            question("A4", "a4_district", "District", "choice", options("Kapchorwa", "Kween"), profile_key="district", required=False),
            question("A5", "a5_subcounty", "Sub-county / Division", "choice", options(
                "East Division", "West Division", "Kaptum", "Kwanyiy", "Moyok"), profile_key="subcounty", required=False),
            question("A6", "a6_village", "Village / Cell", "readonly", profile_key="village", required=False, editable=True),
            question(
                "A7", "a7_gps", "GPS location / plot boundary", "gps", required=False,
                help_text=("Start tracking while walking the boundary of the farmer's main plot. "
                           "If the farmer owns several plots, track only the main plot or the one nearest the home."),
            ),
            question("A8.1", "a8_beneficiary_type", "Type of beneficiary", "choice", options(
                "Direct Reach", "Indirect Reach"), profile_key="beneficiary_type", required=False),
            question("A8.2", "a8_contact", "Contact of the beneficiary", "readonly", profile_key="phone", required=False, editable=True),
            question("A8.3", "a8_gender", "Gender", "choice", options(
                ("F", "F – Female"), ("M", "M – Male")), profile_key="sex", required=False),
            question("A8.4", "a8_pwd", "Person with a disability (PWD)", "choice", YES_NO, profile_key="pwd", required=False),
            question("A8.5", "a8_birth_year", "Date of Birth (year)", "choice", YEAR_OF_BIRTH_OPTIONS,
                     profile_key="birth_year", required=False,
                     help_text="Select the year of birth. Age and age group are calculated automatically in the background."),
            question("A8.6", "a8_group", "Farmer group membership", "readonly", profile_key="group", required=False, editable=True),
            question("A9", "a9_education", "Level of education completed", "choice", options(
                ("none", "Not gone to school"), ("pre_primary", "Pre-primary"), ("primary", "Primary"),
                ("secondary_o", "Secondary O-level"), ("secondary_a", "Secondary A-level"),
                ("certificate", "Certificate"), ("diploma", "Diploma"), ("degree", "Degree"),
                ("other", "Other")), required=False, profile_key="education"),
            question("A10.2", "a10_male", "People living in the household - male", "number", required=False, min_value=0, integer=True),
            question("A10.3", "a10_female", "People living in the household - female", "number", required=False, min_value=0, integer=True),
            question("A11", "a11_trainings", "Trainings received", "training_list", required=False,
                     help_text="Filled from the AE training history and cannot be changed during follow-up."),
        ],
    },
    {
        "id": "B", "title": "Regenerative / sustainable farming practices", "topics": sorted(SECTION_B_TOPICS),
        "intro": "Conduct this as one continuous walking interview. Physical observation takes precedence over self-report.",
        "questions": [
            question("B1", "b1_food_groups", "Walking the plot, which of the following crops do you see planted? (tick all that apply)", "multi", CROP_OPTIONS_WITH_OTHER,
                     help_text="Ask the farmer about crops in other plots or gardens that are not visible from where you are standing."),
            question("B1.a", "b1_other_crop", "Specify the other crop", "text",
                condition=contains("b1_food_groups", "other"), guide_parent="b1_food_groups"),
            question("B2", "b5_preparation", "Looking at the soil surface, how was this plot prepared?", "choice", options(
                ("fully_tilled", "Fully tilled (more than 5 times per year)"), ("minimum", "Minimally tilled (1–4 times in a year)"),
                ("holes", "Planting holes or strips"), ("undisturbed", "Undisturbed between rows"))),
            question("B2.2", "b5_2_extent", "Estimate what share of the plot uses the preparation method", "choice", options(
                ("whole", "Whole plot — no full-tillage sections visible"),
                ("most", "Most of the plot, small full-tillage sections remain"),
                ("half", "About half the plot"),
                ("less_than_half", "Less than half, or isolated patches only")),
                condition=one_of("b5_preparation", ["minimum", "holes", "undisturbed"]), skip_condition=eq("b5_preparation", "fully_tilled")),
            question("B2.1", "b5_1_old_marks", "Are old furrow or ridge marks visible?", "choice", options(
                ("none", "No old marks"), ("visible", "Old furrows visible - recent transition"),
                ("new_plot", "Newly opened plot; no previous cultivation to compare")), condition=one_of("b5_preparation", ["minimum", "holes", "undisturbed"]), skip_condition=eq("b5_preparation", "fully_tilled")),
            question("B3", "b2_cover", "What is mostly covering the soil surface between plants?", "choice", options(
                ("bare", "Bare soil"), ("mulch", "Crop residue or mulch"),
                ("living", "Living ground cover"), ("other", "Other"))),
            question("B3.1", "b2_1_mulch", "What do you observe about the mulch?", "choice", options(
                ("full", "Full cover, at least 3 cm thick"), ("over_80", "More than 80% cover and at least 1 cm thick"),
                ("over_50", "Scattered, above 50% covered"), ("under_50", "Less than 50% covered")), condition=eq("b2_cover", "mulch")),
            question("B3.1", "b2_1_living", "What do you observe about the living cover?", "choice", options(
                ("dense", "Dense deliberate cover crop with little bare ground"),
                ("patchy", "Patchy cover or a mix of planted and volunteer growth"),
                ("sparse", "Sparse or mostly volunteer weeds")), condition=eq("b2_cover", "living")),
            question("B4", "b3_features", "Which erosion features are visible?", "multi", options(
                ("rills", "Rills or small channels"), ("gully", "Gully too wide or deep to step across"),
                ("roots", "Exposed plant or tree roots"), ("loose_soil", "Loose soil deposited at a slope or plot edge"),
                ("compacted", "Bare compacted patches"), ("none", "None")), required=False, exclusive_values=["none"],
                skip_condition=eq("b5_preparation", "fully_tilled")),
            question("B4.1", "b3_1_rills", "What is the depth of the rills?", "choice", options(
                ("shallow", "Ankle-height or less"), ("deep", "Deeper than an ankle")), condition=contains("b3_features", "rills"), skip_condition=eq("b5_preparation", "fully_tilled")),
            question("B4.2", "b3_2_roots", "How widespread are the exposed roots?", "choice", options(
                ("isolated", "Isolated - 1 or 2 plants"), ("widespread", "3 or more plants")), condition=contains("b3_features", "roots"), skip_condition=eq("b5_preparation", "fully_tilled")),
            question("B4.3", "b3_3_soil", "What is the occurrence of the loose soil?", "choice", options(
                ("scatter", "Thin scatter"), ("ridge", "Built-up ridge")), condition=contains("b3_features", "loose_soil"), skip_condition=eq("b5_preparation", "fully_tilled")),
            question("B5", "b4_structures", "Which soil and water conservation structures are visible?", "multi", options(
                ("grass", "Grass strips"), ("trash", "Trash lines"), ("stone", "Stone bunds"),
                ("trenches", "Trenches"), ("terracing", "Bench terracing"), ("none", "None")), exclusive_values=["none"], skip_condition=eq("b5_preparation", "fully_tilled")),
            question("B5.1", "b4_1_width", "What share of the plot's width is covered by the structure(s)?", "choice", options(
                ("full", "Full width — reaches both edges of the plot"),
                ("most", "Most of the width, small uncovered sections remain"),
                ("half", "About half the width"),
                ("less_than_half", "Less than half, or a short isolated section only")),
                condition={"question": "b4_structures", "operator": "has_any_except", "value": "none"}, skip_condition=eq("b5_preparation", "fully_tilled")),
            question("B5.2", "b4_2_contour", "Do the structures follow the contour of the land?", "choice", options(
                ("yes", "Yes"), ("no", "No"), ("unsure", "Unsure")),
                condition={"question": "b4_structures", "operator": "has_any_except", "value": "none"}, skip_condition=eq("b5_preparation", "fully_tilled")),
            question("B6", "b6_weeds", "How are weeds typically managed during the season?", "choice", options(
                ("spot", "Hand-pulling or spot-hoeing"), ("few_full", "Full-bed hoeing a few times per year"),
                ("frequent_full", "Frequent full-bed hoeing"), ("herbicide", "Herbicide")), skip_condition=eq("b5_preparation", "fully_tilled")),
            question("B7", "b7_arrangement", "How are crops arranged on this plot?", "choice", options(
                ("single", "Single crop in uniform rows"), ("mixed", "Mixed crops interplanted"), ("other", "Other"))),
            question("B8", "b8_trees", "Which of the following describe the trees or shrubs on this plot? (tick all that apply)", "multi", options(
                ("boundary", "Along the plot boundary"),
                ("compound", "Near the homestead / compound"),
                ("intercropping", "Interspersed among the crops (intercropped)"),
                ("woodlot", "In a dedicated woodlot or block"),
                ("underplanting", "Something planted directly next to or underneath the trees"),
                ("other", "Other"),
                ("none", "No trees or shrubs present (select only if none of the above apply)")),
                exclusive_values=["none"]),
            question("B9", "b9_traces", "On a random plot, Can a previous crop be identified from residue, stubble or physical traces?", "choice", options(
                ("identified", "Yes - previous crop identified"), ("unidentified", "Traces visible, crop not identifiable"),
                ("none", "No traces of a previous crop"))),
            question("B9.a", "b9_previous_crop", "Select the previous crop", "choice", CROP_OPTIONS, condition=eq("b9_traces", "identified")),
            question("B9.b", "b9_differs", "Is the identified crop different from the current crop?", "choice", YES_NO, condition=eq("b9_traces", "identified")),
            question("B9.1", "b9_1_farmer", "Was a different crop grown here last season? [Ask the farmer]", "choice", options(
                ("yes", "Yes"), ("no", "No"), ("unsure", "Unsure"))),
            question("B10", "b10_pest_method", "Which pest-management method can be observed?", "choice", options(
                ("biological", "Biological or manual method"), ("both", "Both synthetic and biological/manual"),
                ("synthetic", "Synthetic pesticide only"), ("none", "No evidence"))),
            question("B10.1", "b10_1_damage", "Randomly inspect 10 plants, how many show pest or disease damage?", "number", condition=one_of("b10_pest_method", ["biological", "both"]), unit="/ 10", max_value=10, integer=True),
            question("B10.2", "b10_2_severe", "How many have damage affecting over half the leaves or growing point?", "number", condition=one_of("b10_pest_method", ["biological", "both"]), unit="/ 10", max_value=10, integer=True),
            question("B10.3", "b10_3_chemical_change", "Since using bio-pesticide, how has chemical pesticide use changed?", "choice", options(
                ("increased", "Increased"), ("same", "Stayed the same"), ("reduced", "Reduced"),
                ("never_used", "Did not use chemical pesticide before"),
                ("no_prior_biopesticide", "Did not use bio-pesticide before")), condition=one_of("b10_pest_method", ["biological", "both"])),
            question("B11", "b11_inputs", "Which soil-fertility inputs have physical evidence?", "multi", options(
                ("manure", "Manure or compost visible"),
                ("bio", "Bio-fertilizer preparation materials present"), ("synthetic", "Traces of synthetic fertilizer (bags, containers ...)"),
                ("none", "None")), exclusive_values=["none"], skip_condition=eq("b5_preparation", "fully_tilled")),
            question("B11.1", "b11_1_location", "What share of the plot receives this manure or compost?", "choice", options(
                ("whole", "Whole plot, evenly"),
                ("most", "Most of the plot, some sections uncovered"),
                ("half", "About half the plot"),
                ("section", "One section only, or a small isolated area")),
                condition=contains("b11_inputs", "manure"), skip_condition=eq("b5_preparation", "fully_tilled")),
            question("B11.2", "b11_2_method", "How is the fertility input mostly applied?", "choice", options(
                ("surface", "Spread on the surface"), ("mixed", "Dug or mixed into soil"), ("holes", "Placed in planting holes"),
                ("liquid_base", "Liquid poured at plant base"), ("liquid_leaf", "Liquid sprayed on leaves"),
                ("none", "None of these")), condition={"question": "b11_inputs", "operator": "contains_any", "values": ["manure", "bio"]}, skip_condition=eq("b5_preparation", "fully_tilled")),
            question("B12", "b12_macrofauna", "In a 30 cm x 30 cm x 30 cm quadrant, how many earthworms or other macrofauna are found?", "number", integer=True, help_text="Enter the observed count freely: 0, 1, 2 … 10 or more.", skip_condition=eq("b5_preparation", "fully_tilled")),
            question("B13", "b13_synthetic_fertilizer", "Amount of synthetic fertilizer applied last season", "number", required=False, carry_forward=True, help_text="Comparison value only; not scored. Enter the amount, then select its unit."),
            question("B13.1", "b13_unit", "Unit used for the synthetic fertilizer amount", "choice", options(
                ("kilogram", "Kilogram"), ("liter", "Liter"), ("bag", "Bag"), ("basin", "Basin"),
                ("other", "Other (free text)")), carry_forward=True,
                guide_parent="b13_synthetic_fertilizer"),
            question("B13.1a", "b13_unit_other", "Specify the other unit", "text",
                condition=eq("b13_unit", "other"), carry_forward=True, guide_parent="b13_synthetic_fertilizer"),
            question("B14", "b14_manure_loads", "Amount of manure or compost applied last season", "number", required=False, help_text="Comparison value only; not scored. Enter the amount, then select its unit."),
            question("B14.1", "b14_unit", "Unit used for the manure or compost amount", "choice", options(
                ("kilogram", "Kilogram"), ("liter", "Liter"), ("bag", "Bag"), ("basin", "Basin"),
                ("other", "Other (free text)")),
                guide_parent="b14_manure_loads"),
            question("B14.1a", "b14_unit_other", "Specify the other unit", "text",
                condition=eq("b14_unit", "other"), guide_parent="b14_manure_loads"),
            question("B15", "b15_gap", "Main gap observed", "textarea", required=False),
            question("B16", "b16_advice", "Immediate recommendation or advice given", "textarea", required=False),
        ],
    },
    {
        "id": "C", "title": "Household Resource Mapping / PIP", "topics": [PIP],
        "questions": [
            question("C1", "c1_map_drawn", "Did the household draw a Household Resource Map?", "choice", YES_NO),
            question("C2", "c2_current_map", "Is a vision map showing the current household situation present?", "choice", YES_NO, condition=eq("c1_map_drawn", "yes")),
            question("C3", "c3_storage", "How is the map or plan kept?", "choice", options(
                ("displayed", "Displayed or kept in a dedicated book"), ("loose", "Exists but is loose or hard to locate"),
                ("not_shown", "Not shown")), condition=eq("c1_map_drawn", "yes")),
            question("C4", "c4_use", "Does the plan show signs of use since it was made?", "choice", options(
                ("clear", "Clear signs of use"), ("some", "Some signs, unclear"), ("none", "No signs of use")), condition=eq("c1_map_drawn", "yes")),
            question("C5", "c5_gap", "Main gap observed", "textarea", required=False),
            question("C6", "c6_advice", "Immediate recommendation or advice", "textarea", required=False),
        ],
    },
    {
        "id": "D", "title": "Kitchen Garden and Vegetable Growing", "topics": [KITCHEN],
        "intro": "For each food group, indicate: own production, bought, both, or not consumed.",
        "questions": [
            question("D1a", "d1_dark_leafy", "Dark green leafy vegetables (dodo, nightshade, sukuma, spider plant)", "choice", FOOD_SOURCE_OPTIONS, required=False, help_text="D1. For each food group, indicate: own production, bought, both, or not consumed. Food-source inventory; not scored."),
            question("D1b", "d1_other_vegetables", "Other vegetables (tomato, cabbage, onion, eggplant)", "choice", FOOD_SOURCE_OPTIONS, required=False, guide_parent="d1_dark_leafy"),
            question("D1c", "d1_beans_pulses", "Beans & pulses", "choice", FOOD_SOURCE_OPTIONS, required=False, guide_parent="d1_dark_leafy"),
            question("D1d", "d1_roots_tubers", "Roots and tubers (cassava, potato)", "choice", FOOD_SOURCE_OPTIONS, required=False, guide_parent="d1_dark_leafy"),
            question("D1e", "d1_cereals", "Cereals (barley, millet, rice, sorghum)", "choice", FOOD_SOURCE_OPTIONS, required=False, guide_parent="d1_dark_leafy"),
            question("D1f", "d1_bananas", "Bananas", "choice", FOOD_SOURCE_OPTIONS, required=False, guide_parent="d1_dark_leafy"),
            question("D1g", "d1_fruits", "Fruits", "choice", FOOD_SOURCE_OPTIONS, required=False, guide_parent="d1_dark_leafy"),
            question("D1h", "d1_eggs_poultry", "Eggs & poultry", "choice", FOOD_SOURCE_OPTIONS, required=False, guide_parent="d1_dark_leafy"),
            question("D2", "d2_garden_cover", "How much of the kitchen garden is covered by growing vegetables?", "choice", options(
                ("mostly_veg", "Vegetables cover most of the garden"), ("equal", "Vegetables and bare ground/weeds are roughly equal"),
                ("mostly_bare", "Mostly bare ground or weeds"))),
            question("D3", "d3_weeds", "Are weeds taller than the vegetable plants?", "choice", options(
                ("no", "No"), ("patches", "Yes, in patches"), ("most", "Yes, across most of the garden"))),
            question("D4", "d4_unhealthy", "Of 10 vegetable plants, how many are wilted, yellowing or dead?", "number", unit="/ 10", max_value=10, integer=True),
            question("D5", "d5_structures", "Which kitchen-garden structures are visible?", "multi", options(
                ("keyhole", "Keyhole wall/garden"),
                ("raised_bed", "Raised bed edge"),
                ("fencing", "Fencing"),
                ("vertical", "vertical gardens"),
                ("container", "sack/bucket /bag/tire gardening"),
                ("none", "None of the above")), exclusive_values=["none"]),
            question("D6", "d6_seed_storage", "Is seed from this garden stored for the next season?", "choice", options(
                ("visible", "Yes, storage container or hanging bundle visible"), ("claimed", "Farmer says yes, nothing visible"), ("no", "No"))),
            question("D7", "d7_income", "What was the level of income from kitchen gardens last week?", "number",
                     unit="UGX", step_value=100, required=False, help_text="Descriptive only; not scored. Enter the amount in steps of UGX 100."),
            question("D8", "d7_gap", "Main gap observed", "textarea", required=False),
            question("D9", "d8_advice", "Immediate recommendation or advice", "textarea", required=False),
        ],
    },
    {
        "id": "E", "title": "Financial Literacy", "topics": [FINANCIAL],
        "questions": [
            question("E1", "e1_budget", "Ask to see the household's financial record book, budget, or log, in any format.", "choice", options(
                ("complete", "Shown — contains entries for both income and expenses"),
                ("incomplete", "Shown — but incomplete (only one of income/expenses, or sporadic entries)"),
                ("claimed", "Claimed to exist, not shown"),
                ("none", "None kept"))),
            question("E1.1", "e1_frequency", "Based on the dates recorded, how frequently do entries appear?", "choice", options(
                ("monthly", "At least monthly, consistently"),
                ("gaps", "Present but with gaps of several months"),
                ("rare", "Rare — one or two isolated entries only")), condition=one_of("e1_budget", ["complete", "incomplete"])),
            question("E1.2", "e1_12_months", "Are there dated entries visible reaching back at least 12 months?", "choice", options(
                ("yes", "Yes"), ("no", "No — book is more recent, or has gaps")),
                condition=one_of("e1_budget", ["complete", "incomplete"])),
            question("E1.2.1", "e1_income_change", "Compare the earliest entry from around 12 months ago to the most recent entry. Is there a visible increase in recorded income?", "choice", options(
                ("increase", "Clear increase"),
                ("same", "About the same, or unclear from the entries"),
                ("decrease", "Decrease")), condition=eq("e1_12_months", "yes")),
            question("E2", "e2_decisions", "Thinking about the most recent major household purchase, were you involved in that decision?", "choice", options(
                ("involved", "Yes — I was consulted or helped decide"),
                ("informed", "I was informed about it, but not consulted"),
                ("not_involved", "I was not involved or don't know how it was decided"),
                ("single", "Single-adult household — no second adult"))),
            question("E3", "e3_within_means", "Do you live within your means (spending within what you earn or have)?", "choice", YES_NO),
            question("E4", "e4_invested", "Were investments made in the last 12 months?", "choice", YES_NO),
            question("E4.1", "e4_1_investments", "What have you invested in since the training?", "multi", options("Farming", "Land", "Business", "Other"), condition=eq("e4_invested", "yes")),
            question("E5", "e5_savings", "How much do you save on average per month?", "number", unit="UGX", integer=True, step_value=100, carry_forward=True,
                     help_text="Enter the average monthly amount in steps of UGX 100. Saved for future visits."),
            question("E6", "e6_saving_place", "Where do you save part of your income?", "choice", options(
                ("bank", "Bank / SACCO"), ("group", "Saving group"), ("mobile", "Mobile money"),
                ("home", "At home"), ("other", "Other"))),
            question("E7", "e8_2_help", "How has record keeping helped you?", "multi", options(
                "Easy tracking of income and expenses", "Improved planning and budgeting", "Better investment decisions", "To calculate profit or loss", "Other"), required=False, condition=one_of("e1_budget", ["complete", "incomplete"])),
            question("E8", "e9_gap", "Main gap observed", "textarea", required=False),
            question("E9", "e10_advice", "Immediate recommendation or advice", "textarea", required=False),
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
            question("F2", "f2_visible", "How many birds are visible?", "number", condition=one_of("f1_location", ["house", "bounded"]), min_value=1, integer=True),
            question("F2.1", "f2_1_unhealthy", "How many inspected birds show signs of illness?", "number", condition=one_of("f1_location", ["house", "bounded"]), integer=True),
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
        ],
    },
    {
        "id": "H", "title": "Radio Talk Show Impact", "always": True,
        "questions": [
            question("H1", "h1_radio", "Do you listen to the radio?", "choice", YES_NO),
            question("H1.1", "h1_1_hh_show", "Have you listened to a Harvesting Health radio talk show?", "choice", YES_NO, condition=eq("h1_radio", "yes")),
            question("H1.2", "h1_2_practised", "Have you practised something learned from it?", "choice", YES_NO, condition=eq("h1_1_hh_show", "yes")),
            question("H1.3", "h1_3_practised", "What did you practise? (tick all that apply)", "multi", options(
                ("crop_diversification", "Crop diversification / mixed cropping"),
                ("soil_water_conservation", "Soil and water conservation structures (grass strips, trenches, bunds)"),
                ("reduced_tillage", "Reduced tillage / minimum disturbance"),
                ("pest_disease_management", "Pest and disease management"),
                ("soil_fertility", "Soil fertility / manure and compost use"),
                ("pip", "Household resource mapping (PIP)"),
                ("kitchen_garden", "Kitchen garden / vegetable growing"),
                ("financial_literacy", "Financial literacy / saving and budgeting"),
                ("poultry_management", "Poultry management"),
                ("tree_nursery", "Tree nursery establishment")),
                condition=eq("h1_2_practised", "yes"), required=False,
                help_text="Narrative only; not scored."),
        ],
    },
    {
        "id": "I", "title": "Beneficiary Feedback on Trainings", "always": True,
        "intro": "This feedback is not scored.",
        "questions": [
            question("I2", "i2_change", "What is the biggest change noticed since starting these practices?", "choice", options(
                "More harvest / yield", "More food variety at home", "Spending less on inputs", "Less soil erosion / land damage",
                "Better animal / poultry health", "Saving more money", "No noticeable change yet", "Other")),
            question("I3", "i3_improve", "What would make the trainings more useful?", "choice", options(
                "More hands-on demonstrations", "More frequent follow-up visits", "Provide materials in local language",
                "Cover new or different topics", "Nothing - satisfied as is", "Other")),
            question("Consent", "consent", "Permission received to document and share practices for learning and communication", "consent", options(("yes", "Permission received"))),
            question("I4", "i4_photos", "Take or upload one photo of the best practice found during this visit", "photos", required=False,
                     help_text="Use the device camera or choose an existing photo. Maximum 1 photo."),
        ],
    },
]


# Package membership controls adaptive stopping after a critical answer. Shared
# questions remain visible while at least one package that needs them is active.
QUESTION_PACKAGE_KEYS = {
    "b1_food_groups": ["crop_diversification"],
    "b5_preparation": ["reduced_disturbance", "erosion_water", "soil_cover_fertility"],
    "b5_1_old_marks": ["reduced_disturbance"], "b5_2_extent": ["reduced_disturbance"],
    "b2_cover": ["crop_diversification", "erosion_water", "soil_cover_fertility"],
    "b2_1_mulch": ["erosion_water", "soil_cover_fertility"],
    "b2_1_living": ["erosion_water", "soil_cover_fertility"],
    "b3_features": ["erosion_water"], "b3_1_rills": ["erosion_water"],
    "b3_2_roots": ["erosion_water"], "b3_3_soil": ["erosion_water"],
    "b4_structures": ["erosion_water"], "b4_1_width": ["erosion_water"],
    "b4_2_contour": ["erosion_water"], "b6_weeds": ["reduced_disturbance"],
    "b7_arrangement": ["crop_diversification", "pest_management"],
    "b8_trees": ["crop_diversification", "erosion_water", "soil_cover_fertility"],
    "b9_traces": ["crop_diversification", "soil_cover_fertility", "pest_management"],
    "b9_previous_crop": ["crop_diversification", "soil_cover_fertility", "pest_management"],
    "b9_differs": ["crop_diversification", "soil_cover_fertility", "pest_management"],
    "b9_1_farmer": ["crop_diversification", "soil_cover_fertility", "pest_management"],
    "b10_pest_method": ["pest_management"], "b10_1_damage": ["pest_management"],
    "b10_2_severe": ["pest_management"], "b10_3_chemical_change": ["pest_management"],
    "b11_inputs": ["soil_cover_fertility"], "b11_1_location": ["soil_cover_fertility"],
    "b11_2_method": ["soil_cover_fertility"], "b12_macrofauna": ["soil_cover_fertility"],
}

PACKAGE_STOP_RULES = [
    {"packages": ["crop_diversification"], "question": "b1_food_groups", "operator": "crop_gate_failed",
     "excluded": sorted(CASH_ONLY_CROPS),
     "groups": {group: sorted(crops) for group, crops in CROP_GROUPS.items()},
     "min_crops": 3, "min_groups": 2, "critical": True},
    {"packages": ["reduced_disturbance", "erosion_water", "soil_cover_fertility"],
     "question": "b5_preparation", "operator": "equals", "value": "fully_tilled", "critical": True},
    {"packages": ["erosion_water"], "operator": "count_matches_gte", "threshold": 2, "critical": True,
     "matches": [{"question": "b3_1_rills", "value": "deep"},
                 {"question": "b3_2_roots", "value": "widespread"},
                 {"question": "b3_3_soil", "value": "ridge"}]},
    {"packages": ["erosion_water"], "question": "b4_structures", "operator": "contains",
     "value": "none", "critical": True},
    {"packages": ["pest_management"], "question": "b10_pest_method", "operator": "not_in",
     "values": ["biological", "both"], "critical": True},
    {"packages": ["pest_management"], "question": "b10_1_damage", "operator": "number_gte",
     "threshold": 6, "critical": True},
    {"packages": ["pest_management"], "question": "b10_2_severe", "operator": "number_gte",
     "threshold": 3, "critical": True},
    {"packages": ["pip"], "question": "c1_map_drawn", "operator": "equals", "value": "no", "critical": True},
    {"packages": ["kitchen"], "question": "d5_structures", "operator": "contains", "value": "none", "critical": True},
    {"packages": ["financial"], "question": "e1_income_change", "operator": "equals", "value": "decrease", "critical": True},
    {"packages": ["financial"], "question": "e3_within_means", "operator": "equals", "value": "no", "critical": True},
    {"packages": ["financial"], "question": "e4_invested", "operator": "equals", "value": "no", "critical": True},
    {"packages": ["poultry"], "question": "f1_location", "operator": "equals", "value": "free", "critical": True},
    # No visible birds ends the poultry section but is not itself a critical failure.
    {"packages": ["poultry"], "question": "f1_location", "operator": "equals", "value": "none", "critical": False},
    {"packages": ["poultry"], "question": "f2_1_unhealthy", "operator": "number_gte_if",
     "threshold": 4, "if_question": "f2_visible", "if_threshold": 5, "critical": True},
    {"packages": ["poultry"], "question": "f3_isolation", "operator": "equals", "value": "mixed", "critical": True},
]

for _section in SURVEY_SECTIONS:
    for _order, _item in enumerate(_section["questions"]):
        _item["section_order"] = _order
        if _item["id"] in QUESTION_PACKAGE_KEYS:
            _item["packages"] = QUESTION_PACKAGE_KEYS[_item["id"]]
        elif _section["id"] == "C":
            _item["packages"] = ["pip"]
        elif _section["id"] == "D":
            _item["packages"] = ["kitchen"]
        elif _section["id"] == "E":
            _item["packages"] = ["financial"]
        elif _section["id"] == "F":
            _item["packages"] = ["poultry"]

QUESTION_ORDER = {
    item["id"]: item["section_order"]
    for section in SURVEY_SECTIONS for item in section["questions"]
}


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
    previous_answers: dict[str, Any] | None = None,
    editable_options: dict[str, Iterable[str]] | None = None,
    question_options: dict[str, list[dict[str, str]]] | None = None,
) -> dict[str, Any]:
    topics = [topic for topic in TRAINING_TOPICS if topic in set(topics)]
    profile = profile or {}
    previous_answers = previous_answers or {}
    editable_options = editable_options or {}
    question_options = question_options or {}
    history_source = topics if training_history is None else training_history
    history_topics = [topic for topic in TRAINING_TOPICS if topic in set(history_source)]
    sections = survey_sections_for_topics(topics)
    for section in sections:
        section["questions"] = [
            item for item in section["questions"]
            if not item.get("training_topic") or item["training_topic"] in history_topics
        ]
        for item in section["questions"]:
            if item["id"] in question_options:
                item["options"] = deepcopy(question_options[item["id"]])
            if item["type"] == "training_list":
                item["display_values"] = history_topics
            elif item["type"] == "ranking":
                item["options"] = [
                    option for option in item.get("options", []) if option["value"] in history_topics
                ]
                item["required"] = bool(item["options"])
            elif item.get("profile_key") or item.get("carry_forward"):
                source_value = profile.get(item["profile_key"], "") if item.get("profile_key") else previous_answers.get(item["id"], "")
                prefill = str(source_value if source_value is not None else "").strip()
                if item["type"] == "choice" and prefill:
                    match = next((option for option in item.get("options", [])
                                  if option["value"].casefold() == prefill.casefold()
                                  or option["label"].casefold() == prefill.casefold()), None)
                    if match:
                        prefill = match["value"]
                    else:
                        item.setdefault("options", []).append({"value": prefill, "label": prefill})
                item["prefill"] = prefill
                if item.get("editable") and item["id"] in editable_options:
                    values = [
                        str(value).strip() for value in editable_options[item["id"]]
                        if value is not None and str(value).strip()
                    ]
                    if prefill:
                        values.append(prefill)
                    unique_values = {value.casefold(): value for value in values}
                    item["edit_options"] = options(*sorted(unique_values.values(), key=str.casefold))
    return {
        "version": SURVEY_VERSION, "topics": topics, "training_history": history_topics,
        "sections": sections, "package_stop_rules": deepcopy(PACKAGE_STOP_RULES),
    }


def all_questions_for_topics(topics: Iterable[str]) -> list[dict[str, Any]]:
    return [item for section in survey_sections_for_topics(topics) for item in section["questions"]]


def _answer(answers: dict[str, Any], key: str, default: Any = "") -> Any:
    value = answers.get(key, default)
    if isinstance(value, str):
        return value.strip()
    return value


def _rule_trigger_order(rule: dict[str, Any], answers: dict[str, Any]) -> int | None:
    operator = rule["operator"]
    if operator == "count_matches_gte":
        matches = [
            QUESTION_ORDER[item["question"]] for item in rule["matches"]
            if _answer(answers, item["question"]) == item["value"]
        ]
        threshold = int(rule["threshold"])
        return sorted(matches)[threshold - 1] if len(matches) >= threshold else None
    question_id = rule["question"]
    value = _answer(answers, question_id)
    if _is_empty(value):
        return None
    triggered = False
    if operator == "equals":
        triggered = value == rule["value"]
    elif operator == "contains":
        triggered = isinstance(value, list) and rule["value"] in value
    elif operator == "not_in":
        triggered = value not in rule["values"]
    elif operator == "crop_gate_failed":
        eligible = set(value or []).difference(rule["excluded"]) if isinstance(value, list) else set()
        represented_groups = sum(bool(eligible.intersection(crops)) for crops in rule["groups"].values())
        triggered = len(eligible) < rule["min_crops"] or represented_groups < rule["min_groups"]
    elif operator in {"number_gte", "number_gte_if"}:
        try:
            triggered = float(value) >= float(rule["threshold"])
            if operator == "number_gte_if":
                triggered = triggered and float(_answer(answers, rule["if_question"])) >= float(rule["if_threshold"])
        except (TypeError, ValueError):
            triggered = False
    return QUESTION_ORDER[question_id] if triggered else None


def package_stop_triggers(answers: dict[str, Any], *, critical_only: bool = False) -> dict[str, int]:
    triggers: dict[str, int] = {}
    for rule in PACKAGE_STOP_RULES:
        if critical_only and not rule["critical"]:
            continue
        order = _rule_trigger_order(rule, answers)
        if order is None:
            continue
        for package in rule["packages"]:
            triggers[package] = min(triggers.get(package, order), order)
    return triggers


def question_is_active(item: dict[str, Any], answers: dict[str, Any]) -> bool:
    skip_condition = item.get("skip_condition")
    if skip_condition and _condition_matches(skip_condition, answers):
        return False
    condition = item.get("condition")
    if condition and not _condition_matches(condition, answers):
        return False
    packages = item.get("packages", [])
    if packages:
        triggers = package_stop_triggers(answers)
        if all(package in triggers and triggers[package] < item["section_order"] for package in packages):
            return False
    return True


def _condition_matches(condition: dict[str, Any], answers: dict[str, Any]) -> bool:
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
    if operator == "not_empty":
        return not _is_empty(value)
    return False


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == []


def resolve_locked_answers(answers: dict[str, Any], topics: Iterable[str]) -> dict[str, Any]:
    """Return answers with read-only linked fields copied from their source fields."""
    resolved = dict(answers)
    for item in all_questions_for_topics(topics):
        if item.get("locked_to"):
            resolved[item["id"]] = _answer(resolved, item["locked_to"])
    return resolved


def validate_answers(
    answers: dict[str, Any], topics: Iterable[str], *, training_history: Iterable[str] | None = None,
) -> str | None:
    """Validate active questions. Hidden conditional answers are ignored."""
    topics = list(topics)
    answers = resolve_locked_answers(answers, topics)
    recorded_topics = set(training_history) if training_history is not None else None
    for item in all_questions_for_topics(topics):
        if recorded_topics is not None and item.get("training_topic") \
                and item["training_topic"] not in recorded_topics:
            continue
        if item["type"] == "training_list" or not question_is_active(item, answers):
            continue
        value = _answer(answers, item["id"])
        if item.get("required") and _is_empty(value):
            return f"Answer {item['source_id']}: {item['label']}."
        if item["type"] == "ranking" and recorded_topics and _is_empty(value):
            return f"Rank every training received in {item['source_id']}."
        if _is_empty(value):
            continue
        if item["type"] == "date":
            try:
                parsed_date = date.fromisoformat(str(value))
            except (TypeError, ValueError):
                return f"Enter a valid date for {item['source_id']}."
            if parsed_date > date.today():
                return f"{item['source_id']} cannot be in the future."
        if item["id"] == "a8_birth_year" and age_from_birth_year(value) is None:
            return "Select a valid year of birth for A8.5."
        if item["type"] == "number":
            try:
                number = float(value)
            except (TypeError, ValueError):
                return f"Enter a valid number for {item['source_id']}."
            if not isfinite(number):
                return f"Enter a valid number for {item['source_id']}."
            minimum = item.get("min_value", 0)
            if number < minimum:
                if minimum == 0:
                    return f"Enter a non-negative number for {item['source_id']}."
                return f"{item['source_id']} must be at least {minimum:g}."
            if item.get("integer") and not number.is_integer():
                return f"Enter a whole number for {item['source_id']}."
            if item.get("step_value") is not None and number % item["step_value"] != 0:
                return f"{item['source_id']} must be entered in steps of {item['step_value']:g}."
            if item.get("max_value") is not None and number > item["max_value"]:
                return f"{item['source_id']} cannot be greater than {item['max_value']:g}."
        allowed = {option["value"] for option in item.get("options", [])}
        submitted = value if isinstance(value, list) else [value]
        if allowed and any(option not in allowed for option in submitted):
            return f"An answer for {item['source_id']} is invalid."
        if item.get("max_selections") is not None and isinstance(value, list) and len(value) > item["max_selections"]:
            return f"Select no more than {item['max_selections']} answers for {item['source_id']}."
        exclusive = set(item.get("exclusive_values", []))
        if exclusive and isinstance(value, list) and exclusive.intersection(value) and len(value) > 1:
            return f"{item['source_id']}: 'None' cannot be combined with another answer."
        if item["type"] == "ranking":
            if len(submitted) != len(set(submitted)):
                return f"Each training may appear only once in {item['source_id']}."
            expected = recorded_topics or set(allowed)
            if set(submitted) != set(expected):
                return f"Rank every training received in {item['source_id']}."
    damage = _answer(answers, "b10_1_damage")
    severe = _answer(answers, "b10_2_severe")
    if not _is_empty(damage) and not _is_empty(severe):
        try:
            if float(severe) > float(damage):
                return "B10.2 cannot be greater than the number of damaged plants in B10.1."
        except (TypeError, ValueError):
            pass  # Active numeric questions already return their more specific error above.
    unhealthy = _answer(answers, "f2_1_unhealthy")
    inspected = _answer(answers, "f2_visible")
    if not _is_empty(unhealthy) and not _is_empty(inspected):
        try:
            if float(unhealthy) > float(inspected):
                return "F2.1 cannot be greater than the number of visible birds in F2."
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
        eligible = set(value or []).difference(CASH_ONLY_CROPS)
        represented_groups = sum(bool(eligible.intersection(crops)) for crops in CROP_GROUPS.values())
        passed = len(eligible) >= 3 and represented_groups >= 2
        return ItemResult(2 if passed else 0, critical=not passed)
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
        return ItemResult({
            "full": 2, "most": 1, "half": 0, "less_than_half": 0,
            # Retain the former values for historical assessments.
            "partial": 1, "single": 0,
        }.get(value))
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
        return ItemResult({
            "whole": 2, "most": 1, "half": 0, "less_than_half": 0,
            # Retain the former values for historical assessments.
            "part": 0, "varies": 0,
        }.get(value))
    if item_id == "b6_weeds":
        return ItemResult({"spot": 2, "few_full": 1, "frequent_full": 0, "herbicide": 0}.get(value))
    if item_id == "b7_arrangement":
        return ItemResult(2 if value == "mixed" else 0)
    if item_id == "b8_trees":
        if isinstance(value, list):
            return ItemResult(2 if set(value).difference({"none"}) else 0)
        return ItemResult(2 if value == "yes" else 0)  # Historical B8 yes/no answers.
    if item_id == "b8_1_under":
        trees = _answer(answers, "b8_trees")
        if isinstance(trees, list):
            if "none" in trees:
                return ItemResult(None)
            return ItemResult(2 if "underplanting" in trees else 1)
        if trees != "yes":
            return ItemResult(None)
        return ItemResult(2 if value == "yes" else 1)  # Historical B8.1 answers.
    if item_id == "b8_2_arrangement":
        trees = _answer(answers, "b8_trees")
        if isinstance(trees, list):
            if "none" in trees:
                return ItemResult(None)
            selected = set(trees)
        else:
            if trees != "yes":
                return ItemResult(None)
            selected = {value} if isinstance(value, str) else set(value or [])
        return ItemResult(2 if selected.intersection({"boundary", "intercropping", "woodlot"}) else 0)
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
        return ItemResult({
            "increased": 0, "same": 2, "reduced": 2,
            "never_used": None, "no_prior_biopesticide": None,
        }.get(value))
    if item_id == "b11_inputs":
        selected = set(value or [])
        return ItemResult(2 if selected.intersection({"manure", "heap", "bio"}) else 0)
    if item_id == "b11_1_location":
        if "manure" not in set(_answer(answers, "b11_inputs", []) or []):
            return ItemResult(None)
        return ItemResult({
            "whole": 2, "most": 2, "half": 1, "section": 0,
            # Retain the former value for historical assessments.
            "holes": 2,
        }.get(value))
    if item_id == "b11_2_method":
        if not set(_answer(answers, "b11_inputs", []) or []).intersection({"manure", "bio"}):
            return ItemResult(None)
        return ItemResult(0 if value == "none" else 2)
    if item_id == "b12_macrofauna":
        number = _number(answers, item_id)
        return ItemResult(0 if number == 0 else 1 if number <= 4 else 2)
    raise KeyError(item_id)


def _result(points: float, available: float, critical: bool) -> dict[str, Any]:
    score = round((points / available * 100) if available else 0, 1)
    if critical:
        score = 0
    status = "Failed" if critical else "Achieved" if score >= 50 else "Partial"
    return {
        "points_earned": points,
        "points_available": available,
        "score": score,
        "status": status,
        "critical_failed": critical,
    }


def _recommended_action(result: dict[str, Any]) -> str:
    """Apply the same next-action bands to every package and training topic."""
    if result["critical_failed"] or result["score"] < 30:
        return "ct"
    if result["score"] < 50:
        return "followup_3"
    if result["score"] < 70:
        return "followup_1"
    return "none"


def score_packages(answers: dict[str, Any]) -> dict[str, dict[str, Any]]:
    severe_count = sum([
        _answer(answers, "b3_1_rills") == "deep",
        _answer(answers, "b3_2_roots") == "widespread",
        _answer(answers, "b3_3_soil") == "ridge",
    ])
    packages: dict[str, dict[str, Any]] = {}
    for package, item_ids in PACKAGE_ITEMS.items():
        if _answer(answers, "b5_preparation") == "fully_tilled" and package in {
            "reduced_disturbance", "erosion_water", "soil_cover_fertility",
        }:
            package_result = {
                "key": package, "title": PACKAGE_TITLES[package],
                **_result(0, 2, True), "recommendations": PACKAGE_RECOMMENDATIONS[package],
            }
            package_result["next_action"] = "ct"
            package_result["recommendation"] = f"New centralized training: {', '.join(package_result['recommendations'])}"
            packages[package] = package_result
            continue
        points = 0.0
        available = 0.0
        critical = package == "erosion_water" and severe_count >= 2
        for item_id in item_ids:
            item = _score_b_item(item_id, package, answers)
            if item.points is not None:
                points += item.points
                available += 2
            critical = critical or item.critical
        package_result = {
            "key": package,
            "title": PACKAGE_TITLES[package],
            **_result(points, available, critical),
            "recommendations": PACKAGE_RECOMMENDATIONS[package],
        }
        package_result["next_action"] = _recommended_action(package_result)
        package_result["recommendation"] = (
            f"New centralized training: {', '.join(package_result['recommendations'])}"
            if package_result["next_action"] == "ct"
            else FOLLOWUP_LABELS[package_result["next_action"]]
        )
        packages[package] = package_result
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
        ("d5_structures", "multi_presence", {"none"}),
        ("d6_seed_storage", {"visible": 2, "claimed": 0, "no": 0}, set()),
    ],
    FINANCIAL: [
        ("e1_budget", {"complete": 2, "incomplete": 1, "claimed": 0, "none": 0}, set()),
        ("e1_frequency", {"monthly": 2, "gaps": 1, "rare": 0}, set()),
        ("e1_income_change", {"increase": 2, "same": 1, "decrease": 0}, {"decrease"}),
        ("e2_decisions", {
            "involved": 2, "informed": 1, "not_involved": 0, "single": None,
            # Retain former values for historical saved assessments.
            "respondent": 0, "spouse": 0, "both": 2,
        }, set()),
        ("e3_within_means", {"yes": 2, "no": 0}, {"no"}),
        ("e4_invested", {"yes": 2, "no": 0}, {"no"}),
        ("e4_1_investments", "multi_any", set()),
        ("e5_savings", "positive", set()),
        ("e6_saving_place", {"bank": 2, "group": 2, "mobile": 2, "home": 1, "other": 1}, set()),
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
        elif scoring == "multi_presence":
            selected = set(value or [])
            item_points = 0 if not selected.difference({"none"}) else 2
            critical = critical or "none" in selected
        elif scoring == "positive":
            item_points = 2 if _number(answers, item_id) > 0 else 0
        elif scoring == "poultry_structure":
            count = len(value or [])
            item_points = 2 if count > 2 else 1 if count == 1 else 0
        elif scoring == "poultry_health":
            unhealthy = _number(answers, item_id)
            inspected = max(0, _number(answers, "f2_visible"))
            if inspected >= 5:
                item_points = 2 if unhealthy <= 1 else 1 if unhealthy <= 3 else 0
                critical = critical or unhealthy >= 4
            else:
                item_points = 0 if inspected == 0 or unhealthy > inspected / 2 else 2
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
        "Agroforestry": (
            bool(set(_answer(answers, "b8_trees", []) or []).difference({"none"}))
            if isinstance(_answer(answers, "b8_trees"), list)
            else _answer(answers, "b8_trees") == "yes"
        ),
        "Crop rotation": _answer(answers, "b9_traces") == "identified" and _answer(answers, "b9_differs") == "yes",
        "Organic manure / bio-fertilizer": bool(set(_answer(answers, "b11_inputs", []) or []).intersection({"manure", "heap", "bio"})),
        "Integrated pest management": _answer(answers, "b10_pest_method") in {"biological", "both"},
    }
    count = sum(practices.values())
    return {"passed": count >= 2, "count": count, "total": 8, "practices": practices}


def collect_failure_comments(
    answers: dict[str, Any], topics: Iterable[str], *, include_all: bool = False,
) -> list[dict[str, Any]]:
    """Return failure comments, or their full catalogue for the offline scorer."""
    topics = [topic for topic in TRAINING_TOPICS if topic in set(topics)]
    selected_topics = set(topics)
    questions = {item["id"]: item for item in all_questions_for_topics(topics)}
    comments: list[dict[str, Any]] = []

    def add(question_id: str, triggered: bool, comment: str, package_keys: Iterable[str]) -> None:
        item = questions.get(question_id)
        if not item or (not include_all and (not triggered or not question_is_active(item, answers))):
            return
        package_titles = [PACKAGE_TITLES.get(key, key) for key in package_keys]
        comments.append({
            "source_id": item["source_id"],
            "question_id": question_id,
            "question": item["label"],
            "comment": comment,
            "packages": package_titles,
        })

    if selected_topics.intersection(SECTION_B_TOPICS):
        crops = set(_answer(answers, "b1_food_groups", []) or []).difference(CASH_ONLY_CROPS)
        groups = sum(bool(crops.intersection(group_crops)) for group_crops in CROP_GROUPS.values())
        add("b1_food_groups", len(crops) < 3 or groups < 2,
            "Fewer than 3 crops or 2 food groups were found on the plot - food and nutritional diversity is limited.",
            ["crop_diversification"])
        add("b5_preparation", _answer(answers, "b5_preparation") == "fully_tilled",
            "The plot was fully tilled - the preparation method most likely to increase erosion risk and soil disturbance.",
            ["reduced_disturbance", "erosion_water", "soil_cover_fertility"])
        add("b5_2_extent", _answer(answers, "b5_2_extent") in {"half", "less_than_half", "part", "varies"},
            "The reduced-disturbance method is not used consistently across the whole plot.",
            ["reduced_disturbance"])
        add("b2_cover", _answer(answers, "b2_cover") in {"bare", "other"},
            "The soil surface is left bare between plants, with no residue, mulch, or living cover protecting it.",
            ["erosion_water", "soil_cover_fertility", "crop_diversification"])
        add("b2_1_mulch", _answer(answers, "b2_1_mulch") == "under_50",
            "Where mulch is present, it covers less than half the surface - not enough to meaningfully protect the soil.",
            ["soil_cover_fertility", "erosion_water"])
        severe_erosion = sum([
            _answer(answers, "b3_1_rills") == "deep",
            _answer(answers, "b3_2_roots") == "widespread",
            _answer(answers, "b3_3_soil") == "ridge",
        ]) >= 2
        add("b3_features", severe_erosion,
            "Active soil erosion was observed (deep rills, widespread exposed roots, or built-up loose soil) - this plot is losing soil.",
            ["erosion_water"])
        structures = set(_answer(answers, "b4_structures", []) or [])
        add("b4_structures", not structures.difference({"none"}),
            "No soil and water conservation structures - grass strips, bunds, trenches, or similar - were found on this plot.",
            ["erosion_water"])
        add("b4_1_width", _answer(answers, "b4_1_width") in {"half", "less_than_half", "single"},
            "The observed conservation structures seem to be incomplete.", ["erosion_water"])
        add("b4_2_contour", _answer(answers, "b4_2_contour") in {"no", "unsure"},
            "The structures present do not clearly follow the contour of the land, which reduces how well they slow water flow.",
            ["erosion_water"])
        add("b6_weeds", _answer(answers, "b6_weeds") in {"frequent_full", "herbicide"},
            "Weed control relies on frequent full-bed hoeing or herbicide, both of which increase soil disturbance or chemical input.",
            ["reduced_disturbance"])
        add("b7_arrangement", _answer(answers, "b7_arrangement") in {"single", "other"},
            "Only a single crop was seen growing in uniform rows - no interplanting was observed.",
            ["crop_diversification", "pest_management"])
        trees = _answer(answers, "b8_trees", [])
        selected_trees = set(trees or []) if isinstance(trees, list) else ({trees} if trees else set())
        no_trees = not selected_trees.difference({"none", "no"})
        add("b8_trees", no_trees,
            "No trees or shrubs were found on or bordering this plot.",
            ["crop_diversification", "erosion_water", "soil_cover_fertility"])
        add("b8_trees", not no_trees and not selected_trees.intersection({"boundary", "intercropping", "woodlot"}),
            "Trees present are confined to the homestead area or an unclear arrangement, not integrated with the cropped plot.",
            ["crop_diversification", "erosion_water"])
        add("b9_traces", _answer(answers, "b9_traces") == "none",
            "No evidence of crop rotation was found.",
            ["crop_diversification", "pest_management", "soil_cover_fertility"])
        add("b9_1_farmer", _answer(answers, "b9_1_farmer") in {"no", "unsure"},
            "The farmer reported growing the same crop as last season, with no supporting rotation evidence.",
            ["crop_diversification", "pest_management", "soil_cover_fertility"])
        add("b10_pest_method", _answer(answers, "b10_pest_method") in {"synthetic", "none"},
            "Pest management relies on synthetic pesticide alone, or no pest management method was observed.",
            ["pest_management"])
        add("b10_1_damage", _number(answers, "b10_1_damage") >= 6,
            "The current pest management is not working.", ["pest_management"])
        add("b10_2_severe", _number(answers, "b10_2_severe") >= 3,
            "The current pest management is not working; several plants show severe damage.", ["pest_management"])
        add("b10_3_chemical_change", _answer(answers, "b10_3_chemical_change") == "increased",
            "Use of chemical pesticide has increased.", ["pest_management"])
        fertility = set(_answer(answers, "b11_inputs", []) or [])
        add("b11_inputs", not fertility.intersection({"manure", "heap", "bio"}),
            "No organic fertility inputs - manure, compost, or bio-fertilizer materials - were found.",
            ["soil_cover_fertility"])
        add("b11_2_method", _answer(answers, "b11_2_method") == "none",
            "No method of applying fertility inputs was observed or reported.", ["soil_cover_fertility"])
        add("b12_macrofauna", _number(answers, "b12_macrofauna") == 0,
            "No earthworms or other soil macrofauna were found - a sign the soil's biological activity may be poor.",
            ["soil_cover_fertility"])

    if PIP in selected_topics:
        add("c1_map_drawn", _answer(answers, "c1_map_drawn") == "no",
            "No household resource map has been drawn.", [PIP])
        add("c2_current_map", _answer(answers, "c2_current_map") == "no",
            "No map showing the current household situation could be found or shown.", [PIP])
        add("c3_storage", _answer(answers, "c3_storage") == "not_shown",
            "The map or plan was not shown and could not be located.", [PIP])
        add("c4_use", _answer(answers, "c4_use") == "none",
            "The plan shows no signs of use since it was made - no dated notes, ticked items, or visible handling.", [PIP])

    if KITCHEN in selected_topics:
        add("d2_garden_cover", _answer(answers, "d2_garden_cover") == "mostly_bare",
            "The kitchen garden is mostly bare ground or weeds, with few vegetables remaining.", [KITCHEN])
        add("d3_weeds", _answer(answers, "d3_weeds") == "most",
            "Weeds are taller than the vegetables across most of the garden.", [KITCHEN])
        add("d4_unhealthy", _number(answers, "d4_unhealthy") >= 7,
            "Many vegetable plants are wilted, yellowing, or dead.", [KITCHEN])
        add("d5_structures", "none" in set(_answer(answers, "d5_structures", []) or []),
            "None of the listed kitchen-garden structures could be seen.", [KITCHEN])
        add("d6_seed_storage", _answer(answers, "d6_seed_storage") in {"claimed", "no"},
            "No stored seed for next season could be seen, or the farmer reported none is kept.", [KITCHEN])

    if FINANCIAL in selected_topics:
        add("e1_budget", _answer(answers, "e1_budget") in {"claimed", "none"},
            "No financial record book, budget, or log could be shown, or none is kept.", [FINANCIAL])
        add("e1_frequency", _answer(answers, "e1_frequency") == "rare",
            "Entries in the record book are rare - only one or two isolated entries were found.", [FINANCIAL])
        add("e1_income_change", _answer(answers, "e1_income_change") == "decrease",
            "Recorded income shows a decrease.", [FINANCIAL])
        add("e2_decisions", _answer(answers, "e2_decisions") in {"not_involved", "respondent", "spouse"},
            "The respondent was not involved in the most recent major household purchase decision.", [FINANCIAL])
        add("e3_within_means", _answer(answers, "e3_within_means") == "no",
            "The household reports spending beyond what it earns or has.", [FINANCIAL])
        add("e4_invested", _answer(answers, "e4_invested") == "no",
            "No investments were made in the last 12 months.", [FINANCIAL])
        add("e5_savings", _number(answers, "e5_savings") <= 0,
            "No savings were reported for an average month.", [FINANCIAL])

    if POULTRY in selected_topics:
        add("f1_location", _answer(answers, "f1_location") == "free",
            "Birds are free-ranging, with no boundary or enclosure at the time of the visit.", [POULTRY])
        add("f1_1_structure", len(_answer(answers, "f1_1_structure", []) or []) == 0,
            "No sufficient housing features were visible.", [POULTRY])
        inspected = _number(answers, "f2_visible")
        unhealthy = _number(answers, "f2_1_unhealthy")
        health_failed = unhealthy >= 4 if inspected >= 5 else inspected > 0 and unhealthy > inspected / 2
        add("f2_1_unhealthy", health_failed,
            "Several birds show signs of illness.", [POULTRY])
        add("f3_isolation", _answer(answers, "f3_isolation") == "mixed",
            "Sick birds are kept mixed with the rest of the flock, with no separate space.", [POULTRY])
        add("f4_records", _answer(answers, "f4_records") == "none",
            "No poultry record book could be shown, or none exists.", [POULTRY])
        add("f5_vaccination", _answer(answers, "f5_vaccination") == "none",
            "No vaccination record or vaccine container could be shown.", [POULTRY])
    return comments


def offline_scoring_rules() -> dict[str, Any]:
    """Share question gates, score tables and result wording with the tablet."""
    return {
        "version": SURVEY_VERSION,
        "topics": TRAINING_TOPICS,
        "section_b_topics": sorted(SECTION_B_TOPICS),
        "questions": all_questions_for_topics(TRAINING_TOPICS),
        "stop_rules": PACKAGE_STOP_RULES,
        "question_order": QUESTION_ORDER,
        "package_items": PACKAGE_ITEMS,
        "package_titles": PACKAGE_TITLES,
        "package_recommendations": PACKAGE_RECOMMENDATIONS,
        "training_packages": TRAINING_PACKAGES,
        "section_items": {
            topic: [[item_id, scoring, sorted(critical)] for item_id, scoring, critical in items]
            for topic, items in SECTION_ITEMS.items()
        },
        "followup_labels": FOLLOWUP_LABELS,
        "cash_only_crops": sorted(CASH_ONLY_CROPS),
        "crop_groups": {key: sorted(crops) for key, crops in CROP_GROUPS.items()},
        "failure_comments": collect_failure_comments({}, TRAINING_TOPICS, include_all=True),
    }


def score_survey(answers: dict[str, Any], topics: Iterable[str]) -> dict[str, Any]:
    topics = [topic for topic in TRAINING_TOPICS if topic in set(topics)]
    has_section_b = bool(set(topics).intersection(SECTION_B_TOPICS))
    packages = score_packages(answers) if has_section_b else {}
    rvo = rvo_result(answers) if has_section_b else None
    breadth = None
    depth = None
    household = None
    project_passed = None
    if has_section_b:
        achieved = sum(item["status"] == "Achieved" for item in packages.values())
        breadth = {"achieved": achieved, "total": len(packages)}
        project_passed = achieved >= 2
        any_weak = any(item["status"] != "Achieved" for item in packages.values())
        if not rvo["passed"]:
            household = {"code": "D", "label": "Below both bars", "action": "Priority household. Apply the score-based next action for each training type."}
        elif not project_passed:
            household = {"code": "C", "label": "RVO-Compliant", "action": "Practices are present but shallow. Apply the score-based next action for each training type."}
        elif any_weak:
            household = {"code": "B", "label": "Compliant, gaps remain", "action": "Apply the score-based next action for each training type."}
        else:
            household = {"code": "A", "label": "Strong adopter", "action": "No training needed."}

    trainings: dict[str, dict[str, Any]] = {}
    for topic in topics:
        if topic in TRAINING_PACKAGES:
            components = [packages[key] for key in TRAINING_PACKAGES[topic]]
            result = _result(
                sum(item["points_earned"] for item in components),
                sum(item["points_available"] for item in components),
                any(item["critical_failed"] for item in components),
            )
            result["next_action"] = _recommended_action(result)
            retraining_packages = [item for item in components if item["next_action"] == "ct"]
            recommendations = sorted({training for item in retraining_packages for training in item["recommendations"]})
            result["recommendation"] = (
                f"New centralized training: {', '.join(recommendations or [topic])}"
                if result["next_action"] == "ct"
                else FOLLOWUP_LABELS[result["next_action"]]
            )
        elif topic in SECTION_ITEMS:
            result = score_single_training(topic, answers)
            result["next_action"] = _recommended_action(result)
            result["recommendation"] = (
                f"New centralized training: {topic}"
                if result["next_action"] == "ct"
                else FOLLOWUP_LABELS[result["next_action"]]
            )
        else:
            continue
        result["next_action_label"] = FOLLOWUP_LABELS[result["next_action"]]
        trainings[topic] = result

    depth = round(mean(item["score"] for item in trainings.values()), 1) if trainings else None

    package_training_recommendations = sorted({
        training
        for item in packages.values() if item["next_action"] == "ct"
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
        "failure_comments": collect_failure_comments(answers, topics),
        "centralized_training_recommendations": package_training_recommendations,
    }
