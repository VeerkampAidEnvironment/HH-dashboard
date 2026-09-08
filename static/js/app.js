window.arfsaPackageStopTriggers = ({ rules = [], valueFor, orderFor }) => {
  const triggers = new Map();
  const setTrigger = (packages, order) => {
    (packages || []).forEach((packageKey) => {
      if (!triggers.has(packageKey) || order < triggers.get(packageKey)) triggers.set(packageKey, order);
    });
  };
  rules.forEach((rule) => {
    let triggerOrder = null;
    if (rule.operator === "count_matches_gte") {
      const matches = (rule.matches || [])
        .filter((item) => valueFor(item.question) === item.value)
        .map((item) => orderFor(item.question))
        .sort((a, b) => a - b);
      if (matches.length >= Number(rule.threshold)) triggerOrder = matches[Number(rule.threshold) - 1];
    } else {
      const value = valueFor(rule.question);
      const empty = Array.isArray(value) ? value.length === 0 : value === "" || value === null || value === undefined;
      if (empty) return;
      let triggered = false;
      if (rule.operator === "equals") triggered = value === rule.value;
      else if (rule.operator === "contains") triggered = Array.isArray(value) && value.includes(rule.value);
      else if (rule.operator === "not_in") triggered = !(rule.values || []).includes(value);
      else if (rule.operator === "crop_gate_failed") {
        const eligible = new Set(Array.isArray(value)
          ? value.filter((item) => !(rule.excluded || []).includes(item))
          : []);
        const representedGroups = Object.values(rule.groups || {})
          .filter((crops) => crops.some((crop) => eligible.has(crop))).length;
        triggered = eligible.size < Number(rule.min_crops) || representedGroups < Number(rule.min_groups);
      } else if (rule.operator === "number_gte") triggered = Number(value) >= Number(rule.threshold);
      else if (rule.operator === "number_gte_if") {
        triggered = Number(value) >= Number(rule.threshold)
          && Number(valueFor(rule.if_question)) >= Number(rule.if_threshold);
      }
      if (triggered) triggerOrder = orderFor(rule.question);
    }
    if (triggerOrder !== null && triggerOrder !== undefined) setTrigger(rule.packages, triggerOrder);
  });
  return triggers;
};

window.arfsaSetupPhotoPicker = (picker) => {
  if (!picker || picker.dataset.photoReady === "true") return;
  const input = picker.querySelector('input[type="file"]');
  const status = picker.querySelector("[data-photo-status]");
  const preview = picker.querySelector("[data-photo-preview]");
  if (!input || !status || !preview) return;
  picker.dataset.photoReady = "true";
  let selectedFiles = Array.from(input.files || []);
  let previewUrls = [];
  const fileKey = (file) => `${file.name}::${file.size}::${file.lastModified}`;
  const synchronizeInput = () => {
    const transfer = new DataTransfer();
    selectedFiles.forEach((file) => transfer.items.add(file));
    input.files = transfer.files;
    input._arfsaFiles = [...selectedFiles];
  };
  const render = (limitReached = false) => {
    previewUrls.forEach((url) => URL.revokeObjectURL(url));
    previewUrls = [];
    preview.replaceChildren();
    selectedFiles.forEach((file, index) => {
      const card = document.createElement("div");
      card.className = "survey-photo-card";
      const image = document.createElement("img");
      const url = URL.createObjectURL(file);
      previewUrls.push(url);
      image.src = url;
      image.alt = `Preview of ${file.name}`;
      image.addEventListener("error", () => card.classList.add("preview-unavailable"), { once: true });
      const name = document.createElement("span");
      name.textContent = file.name;
      name.title = file.name;
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "survey-photo-remove";
      remove.textContent = "Remove";
      remove.setAttribute("aria-label", `Remove ${file.name}`);
      remove.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        selectedFiles.splice(index, 1);
        synchronizeInput();
        render();
      });
      card.append(image, name, remove);
      preview.append(card);
    });
    if (limitReached) status.textContent = `${selectedFiles.length} photos added · maximum 6 reached`;
    else if (selectedFiles.length) status.textContent = `${selectedFiles.length} ${selectedFiles.length === 1 ? "photo" : "photos"} added · click Remove to delete`;
    else status.textContent = "No photos added · maximum 6";
  };
  input.addEventListener("change", () => {
    const combined = [...selectedFiles, ...Array.from(input.files || [])];
    const unique = combined.filter((file, index, files) => files.findIndex((candidate) => fileKey(candidate) === fileKey(file)) === index);
    const limitReached = unique.length > 6;
    selectedFiles = unique.slice(0, 6);
    synchronizeInput();
    render(limitReached || selectedFiles.length === 6);
  });
  window.addEventListener("beforeunload", () => previewUrls.forEach((url) => URL.revokeObjectURL(url)));
  render();
};

window.arfsaConfirmPackage = ({ finalPackage = false } = {}) => new Promise((resolve) => {
  const previousFocus = document.activeElement;
  const overlay = document.createElement("div");
  overlay.className = "package-confirm-overlay";
  overlay.innerHTML = `
    <section class="package-confirm-modal" role="alertdialog" aria-modal="true" aria-labelledby="package-confirm-title" aria-describedby="package-confirm-description">
      <div class="package-confirm-icon" aria-hidden="true">✓</div>
      <p class="eyebrow">Final check</p>
      <h2 id="package-confirm-title">${finalPackage ? "Finalize the last package?" : "Finalize this package?"}</h2>
      <p id="package-confirm-description">Please check the answers carefully before continuing.</p>
      <div class="package-confirm-warning"><span aria-hidden="true">!</span><strong>After finalizing, you cannot return to edit this package.</strong></div>
      <div class="package-confirm-actions">
        <button class="button button-ghost" type="button" data-package-cancel>Keep editing</button>
        <button class="button button-primary" type="button" data-package-confirm>${finalPackage ? "Finalize and calculate results" : "Finalize package and continue"}</button>
      </div>
    </section>`;
  document.body.append(overlay);
  document.body.classList.add("modal-open");
  const cancelButton = overlay.querySelector("[data-package-cancel]");
  const confirmButton = overlay.querySelector("[data-package-confirm]");
  const finish = (confirmed) => {
    document.removeEventListener("keydown", onKeydown);
    overlay.classList.remove("visible");
    document.body.classList.remove("modal-open");
    window.setTimeout(() => overlay.remove(), 160);
    previousFocus?.focus?.();
    resolve(confirmed);
  };
  const onKeydown = (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      finish(false);
    }
    if (event.key === "Tab") {
      const buttons = [cancelButton, confirmButton];
      const index = buttons.indexOf(document.activeElement);
      event.preventDefault();
      buttons[(index + (event.shiftKey ? -1 : 1) + buttons.length) % buttons.length].focus();
    }
  };
  cancelButton.addEventListener("click", () => finish(false));
  confirmButton.addEventListener("click", () => finish(true));
  overlay.addEventListener("click", (event) => { if (event.target === overlay) finish(false); });
  document.addEventListener("keydown", onKeydown);
  window.requestAnimationFrame(() => {
    overlay.classList.add("visible");
    cancelButton.focus();
  });
});

