"""Human-readable AE follow-up questionnaire used by the web application.

Edit this file when questionnaire wording, choices, or topic mappings change.
Each question has a stable ``id`` because saved answers refer to it. Keep an id
unchanged when only correcting wording; create a new id when its meaning changes.

Question types:
  choice       - select one answer
  multi        - select any number of answers
  number       - numeric answer
  text         - short written answer
  textarea     - longer written answer
  adoption_rate- percentage used as the final follow-up score (temporary rule)
  next_action  - schedule another follow-up, request CT, or close the follow-up

Sections A-C of the source questionnaire are not repeated in the form because
the visit date, data collector, farmer and location already come from the app.
Sections M (radio), N (schools) and O (tree nurseries) remain unmapped because
there is currently no corresponding AE farmer-training topic.
"""

from __future__ import annotations

from copy import deepcopy


QUESTIONNAIRE_VERSION = "2026-08-05"

YES_NO = ["Yes", "No"]
YES_NO_OTHER = ["Yes", "No", "Other"]
FREQUENCY = ["Always", "Sometimes", "Never"]

CROPS = [
    "Bananas / Matooke", "Sweet bananas", "Barley", "Beans", "Cabbages", "Carrots",
    "Cassava", "Coffee", "Cowpeas", "Egg plants", "Fruit trees", "Groundnuts",
    "Irish potatoes", "Maize", "Millet", "Onions", "Rice", "Sorghum",
    "Sukuma wiki", "Sweet potatoes", "Tomatoes", "Vegetables", "Other",
]

SUSTAINABLE_PRACTICES = [
    "Agroforestry", "Crop rotation", "Cover cropping",
    "Conservation or minimum tillage", "Integrated pest and disease management",
    "Integrated crop and livestock management", "Mulching",
    "Organic manure or bio-fertilizers", "Intercropping",
    "Soil and water conservation", "Other",
]

SWC_PRACTICES = [
    "Trenches", "Stone bunds", "Trash lines", "Grass strips", "Agroforestry",
    "Mulching", "Cover cropping", "Crop rotation", "Intercropping",
    "Bench terracing", "Other",
]

NEXT_ACTION_OPTIONS = [
    {"value": "followup_1", "label": "Follow up after 1 month", "months": 1},
    {"value": "followup_3", "label": "Follow up after 3 months", "months": 3},
    {"value": "followup_6", "label": "Follow up after 6 months", "months": 6},
    {"value": "ct", "label": "Centralized training (CT) needed", "months": None},
    {"value": "none", "label": "No further follow-up needed", "months": None},
]


def q(source_id, question_id, label, question_type="text", options=None, *, help_text=""):
    item = {
        "source_id": source_id,
        "id": question_id,
        "label": label,
        "type": question_type,
    }
    if options:
        item["options"] = options
    if help_text:
        item["help"] = help_text
    return item


def topic(section, title, questions, adoption_source, adoption_help):
    return {
        "source_section": section,
        "title": title,
        "questions": [
            *questions,
            q(
                adoption_source,
                "adoption_rate",
                "Adoption rate",
                "adoption_rate",
                help_text=(
                    f"{adoption_help} Temporary scoring rule: this entered percentage is saved "
                    "directly as the final score; no additional calculation is applied."
                ),
            ),
            q(
                f"{section}.next",
                "next_action",
                "What should happen next?",
                "next_action",
                NEXT_ACTION_OPTIONS,
            ),
        ],
    }


