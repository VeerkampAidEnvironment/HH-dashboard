(() => {
  "use strict";

  // Browser equivalent of followup_survey.score_survey. Score tables, question
  // gates and wording come from the server; parity tests cover both engines.
  const valueOf = (answers, key, fallback = "") => {
    const value = Object.hasOwn(answers, key) ? answers[key] : fallback;
    return typeof value === "string" ? value.trim() : value;
  };
  const numberOf = (answers, key) => Number(valueOf(answers, key, 0)) || 0;
  const itemsOf = (value) => Array.isArray(value) ? value : value ? [value] : [];
  const hasAny = (value, choices) => itemsOf(value).some((item) => choices.includes(item));
  const hasExcept = (value, excluded) => itemsOf(value).some((item) => !excluded.includes(item));
  const empty = (value) => value == null || value === "" || (Array.isArray(value) && !value.length);

  // Match Python's one-decimal rounding, including half-to-even and the exact
  // binary value at boundaries, rather than JavaScript's half-up Math.round.
  const round1 = (value) => {
    if (!value) return 0;
    const bytes = new DataView(new ArrayBuffer(8));
    bytes.setFloat64(0, value);
    const bits = bytes.getBigUint64(0);
    const exponent = Number((bits >> 52n) & 2047n) - 1023 - 52;
    let numerator = ((bits & ((1n << 52n) - 1n)) | (1n << 52n)) * 10n;
    const denominator = exponent < 0 ? 1n << BigInt(-exponent) : 1n;
    if (exponent > 0) numerator <<= BigInt(exponent);
    let rounded = numerator / denominator;
    const remainder = (numerator % denominator) * 2n;
    if (remainder > denominator || (remainder === denominator && rounded % 2n)) rounded++;
    return Number(rounded) / 10;
  };
  const meanOf = (values) => {
    // statistics.mean sums the exact binary fractions before converting back
    // to a float. Doing the same avoids a 0.1 difference in displayed Depth.
    const fractions = values.filter(Boolean).map((value) => {
      const bytes = new DataView(new ArrayBuffer(8));
      bytes.setFloat64(0, value);
      const bits = bytes.getBigUint64(0);
      return { mantissa: (bits & ((1n << 52n) - 1n)) | (1n << 52n), exponent: Number((bits >> 52n) & 2047n) - 1023 - 52 };
    });
    if (!fractions.length) return 0;
    const commonExponent = Math.min(...fractions.map((item) => item.exponent));
    let numerator = fractions.reduce((sum, item) => sum + (item.mantissa << BigInt(item.exponent - commonExponent)), 0n);
    let denominator = BigInt(values.length);
    if (commonExponent < 0) denominator <<= BigInt(-commonExponent);
    else numerator <<= BigInt(commonExponent);
    let exponent = numerator.toString(2).length - denominator.toString(2).length;
    if (exponent >= 0 ? numerator < (denominator << BigInt(exponent)) : (numerator << BigInt(-exponent)) < denominator) exponent--;
    const scaled = numerator << BigInt(52 - exponent);
    let mantissa = scaled / denominator;
    const remainder = (scaled % denominator) * 2n;
    if (remainder > denominator || (remainder === denominator && mantissa % 2n)) mantissa++;
    return Number(mantissa) * 2 ** (exponent - 52);
  };
  const resultOf = (points, available, critical) => {
    const score = critical ? 0 : round1(available ? points / available * 100 : 0);
    return { points_earned: points, points_available: available, score,
      status: critical ? "Failed" : score >= 50 ? "Achieved" : "Partial", critical_failed: critical };
  };
  const nextAction = (result) => result.critical_failed || result.score < 30 ? "ct"
    : result.score < 50 ? "followup_3" : result.score < 70 ? "followup_1" : "none";

  const conditionMatches = (condition, answers) => {
    const value = valueOf(answers, condition.question);
    switch (condition.operator || "equals") {
      case "equals": return value === condition.value;
      case "in": return (condition.values || []).includes(value);
      case "contains": return Array.isArray(value) && value.includes(condition.value);
      case "contains_any": return Array.isArray(value) && hasAny(value, condition.values || []);
      case "has_any_except": return Array.isArray(value) && hasExcept(value, [condition.value]);
      case "not_empty": return !empty(value);
      default: return false;
    }
  };
  const isActive = (question, answers, stops) => {
    if (question.skip_condition && conditionMatches(question.skip_condition, answers)) return false;
    if (question.condition && !conditionMatches(question.condition, answers)) return false;
    return !(question.packages?.length && question.packages.every((key) =>
      stops.has(key) && stops.get(key) < question.section_order));
  };

  const scoreBItem = (id, packageKey, answers, rules) => {
    const get = (key) => valueOf(answers, key);
    const value = get(id);
    const number = numberOf(answers, id);
    const item = (points, critical = false) => ({ points: points ?? null, critical });
    switch (id) {
      case "b1_food_groups": {
        const crops = [...new Set(itemsOf(value))].filter((crop) => !rules.cash_only_crops.includes(crop));
        const groups = Object.values(rules.crop_groups).filter((group) => hasAny(crops, group)).length;
        const passed = crops.length >= 3 && groups >= 2;
        return item(passed ? 2 : 0, !passed);
      }
      case "b2_cover": return item(["mulch", "living"].includes(value) ? 2 : 0);
      case "b2_detail": return item(get("b2_cover") === "mulch"
        ? ({ full: 2, over_80: 2, over_50: 1, under_50: 0 })[get("b2_1_mulch")]
        : get("b2_cover") === "living" ? ({ dense: 2, patchy: 1, sparse: 0 })[get("b2_1_living")] : null);
      case "b4_structures": {
        const present = hasExcept(value, ["none"]);
        return item(present ? 2 : 0, !present);
      }
      case "b4_1_width": return item(hasExcept(get("b4_structures"), ["none"])
        ? ({ full: 2, most: 1, half: 0, less_than_half: 0, partial: 1, single: 0 })[value] : null);
      case "b4_2_contour": return item(hasExcept(get("b4_structures"), ["none"]) ? (value === "yes" ? 2 : 0) : null);
      case "b5_preparation": return item(({ fully_tilled: 0, minimum: 1, holes: 2, undisturbed: 2 })[value],
        packageKey === "reduced_disturbance" && value === "fully_tilled");
      case "b5_1_old_marks": return item(["minimum", "holes", "undisturbed"].includes(get("b5_preparation"))
        ? ({ none: 2, visible: 1, new_plot: null })[value] : null);
      case "b5_2_extent": return item(["minimum", "holes", "undisturbed"].includes(get("b5_preparation"))
        ? ({ whole: 2, most: 1, half: 0, less_than_half: 0, part: 0, varies: 0 })[value] : null);
      case "b6_weeds": return item(({ spot: 2, few_full: 1, frequent_full: 0, herbicide: 0 })[value]);
      case "b7_arrangement": return item(value === "mixed" ? 2 : 0);
      case "b8_trees": return item((Array.isArray(value) ? hasExcept(value, ["none"]) : value === "yes") ? 2 : 0);
      case "b8_1_under": {
        const trees = get("b8_trees");
        if (Array.isArray(trees)) return item(trees.includes("none") ? null : trees.includes("underplanting") ? 2 : 1);
        return item(trees !== "yes" ? null : value === "yes" ? 2 : 1);
      }
      case "b8_2_arrangement": {
        const trees = get("b8_trees");
        if (Array.isArray(trees) ? trees.includes("none") : trees !== "yes") return item(null);
        return item(hasAny(Array.isArray(trees) ? trees : value, ["boundary", "intercropping", "woodlot"]) ? 2 : 0);
      }
      case "b9_traces":
        if (packageKey === "soil_cover_fertility") return item(["identified", "unidentified"].includes(value) ? 2 : 0);
        return item(value === "identified" ? (get("b9_differs") === "yes" ? 2 : 1) : value === "unidentified" ? 1 : 0);
      case "b9_1_farmer": return item(value === "yes" ? 2 : 0);
      case "b10_pest_method": return item(["biological", "both"].includes(value) ? 2 : 0, !["biological", "both"].includes(value));
      case "b10_1_damage": return ["biological", "both"].includes(get("b10_pest_method"))
        ? item(number <= 2 ? 2 : number <= 5 ? 1 : 0, number >= 6) : item(null);
      case "b10_2_severe": return ["biological", "both"].includes(get("b10_pest_method"))
        ? item(number <= 1 ? 2 : number === 2 ? 1 : 0, number >= 3) : item(null);
      case "b10_3_chemical_change": return item(["biological", "both"].includes(get("b10_pest_method"))
        ? ({ increased: 0, same: 2, reduced: 2, never_used: null, no_prior_biopesticide: null })[value] : null);
      case "b11_inputs": return item(hasAny(value, ["manure", "heap", "bio"]) ? 2 : 0);
      case "b11_1_location": return item(hasAny(get("b11_inputs"), ["manure"])
        ? ({ whole: 2, most: 2, half: 1, section: 0, holes: 2 })[value] : null);
      case "b11_2_method": return item(hasAny(get("b11_inputs"), ["manure", "bio"]) ? (value === "none" ? 0 : 2) : null);
      case "b12_macrofauna": return item(number === 0 ? 0 : number <= 4 ? 1 : 2);
      default: throw new Error(`Unknown scoring item: ${id}`);
    }
  };

  const scoreTraining = (topic, answers, rules, active) => {
    let points = 0, available = 0, critical = false;
    for (const [id, scoring, criticalValues] of rules.section_items[topic]) {
      if (!active.has(id)) continue;
      const value = valueOf(answers, id);
      const number = numberOf(answers, id);
      let earned;
      switch (scoring) {
        case "d4_count": earned = number <= 3 ? 2 : number <= 6 ? 1 : 0; break;
        case "multi_any": earned = !empty(value) ? 2 : 0; break;
        case "multi_presence": {
          const selected = itemsOf(value);
          earned = hasExcept(selected, ["none"]) ? 2 : 0;
          critical ||= selected.includes("none");
          break;
        }
        case "positive": earned = number > 0 ? 2 : 0; break;
        case "poultry_structure": earned = itemsOf(value).length > 2 ? 2 : itemsOf(value).length === 1 ? 1 : 0; break;
        case "poultry_health": {
          const inspected = Math.max(0, numberOf(answers, "f2_visible"));
          if (inspected >= 5) { earned = number <= 1 ? 2 : number <= 3 ? 1 : 0; critical ||= number >= 4; }
          else earned = inspected === 0 || number > inspected / 2 ? 0 : 2;
          break;
        }
        default: earned = scoring[value];
      }
      if (earned == null) continue;
      points += earned;
      available += 2;
      critical ||= !Array.isArray(value) && criticalValues.includes(value);
    }
    return resultOf(points, available, critical);
  };

  const failureComments = (answers, rules, active, hasB) => {
    const get = (key) => valueOf(answers, key);
    const num = (key) => numberOf(answers, key);
    const crops = [...new Set(itemsOf(get("b1_food_groups")))].filter((crop) => !rules.cash_only_crops.includes(crop));
    const trees = itemsOf(get("b8_trees"));
    const noTrees = !hasExcept(trees, ["none", "no"]);
    const severe = [get("b3_1_rills") === "deep", get("b3_2_roots") === "widespread", get("b3_3_soil") === "ridge"].filter(Boolean).length >= 2;
    const triggers = {
      b1_food_groups: crops.length < 3 || Object.values(rules.crop_groups).filter((group) => hasAny(crops, group)).length < 2,
      b5_preparation: get("b5_preparation") === "fully_tilled",
      b5_2_extent: ["half", "less_than_half", "part", "varies"].includes(get("b5_2_extent")),
      b2_cover: ["bare", "other"].includes(get("b2_cover")), b2_1_mulch: get("b2_1_mulch") === "under_50",
      b3_features: severe, b4_structures: !hasExcept(get("b4_structures"), ["none"]),
      b4_1_width: ["half", "less_than_half", "single"].includes(get("b4_1_width")),
      b4_2_contour: ["no", "unsure"].includes(get("b4_2_contour")),
      b6_weeds: ["frequent_full", "herbicide"].includes(get("b6_weeds")),
      b7_arrangement: ["single", "other"].includes(get("b7_arrangement")),
      b9_traces: get("b9_traces") === "none", b9_1_farmer: ["no", "unsure"].includes(get("b9_1_farmer")),
      b10_pest_method: ["synthetic", "none"].includes(get("b10_pest_method")),
      b10_1_damage: num("b10_1_damage") >= 6, b10_2_severe: num("b10_2_severe") >= 3,
      b10_3_chemical_change: get("b10_3_chemical_change") === "increased",
      b11_inputs: !hasAny(get("b11_inputs"), ["manure", "heap", "bio"]),
      b11_2_method: get("b11_2_method") === "none", b12_macrofauna: num("b12_macrofauna") === 0,
      c1_map_drawn: get("c1_map_drawn") === "no", c2_current_map: get("c2_current_map") === "no",
      c3_storage: get("c3_storage") === "not_shown", c4_use: get("c4_use") === "none",
      d2_garden_cover: get("d2_garden_cover") === "mostly_bare", d3_weeds: get("d3_weeds") === "most",
      d4_unhealthy: num("d4_unhealthy") >= 7, d5_structures: itemsOf(get("d5_structures")).includes("none"),
      d6_seed_storage: ["claimed", "no"].includes(get("d6_seed_storage")),
      e1_budget: ["claimed", "none"].includes(get("e1_budget")), e1_frequency: get("e1_frequency") === "rare",
      e1_income_change: get("e1_income_change") === "decrease",
      e2_decisions: ["not_involved", "respondent", "spouse"].includes(get("e2_decisions")),
      e3_within_means: get("e3_within_means") === "no", e4_invested: get("e4_invested") === "no", e5_savings: num("e5_savings") <= 0,
      f1_location: get("f1_location") === "free", f1_1_structure: itemsOf(get("f1_1_structure")).length === 0,
      f2_1_unhealthy: num("f2_visible") >= 5 ? num("f2_1_unhealthy") >= 4 : num("f2_visible") > 0 && num("f2_1_unhealthy") > num("f2_visible") / 2,
      f3_isolation: get("f3_isolation") === "mixed", f4_records: get("f4_records") === "none", f5_vaccination: get("f5_vaccination") === "none",
    };
    let treeComment = 0;
    return rules.failure_comments.filter((entry) => {
      let triggered = triggers[entry.question_id];
      // B8 has separate findings for absent trees and an unsuitable arrangement.
      if (entry.question_id === "b8_trees") triggered = treeComment++ === 0 ? noTrees : !noTrees && !hasAny(trees, ["boundary", "intercropping", "woodlot"]);
      return triggered && active.has(entry.question_id) && (hasB || !entry.question_id.startsWith("b"));
    });
  };

  window.arfsaScoreFollowup = (submittedAnswers, requestedTopics, rules = window.ARFSA_FOLLOWUP_RULES) => {
    if (!rules) throw new Error("Offline scoring rules are unavailable. Update the app while online.");
    const topics = rules.topics.filter((topic) => requestedTopics.includes(topic));
    const answers = { ...submittedAnswers };
    // Match the server's compatibility handling for older prepared tablets.
    answers.b12_macrofauna = ({ zero: 0, one_four: 1, five_plus: 5 })[answers.b12_macrofauna] ?? answers.b12_macrofauna;
    answers.f2_visible = ({ five_plus: 5, under_five: answers.f2_1_total || 1 })[answers.f2_visible] ?? answers.f2_visible;
    rules.questions.forEach((question) => { if (question.locked_to) answers[question.id] = valueOf(answers, question.locked_to); });
    const stops = window.arfsaPackageStopTriggers({ rules: rules.stop_rules,
      valueFor: (id) => valueOf(answers, id), orderFor: (id) => rules.question_order[id] });
    const hasB = topics.some((topic) => rules.section_b_topics.includes(topic));
    const selectedSections = new Set(topics.flatMap((topic) => Object.keys(rules.section_items).includes(topic)
      ? rules.section_items[topic].map(([id]) => id.slice(0, 1)) : ["b"]));
    const active = new Set(rules.questions.filter((question) =>
      selectedSections.has(question.id.slice(0, 1)) && isActive(question, answers, stops)).map((question) => question.id));
    const packages = {};
    if (hasB) {
      const severe = [valueOf(answers, "b3_1_rills") === "deep", valueOf(answers, "b3_2_roots") === "widespread", valueOf(answers, "b3_3_soil") === "ridge"].filter(Boolean).length >= 2;
      for (const [key, itemIds] of Object.entries(rules.package_items)) {
        let points = 0, available = 0, critical = key === "erosion_water" && severe;
        if (valueOf(answers, "b5_preparation") === "fully_tilled" && ["reduced_disturbance", "erosion_water", "soil_cover_fertility"].includes(key)) {
          available = 2; critical = true;
        } else {
          for (const id of itemIds) {
            const item = scoreBItem(id, key, answers, rules);
            if (item.points !== null) { points += item.points; available += 2; }
            critical ||= item.critical;
          }
        }
        const result = { key, title: rules.package_titles[key], ...resultOf(points, available, critical), recommendations: rules.package_recommendations[key] };
        result.next_action = nextAction(result);
        result.recommendation = result.next_action === "ct" ? `New centralized training: ${result.recommendations.join(", ")}` : rules.followup_labels[result.next_action];
        packages[key] = result;
      }
    }
    let rvo = null, breadth = null, projectPassed = null, household = null;
    if (hasB) {
      const get = (id) => valueOf(answers, id);
      const practices = {
        "Mulching / soil cover": ["mulch", "living"].includes(get("b2_cover")),
        "Soil & water conservation structures": hasExcept(get("b4_structures"), ["none"]),
        "Conservation / minimum tillage": ["minimum", "holes", "undisturbed"].includes(get("b5_preparation")),
        "Intercropping": get("b7_arrangement") === "mixed",
        "Agroforestry": Array.isArray(get("b8_trees")) ? hasExcept(get("b8_trees"), ["none"]) : get("b8_trees") === "yes",
        "Crop rotation": get("b9_traces") === "identified" && get("b9_differs") === "yes",
        "Organic manure / bio-fertilizer": hasAny(get("b11_inputs"), ["manure", "heap", "bio"]),
        "Integrated pest management": ["biological", "both"].includes(get("b10_pest_method")),
      };
      const count = Object.values(practices).filter(Boolean).length;
      rvo = { passed: count >= 2, count, total: 8, practices };
      breadth = { achieved: Object.values(packages).filter((item) => item.status === "Achieved").length, total: Object.keys(packages).length };
      projectPassed = breadth.achieved >= 2;
      household = !rvo.passed
        ? { code: "D", label: "Below both bars", action: "Priority household. Apply the score-based next action for each training type." }
        : !projectPassed ? { code: "C", label: "RVO-Compliant", action: "Practices are present but shallow. Apply the score-based next action for each training type." }
        : breadth.achieved < breadth.total ? { code: "B", label: "Compliant, gaps remain", action: "Apply the score-based next action for each training type." }
        : { code: "A", label: "Strong adopter", action: "No training needed." };
    }
    const trainings = {};
    for (const topic of topics) {
      const components = (rules.training_packages[topic] || []).map((key) => packages[key]);
      const result = components.length ? resultOf(components.reduce((sum, item) => sum + item.points_earned, 0),
        components.reduce((sum, item) => sum + item.points_available, 0), components.some((item) => item.critical_failed))
        : scoreTraining(topic, answers, rules, active);
      result.next_action = nextAction(result);
      const recommendations = [...new Set(components.filter((item) => item.next_action === "ct").flatMap((item) => item.recommendations))].sort();
      result.recommendation = result.next_action === "ct" ? `New centralized training: ${(recommendations.length ? recommendations : [topic]).join(", ")}` : rules.followup_labels[result.next_action];
      result.next_action_label = rules.followup_labels[result.next_action];
      trainings[topic] = result;
    }
    const scores = Object.values(trainings).map((item) => item.score);
    return { version: rules.version, rvo, project_passed: projectPassed, household_outcome: household, breadth,
      depth: scores.length ? round1(meanOf(scores)) : null,
      packages, trainings, failure_comments: failureComments(answers, rules, active, hasB),
      centralized_training_recommendations: [...new Set(Object.values(packages).filter((item) => item.next_action === "ct").flatMap((item) => item.recommendations))].sort() };
  };
})();