document.addEventListener("DOMContentLoaded", () => {
  const menuButton = document.querySelector("[data-menu-toggle]");
  const navigation = document.querySelector("[data-main-nav]");
  menuButton?.addEventListener("click", () => navigation?.classList.toggle("open"));
  document.querySelectorAll("[data-photo-picker]").forEach(window.arfsaSetupPhotoPicker);

  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm || "Continue?")) event.preventDefault();
    });
  });

  document.querySelectorAll("[data-select-all]").forEach((button) => {
    button.addEventListener("click", () => {
      const name = button.dataset.selectAll;
      const boxes = Array.from(document.querySelectorAll(`input[name="${CSS.escape(name)}"]`));
      const shouldCheck = boxes.some((box) => !box.checked);
      boxes.forEach((box) => { box.checked = shouldCheck; });
      button.textContent = shouldCheck ? "Clear selection" : "Select all";
    });
  });

  document.querySelectorAll("[data-adaptive-survey]").forEach((form) => {
    const questionNodes = Array.from(form.querySelectorAll("[data-survey-question]"));
    let packageStopRules = [];
    try { packageStopRules = JSON.parse(form.dataset.packageStopRules || "[]"); } catch (_error) { packageStopRules = []; }
    let refreshQuestionGuides = () => {};
    const applyMultiRules = (node, changedControl = null) => {
      const boxes = Array.from(node.querySelectorAll('input[type="checkbox"]'));
      if (!boxes.length) return;
      let exclusiveValues = [];
      try { exclusiveValues = node.dataset.exclusiveValues ? JSON.parse(node.dataset.exclusiveValues) : []; } catch (_error) { exclusiveValues = []; }
      if (changedControl?.checked) {
        if (exclusiveValues.includes(changedControl.value)) boxes.forEach((box) => { if (box !== changedControl) box.checked = false; });
        else boxes.forEach((box) => { if (exclusiveValues.includes(box.value)) box.checked = false; });
      }
      const checked = boxes.filter((box) => box.checked);
      const maximum = Number(node.dataset.maxSelections || 0);
      const exclusiveSelected = checked.some((box) => exclusiveValues.includes(box.value));
      boxes.forEach((box) => {
        box.disabled = node.hidden || (!box.checked && (exclusiveSelected || (maximum > 0 && checked.length >= maximum)));
      });
    };
    const valueFor = (questionId) => {
      const node = form.querySelector(`[data-survey-question="${CSS.escape(questionId)}"]`);
      if (!node) return "";
      const checked = Array.from(node.querySelectorAll("input[type=checkbox]:checked"));
      if (node.querySelector("input[type=checkbox]")) return checked.map((input) => input.value);
      return node.querySelector("input[type=radio]:checked")?.value
        ?? node.querySelector("select")?.value
        ?? node.querySelector("input:not([type=radio]):not([type=checkbox]), textarea")?.value?.trim()
        ?? "";
    };
    const conditionMatches = (condition) => {
      if (!condition) return true;
      const value = valueFor(condition.question);
      if (condition.operator === "equals") return value === condition.value;
      if (condition.operator === "in") return (condition.values || []).includes(value);
      if (condition.operator === "contains") return Array.isArray(value) && value.includes(condition.value);
      if (condition.operator === "contains_any") return Array.isArray(value) && value.some((item) => (condition.values || []).includes(item));
      if (condition.operator === "has_any_except") return Array.isArray(value) && value.some((item) => item !== condition.value);
      if (condition.operator === "not_empty") return Array.isArray(value) ? value.length > 0 : value !== "";
      return false;
    };
    const updateSurvey = () => {
      const packageStops = window.arfsaPackageStopTriggers({
        rules: packageStopRules,
        valueFor,
        orderFor: (questionId) => Number(form.querySelector(`[data-survey-question="${CSS.escape(questionId)}"]`)?.dataset.sectionOrder || 0),
      });
      questionNodes.forEach((node) => {
        if (node.dataset.lockedTo) {
          const lockedValue = valueFor(node.dataset.lockedTo);
          node.querySelectorAll('input[type="radio"], input[type="checkbox"]').forEach((control) => {
            control.checked = Array.isArray(lockedValue) ? lockedValue.includes(control.value) : control.value === lockedValue;
          });
          const select = node.querySelector("select");
          if (select) select.value = lockedValue;
          const input = node.querySelector('input:not([type="radio"]):not([type="checkbox"]), textarea');
          if (input) input.value = lockedValue;
        }
        let condition = null;
        let skipCondition = null;
        try { condition = node.dataset.condition ? JSON.parse(node.dataset.condition) : null; } catch (_error) { condition = null; }
        try { skipCondition = node.dataset.skipCondition ? JSON.parse(node.dataset.skipCondition) : null; } catch (_error) { skipCondition = null; }
        let packages = [];
        try { packages = node.dataset.questionPackages ? JSON.parse(node.dataset.questionPackages) : []; } catch (_error) { packages = []; }
        const stopped = packages.length > 0 && packages.every(
          (packageKey) => packageStops.has(packageKey) && packageStops.get(packageKey) < Number(node.dataset.sectionOrder || 0),
        );
        const visible = conditionMatches(condition) && !(skipCondition && conditionMatches(skipCondition)) && !stopped;
        node.hidden = !visible;
        node.querySelectorAll("input, select, textarea").forEach((control) => {
          control.disabled = !visible || Boolean(node.dataset.lockedTo);
          if (!visible) control.required = false;
          else if (node.dataset.required === "true" && control.type !== "checkbox") {
            if (control.type !== "radio" || control === node.querySelector("input[type=radio]")) control.required = true;
          }
        });
        applyMultiRules(node);
      });
      refreshQuestionGuides();
    };
    form.addEventListener("change", (event) => {
      const node = event.target.closest?.("[data-survey-question]");
      if (node && event.target.matches('input[type="checkbox"]')) applyMultiRules(node, event.target);
      updateSurvey();
    });
    form.addEventListener("input", updateSurvey);
    updateSurvey();

    form.querySelectorAll("[data-editable-profile]").forEach((container) => {
      const display = container.querySelector("[data-editable-display]");
      const label = container.querySelector("[data-editable-label]");
      const stored = container.querySelector("[data-editable-stored]");
      const editor = container.querySelector("[data-editable-editor]");
      const input = container.querySelector("[data-editable-input]");
      const openButton = container.querySelector("[data-editable-open]");
      const closeEditor = () => {
        editor.hidden = true;
        display.hidden = false;
        openButton.hidden = false;
      };
      openButton.addEventListener("click", () => {
        input.value = stored.value;
        display.hidden = true;
        openButton.hidden = true;
        editor.hidden = false;
        input.focus();
      });
      container.querySelector("[data-editable-save]").addEventListener("click", () => {
        stored.value = input.value.trim();
        label.textContent = stored.value || "Not recorded";
        closeEditor();
        stored.dispatchEvent(new Event("input", { bubbles: true }));
      });
      container.querySelector("[data-editable-cancel]").addEventListener("click", closeEditor);
      input.addEventListener("keydown", (event) => {
        if (event.key === "Enter") { event.preventDefault(); container.querySelector("[data-editable-save]").click(); }
        if (event.key === "Escape") { event.preventDefault(); closeEditor(); }
      });
    });

    form.querySelectorAll("[data-gps-tracker]").forEach((tracker) => {
      const input = tracker.querySelector('input[type="hidden"]');
      const status = tracker.querySelector("[data-gps-status]");
      const startButton = tracker.querySelector("[data-gps-start]");
      const stopButton = tracker.querySelector("[data-gps-stop]");
      const clearButton = tracker.querySelector("[data-gps-clear]");
      let watchId = null;
      let points = [];
      const distanceFromLastPoint = (point) => {
        const previous = points.at(-1);
        if (!previous) return Infinity;
        const radians = (degrees) => degrees * Math.PI / 180;
        const latitudeDelta = radians(point.latitude - previous.latitude);
        const longitudeDelta = radians(point.longitude - previous.longitude);
        const a = Math.sin(latitudeDelta / 2) ** 2
          + Math.cos(radians(previous.latitude)) * Math.cos(radians(point.latitude))
          * Math.sin(longitudeDelta / 2) ** 2;
        return 6371000 * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
      };
      const render = () => {
        tracker.dataset.tracking = watchId === null ? "false" : "true";
        startButton.hidden = watchId !== null;
        stopButton.hidden = watchId === null;
        clearButton.hidden = watchId !== null || points.length === 0;
        status.classList.toggle("captured", points.length > 0);
        status.textContent = watchId !== null
          ? `Tracking plot boundary… ${points.length} GPS ${points.length === 1 ? "point" : "points"} captured.`
          : points.length
            ? `Plot boundary saved · ${points.length} GPS ${points.length === 1 ? "point" : "points"}.`
            : "No plot boundary recorded yet.";
      };
      const stop = () => {
        if (watchId !== null) navigator.geolocation.clearWatch(watchId);
        watchId = null;
        input.value = points.length ? JSON.stringify({ points }) : "";
        render();
      };
      startButton.addEventListener("click", () => {
        if (!window.isSecureContext || !("geolocation" in navigator)) {
          status.textContent = "GPS tracking is unavailable. Use HTTPS and enable location services on this device.";
          return;
        }
        points = [];
        input.value = "";
        watchId = navigator.geolocation.watchPosition((position) => {
          if (points.length >= 5000) { stop(); return; }
          const point = {
            latitude: position.coords.latitude,
            longitude: position.coords.longitude,
            accuracy: position.coords.accuracy,
            capturedAt: new Date(position.timestamp).toISOString(),
          };
          if (distanceFromLastPoint(point) >= 4) points.push(point);
          render();
        }, (error) => {
          stop();
          status.textContent = error.code === 1
            ? "Location permission was declined. Enable it in the browser to track A7."
            : "The plot boundary could not be tracked. Check location services and try again.";
        }, { enableHighAccuracy: true, maximumAge: 0, timeout: 20000 });
        render();
      });
      stopButton.addEventListener("click", stop);
      clearButton.addEventListener("click", () => { points = []; input.value = ""; render(); });
      window.addEventListener("beforeunload", () => { if (watchId !== null) navigator.geolocation.clearWatch(watchId); });
      render();
    });

    if (form.matches("[data-survey-wizard]")) {
      const steps = Array.from(form.querySelectorAll("[data-survey-step]"));
      let currentStep = 0;
      const failQuestion = (node, message) => {
        node.closest("details")?.setAttribute("open", "");
        node.scrollIntoView({ behavior: "smooth", block: "center" });
        window.alert(message);
        node.querySelector("input:not([type=hidden]), select, textarea")?.focus();
        return false;
      };
      const validateGuideQuestion = (node) => {
        if (!node || node.hidden) return true;
        if (node.querySelector('[data-gps-tracker][data-tracking="true"]')) {
          return failQuestion(node, "Stop and save the A7 GPS tracking before continuing.");
        }
        const controls = Array.from(node.querySelectorAll("input:not([type=hidden]), select, textarea"));
        const invalid = controls.find((control) => !control.checkValidity());
        if (invalid) { invalid.reportValidity(); return false; }
        const value = valueFor(node.dataset.surveyQuestion);
        const empty = Array.isArray(value) ? value.length === 0 : value === "";
        if (node.dataset.required === "true" && empty && !node.classList.contains("question-training_list")) {
          return failQuestion(node, `Complete required item ${node.querySelector(".question-copy > span")?.textContent || "before continuing"}.`);
        }
        return true;
      };
      const validateStep = (step) => {
        updateSurvey();
        for (const node of Array.from(step.querySelectorAll("[data-survey-question]"))) {
          if (node.hidden) continue;
          if (node.querySelector('[data-gps-tracker][data-tracking="true"]')) {
            return failQuestion(node, "Stop and save the A7 GPS tracking before finalizing this section.");
          }
          const controls = Array.from(node.querySelectorAll("input:not([type=hidden]), select, textarea"));
          const invalid = controls.find((control) => !control.checkValidity());
          if (invalid) {
            invalid.reportValidity();
            return false;
          }
          const value = valueFor(node.dataset.surveyQuestion);
          const empty = Array.isArray(value) ? value.length === 0 : value === "";
          if (node.dataset.required === "true" && empty && !node.classList.contains("question-training_list")) {
            return failQuestion(node, `Complete required item ${node.querySelector(".question-copy > span")?.textContent || "in this package"}.`);
          }
        }
        const damage = Number(valueFor("b10_1_damage"));
        const severe = Number(valueFor("b10_2_severe"));
        if (step.querySelector('[data-survey-question="b10_2_severe"]:not([hidden])') && severe > damage) {
          return failQuestion(step.querySelector('[data-survey-question="b10_2_severe"]'), "B10.2 cannot be greater than the number of damaged plants in B10.1.");
        }
        const visibleBirds = Number(valueFor("f2_visible"));
        const unhealthyBirds = Number(valueFor("f2_1_unhealthy"));
        if (step.querySelector('[data-survey-question="f2_1_unhealthy"]:not([hidden])') && unhealthyBirds > visibleBirds) {
          return failQuestion(step.querySelector('[data-survey-question="f2_1_unhealthy"]'), "F2.1 cannot be greater than the number of visible birds in F2.");
        }
        return true;
      };
      steps.forEach((step, index) => {
        step.hidden = index !== 0;
        step.open = index === 0;
        const nodes = Array.from(step.querySelectorAll("[data-survey-question]"));
        const useQuestionGuide = step.dataset.surveySection !== "A";
        const packageActions = step.querySelector(".survey-step-actions");
        const guide = document.createElement("div");
        guide.className = "survey-question-guide";
        guide.innerHTML = '<button class="button button-ghost button-small" type="button" data-guide-previous>Previous question</button><span data-guide-progress></span><button class="button button-primary button-small" type="button" data-guide-next>Next question</button>';
        packageActions.before(guide);
        guide.hidden = !useQuestionGuide;
        const previousButton = guide.querySelector("[data-guide-previous]");
        const nextQuestionButton = guide.querySelector("[data-guide-next]");
        const progress = guide.querySelector("[data-guide-progress]");
        let currentQuestionId = nodes[0]?.dataset.surveyQuestion || "";
        const refreshGuide = () => {
          if (!useQuestionGuide) {
            nodes.forEach((node) => node.classList.remove("survey-guide-hidden"));
            packageActions.hidden = false;
            return;
          }
          const active = nodes.filter((node) => !node.hidden);
          const pages = active.filter((node) => !node.dataset.guideParent);
          let position = pages.findIndex((node) => node.dataset.surveyQuestion === currentQuestionId);
          if (position < 0) position = 0;
          const current = pages[position];
          if (current) currentQuestionId = current.dataset.surveyQuestion;
          nodes.forEach((node) => {
            const belongsToCurrentPage = node === current || node.dataset.guideParent === currentQuestionId;
            node.classList.toggle("survey-guide-hidden", !belongsToCurrentPage);
          });
          previousButton.hidden = position <= 0;
          nextQuestionButton.hidden = !current || position >= pages.length - 1;
          packageActions.hidden = !current || position < pages.length - 1;
          progress.textContent = current ? `Question ${position + 1} of ${pages.length}` : "No applicable questions";
        };
        previousButton.addEventListener("click", () => {
          const pages = nodes.filter((node) => !node.hidden && !node.dataset.guideParent);
          const position = pages.findIndex((node) => node.dataset.surveyQuestion === currentQuestionId);
          if (position > 0) currentQuestionId = pages[position - 1].dataset.surveyQuestion;
          refreshGuide();
        });
        nextQuestionButton.addEventListener("click", () => {
          const active = nodes.filter((node) => !node.hidden);
          const pages = active.filter((node) => !node.dataset.guideParent);
          const position = pages.findIndex((node) => node.dataset.surveyQuestion === currentQuestionId);
          const visiblePage = active.filter((node) => node === pages[position] || node.dataset.guideParent === currentQuestionId);
          if (!visiblePage.every((node) => validateGuideQuestion(node))) return;
          if (position >= 0 && position < pages.length - 1) currentQuestionId = pages[position + 1].dataset.surveyQuestion;
          refreshGuide();
        });
        step.refreshQuestionGuide = refreshGuide;
        step.querySelector("[data-survey-next]")?.addEventListener("click", async () => {
          if (!validateStep(step)) return;
          if (!await window.arfsaConfirmPackage()) return;
          step.hidden = true;
          step.open = false;
          currentStep = index + 1;
          const next = steps[currentStep];
          if (next) {
            next.hidden = false;
            next.open = true;
            next.scrollIntoView({ behavior: "smooth", block: "start" });
          }
        });
      });
      refreshQuestionGuides = () => steps.forEach((step) => step.refreshQuestionGuide?.());
      refreshQuestionGuides();
      let finalSubmitConfirmed = false;
      form.addEventListener("submit", async (event) => {
        if (finalSubmitConfirmed) return;
        event.preventDefault();
        if (!validateStep(steps[currentStep] || steps.at(-1))) {
          return;
        }
        if (!await window.arfsaConfirmPackage({ finalPackage: true })) return;
        finalSubmitConfirmed = true;
        form.requestSubmit(event.submitter || undefined);
      });
    }
  });

  document.querySelectorAll("[data-dashboard-cbf-multiselect]").forEach((control) => {
    const allBox = control.querySelector("[data-dashboard-cbf-all]");
    const boxes = Array.from(control.querySelectorAll("[data-dashboard-cbf-option]"));
    const hidden = control.querySelector("[data-dashboard-cbf-value]");
    const summary = control.querySelector("[data-dashboard-cbf-summary]");
    const update = () => {
      const selected = boxes.filter((box) => box.checked);
      const allSelected = boxes.length > 0 && selected.length === boxes.length;
      if (allBox) allBox.checked = allSelected;
      if (hidden) {
        hidden.value = allSelected
          ? ""
          : (selected.length ? selected.map((box) => box.value).join("|") : "__none__");
      }
      if (summary) summary.textContent = allSelected ? "All CBFs" : selected.length === 0
        ? "No CBFs selected" : selected.length === 1 ? selected[0].value : `${selected.length} CBFs selected`;
    };
    allBox?.addEventListener("change", () => {
      boxes.forEach((box) => { box.checked = allBox.checked; });
      update();
    });
    boxes.forEach((box) => box.addEventListener("change", update));
    update();
  });

  document.querySelectorAll("[data-dashboard-topic-multiselect]").forEach((control) => {
    const allBox = control.querySelector("[data-dashboard-topic-all]");
    const boxes = Array.from(control.querySelectorAll("[data-dashboard-topic-option]"));
    const hidden = control.querySelector("[data-dashboard-topic-value]");
    const summary = control.querySelector("[data-dashboard-topic-summary]");
    const update = () => {
      const selected = boxes.filter((box) => box.checked);
      const allSelected = boxes.length > 0 && selected.length === boxes.length;
      if (allBox) allBox.checked = allSelected;
      if (hidden) hidden.value = allSelected ? "" : selected.length
        ? selected.map((box) => box.value).join("|") : "__none__";
      if (summary) summary.textContent = allSelected ? "All training types" : selected.length === 0
        ? "No training types selected" : selected.length === 1 ? selected[0].value
          : `${selected.length} training types selected`;
    };
    allBox?.addEventListener("change", () => {
      boxes.forEach((box) => { box.checked = allBox.checked; });
      update();
    });
    boxes.forEach((box) => box.addEventListener("change", update));
    update();
  });

  document.querySelectorAll("[data-pathway-expand]").forEach((button) => {
    const panel = button.closest(".training-combination-section");
    const rows = Array.from(panel?.querySelectorAll("[data-pathway-extra][hidden]") || []);
    const allRows = Array.from(panel?.querySelectorAll("[data-pathway-extra]") || []);
    const combinationsBody = panel?.querySelector("[data-pathway-combinations]");
    const limit = Number(combinationsBody?.dataset.pathwayLimit || 12);
    button.addEventListener("click", () => {
      const expanded = button.getAttribute("aria-expanded") === "true";
      allRows.forEach((row, index) => {
        row.hidden = expanded && index >= limit;
      });
      button.setAttribute("aria-expanded", String(!expanded));
      button.textContent = expanded ? button.dataset.expandLabel : button.dataset.collapseLabel;
    });
    if (rows.length) button.setAttribute("aria-expanded", "false");
  });

  document.querySelectorAll("[data-pathway-sort]").forEach((button) => {
    const panel = button.closest(".training-combination-section");
    const body = panel?.querySelector("[data-pathway-combinations]");
    const expandButton = panel?.querySelector("[data-pathway-expand]");
    if (!body) return;
    button.addEventListener("click", () => {
      const defaultDirection = button.dataset.defaultDirection || "asc";
      const ascending = button.dataset.direction
        ? button.dataset.direction !== "asc"
        : defaultDirection === "asc";
      const sortKey = button.dataset.pathwaySortKey || "trainingCount";
      const rows = Array.from(body.querySelectorAll("[data-pathway-extra]"));
      rows.sort((left, right) => {
        const difference = Number(left.dataset[sortKey]) - Number(right.dataset[sortKey]);
        if (difference) return ascending ? difference : -difference;
        return Number(left.dataset.defaultRank) - Number(right.dataset.defaultRank);
      });
      rows.forEach((row, index) => {
        body.appendChild(row);
        row.querySelector(".combination-rank").textContent = String(index + 1);
      });
      const expanded = expandButton?.getAttribute("aria-expanded") === "true";
      const limit = Number(body.dataset.pathwayLimit || 12);
      rows.forEach((row, index) => { row.hidden = !expanded && index >= limit; });
      button.dataset.direction = ascending ? "asc" : "desc";
      button.setAttribute("aria-sort", ascending ? "ascending" : "descending");
      button.querySelector("span").textContent = ascending ? "↑" : "↓";
    });
  });

  document.querySelectorAll("[data-dashboard-group-multiselect]").forEach((control) => {
    const allBox = control.querySelector("[data-dashboard-group-all]");
    const boxes = Array.from(control.querySelectorAll("[data-dashboard-group-option]"));
    const hidden = control.querySelector("[data-dashboard-group-value]");
    const summary = control.querySelector("[data-dashboard-group-summary]");
    const sections = Array.from(control.querySelectorAll("[data-dashboard-group-section]"));
    const update = () => {
      const selected = boxes.filter((box) => box.checked);
      const allSelected = boxes.length > 0 && selected.length === boxes.length;
      if (allBox) allBox.checked = allSelected;
      sections.forEach((section) => {
        const sectionAll = section.querySelector("[data-dashboard-section-all]");
        const sectionBoxes = Array.from(section.querySelectorAll("[data-dashboard-group-option]"));
        if (sectionAll) sectionAll.checked = sectionBoxes.length > 0 && sectionBoxes.every((box) => box.checked);
      });
      if (hidden) hidden.value = allSelected ? "" : selected.length
        ? selected.map((box) => box.value).join("|") : "__none__";
      if (summary) summary.textContent = allSelected ? "All groups and schools" : selected.length === 0
        ? "No groups or schools selected" : selected.length === 1 ? selected[0].value : `${selected.length} selected`;
    };
    allBox?.addEventListener("change", () => {
      boxes.forEach((box) => { box.checked = allBox.checked; });
      update();
    });
    sections.forEach((section) => {
      const sectionAll = section.querySelector("[data-dashboard-section-all]");
      const sectionBoxes = Array.from(section.querySelectorAll("[data-dashboard-group-option]"));
      sectionAll?.addEventListener("change", () => {
        sectionBoxes.forEach((box) => { box.checked = sectionAll.checked; });
        update();
      });
    });
    boxes.forEach((box) => box.addEventListener("change", update));
    update();
  });

  document.querySelectorAll("form[data-auto-submit]").forEach((form) => {
    form.querySelectorAll("[data-submit-on-change]").forEach((control) => {
      control.addEventListener("change", () => {
        if (form.dataset.autoSubmit === "complete") {
          const cbf = form.querySelector('[name="cbf"]:checked, select[name="cbf"]');
          const mode = form.querySelector('[name="mode"]:checked, select[name="mode"]');
          if (!cbf?.value || !mode?.value) return;
        }
        if (control.required && !control.value) return;
        if (form.dataset.autoSubmit === "selection") {
          // Filter forms may contain another required field that is intentionally
          // still empty. Native form submission preserves the selected filters
          // without triggering validation intended for the final data form.
          form.submit();
          return;
        }

        form.requestSubmit();
      });
    });
  });

  document.querySelectorAll("[data-fill-date]").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll('[name="event_date"]').forEach((input) => {
        input.value = button.dataset.fillDate;
      });
      const visibleDate = button.closest("form")?.querySelector('input[type="date"][name="event_date"]');
      visibleDate?.focus();
    });
  });

  document.querySelectorAll('input[type="date"][name="event_date"]').forEach((dateInput) => {
    dateInput.addEventListener("change", () => {
      document.querySelectorAll('input[name="event_date"]').forEach((input) => {
        input.value = dateInput.value;
      });
    });
  });

  document.querySelectorAll("[data-venue-selector]").forEach((selector) => {
    const select = selector.querySelector("[data-venue-select]");
    const newVenueField = selector.querySelector("[data-new-venue-field]");
    const newVenueInput = newVenueField?.querySelector("input");
    const updateVenueMode = (focusNewVenue = false) => {
      const addingNewVenue = select?.value === "__new__";
      if (newVenueField) newVenueField.hidden = !addingNewVenue;
      if (newVenueInput) {
        newVenueInput.disabled = !addingNewVenue;
        newVenueInput.required = addingNewVenue;
        if (addingNewVenue && focusNewVenue) newVenueInput.focus();
      }
    };
    select?.addEventListener("change", () => updateVenueMode(true));
    updateVenueMode();
  });

  document.querySelectorAll("[data-demographic-explorer]").forEach((explorer) => {
    const tabs = Array.from(explorer.querySelectorAll("[data-demographic-view]"));
    const panels = Array.from(explorer.querySelectorAll("[data-demographic-panel]"));
    const selectView = (view) => {
      tabs.forEach((tab) => {
        const selected = tab.dataset.demographicView === view;
        tab.classList.toggle("active", selected);
        tab.setAttribute("aria-selected", String(selected));
        tab.tabIndex = selected ? 0 : -1;
      });
      panels.forEach((panel) => {
        panel.hidden = panel.dataset.demographicPanel !== view;
      });
    };

    tabs.forEach((tab, index) => {
      tab.addEventListener("click", () => selectView(tab.dataset.demographicView));
      tab.addEventListener("keydown", (event) => {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        let nextIndex = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : index + (event.key === 'ArrowRight' ? 1 : -1);
        nextIndex = (nextIndex + tabs.length) % tabs.length;
        tabs[nextIndex].focus();
        selectView(tabs[nextIndex].dataset.demographicView);
      });
    });
  });

  document.querySelectorAll("[data-activity-explorer]").forEach((explorer) => {
    const dataNode = explorer.querySelector("[data-activity-data]");
    const chart = explorer.querySelector("[data-activity-chart]");
    const description = explorer.querySelector("[data-activity-description]");
    const result = explorer.querySelector("[data-activity-result]");
    const activityControl = explorer.querySelector("[data-activity-type]");
    const genderControl = explorer.querySelector("[data-activity-gender]");
    const ageControl = explorer.querySelector("[data-activity-age]");
    const topicControl = explorer.querySelector("[data-activity-topic]");
    const stackControl = explorer.querySelector("[data-activity-stack-mode]");
    const monthStartControl = explorer.querySelector("[data-activity-month-start]");
    const monthEndControl = explorer.querySelector("[data-activity-month-end]");
    const legend = explorer.querySelector("[data-activity-legend]");
    const cbfControl = explorer.querySelector("[data-cbf-multiselect]");
    const cbfSummary = explorer.querySelector("[data-cbf-summary]");
    const cbfBoxes = Array.from(cbfControl?.querySelectorAll('input[type="checkbox"]') || []);
    let timeline = { months: [], events: [] };
    try { timeline = JSON.parse(dataNode?.textContent || "{}"); } catch (_error) { timeline = { months: [], events: [] }; }

    const selectedButtonValue = (control) => control?.querySelector("button.active")?.dataset.value || "all";
    const updateCbfSummary = () => {
      const selected = cbfBoxes.filter((box) => box.checked);
      if (cbfSummary) cbfSummary.textContent = !selected.length ? "All CBFs" : selected.length === 1 ? selected[0].value : `${selected.length} CBFs selected`;
    };
    const render = () => {
      const activity = selectedButtonValue(activityControl);
      const gender = selectedButtonValue(genderControl);
      const age = selectedButtonValue(ageControl);
      const topic = topicControl?.value || "all";
      const stackMode = selectedButtonValue(stackControl) || "total";
      const selectedCbfs = new Set(cbfBoxes.filter((box) => box.checked).map((box) => box.value));
      const availableMonths = timeline.months || [];
      const startMonth = monthStartControl?.value || availableMonths[0] || "";
      const endMonth = monthEndControl?.value || availableMonths[availableMonths.length - 1] || "";
      const visibleMonths = availableMonths.filter((month) => (!startMonth || month >= startMonth) && (!endMonth || month <= endMonth));
      const visibleMonthSet = new Set(visibleMonths);
      const monthFarmers = new Map(visibleMonths.map((month) => [month, new Set()]));
      const allFarmers = new Set();
      const filteredEvents = (timeline.events || []).filter((event) => {
        if (!visibleMonthSet.has(event.month)) return false;
        if (activity !== "all" && event.activity !== activity) return;
        if (gender !== "all" && event.gender !== gender) return;
        if (age !== "all" && event.age !== age) return;
        if (topic !== "all" && event.topic !== topic) return;
        if (selectedCbfs.size && !selectedCbfs.has(event.cbf)) return;
        return true;
      });
      filteredEvents.forEach((event) => {
        monthFarmers.get(event.month)?.add(event.farmer);
        allFarmers.add(event.farmer);
      });
      const categoryLabel = (event) => {
        if (stackMode === "activity") return event.activity === "ct" ? "CT" : "Follow-up";
        return event[stackMode] || "Not recorded";
      };
      let categories = [];
      const monthCategories = new Map();
      if (stackMode !== "total") {
        categories = Array.from(new Set(filteredEvents.map(categoryLabel))).sort((a, b) => a.localeCompare(b));
        if (stackMode === "activity") categories.sort((a) => a === "CT" ? -1 : 1);
        visibleMonths.forEach((month) => {
          monthCategories.set(month, new Map(categories.map((category) => [category, new Set()])));
        });
        filteredEvents.forEach((event) => monthCategories.get(event.month)?.get(categoryLabel(event))?.add(event.farmer));
      }
      const items = visibleMonths.map((month) => {
        const segments = stackMode === "total" ? [] : categories.map((category) => ({
          category, value: monthCategories.get(month)?.get(category)?.size || 0,
        }));
        return {
          label: month,
          value: stackMode === "total" ? monthFarmers.get(month)?.size || 0 : segments.reduce((sum, item) => sum + item.value, 0),
          segments,
        };
      });
      const activityLabel = activity === "ct" ? "centralized training" : activity === "fu" ? "follow-up" : "all recorded activity";
      const details = [activityLabel];
      if (topic !== "all") details.push(topic);
      if (gender !== "all") details.push(gender);
      if (age !== "all") details.push(age);
      if (selectedCbfs.size) details.push(`${selectedCbfs.size} selected ${selectedCbfs.size === 1 ? "CBF" : "CBFs"}`);
      if (visibleMonths.length && (startMonth !== availableMonths[0] || endMonth !== availableMonths[availableMonths.length - 1])) details.push(`${startMonth} to ${endMonth}`);
      if (stackMode !== "total") details.push(`stacked by ${stackMode === "topic" ? "training type" : stackMode}`);
      if (description) description.textContent = `Showing ${details.join(" · ")} by month.`;
      if (result) result.textContent = `${allFarmers.size} beneficiaries`;
      const colorFor = (index) => index < 8
        ? ["#087880", "#EFB417", "#77aa2a", "#3b82b8", "#8668b1", "#d66a4a", "#36a9a3", "#6d7d80"][index]
        : `hsl(${(174 + index * 47) % 360} 52% 46%)`;
      legend?.replaceChildren();
      if (legend) {
        legend.hidden = stackMode === "total" || !categories.length;
        categories.forEach((category, index) => {
          const item = document.createElement("span");
          const marker = document.createElement("i");
          marker.style.background = colorFor(index);
          item.append(marker, document.createTextNode(category));
          legend.append(item);
        });
      }
      chart?.replaceChildren();
      if (!chart || !items.length) {
        const empty = document.createElement("p");
        empty.className = "muted activity-empty";
        empty.textContent = "No activity is available for this selection.";
        chart?.append(empty);
        return;
      }
      const maximum = Math.max(...items.map((item) => Number(item.value) || 0), 1);
      const bars = document.createElement("div");
      bars.className = "activity-timeline-bars";
      items.forEach((item) => {
        const column = document.createElement("div");
        const value = document.createElement("span");
        const barArea = document.createElement("div");
        const label = document.createElement("small");
        const numericValue = Number(item.value) || 0;
        column.className = "activity-timeline-column";
        value.textContent = Number(item.value || 0).toLocaleString();
        label.textContent = item.label;
        if (stackMode === "total") {
          const bar = document.createElement("i");
          bar.style.height = `${numericValue ? Math.max(numericValue / maximum * 100, 5) : 0}%`;
          barArea.append(bar);
        } else {
          const stack = document.createElement("div");
          stack.className = "activity-stack";
          stack.style.height = `${numericValue ? Math.max(numericValue / maximum * 100, 5) : 0}%`;
          item.segments.forEach((segment, index) => {
            if (!segment.value) return;
            const part = document.createElement("i");
            part.style.height = `${segment.value / numericValue * 100}%`;
            part.style.background = colorFor(index);
            part.title = `${segment.category}: ${segment.value.toLocaleString()}`;
            stack.append(part);
          });
          barArea.append(stack);
        }
        column.append(value, barArea, label);
        bars.append(column);
      });
      chart.append(bars);
    };
    [activityControl, genderControl, ageControl, stackControl].forEach((control) => {
      control?.querySelectorAll("button").forEach((button) => button.addEventListener("click", () => {
        control.querySelectorAll("button").forEach((item) => item.classList.toggle("active", item === button));
        render();
      }));
    });
    topicControl?.addEventListener("change", render);
    const updateMonthRange = (changedControl) => {
      if (monthStartControl && monthEndControl && monthStartControl.value > monthEndControl.value) {
        if (changedControl === monthStartControl) monthEndControl.value = monthStartControl.value;
        else monthStartControl.value = monthEndControl.value;
      }
      render();
    };
    monthStartControl?.addEventListener("change", () => updateMonthRange(monthStartControl));
    monthEndControl?.addEventListener("change", () => updateMonthRange(monthEndControl));
    cbfBoxes.forEach((box) => box.addEventListener("change", () => { updateCbfSummary(); render(); }));
    explorer.querySelector("[data-cbf-clear]")?.addEventListener("click", () => {
      cbfBoxes.forEach((box) => { box.checked = false; });
      updateCbfSummary();
      render();
    });
    explorer.querySelector("[data-activity-reset]")?.addEventListener("click", () => {
      [activityControl, genderControl, ageControl].forEach((control) => {
        control?.querySelectorAll("button").forEach((button) => button.classList.toggle("active", button.dataset.value === "all"));
      });
      stackControl?.querySelectorAll("button").forEach((button) => button.classList.toggle("active", button.dataset.value === "total"));
      if (topicControl) topicControl.value = "all";
      if (monthStartControl) monthStartControl.selectedIndex = 0;
      if (monthEndControl) monthEndControl.selectedIndex = Math.max(0, monthEndControl.options.length - 1);
      cbfBoxes.forEach((box) => { box.checked = false; });
      updateCbfSummary();
      render();
    });
    updateCbfSummary();
    render();
  });

  document.querySelectorAll("[data-momentum-explorer]").forEach((explorer) => {
    const dataNode = explorer.querySelector("[data-momentum-data]");
    const chart = explorer.querySelector("[data-momentum-chart]");
    const tableBody = explorer.querySelector("[data-momentum-table-body]");
    const periodBadge = explorer.querySelector("[data-momentum-period]");
    const monthStartControl = explorer.querySelector("[data-momentum-month-start]");
    const monthEndControl = explorer.querySelector("[data-momentum-month-end]");
    let momentum = { months: [], series: [] };
    try { momentum = JSON.parse(dataNode?.textContent || "{}"); } catch (_error) { momentum = { months: [], series: [] }; }
    const svgNamespace = "http://www.w3.org/2000/svg";
    const svgElement = (name, attributes = {}) => {
      const element = document.createElementNS(svgNamespace, name);
      Object.entries(attributes).forEach(([key, value]) => element.setAttribute(key, String(value)));
      return element;
    };
    const scaleFor = (values) => {
      const peak = Math.max(...values.map((value) => Number(value) || 0), 0);
      if (!peak) return { peak: 0, maximum: 1, ticks: [0] };
      const rawStep = peak / 2;
      const magnitude = 10 ** Math.floor(Math.log10(rawStep || 1));
      const normalizedStep = rawStep / magnitude;
      const factor = normalizedStep <= 1 ? 1 : normalizedStep <= 2 ? 2 : normalizedStep <= 5 ? 5 : 10;
      const step = Math.max(1, Math.ceil(factor * magnitude));
      return { peak, maximum: step * 2, ticks: [0, step, step * 2] };
    };
    const renderMomentum = () => {
      const startMonth = monthStartControl?.value || momentum.months[0]?.key || "";
      const endMonth = monthEndControl?.value || momentum.months[momentum.months.length - 1]?.key || "";
      const visibleIndexes = momentum.months
        .map((month, index) => ({ month, index }))
        .filter(({ month }) => (!startMonth || month.key >= startMonth) && (!endMonth || month.key <= endMonth));
      if (periodBadge) periodBadge.textContent = visibleIndexes.length === momentum.months.length
        ? `Last ${visibleIndexes.length} months`
        : `${visibleIndexes[0]?.month.label || startMonth} to ${visibleIndexes[visibleIndexes.length - 1]?.month.label || endMonth}`;
      chart?.replaceChildren();
      const plotLeft = 40;
      const plotTop = 8;
      const plotWidth = 706;
      const plotHeight = 52;
      (momentum.series || []).forEach((series, seriesIndex) => {
        const values = visibleIndexes.map(({ index }) => Number(series.values[index]) || 0);
        const scale = scaleFor(values);
        const showMonths = seriesIndex === momentum.series.length - 1;
        const section = document.createElement("section");
        section.className = "momentum-series-row";
        const header = document.createElement("header");
        const heading = document.createElement("span");
        const marker = document.createElement("i");
        marker.style.background = series.color;
        heading.append(marker, document.createTextNode(series.label));
        const description = document.createElement("p");
        description.textContent = series.description;
        const peak = document.createElement("strong");
        peak.append(document.createTextNode(scale.peak.toLocaleString()));
        const peakLabel = document.createElement("small");
        peakLabel.textContent = "monthly peak";
        peak.append(peakLabel);
        header.append(heading, description, peak);
        const chartWrap = document.createElement("div");
        chartWrap.className = "momentum-series-chart";
        const svg = svgElement("svg", {
          viewBox: `0 0 760 ${showMonths ? 92 : 68}`,
          role: "img",
          "aria-label": `${series.label} by month, using its own y-axis from 0 to ${scale.maximum}`,
        });
        scale.ticks.forEach((tick) => {
          const y = plotTop + plotHeight - tick / scale.maximum * plotHeight;
          const group = svgElement("g", { class: "momentum-gridline" });
          group.append(svgElement("line", { x1: plotLeft, y1: y, x2: 746, y2: y }));
          const text = svgElement("text", { x: 33, y: y + 3 });
          text.textContent = tick.toLocaleString();
          group.append(text);
          svg.append(group);
        });
        const coordinates = values.map((value, index) => {
          const x = plotLeft + (values.length > 1 ? plotWidth / (values.length - 1) * index : plotWidth / 2);
          const y = plotTop + plotHeight - value / scale.maximum * plotHeight;
          return { x, y, value };
        });
        svg.append(svgElement("polyline", {
          class: "momentum-line",
          points: coordinates.map(({ x, y }) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" "),
          fill: "none",
          stroke: series.color,
        }));
        coordinates.forEach(({ x, y, value }, index) => {
          const circle = svgElement("circle", { class: "momentum-marker", cx: x, cy: y, r: 3.3, fill: series.color });
          const title = svgElement("title");
          title.textContent = `${visibleIndexes[index].month.label} · ${series.label}: ${value.toLocaleString()}`;
          circle.append(title);
          svg.append(circle);
        });
        if (showMonths) visibleIndexes.forEach(({ month }, index) => {
          const x = plotLeft + (visibleIndexes.length > 1 ? plotWidth / (visibleIndexes.length - 1) * index : plotWidth / 2);
          const text = svgElement("text", { class: "momentum-month", x, y: 84 });
          text.textContent = month.label;
          svg.append(text);
        });
        chartWrap.append(svg);
        section.append(header, chartWrap);
        chart?.append(section);
      });
      tableBody?.replaceChildren();
      visibleIndexes.forEach(({ month, index }) => {
        const row = document.createElement("tr");
        const monthCell = document.createElement("td");
        const monthLabel = document.createElement("strong");
        monthLabel.textContent = month.label;
        monthCell.append(monthLabel);
        row.append(monthCell);
        momentum.series.forEach((series) => {
          const cell = document.createElement("td");
          cell.textContent = Number(series.values[index] || 0).toLocaleString();
          row.append(cell);
        });
        tableBody?.append(row);
      });
    };
    const updateMomentumRange = (changedControl) => {
      if (monthStartControl && monthEndControl && monthStartControl.value > monthEndControl.value) {
        if (changedControl === monthStartControl) monthEndControl.value = monthStartControl.value;
        else monthStartControl.value = monthEndControl.value;
      }
      renderMomentum();
    };
    monthStartControl?.addEventListener("change", () => updateMomentumRange(monthStartControl));
    monthEndControl?.addEventListener("change", () => updateMomentumRange(monthEndControl));
    explorer.querySelector("[data-momentum-reset]")?.addEventListener("click", () => {
      if (monthStartControl) monthStartControl.selectedIndex = 0;
      if (monthEndControl) monthEndControl.selectedIndex = Math.max(0, monthEndControl.options.length - 1);
      renderMomentum();
    });
    renderMomentum();
  });

  document.querySelectorAll("select[data-enhanced-select]").forEach((select, selectIndex) => {
    const options = Array.from(select.options).map((option) => ({
      value: option.value,
      label: option.textContent.trim(),
      placeholder: !option.value,
    }));
    const selectedOption = options.find((option) => option.value === select.value && option.value);
    const wasRequired = select.required;
    const listId = `enhanced-select-${selectIndex}`;
    const container = document.createElement("div");
    const control = document.createElement("div");
    const input = document.createElement("input");
    const indicator = document.createElement("span");
    const panel = document.createElement("div");
    const results = document.createElement("div");
    const summary = document.createElement("div");

    container.className = "enhanced-select";
    control.className = "enhanced-select-control";
    input.className = "enhanced-select-input";
    input.type = "text";
    input.autocomplete = "off";
    input.spellcheck = false;
    input.placeholder = select.dataset.searchPlaceholder || "Search and select";
    input.value = selectedOption?.label || "";
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-label", select.dataset.selectLabel || "Select option");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-expanded", "false");
    input.setAttribute("aria-controls", listId);
    indicator.className = "enhanced-select-indicator";
    indicator.setAttribute("aria-hidden", "true");
    panel.className = "enhanced-select-panel";
    panel.hidden = true;
    results.className = "enhanced-select-results";
    results.id = listId;
    results.setAttribute("role", "listbox");
    summary.className = "enhanced-select-summary";

    const close = () => {
      panel.hidden = true;
      container.classList.remove("open");
      input.setAttribute("aria-expanded", "false");
    };
    const choose = (option) => {
      select.value = option.value;
      input.value = option.placeholder ? "" : option.label;
      input.setCustomValidity("");
      close();
      select.dispatchEvent(new Event("change", { bubbles: true }));
    };
    const render = (showAll = false) => {
      const query = showAll ? "" : input.value.trim().toLocaleLowerCase();
      const matches = options.filter((option) => !option.placeholder && option.label.toLocaleLowerCase().includes(query));
      results.replaceChildren();
      summary.textContent = query ? `${matches.length} matching ${matches.length === 1 ? "result" : "results"}` : `${matches.length} choices · Type to search`;
      if (!matches.length) {
        const empty = document.createElement("div");
        empty.className = "enhanced-select-empty";
        empty.textContent = "No matching options";
        results.append(empty);
        return;
      }
      matches.forEach((option) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "enhanced-select-option";
        button.setAttribute("role", "option");
        button.setAttribute("aria-selected", String(select.value === option.value));
        button.textContent = option.label;
        button.addEventListener("click", () => choose(option));
        results.append(button);
      });
    };
    const open = (showAll = false) => {
      render(showAll);
      panel.hidden = false;
      container.classList.add("open");
      input.setAttribute("aria-expanded", "true");
    };

    input.addEventListener("focus", () => {
      if (select.value) input.select();
      open(Boolean(select.value));
    });
    input.addEventListener("input", () => {
      select.value = "";
      input.setCustomValidity("");
      open();
    });
    input.addEventListener("keydown", (event) => {
      const optionButtons = Array.from(results.querySelectorAll(".enhanced-select-option"));
      if (event.key === "ArrowDown") {
        event.preventDefault();
        (optionButtons[0] || input).focus();
      } else if (event.key === "Enter" && optionButtons.length) {
        event.preventDefault();
        optionButtons[0].click();
      } else if (event.key === "Escape") {
        close();
      }
    });
    results.addEventListener("keydown", (event) => {
      const optionButtons = Array.from(results.querySelectorAll(".enhanced-select-option"));
      const currentIndex = optionButtons.indexOf(document.activeElement);
      if (event.key === "ArrowDown") {
        event.preventDefault();
        optionButtons[Math.min(currentIndex + 1, optionButtons.length - 1)]?.focus();
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        if (currentIndex <= 0) input.focus();
        else optionButtons[currentIndex - 1].focus();
      } else if ((event.key === "Enter" || event.key === " ") && currentIndex >= 0) {
        event.preventDefault();
        optionButtons[currentIndex].click();
      } else if (event.key === "Escape") {
        input.focus();
        close();
      }
    });
    select.form?.addEventListener("submit", (event) => {
      if (wasRequired && !select.value) {
        event.preventDefault();
        input.setCustomValidity("Choose an option from the list.");
        input.reportValidity();
        open();
      }
    });
    document.addEventListener("click", (event) => {
      if (!container.contains(event.target)) close();
    });

    select.required = false;
    select.tabIndex = -1;
    select.setAttribute("aria-hidden", "true");
    select.hidden = true;
    select.classList.add("select-native-enhanced");
    control.append(input, indicator);
    panel.append(summary, results);
    container.append(control, panel);
    select.insertAdjacentElement("afterend", container);
  });

  const exportableFigures = document.querySelectorAll(
    ".analytics-grid > .panel, .fh-track-grid > .panel, .interaction-grid > .panel"
  );

  const safeFileName = (value) => String(value || "dashboard-figure")
    .normalize("NFKD")
    .replace(/[^a-zA-Z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .toLowerCase() || "dashboard-figure";

  const copyLiveControlState = (source, clone) => {
    const sourceControls = source.querySelectorAll("input, select, textarea, details");
    const clonedControls = clone.querySelectorAll("input, select, textarea, details");
    sourceControls.forEach((control, index) => {
      const cloned = clonedControls[index];
      if (!cloned) return;
      if (control instanceof HTMLInputElement) {
        cloned.toggleAttribute("checked", control.checked);
        cloned.setAttribute("value", control.value);
      } else if (control instanceof HTMLTextAreaElement) {
        cloned.textContent = control.value;
      } else if (control instanceof HTMLSelectElement) {
        Array.from(cloned.options).forEach((option, optionIndex) => {
          option.toggleAttribute("selected", control.options[optionIndex]?.selected || false);
        });
      } else if (control instanceof HTMLDetailsElement) {
        cloned.toggleAttribute("open", control.open);
      }
    });
  };

  const downloadBlob = (blob, fileName) => {
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = fileName;
    document.body.append(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  };

  let figureExporterPromise;
  const ensureFigureExporter = () => {
    if (typeof window.html2canvas === "function") return Promise.resolve(window.html2canvas);
    if (figureExporterPromise) return figureExporterPromise;
    figureExporterPromise = new Promise((resolve, reject) => {
      const appScript = Array.from(document.scripts).find((script) => /\/static\/js\/app\.js(?:\?|$)/.test(script.src));
      let exporterUrl = "/static/vendor/html2canvas.min.js";
      if (appScript?.src) {
        const scriptUrl = new URL(appScript.src, window.location.href);
        scriptUrl.pathname = scriptUrl.pathname.replace(/\/js\/app\.js$/, "/vendor/html2canvas.min.js");
        exporterUrl = scriptUrl.toString();
      }
      const script = document.createElement("script");
      script.src = exporterUrl;
      script.dataset.figureExporterLoader = "";
      script.addEventListener("load", () => {
        if (typeof window.html2canvas === "function") resolve(window.html2canvas);
        else reject(new Error("The image exporter loaded without becoming available."));
      }, { once: true });
      script.addEventListener("error", () => reject(new Error("The image exporter could not be loaded.")), { once: true });
      document.head.append(script);
    }).catch((error) => {
      figureExporterPromise = undefined;
      throw error;
    });
    return figureExporterPromise;
  };

  const renderFigureToPng = async (figure) => {
    const html2canvas = await ensureFigureExporter();
    const clone = figure.cloneNode(true);
    copyLiveControlState(figure, clone);
    clone.querySelectorAll("[data-download-figure], [data-export-status]").forEach((node) => node.remove());
    clone.querySelectorAll("details:not([open])").forEach((details) => {
      Array.from(details.children).forEach((child) => {
        if (!(child instanceof HTMLElement) || child.tagName === "SUMMARY") return;
        child.style.display = "none";
      });
    });
    clone.classList.add("figure-export-copy");
    clone.style.margin = "0";
    clone.querySelectorAll(".table-wrap, .training-heatmap-wrap").forEach((node) => {
      node.style.overflow = "visible";
      node.style.maxHeight = "none";
    });

    const candidates = [figure, ...figure.querySelectorAll("table, svg, .table-wrap, .training-heatmap-wrap")];
    const contentWidth = Math.max(...candidates.map((node) => node.scrollWidth || 0));
    const width = Math.ceil(Math.min(2400, Math.max(figure.getBoundingClientRect().width, contentWidth)));
    const stage = document.createElement("div");
    stage.className = "figure-export-stage";
    stage.style.width = `${width}px`;
    clone.style.width = `${width}px`;
    stage.append(clone);
    document.body.append(stage);
    const height = Math.ceil(clone.scrollHeight);
    const scale = Math.max(0.75, Math.min(2, 8192 / width, 8192 / Math.max(height, 1)));
    try {
      const canvas = await html2canvas(clone, {
        backgroundColor: "#ffffff",
        logging: false,
        scale,
        useCORS: true,
        width,
        height,
        windowWidth: width,
        windowHeight: height,
        scrollX: 0,
        scrollY: 0,
      });
      return await new Promise((resolve, reject) => canvas.toBlob(
        (blob) => blob ? resolve(blob) : reject(new Error("The browser could not create the image.")),
        "image/png"
      ));
    } finally {
      stage.remove();
    }
  };

  exportableFigures.forEach((figure) => {
    const title = figure.querySelector("h2, h3")?.textContent?.trim() || "Dashboard figure";
    figure.dataset.figureExport = "";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "figure-download-button";
    button.dataset.downloadFigure = "";
    button.setAttribute("aria-label", `Download ${title} as an image`);
    button.innerHTML = '<span aria-hidden="true">&#8595;</span><span>Download</span>';
    const buttonLabel = button.lastElementChild;
    const status = document.createElement("span");
    status.className = "figure-export-status";
    status.dataset.exportStatus = "";
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    figure.append(button, status);

    button.addEventListener("click", async () => {
      button.disabled = true;
      button.classList.add("loading");
      if (buttonLabel) buttonLabel.textContent = "Preparing...";
      status.textContent = "Preparing image...";
      try {
        const blob = await renderFigureToPng(figure);
        const date = new Date().toISOString().slice(0, 10);
        downloadBlob(blob, `${safeFileName(title)}-${date}.png`);
        if (buttonLabel) buttonLabel.textContent = "Downloaded";
        status.textContent = "Downloaded";
      } catch (error) {
        console.error("Figure download failed", error);
        if (buttonLabel) buttonLabel.textContent = "Try again";
        status.textContent = "Download failed. Please try again.";
      } finally {
        button.disabled = false;
        button.classList.remove("loading");
        window.setTimeout(() => {
          status.textContent = "";
          if (buttonLabel) buttonLabel.textContent = "Download";
        }, 3000);
      }
    });
  });
});