FOLLOWUP_QUESTIONNAIRE = {
    "Household Resource Mapping (PIP)": topic(
        "D",
        "Household Resource Mapping / PIP",
        [
            q("D.2", "trained", "Has the farmer been trained in Household Resource Mapping?", "choice", YES_NO),
            q("D.3", "maps_drawn", "Did the farmer draw household resource maps?", "choice", YES_NO),
            q("D.4", "plans_achieved", "Which household plans have been achieved since drawing the maps?", "textarea"),
            q("D.5", "observed_practices", "Which mapping practices can be observed?", "multi", [
                "Current map", "Vision map", "Action plan", "Maps displayed on a wall or kept in a good book",
                "Farmer is following the plans", "None of the above",
            ]),
            q("D.6", "observed_count", "Number of mapping practices observed", "number"),
            q("D.8", "main_gap", "Main gap observed", "textarea"),
            q("D.9", "immediate_advice", "Immediate recommendation or advice given", "textarea"),
            q("D.11", "photo_reference", "Photo reference for an observed practice", "text", help_text="Enter a filename or reference; file uploads can be added later."),
        ],
        "D.7",
        "Source guidance: 0-39% low, 40-59% medium, 60-100% high adoption.",
    ),
    "Sustainable/Regenerative Agriculture": topic(
        "E",
        "Regenerative / Sustainable Farming",
        [
            q("E.1", "trained", "Has the farmer been trained in sustainable farming?", "choice", YES_NO),
            q("E.2", "trained_practices", "Which sustainable farming practices were covered in training?", "multi", SUSTAINABLE_PRACTICES),
            q("E.3", "practices_visible", "Can sustainable farming practices be observed on the farm?", "choice", YES_NO),
            q("E.4", "observed_practices", "Which sustainable farming practices can be observed?", "multi", SUSTAINABLE_PRACTICES),
            q("E.5", "observed_count", "Number of sustainable farming practices observed", "number"),
            q("E.7", "implemented_acres", "Land area where the practices are implemented (acres)", "number"),
            q("E.8", "main_gap", "Main gap observed", "textarea"),
            q("E.9", "immediate_advice", "Immediate recommendation or advice given", "textarea"),
            q("E.11", "photo_reference", "Photo reference for observed practices", "text", help_text="Enter a filename or reference; file uploads can be added later."),
        ],
        "E.6",
        "Source guidance: 0% no adoption, 1-29% low, 30% medium, 31-100% high adoption.",
    ),
    "SWC": topic(
        "F",
        "Soil and Water Conservation",
        [
            q("F.1", "trained", "Has the farmer been trained in soil and water conservation?", "choice", YES_NO),
            q("F.2", "implemented_practices", "Which soil and water conservation practices has the farmer implemented?", "multi", SWC_PRACTICES),
            q("F.3", "practices_visible", "Can soil and water conservation practices be observed on the farm?", "choice", YES_NO),
            q("F.4", "observed_practices", "Which corresponding practices can be observed?", "multi", SWC_PRACTICES),
            q("F.5", "observed_count", "Number of soil and water conservation practices observed", "number"),
            q("F.7", "implemented_acres", "Land area where SWC practices are implemented (acres)", "number"),
            q("F.8", "main_gap", "Main gap observed", "textarea"),
            q("F.9", "quality_checks", "Which quality and maintenance checks are satisfied?", "multi", [
                "Structures are well aligned on contours", "Structures are maintained",
                "Vegetative measures are healthy", "Farmer understands the purpose", "Other",
            ]),
            q("F.10", "immediate_advice", "Immediate advice based on the quality and maintenance checks", "textarea"),
            q("F.12", "photo_reference", "Photo reference for observed practices", "text", help_text="Enter a filename or reference; file uploads can be added later."),
        ],
        "F.6",
        "Source guidance: 0% no adoption, 1-29% low, 30% medium, 31-100% high adoption.",
    ),
    "Tree planting/Agroforestry": topic(
        "G",
        "Tree Planting / Agroforestry",
        [
            q("G.1", "practices_agroforestry", "Does the farmer practise agroforestry?", "choice", YES_NO),
            q("G.2", "tree_cover_acres", "Land area under tree cover (acres)", "number"),
            q("G.3", "tree_species", "Which tree species are present on the farm?", "multi", [
                "Calliandra", "Cordia africana (Mukengeret)", "Croton (Toboswet)",
                "Ficus (Mokoywet / Munyambet)", "Grevillea", "Markhamia (Swayet)", "Fruit trees", "Other",
            ]),
            q("G.4", "received_seedlings", "Did the farmer receive tree seedlings?", "choice", YES_NO),
            q("G.5", "seedlings_planted", "Number of tree seedlings planted in 2025", "number"),
            q("G.6", "planting_systems", "Which tree-planting systems were used?", "multi", ["Boundary", "Compound", "Intercropping", "Woodlots", "Other"]),
            q("G.7", "trees_survived", "Number of planted trees that survived", "number"),
            q("G.9", "immediate_advice", "Immediate recommendation or advice given", "textarea"),
            q("G.11", "photo_reference", "Photo reference for observed practices", "text", help_text="Enter a filename or reference; file uploads can be added later."),
        ],
        "G.8",
        "Source guidance: 0% no adoption, 1-29% low, 30% medium, 31-100% high adoption.",
    ),
    "Bio-inputs training": topic(
        "H-I",
        "Bio-fertilizers and Bio-pesticides",
        [
            q("H.1", "fertilizer_trained", "Was the farmer trained to make bio-fertilizers?", "choice", YES_NO),
            q("H.2", "fertilizer_ever_used", "Has the farmer made and used bio-fertilizer since training?", "choice", YES_NO),
            q("H.3", "fertilizer_currently_used", "Is the farmer currently using bio-fertilizer?", "choice", YES_NO),
            q("H.4", "fertilizer_crops", "Which crops receive bio-fertilizer?", "multi", CROPS),
            q("H.5", "fertilizer_application", "How is bio-fertilizer applied?", "multi", ["Foliar spray", "Soil drench", "Other"]),
            q("H.6", "fertilizer_improvement", "Has the farmer noticed improvement after use?", "choice", YES_NO),
            q("H.7", "fertilizer_continue", "Does the farmer plan to continue making and using bio-fertilizer?", "choice", YES_NO),
            q("H.8", "fertilizer_material_access", "Can the materials be accessed easily?", "choice", YES_NO),
            q("H.9", "fertilizer_challenges", "Challenges faced when making bio-fertilizer", "textarea"),
            q("H.10", "chemical_fertilizer_change", "Effect on use of inorganic or chemical fertilizer", "choice", ["Increased", "Remained the same", "Reduced"]),
            q("H.11", "fertilizer_recommend", "Would the farmer recommend bio-fertilizer to others?", "choice", YES_NO),
            q("H.14", "fertilizer_advice", "Immediate bio-fertilizer recommendation or advice", "textarea"),
            q("I.1", "pesticide_trained", "Was the farmer trained to make bio-pesticides?", "choice", YES_NO),
            q("I.2", "pesticide_ever_used", "Has the farmer made and used bio-pesticide since training?", "choice", YES_NO),
            q("I.3", "pesticide_currently_used", "Is the farmer currently using bio-pesticide?", "choice", YES_NO),
            q("I.4", "pesticide_crops", "Which crops receive bio-pesticide?", "multi", CROPS),
            q("I.5", "pesticide_application", "How is bio-pesticide applied?", "multi", ["Foliar spray", "Soil drench", "Other"]),
            q("I.6", "pesticide_improvement", "Has the farmer noticed improvement after use?", "choice", YES_NO),
            q("I.7", "pesticide_continue", "Does the farmer plan to continue making and using bio-pesticide?", "choice", YES_NO),
            q("I.8", "pesticide_material_access", "Can the materials be accessed easily?", "choice", YES_NO),
            q("I.9", "pesticide_challenges", "Challenges faced when making bio-pesticide", "textarea"),
            q("I.10", "chemical_pesticide_change", "Effect on use of inorganic or chemical pesticide", "choice", ["Increased", "Remained the same", "Reduced"]),
            q("I.11", "pesticide_recommend", "Would the farmer recommend bio-pesticide to others?", "choice", YES_NO),
            q("I.14", "pesticide_advice", "Immediate bio-pesticide recommendation or advice", "textarea"),
        ],
        "H.12-H.13 / I.12-I.13",
        "Enter the overall adoption percentage for this combined bio-inputs follow-up.",
    ),
    "Kitchen garden Establishment and Vegetable growing": topic(
        "J",
        "Kitchen Garden and Vegetable Growing",
        [
            q("J.1", "trained", "Has the farmer been trained in kitchen gardening and vegetable growing?", "choice", YES_NO),
            q("J.2", "garden_established", "Has the farmer established a kitchen garden?", "choice", YES_NO),
            q("J.3", "garden_types", "Which kind of kitchen garden was established?", "multi", ["Backyard garden", "Keyhole garden", "Raised bed", "Sack garden", "Other"]),
            q("J.4", "vegetables", "Which vegetables are grown?", "multi", [
                "Amaranthus / Dodo", "Black nightshade", "Cabbages", "Carrots", "Egg plants",
                "Pumpkins", "Spider plant", "Spinach", "Sukuma wiki", "Tomatoes", "Other",
            ]),
            q("J.5", "other_crops", "Which other crops are grown in the kitchen garden?", "multi", CROPS),
            q("J.6", "sells_vegetables", "Does the farmer sell vegetables from the kitchen garden?", "choice", YES_NO),
            q("J.7", "seeds_stored", "Has the farmer stored seeds from the kitchen garden?", "choice", YES_NO),
            q("J.8", "seed_storage", "How are seeds stored for the next planting season?", "multi", ["Plastic container", "Tied and hung", "Granary", "Other"]),
            q("J.9", "plans_more_vegetables", "Can the farmer plant more vegetables in the coming season?", "choice", YES_NO),
            q("J.10", "practices_visible", "Can kitchen-garden practices be observed?", "choice", YES_NO),
            q("J.11", "observed_practices", "Which knowledge and practices can be observed?", "multi", [
                "Kitchen garden established", "Different crops or vegetables grown", "Local seedbank created",
                "Farmer understands the importance of a kitchen garden", "Farmer intends to continue growing vegetables", "Other",
            ]),
            q("J.12", "observed_count", "Number of kitchen-garden practices observed", "number"),
            q("J.14", "main_gap", "Main gap observed", "textarea"),
            q("J.15", "immediate_advice", "Immediate recommendation or advice given", "textarea"),
            q("J.17", "photo_reference", "Photo or record reference", "text", help_text="Enter a filename or reference; file uploads can be added later."),
        ],
        "J.13",
        "Source guidance: 0% no adoption, 1-39% low, 40-79% medium, 80-100% high adoption.",
    ),
    "Financial Literacy": topic(
        "K",
        "Financial Literacy",
        [
            q("K.1", "trained", "Was the farmer trained in financial literacy?", "choice", YES_NO),
            q("K.2", "sets_goals", "Does the farmer set financial goals or plans?", "choice", YES_NO),
            q("K.3", "prepares_budget", "Does the farmer prepare a farm or household budget?", "choice", YES_NO),
            q("K.4", "follows_budget", "How consistently is the budget followed?", "choice", FREQUENCY),
            q("K.5", "budget_benefits", "How does budgeting help?", "multi", ["Control expenses", "Track income", "Plan for family and future", "Make proper decisions", "Support saving and investment"]),
            q("K.6", "compares_prices", "Before spending, does the farmer compare prices and priorities?", "choice", FREQUENCY),
            q("K.7", "lives_within_means", "Does the farmer spend within what is earned or available?", "choice", YES_NO),
            q("K.8", "invests_income", "Does the farmer invest part of the income?", "choice", YES_NO),
            q("K.9", "investments", "What has the farmer invested in since training?", "multi", ["Farming", "Land", "Business", "Other"]),
            q("K.10", "saves_income", "Does the farmer save part of the income?", "choice", YES_NO),
            q("K.11", "saving_frequency", "How often does the farmer save?", "choice", ["Daily", "Weekly", "Bi-weekly", "Monthly", "Annually", "Other"]),
            q("K.12", "saving_amount", "Amount saved (UGX)", "number"),
            q("K.13", "saving_locations", "Where is income saved?", "multi", ["Bank / SACCO", "Saving group", "Mobile money", "At home", "Other"]),
            q("K.14", "saving_reasons", "Why does the farmer save?", "multi", ["Emergencies", "Farm inputs", "Reduce borrowing and debt", "Family needs", "Other"]),
            q("K.15", "keeps_records", "Does the farmer keep income and expense records?", "choice", YES_NO),
            q("K.16", "record_book", "Does the farmer have a record-keeping book?", "choice", YES_NO),
            q("K.17", "record_types", "Which records are kept?", "multi", ["Farm records", "Savings and investment records", "Household income and expenditure records", "Loan and debt records", "Other"]),
            q("K.18", "record_update_frequency", "How often are records updated?", "choice", ["Never", "Daily", "Weekly", "Monthly", "Annually", "Other"]),
            q("K.19", "record_benefits", "How has record keeping helped?", "multi", ["Track income and expenses", "Improve planning and budgeting", "Make better investment decisions", "Calculate profit or loss", "Other"]),
            q("K.20", "observed_practices", "Which financial-literacy practices can be observed or confirmed?", "multi", ["Sets financial goals", "Prepares and follows a budget", "Spends within means", "Invests part of income", "Saves part of income", "Keeps records", "Other"]),
            q("K.21", "observed_count", "Number of financial-literacy practices observed", "number"),
            q("K.23", "main_gap", "Main gap observed", "textarea"),
            q("K.25", "photo_reference", "Record or photo reference", "text", help_text="Enter a filename or reference; file uploads can be added later."),
        ],
        "K.22",
        "Source guidance: 0% no adoption, 1-29% low, 30-49% medium, 50-100% high adoption.",
    ),
    "Poultry Mgt and Vaccination": topic(
        "L",
        "Poultry Production and Management",
        [
            q("L.1", "trained", "Has the farmer been trained in poultry production and management?", "choice", YES_NO),
            q("L.2", "has_poultry", "Does the farmer have poultry?", "choice", YES_NO),
            q("L.3", "breeds", "Which poultry breeds are kept?", "multi", ["Broilers", "Kuroilers", "Layers", "Local", "Other"]),
            q("L.4", "management_system", "Which poultry management system is used?", "choice", ["Battery cage", "Deep litter", "Free range", "Other"]),
            q("L.5", "brooding", "Does the farmer practise brooding?", "choice", YES_NO),
            q("L.6", "poultry_house", "Does the farmer have a poultry house?", "choice", YES_NO),
            q("L.7", "house_features", "Which poultry-house features are present?", "multi", ["Clean and dry litter", "Door", "Drinkers", "Feed troughs", "Good ventilation", "Other", "None"]),
            q("L.8", "routine_practices", "Which routine management practices are implemented?", "multi", ["Feed birds", "Provide water", "Clean poultry house", "Clean drinkers and feeders", "Change litter", "Monitor bird health", "Other", "None"]),
            q("L.9", "biosecurity", "Which biosecurity measures are enforced?", "multi", ["Footbath", "Isolate new flock", "Isolate sick birds", "Disinfect house and equipment", "Restrict visitors", "Dispose of dead birds properly", "Other", "None"]),
            q("L.10", "keeps_records", "Does the farmer keep poultry records?", "choice", YES_NO),
            q("L.11", "record_types", "Which poultry records are kept?", "multi", ["Production", "Expenditure", "Income", "Deaths", "Damage", "Vaccination schedule", "Other", "None"]),
            q("L.12", "health_practices", "Which health and vice-management practices are implemented?", "multi", ["Vaccination", "Treatment", "Deworming", "Debeaking", "Reduced stock density", "Balanced feeding", "Provide greens or occupation materials", "Other", "None"]),
            q("L.13", "observed_practices", "Which poultry practices can be observed or confirmed?", "multi", ["Rears poultry", "House is ventilated", "House is clean", "Clean feeding and watering equipment", "Functional door", "Poultry is well fed", "Water is provided", "Records are kept", "Biosecurity and health measures implemented", "Poultry is vaccinated", "Other", "None"]),
            q("L.14", "observed_count", "Number of poultry-production practices observed", "number"),
            q("L.16", "main_gap", "Main gap observed or reported", "textarea"),
            q("L.17", "immediate_advice", "Immediate advice given", "textarea"),
            q("L.19", "photo_reference", "Photo reference for poultry-management practices", "text", help_text="Enter a filename or reference; file uploads can be added later."),
        ],
        "L.15",
        "Source guidance: 0% no adoption, 1-29% low, 30-49% medium, 50-100% high adoption.",
    ),
}


UNMAPPED_SOURCE_SECTIONS = {
    "M": "Radio talk-show impact",
    "N": "Regenerative / sustainable farming practices for schools",
    "O": "Tree nursery establishment and management",
}


def questionnaire_for_topic(topic_name: str) -> dict:
    """Return an isolated copy so request-specific changes cannot mutate the source."""
    return deepcopy(FOLLOWUP_QUESTIONNAIRE.get(topic_name, {
        "source_section": "Unmapped",
        "title": topic_name,
        "questions": [
            q("temporary", "adoption_rate", "Adoption rate", "adoption_rate"),
            q("temporary", "next_action", "What should happen next?", "next_action", NEXT_ACTION_OPTIONS),
        ],
    }))
