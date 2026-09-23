(() => {
  "use strict";

  const root = document.querySelector("[data-field-app]");
  const configNode = document.getElementById("field-app-config");
  if (!root || !configNode) return;

  const config = JSON.parse(configNode.textContent);
  const DB_NAME = "arfsa-ae-field-app";
  const DB_VERSION = 3;
  const environment = config.environment || "live";
  let fieldPackage = null;
  let currentCbf = config.assignedCbf || "";
  let outbox = [];
  let savedResults = [];
  let selectedResultId = null;
  let selectedFarmerId = null;
  let capturedLocation = null;
  let syncing = false;
  const sendingIds = new Set();
  let preparing = false;
  let saving = false;
  let storageReady = false;
  let focusedVisit = false;
  const unfinishedForms = new Set();

  const $ = (selector, scope = root) => scope.querySelector(selector);
  const $$ = (selector, scope = root) => Array.from(scope.querySelectorAll(selector));
  const create = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const localDate = () => {
    const now = new Date();
    const offset = now.getTimezoneOffset() * 60000;
    return new Date(now.getTime() - offset).toISOString().slice(0, 10);
  };
  const formatDateTime = (value) => value
    ? new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value))
    : "Not prepared";
  const submissionId = () => globalThis.crypto?.randomUUID?.()
    || `field-${Date.now()}-${Math.random().toString(16).slice(2)}`;

  const dbPromise = new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains("packages")) db.createObjectStore("packages", { keyPath: "cbf" });
      if (!db.objectStoreNames.contains("outbox")) db.createObjectStore("outbox", { keyPath: "id" });
      if (!db.objectStoreNames.contains("meta")) db.createObjectStore("meta", { keyPath: "key" });
      if (!db.objectStoreNames.contains("packagesV2")) db.createObjectStore("packagesV2", { keyPath: "key" });
      if (!db.objectStoreNames.contains("followupResults")) db.createObjectStore("followupResults", { keyPath: "id" });
    };
    request.onsuccess = () => {
      request.result.onversionchange = () => request.result.close();
      resolve(request.result);
    };
    request.onerror = () => reject(request.error);
    request.onblocked = () => alertUser("Close other Field App windows so this tablet can finish updating its offline storage.", "warning");
  });

  const dbRequest = async (storeName, mode, operation) => {
    const db = await dbPromise;
    return new Promise((resolve, reject) => {
      const transaction = db.transaction(storeName, mode);
      const store = transaction.objectStore(storeName);
      const request = operation(store);
      // A successful request can still be rolled back. Only report a save after
      // the transaction commits, so a restart cannot interrupt a pending write.
      transaction.oncomplete = () => resolve(request.result);
      transaction.onabort = () => reject(transaction.error || new Error("Offline storage transaction was interrupted."));
      request.onerror = () => reject(request.error);
    });
  };
  const getStored = (store, key) => dbRequest(store, "readonly", (target) => target.get(key));
  const getAllStored = (store) => dbRequest(store, "readonly", (target) => target.getAll());
  const putStored = (store, value) => dbRequest(store, "readwrite", (target) => target.put(value));
  const packageKey = (cbf) => `${environment}::${cbf}`;
  const metaKey = (key) => `${environment}::${key}`;

  const alertUser = (message, tone = "info") => {
    const alert = $("[data-field-alert]");
    alert.textContent = message;
    alert.className = `field-alert field-alert-${tone}`;
    alert.hidden = false;
    clearTimeout(alertUser.timer);
    alertUser.timer = setTimeout(() => { alert.hidden = true; }, 8000);
  };

  const updateConnection = () => {
    const status = $("[data-connection-status]");
    const online = navigator.onLine;
    status.classList.toggle("offline", !online);
    $("span", status).textContent = online ? "Internet available" : "Offline · save on this tablet";
    refreshCounts();
    renderOutbox();
    if (savedResults.length) renderResults();
  };

  const updateLocationStatus = () => {
    const status = $("[data-location-status]");
    const captureButton = $("[data-capture-location]");
    const clearButton = $("[data-clear-location]");
    if (!status || !captureButton || !clearButton) return;
    if (!capturedLocation) {
      status.textContent = "Optional — add the device's current GPS location to this follow-up.";
      status.classList.remove("captured");
      captureButton.textContent = "Add current location";
      clearButton.hidden = true;
      return;
    }
    const accuracy = Math.round(capturedLocation.accuracy);
    status.textContent = `Location added · accuracy approximately ${accuracy} m`;
    status.classList.add("captured");
    captureButton.textContent = "Update location";
    clearButton.hidden = false;
  };

  const captureCurrentLocation = () => {
    if (!window.isSecureContext) {
      alertUser("Location is available only when the app uses HTTPS or localhost.", "warning");
      return;
    }
    if (!("geolocation" in navigator)) {
      alertUser("This device or browser does not provide location services.", "warning");
      return;
    }
    const button = $("[data-capture-location]");
    button.disabled = true;
    button.textContent = "Finding location…";
    navigator.geolocation.getCurrentPosition(
      (position) => {
        capturedLocation = {
          latitude: position.coords.latitude,
          longitude: position.coords.longitude,
          accuracy: position.coords.accuracy,
          capturedAt: new Date(position.timestamp).toISOString(),
        };
        unfinishedForms.add($("[data-followup-form]"));
        button.disabled = false;
        updateLocationStatus();
        alertUser("Current location added to this follow-up.", "success");
      },
      (error) => {
        button.disabled = false;
        updateLocationStatus();
        const messages = {
          1: "Location permission was declined. You can still save the follow-up without it.",
          2: "The device could not determine its location. You can try again or continue without it.",
          3: "Finding the location took too long. You can try again or continue without it.",
        };
        alertUser(messages[error.code] || "The current location could not be added.", "warning");
      },
      { enableHighAccuracy: true, timeout: 15000, maximumAge: 60000 },
    );
  };

  const currentOutbox = () => outbox.filter((item) => item.cbf === currentCbf && (item.environment || "live") === environment);
  const queuedCtKeys = () => new Set(currentOutbox()
    .filter((item) => item.type === "centralized")
    .flatMap((item) => item.entries.map((entry) => `${entry.recordId}::${entry.topic}`)));
  const matchingHouseholdTopics = (recordId, topics) => {
    const candidate = (fieldPackage?.householdCandidates || fieldPackage?.farmers || [])
      .find((person) => String(person.id) === String(recordId));
    const dueTopics = candidate?.dueTopics || (candidate?.followupTopics || []).map((item) => item.topic);
    return topics.filter((topic) => dueTopics.includes(topic));
  };
  const queuedFuKeys = () => new Set(currentOutbox()
    .filter((item) => item.type === "followup")
    .flatMap((item) => {
      const topics = item.topics || (item.responses || []).map((entry) => entry.topic);
      const sharedIds = [...(item.sharedRecordIds || []), item.sharedRecordId].filter(Boolean);
      return [
        ...topics.map((topic) => `${item.recordId}::${topic}`),
        ...sharedIds.flatMap((recordId) => matchingHouseholdTopics(recordId, topics)
          .map((topic) => `${recordId}::${topic}`)),
      ];
    }));

  const refreshCounts = () => {
    const items = currentOutbox();
    $$('[data-summary-pending], [data-pending-badge]').forEach((node) => { node.textContent = items.length; });
    $("[data-summary-pending-label]").textContent = "saved here · server not confirmed";
    const summary = $("[data-transfer-summary]");
    summary.hidden = !fieldPackage;
    const errors = items.filter((item) => item.status === "error").length;
    const failed = items.some((item) => item.syncError);
    summary.dataset.state = errors || failed ? "attention" : items.length ? "pending" : "complete";
    $("[data-transfer-title]").textContent = items.length
      ? `${items.length} saved ${items.length === 1 ? "entry still needs" : "entries still need"} to reach the server`
      : "No saved entries waiting to send";
    $("[data-transfer-detail]").textContent = syncing
      ? "Sending saved entries… Keep the app open until the server confirms them."
      : errors ? `${errors} ${errors === 1 ? "entry needs" : "entries need"} attention. Open Pending to see the server message. Your saved entries are still on this tablet.`
      : failed ? "The last upload was not confirmed. Your entries are saved on this tablet. Check your connection and try Synchronize now."
      : items.length ? "You can close the app and continue your visits. Back at the office, connect to the internet and open this app to send them."
      : `For ${currentCbf || "the selected CBF"}${environment === "test" ? " in testing mode" : ""}. Follow-up save receipts remain in Results.`;
    $("[data-transfer-review]").hidden = !items.length;
    $$('[data-sync-now]').forEach((button) => {
      button.disabled = syncing || !navigator.onLine || !items.length;
      button.textContent = syncing ? "Sending to server…" : "Synchronize now";
    });
  };

  const openPane = (name) => {
    if (focusedVisit && name !== "followup" && name !== "results") return;
    $$('[data-field-tab]').forEach((button) => button.classList.toggle("active", button.dataset.fieldTab === name));
    $$('[data-field-pane]').forEach((pane) => {
      const active = pane.dataset.fieldPane === name;
      pane.classList.toggle("active", active);
      pane.hidden = !active;
    });
    if (name === "pending") renderOutbox();
    if (name === "results") renderResults();
    if (name === "centralized") renderCentralizedTopics();
    if (name === "followup") renderFarmerList();
    if (name === "home") renderOutbox();
    $("[data-field-tab].active")?.scrollIntoView({ block: "nearest", inline: "nearest", behavior: "smooth" });
    window.scrollTo({ top: 0, behavior: "smooth" });
  };

  const focusVisit = (name, saved = false) => {
    focusedVisit = true;
    document.body.classList.add("field-visit-active");
    $("[data-visit-header]").hidden = false;
    $("[data-visit-name]").textContent = name;
    $("[data-visit-stage]").textContent = saved
      ? "Visit saved. Review the results, then finish and return to the Field App."
      : "Complete this visit, then save and review its results. Your answers are not saved yet.";
    $("[data-cancel-visit]").hidden = saved;
  };

  const leaveVisit = () => {
    if (saving) return;
    focusedVisit = false;
    document.body.classList.remove("field-visit-active");
    $("[data-visit-header]").hidden = true;
    openPane("home");
    const summary = currentOutbox().length ? $("[data-home-saved]") : $("[data-transfer-summary]");
    summary.tabIndex = -1;
    summary.focus({ preventScroll: true });
    summary.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  const renderPackage = () => {
    if (!fieldPackage) return;
    currentCbf = fieldPackage.cbf;
    $("[data-field-ready]").hidden = false;
    $("[data-summary-cbf]").textContent = fieldPackage.cbf;
    $("[data-summary-time]").textContent = `Prepared ${formatDateTime(fieldPackage.preparedAt)}`;
    $("[data-summary-farmers]").textContent = fieldPackage.summary.farmers;
    $("[data-summary-ct]").textContent = fieldPackage.summary.centralizedTrainingDue;
    $("[data-summary-fu]").textContent = fieldPackage.summary.followupsDue;
    const cbfSelect = $("[data-cbf-select]");
    if (cbfSelect) cbfSelect.value = fieldPackage.cbf;
    const unvisited = $("[data-fu-unvisited]");
    if (unvisited && fieldPackage.coordinator && !unvisited.dataset.initialized) {
      unvisited.checked = true;
      unvisited.dataset.initialized = "true";
    }
    populateFilters();
    renderCentralizedTopics();
    renderFarmerList();
    refreshCounts();
    renderOutbox();
  };

  const populateFilters = () => {
    [$("[data-ct-group]"), $("[data-fu-group]")].forEach((select) => {
      const selected = select.value;
      select.replaceChildren(new Option("All groups", ""));
      fieldPackage.groups.forEach((group) => select.append(new Option(group, group)));
      select.value = fieldPackage.groups.includes(selected) ? selected : "";
    });
    const venues = $("[data-venue-list]");
    venues.replaceChildren(...fieldPackage.venues.map((venue) => {
      const option = document.createElement("option");
      option.value = venue;
      return option;
    }));
  };

  const prepareTablet = async ({ quiet = false } = {}) => {
    if (root.inert || preparing) return false;
    const select = $("[data-cbf-select]");
    const cbf = config.assignedCbf || select?.value || currentCbf;
    if (!cbf) {
      alertUser("Select the CBF whose data should be prepared on this tablet.", "warning");
      return false;
    }
    if (!navigator.onLine) {
      if (!quiet) alertUser("An internet connection is required to prepare or update field data.", "warning");
      return false;
    }
    const button = $("[data-prepare]");
    const originalText = button.textContent;
    preparing = true;
    button.disabled = true;
    button.textContent = "Preparing…";
    try {
      const response = await fetch(`${config.bootstrapUrl}?cbf=${encodeURIComponent(cbf)}`, {
        credentials: "same-origin",
        headers: { Accept: "application/json", "X-ARFSA-Environment": environment },
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || !payload.ok) throw new Error(payload.error || "Field data could not be prepared.");
      fieldPackage = payload.package;
      currentCbf = fieldPackage.cbf;
      await putStored("packagesV2", { key: packageKey(currentCbf), package: fieldPackage });
      await putStored("meta", { key: metaKey("lastCbf"), value: currentCbf });
      renderPackage();
      if (!quiet) alertUser(`${fieldPackage.summary.farmers} beneficiaries are now available offline for ${currentCbf}.`, "success");
      return true;
    } catch (error) {
      if (!quiet) alertUser(error.message || "Field data could not be prepared.", "error");
      return false;
    } finally {
      preparing = false;
      button.disabled = false;
      button.textContent = originalText;
    }
  };

  const renderCentralizedTopics = () => {
    if (!fieldPackage) return;
    const container = $("[data-ct-topics]");
    const group = $("[data-ct-group]").value;
    const claimed = queuedCtKeys();
    container.replaceChildren();
    fieldPackage.topics.forEach((topic, topicIndex) => {
      const eligible = fieldPackage.farmers.filter((farmer) =>
        farmer.ctTopics.includes(topic)
        && (!group || farmer.group === group)
        && !claimed.has(`${farmer.id}::${topic}`));
      const details = create("details", "field-topic-card");
      if (topicIndex === 0 && eligible.length) details.open = true;
      const summary = create("summary");
      const title = create("span", "field-topic-title");
      title.append(create("i", "field-topic-number", String(topicIndex + 1)), create("strong", "", topic));
      summary.append(title, create("span", "field-topic-count", `${eligible.length} eligible`));
      const body = create("div", "field-topic-body");
      if (!eligible.length) {
        body.append(create("p", "field-empty", "No eligible beneficiaries in this group, or their attendance is already waiting to synchronize."));
      } else {
        const toolbar = create("div", "field-check-toolbar");
        toolbar.append(create("span", "", "Select everyone who attended this topic"));
        const selectAll = create("button", "button button-ghost button-small", "Select all");
        selectAll.type = "button";
        selectAll.addEventListener("click", () => {
          const boxes = $$("input[type=checkbox]", body);
          const shouldCheck = boxes.some((box) => !box.checked);
          boxes.forEach((box) => { box.checked = shouldCheck; });
          unfinishedForms.add($("[data-ct-form]"));
          updateCtSelectionCount();
          selectAll.textContent = shouldCheck ? "Clear" : "Select all";
        });
        toolbar.append(selectAll);
        body.append(toolbar);
        const list = create("div", "field-beneficiary-checklist");
        eligible.forEach((farmer) => {
          const label = create("label", "field-beneficiary-check");
          const input = document.createElement("input");
          input.type = "checkbox";
          input.dataset.recordId = farmer.id;
          input.dataset.topic = topic;
          input.addEventListener("change", updateCtSelectionCount);
          const copy = create("span");
          copy.append(create("strong", "", farmer.name), create("small", "", [farmer.uid, farmer.group || "No group", farmer.village].filter(Boolean).join(" · ")));
          label.append(input, copy);
          list.append(label);
        });
        body.append(list);
      }
      details.append(summary, body);
      container.append(details);
    });
    updateCtSelectionCount();
  };

  const updateCtSelectionCount = () => {
    const count = $$("[data-ct-topics] input[type=checkbox]:checked").length;
    $("[data-ct-selection-count]").textContent = count
      ? `${count} attendance ${count === 1 ? "entry" : "entries"} selected`
      : "No attendees selected";
  };

  const dueFarmers = () => {
    const claimed = queuedFuKeys();
    return fieldPackage.farmers.map((farmer) => ({
      ...farmer,
      availableFollowups: farmer.followupTopics.filter((item) => !claimed.has(`${farmer.id}::${item.topic}`)),
    })).filter((farmer) => farmer.availableFollowups.length);
  };

  const renderFarmerList = () => {
    if (!fieldPackage) return;
    const group = $("[data-fu-group]").value;
    const query = $("[data-fu-search]").value.trim().toLocaleLowerCase();
    const unvisitedOnly = $("[data-fu-unvisited]")?.checked;
    const farmers = dueFarmers().filter((farmer) => {
      if (group && farmer.group !== group) return false;
      if (unvisitedOnly && farmer.visitedThisRound) return false;
      const haystack = [farmer.name, farmer.uid, farmer.village, farmer.group].join(" ").toLocaleLowerCase();
      return !query || haystack.includes(query);
    });
    const list = $("[data-farmer-list]");
    list.replaceChildren();
    if (!farmers.length) {
      list.append(create("p", "field-empty", "No beneficiaries with unsaved follow-ups match this worklist."));
      return;
    }
    const heading = create("div", "field-list-heading");
    heading.append(create("strong", "", `${farmers.length} beneficiaries`), create("small", "", "Select the person visited"));
    list.append(heading);
    farmers.forEach((farmer) => {
      const button = create("button", `field-farmer-row${selectedFarmerId === farmer.id ? " active" : ""}`);
      button.type = "button";
      const avatar = create("span", "field-farmer-avatar", farmer.name.slice(0, 1).toUpperCase());
      const copy = create("span");
      copy.append(create("strong", "", farmer.name), create("small", "", [farmer.uid, farmer.group || "No group", farmer.village].filter(Boolean).join(" · ")));
      button.append(avatar, copy, create("b", "", `${farmer.availableFollowups.length} due`));
      button.addEventListener("click", () => selectFarmer(farmer.id));
      list.append(button);
    });
  };

  const questionInput = (question, farmer) => {
    const name = `survey__${question.id}`;
    const wrap = create("section", `field-question field-question-${question.type}`);
    wrap.dataset.surveyQuestion = question.id;
    wrap.dataset.sectionOrder = String(question.section_order ?? 0);
    if (question.packages) wrap.dataset.questionPackages = JSON.stringify(question.packages);
    if (question.condition) wrap.dataset.condition = JSON.stringify(question.condition);
    if (question.skip_condition) wrap.dataset.skipCondition = JSON.stringify(question.skip_condition);
    if (question.guide_parent) wrap.dataset.guideParent = question.guide_parent;
    if (question.locked_to) wrap.dataset.lockedTo = question.locked_to;
    if (question.training_topic) {
      wrap.dataset.trainingApplicable = (farmer.trainingHistory || []).includes(question.training_topic) ? "true" : "false";
    }
    wrap.dataset.required = question.required ? "true" : "false";
    if (question.max_selections) wrap.dataset.maxSelections = String(question.max_selections);
    if (question.exclusive_values) wrap.dataset.exclusiveValues = JSON.stringify(question.exclusive_values);
    const copy = create("div", "field-question-copy");
    copy.append(create("small", "", question.source_id), create("h3", "", question.label));
    if (question.help) copy.append(create("p", "", question.help));
    if (question.required) copy.append(create("b", "field-required", "Required"));
    wrap.append(copy);
    if (question.id === "a0_shared_person") {
      const picker = create("div", "household-picker");
      window.arfsaSetupHouseholdPicker(
        picker,
        (fieldPackage.householdCandidates || fieldPackage.farmers || []).filter((person) => person.id !== farmer.id),
        currentCbf,
        name,
        farmer.availableFollowups.map((item) => item.topic),
      );
      wrap.append(picker);
      return wrap;
    }
    let options = question.options || [];
    if (question.type === "ranking") {
      const history = new Set(farmer.trainingHistory || []);
      options = options.filter((option) => history.has(typeof option === "object" ? option.value : option));
      wrap.dataset.required = options.length ? "true" : "false";
    }
    const prefillSource = question.profile_key
      ? farmer.profile?.[question.profile_key]
      : (question.carry_forward ? farmer.previousAnswers?.[question.id] : "");
    const rawPrefill = String(prefillSource ?? "").trim();
    const matchedPrefill = options.find((option) => {
      const value = typeof option === "object" ? option.value : option;
      const label = typeof option === "object" ? option.label : option;
      return String(value).toLowerCase() === rawPrefill.toLowerCase() || String(label).toLowerCase() === rawPrefill.toLowerCase();
    });
    const prefill = matchedPrefill ? (typeof matchedPrefill === "object" ? matchedPrefill.value : matchedPrefill) : rawPrefill;
    if (question.type === "training_list") {
      const list = create("div", "field-training-history");
      (farmer.trainingHistory || []).forEach((topic) => list.append(create("span", "", `✓ ${topic}`)));
      if (!(farmer.trainingHistory || []).length) list.append(create("em", "", "No recorded trainings"));
      wrap.append(list);
    } else if (question.type === "readonly") {
      const value = create("div", "field-readonly-value");
      const label = create("strong", "", prefill || "Not recorded");
      const note = create("small", "", "Filled from the beneficiary database");
      const input = document.createElement("input");
      input.type = "hidden";
      input.name = name;
      input.value = prefill;
      value.append(label, note, input);
      if (question.editable) {
        value.classList.add("survey-editable-value");
        const editButton = create("button", "button button-ghost button-small", "Edit");
        editButton.type = "button";
        const editor = create("div", "survey-inline-editor");
        editor.hidden = true;
        const hasEditOptions = Array.isArray(question.edit_options);
        const editorInput = document.createElement(hasEditOptions ? "select" : "input");
        if (hasEditOptions) {
          [{ value: "", label: "Not recorded" }, ...question.edit_options].forEach((option) => {
            const choice = document.createElement("option");
            choice.value = option.value;
            choice.textContent = option.label;
            editorInput.append(choice);
          });
        } else {
          editorInput.type = question.id === "a8_contact" ? "tel" : "text";
        }
        editorInput.value = prefill;
        editorInput.setAttribute("aria-label", `Edit ${question.label}`);
        const saveButton = create("button", "button button-primary button-small", "Save");
        const cancelButton = create("button", "button button-ghost button-small", "Cancel");
        saveButton.type = "button";
        cancelButton.type = "button";
        const closeEditor = () => { editor.hidden = true; label.hidden = false; note.hidden = false; editButton.hidden = false; };
        editButton.addEventListener("click", () => {
          editorInput.value = input.value;
          label.hidden = true;
          note.hidden = true;
          editButton.hidden = true;
          editor.hidden = false;
          editorInput.focus();
        });
        saveButton.addEventListener("click", () => {
          input.value = editorInput.value.trim();
          label.textContent = input.value || "Not recorded";
          closeEditor();
        });
        cancelButton.addEventListener("click", closeEditor);
        editorInput.addEventListener("keydown", (event) => {
          if (event.key === "Enter") { event.preventDefault(); saveButton.click(); }
          if (event.key === "Escape") { event.preventDefault(); closeEditor(); }
        });
        editor.append(editorInput, saveButton, cancelButton);
        value.append(editButton, editor);
      }
      wrap.append(value);
    } else if (question.type === "gps") {
      const tracker = create("div", "survey-gps-capture");
      tracker.dataset.gpsTracker = "";
      tracker.dataset.tracking = "false";
      const input = document.createElement("input");
      input.type = "hidden";
      input.name = name;
      const status = create("div", "survey-gps-status", "No plot boundary recorded yet.");
      const actions = create("div", "survey-gps-actions");
      const startButton = create("button", "button button-secondary button-small", "Start tracking");
      const stopButton = create("button", "button button-primary button-small", "Stop and save");
      const clearButton = create("button", "button button-ghost button-small", "Clear");
      [startButton, stopButton, clearButton].forEach((button) => { button.type = "button"; });
      stopButton.hidden = true;
      clearButton.hidden = true;
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
      const renderGps = () => {
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
      const stopGps = () => {
        if (watchId !== null) navigator.geolocation.clearWatch(watchId);
        watchId = null;
        input.value = points.length ? JSON.stringify({ points }) : "";
        renderGps();
      };
      startButton.addEventListener("click", () => {
        if (!window.isSecureContext || !("geolocation" in navigator)) {
          alertUser("GPS tracking requires HTTPS and enabled location services.", "warning");
          return;
        }
        points = [];
        input.value = "";
        watchId = navigator.geolocation.watchPosition((position) => {
          if (points.length >= 5000) { stopGps(); return; }
          const point = {
            latitude: position.coords.latitude, longitude: position.coords.longitude,
            accuracy: position.coords.accuracy, capturedAt: new Date(position.timestamp).toISOString(),
          };
          if (distanceFromLastPoint(point) < 4) return;
          points.push(point);
          capturedLocation = { ...point };
          renderGps();
        }, (error) => {
          stopGps();
          alertUser(error.code === 1 ? "Location permission was declined." : "The plot boundary could not be tracked.", "warning");
        }, { enableHighAccuracy: true, maximumAge: 0, timeout: 20000 });
        renderGps();
      });
      stopButton.addEventListener("click", stopGps);
      clearButton.addEventListener("click", () => { points = []; input.value = ""; capturedLocation = null; renderGps(); });
      actions.append(startButton, stopButton, clearButton);
      tracker.append(input, status, actions);
      wrap.append(tracker);
    } else if (question.type === "photos") {
      const upload = create("label", "field-photo-upload");
      upload.dataset.photoPicker = "";
      const input = document.createElement("input");
      input.type = "file";
      input.name = name;
      input.accept = "image/jpeg,image/png,image/webp,image/heic,image/heif";
      input.capture = "environment";
      const button = create("span", "button button-secondary", "Take or choose a photo");
      const status = create("small", "", "No photo added · maximum 1");
      status.dataset.photoStatus = "";
      const preview = create("div", "survey-photo-preview");
      preview.dataset.photoPreview = "";
      upload.append(input, button, status, preview);
      window.arfsaSetupPhotoPicker(upload);
      wrap.append(upload);
    } else if (question.type === "ranking") {
      const ranking = create("div", "survey-ranking");
      ranking.dataset.ranking = "";
      ranking.append(create("p", "", "Drag the trainings into order, or use the arrow buttons."));
      const list = document.createElement("ol");
      list.dataset.rankingList = "";
      options.forEach((option) => {
        const item = document.createElement("li");
        item.draggable = true;
        item.dataset.rankingItem = "";
        const handle = create("span", "survey-ranking-handle", "↕");
        handle.setAttribute("aria-hidden", "true");
        const label = create("strong", "", typeof option === "object" ? option.label : option);
        const hidden = document.createElement("input");
        hidden.type = "hidden";
        hidden.name = name;
        hidden.value = typeof option === "object" ? option.value : option;
        const actions = create("span", "survey-ranking-actions");
        const up = create("button", "", "↑");
        const down = create("button", "", "↓");
        up.type = down.type = "button";
        up.dataset.rankingUp = "";
        down.dataset.rankingDown = "";
        up.setAttribute("aria-label", `Move ${label.textContent} up`);
        down.setAttribute("aria-label", `Move ${label.textContent} down`);
        actions.append(up, down);
        item.append(handle, label, hidden, actions);
        list.append(item);
      });
      ranking.append(list);
      wrap.append(ranking);
      window.arfsaSetupRanking(ranking);
    } else if (question.type === "textarea") {
      const input = document.createElement("textarea");
      input.name = name;
      input.rows = 3;
      input.placeholder = "Enter observations or advice";
      input.value = prefill;
      wrap.append(input);
    } else if (question.type === "date") {
      const input = document.createElement("input");
      input.type = "date";
      input.name = name;
      input.value = prefill;
      wrap.append(input);
    } else if (question.type === "number") {
      const numberWrap = create("div", "field-number-input");
      const input = document.createElement("input");
      input.type = "number";
      input.name = name;
      input.min = String(question.min_value ?? 0);
      if (question.max_value !== undefined) input.max = String(question.max_value);
      input.step = String(question.step_value ?? (question.integer ? 1 : "any"));
      input.value = prefill;
      input.placeholder = question.integer ? "Enter a whole number" : "Enter a number";
      numberWrap.append(input);
      if (question.unit) {
        numberWrap.classList.add("has-unit");
        numberWrap.append(create("span", "", question.unit));
      }
      wrap.append(numberWrap);
    } else if (question.type === "multi") {
      const choices = create("div", "field-choice-grid");
      options.forEach((option) => {
        const label = create("label", "field-choice");
        const input = document.createElement("input");
        input.type = "checkbox";
        input.name = name;
        input.value = typeof option === "object" ? option.value : option;
        label.append(input, create("span", "", typeof option === "object" ? option.label : option));
        choices.append(label);
      });
      wrap.append(choices);
    } else if ((question.type === "choice" || question.type === "consent") && options.length <= 6) {
      const choices = create("div", "field-choice-grid");
      options.forEach((option) => {
        const label = create("label", "field-choice");
        const input = document.createElement("input");
        input.type = "radio";
        input.name = name;
        input.value = typeof option === "object" ? option.value : option;
        input.checked = prefill === input.value;
        label.append(input, create("span", "", typeof option === "object" ? option.label : option));
        choices.append(label);
      });
      wrap.append(choices);
    } else if (options.length) {
      const select = document.createElement("select");
      select.name = name;
      select.append(new Option("Select an answer", ""));
      options.forEach((option) => select.append(new Option(
        typeof option === "object" ? option.label : option,
        typeof option === "object" ? option.value : option,
      )));
      wrap.append(select);
    } else {
      const input = document.createElement("input");
      input.type = "text";
      input.name = name;
      input.value = prefill;
      wrap.append(input);
    }
    return wrap;
  };

  const selectFarmer = (farmerId) => {
    selectedFarmerId = farmerId;
    unfinishedForms.add($("[data-followup-form]"));
    renderFarmerList();
    const farmer = dueFarmers().find((item) => item.id === farmerId);
    const container = $("[data-followup-questionnaires]");
    container.replaceChildren();
    if (!farmer) {
      container.hidden = true;
      $("[data-followup-save]").hidden = true;
      return;
    }
    focusVisit(farmer.name);
    const heading = create("div", "field-selected-farmer");
    heading.append(create("span", "field-farmer-avatar", farmer.name.slice(0, 1).toUpperCase()));
    const copy = create("div");
    copy.append(create("small", "", "Selected beneficiary"), create("h2", "", farmer.name), create("p", "", [farmer.uid, farmer.group || "No group", farmer.village].filter(Boolean).join(" · ")));
    heading.append(copy);
    container.append(heading);
    const dueTopics = farmer.availableFollowups.map((item) => item.topic);
    const dueSet = new Set(dueTopics);
    // Apply the revised final photo question to previously prepared worklists too.
    // Saved outbox submissions are separate and must never be changed here.
    const finalPhoto = window.ARFSA_FOLLOWUP_RULES.questions.find((question) => question.id === "i4_photos");
    const sections = (fieldPackage.survey?.sections || []).filter((section) =>
      section.always || (section.topics || []).some((topic) => dueSet.has(topic))).map((section) => ({
        ...section,
        ...(section.id === "I" ? { intro: "This feedback is not scored." } : {}),
        questions: [
          ...section.questions.filter((question) => question.id !== "i1_training_ranking" && question.type !== "photos"),
          ...(section.id === "I" ? [finalPhoto] : []),
        ],
      }));
    const dueCard = create("div", "field-survey-due");
    dueCard.append(create("strong", "", "Due for follow-up"));
    const dueList = create("div");
    dueTopics.forEach((topic) => dueList.append(create("span", "", topic)));
    dueCard.append(dueList);
    container.append(dueCard);
    sections.forEach((section, sectionIndex) => {
      const details = create("details", "field-questionnaire");
      details.dataset.section = section.id;
      if (sectionIndex === 0) details.open = true;
      details.hidden = sectionIndex !== 0;
      const summary = create("summary");
      const title = create("span");
      title.append(create("strong", "", `Section ${section.id} · ${section.title}`));
      if (section.intro) title.append(create("small", "", section.intro));
      summary.append(title, create("b", "", `${section.questions.length} items`));
      const questions = create("div", "field-question-list");
      if (section.instruction) questions.append(create("div", "field-section-instruction", section.instruction));
      section.questions.forEach((question) => questions.append(questionInput(question, farmer)));
      const guide = create("div", "survey-question-guide");
      const previousQuestion = create("button", "button button-ghost button-small", "Previous question");
      const questionProgress = create("span", "", "");
      const nextQuestion = create("button", "button button-primary button-small", "Next question");
      previousQuestion.type = "button";
      nextQuestion.type = "button";
      guide.append(previousQuestion, questionProgress, nextQuestion);
      const actions = create("div", "field-survey-step-actions");
      actions.append(create("small", "", `Package ${sectionIndex + 1} of ${sections.length}`));
      const nextButton = create("button", "button button-primary", sectionIndex === sections.length - 1 ? "Finish final package" : "Finish this package and continue");
      nextButton.type = "button";
      nextButton.addEventListener("click", async () => {
        if (!validateFieldStep(details, container)) return;
        if (!await window.arfsaConfirmPackage({ finalPackage: sectionIndex === sections.length - 1 })) return;
        details.hidden = true;
        details.open = false;
        const next = container.querySelector(`[data-section="${CSS.escape(sections[sectionIndex + 1]?.id || "")}"]`);
        if (next) {
          next.hidden = false;
          next.open = true;
          next.scrollIntoView({ behavior: "smooth", block: "start" });
        } else {
          // Move the existing controls, retaining the selected file and any
          // in-flight compression without reopening completed survey answers.
          $$(".field-question-photos", container).forEach((node) => {
            node.classList.remove("survey-guide-hidden");
            $(".field-question-copy h3", node).textContent = "Visit photo";
            const help = $(".field-question-copy p", node);
            if (help) help.hidden = true;
            finalize.append(node);
          });
          if ($("[data-photo-picker]", finalize)) {
            const withoutPhoto = create("button", "button button-secondary", "Save without photo");
            // Keep normal/keyboard form submission attached to the primary save.
            withoutPhoto.type = "button";
            withoutPhoto.dataset.saveWithoutPhoto = "";
            withoutPhoto.addEventListener("click", () => {
              if (saving) return;
              $$("[data-photo-picker]", finalize).forEach((picker) => picker.arfsaPhotoPicker?.clear());
              withoutPhoto.closest("form").requestSubmit();
            });
            finalize.append(withoutPhoto);
            const refreshPhotoActions = () => {
              withoutPhoto.hidden = !$$("[data-photo-picker]", finalize)
                .some((picker) => picker.arfsaPhotoPicker?.needsRecovery);
            };
            finalize.addEventListener("photo-state-change", refreshPhotoActions);
            $$("[data-photo-picker]", finalize).forEach((picker) => picker.arfsaPhotoPicker?.setRecoveryOnly());
            refreshPhotoActions();
          }
          finalize.hidden = false;
          $("[data-followup-save]").hidden = false;
          finalize.scrollIntoView({ behavior: "smooth", block: "center" });
        }
      });
      actions.append(nextButton);
      const questionNodes = $$('[data-survey-question]', questions);
      const useQuestionGuide = section.id !== "A";
      guide.hidden = !useQuestionGuide;
      let currentQuestionId = questionNodes[0]?.dataset.surveyQuestion || "";
      const refreshQuestionGuide = () => {
        if (!useQuestionGuide) {
          questionNodes.forEach((node) => node.classList.remove("survey-guide-hidden"));
          actions.hidden = false;
          return;
        }
        const active = questionNodes.filter((node) => !node.hidden);
        const pages = active.filter((node) => !node.dataset.guideParent);
        let position = pages.findIndex((node) => node.dataset.surveyQuestion === currentQuestionId);
        if (position < 0) position = 0;
        const current = pages[position];
        if (current) currentQuestionId = current.dataset.surveyQuestion;
        questionNodes.forEach((node) => {
          if (!questions.contains(node)) return;
          const belongsToCurrentPage = node === current || node.dataset.guideParent === currentQuestionId;
          node.classList.toggle("survey-guide-hidden", !belongsToCurrentPage);
        });
        previousQuestion.hidden = position <= 0;
        nextQuestion.hidden = !current || position >= pages.length - 1;
        actions.hidden = !current || position < pages.length - 1;
        questionProgress.textContent = current ? `Question ${position + 1} of ${pages.length}` : "No applicable questions";
      };
      previousQuestion.addEventListener("click", () => {
        const pages = questionNodes.filter((node) => !node.hidden && !node.dataset.guideParent);
        const position = pages.findIndex((node) => node.dataset.surveyQuestion === currentQuestionId);
        if (position > 0) currentQuestionId = pages[position - 1].dataset.surveyQuestion;
        refreshQuestionGuide();
      });
      nextQuestion.addEventListener("click", () => {
        const active = questionNodes.filter((node) => !node.hidden);
        const pages = active.filter((node) => !node.dataset.guideParent);
        const position = pages.findIndex((node) => node.dataset.surveyQuestion === currentQuestionId);
        const visiblePage = active.filter((node) => node === pages[position] || node.dataset.guideParent === currentQuestionId);
        if (!visiblePage.every((node) => validateFieldGuideQuestion(node, container))) return;
        if (position >= 0 && position < pages.length - 1) currentQuestionId = pages[position + 1].dataset.surveyQuestion;
        refreshQuestionGuide();
      });
      details.refreshQuestionGuide = refreshQuestionGuide;
      details.append(summary, questions, guide, actions);
      container.append(details);
    });
    const finalize = create("div", "field-survey-finalize");
    finalize.hidden = true;
    finalize.append(create("strong", "", "Questions complete · save to finish"), create("p", "", "This visit has not been saved yet. Press Save follow-up and view results to store your answers on this tablet and see the results, even offline."));
    container.append(finalize);
    container.dataset.topics = JSON.stringify(dueTopics);
    const refresh = () => updateFieldSurveyConditions(container);
    container.addEventListener("change", (event) => {
      const node = event.target.closest?.("[data-survey-question]");
      if (node && event.target.matches('input[type="checkbox"]')) applyFieldMultiRules(node, event.target);
      refresh();
    });
    container.addEventListener("input", refresh);
    refresh();
    container.hidden = false;
    $("[data-followup-save]").hidden = true;
    $("[data-followup-selection]").textContent = `${farmer.availableFollowups.length} ${farmer.availableFollowups.length === 1 ? "topic" : "topics"} due for ${farmer.name}`;
    container.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  const surveyValue = (container, questionId) => {
    const node = $(`[data-survey-question="${CSS.escape(questionId)}"]`, container);
    if (!node || node.hidden) return "";
    const ranking = $("[data-ranking-list]", node);
    if (ranking) return $$('input[type="hidden"]', ranking).map((input) => input.value);
    if ($('input[type="file"]', node)) return [];
    if ($("input[type=checkbox]", node)) return $$(`input[type=checkbox]:checked`, node).map((input) => input.value);
    return $("input[type=radio]:checked", node)?.value
      ?? $("select", node)?.value
      ?? $("input:not([type=radio]):not([type=checkbox]), textarea", node)?.value?.trim()
      ?? "";
  };

  const surveyConditionMatches = (container, condition) => {
    if (!condition) return true;
    const value = surveyValue(container, condition.question);
    if (condition.operator === "equals") return value === condition.value;
    if (condition.operator === "in") return (condition.values || []).includes(value);
    if (condition.operator === "contains") return Array.isArray(value) && value.includes(condition.value);
    if (condition.operator === "contains_any") return Array.isArray(value) && value.some((item) => (condition.values || []).includes(item));
    if (condition.operator === "has_any_except") return Array.isArray(value) && value.some((item) => item !== condition.value);
    if (condition.operator === "not_empty") return Array.isArray(value) ? value.length > 0 : value !== "";
    return false;
  };

  const updateFieldSurveyConditions = (container) => {
    const packageStops = window.arfsaPackageStopTriggers({
      rules: fieldPackage?.survey?.package_stop_rules || [],
      valueFor: (questionId) => surveyValue(container, questionId),
      orderFor: (questionId) => Number($(`[data-survey-question="${CSS.escape(questionId)}"]`, container)?.dataset.sectionOrder || 0),
    });
    $$('[data-survey-question]', container).forEach((node) => {
      if (node.dataset.lockedTo) {
        const lockedValue = surveyValue(container, node.dataset.lockedTo);
        $$('input[type="radio"], input[type="checkbox"]', node).forEach((control) => {
          control.checked = Array.isArray(lockedValue) ? lockedValue.includes(control.value) : control.value === lockedValue;
        });
        const select = $("select", node);
        if (select) select.value = lockedValue;
        const input = $('input:not([type="radio"]):not([type="checkbox"]), textarea', node);
        if (input) input.value = lockedValue;
      }
      const condition = node.dataset.condition ? JSON.parse(node.dataset.condition) : null;
      const skipCondition = node.dataset.skipCondition ? JSON.parse(node.dataset.skipCondition) : null;
      let packages = [];
      try { packages = node.dataset.questionPackages ? JSON.parse(node.dataset.questionPackages) : []; } catch (_error) { packages = []; }
      const stopped = packages.length > 0 && packages.every(
        (packageKey) => packageStops.has(packageKey) && packageStops.get(packageKey) < Number(node.dataset.sectionOrder || 0),
      );
      const visible = node.dataset.trainingApplicable !== "false" && surveyConditionMatches(container, condition)
        && !(skipCondition && surveyConditionMatches(container, skipCondition)) && !stopped;
      node.hidden = !visible;
      $$('input, select, textarea', node).forEach((control) => { control.disabled = !visible || Boolean(node.dataset.lockedTo); });
      applyFieldMultiRules(node);
    });
    $$('[data-section]', container).forEach((section) => section.refreshQuestionGuide?.());
  };

  const applyFieldMultiRules = (node, changedControl = null) => {
    const boxes = $$('input[type="checkbox"]', node);
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

  const readPhotoFiles = async (node) => {
    try {
      const picker = $("[data-photo-picker]", node)?.arfsaPhotoPicker;
      const files = picker ? await picker.read() : Array.from($('input[type="file"]', node)?.files || []);
      if (files.length > 1) throw new Error("Upload no more than 1 photo of the best practice found during this visit.");
      if (files.some((file) => file.size > window.ARFSA_MAX_PHOTO_BYTES)) throw new Error("Each follow-up photo must be smaller than 8 MB. Replace or remove the photo, or save without it.");
      return await Promise.all(files.map((file) => new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve({ name: file.name, type: file.type, data: reader.result });
        reader.onerror = () => reject(new Error(`Could not read ${file.name}. Replace or remove the photo, or save without it.`));
        reader.readAsDataURL(file);
      })));
    } catch (error) {
      $("[data-photo-picker]", node)?.arfsaPhotoPicker?.showError(error.message);
      error.photoError = true;
      throw error;
    }
  };

  const validateFieldStep = (step, container) => {
    updateFieldSurveyConditions(container);
    for (const node of $$('[data-survey-question]', step)) {
      if (node.hidden) continue;
      if ($('[data-gps-tracker][data-tracking="true"]', node)) {
        alertUser("Stop and save the A7 GPS tracking before finalizing this section.", "warning");
        node.scrollIntoView({ behavior: "smooth", block: "center" });
        return false;
      }
      const controls = $$('input:not([type="hidden"]), select, textarea', node);
      const invalid = controls.find((control) => !control.checkValidity());
      if (invalid) {
        invalid.reportValidity();
        return false;
      }
      const fileInput = $('input[type="file"]', node);
      if (fileInput?.files.length > 1) {
        alertUser("Upload no more than 1 photo of the best practice found during this visit.", "warning");
        return false;
      }
      const value = surveyValue(container, node.dataset.surveyQuestion);
      const empty = fileInput ? fileInput.files.length === 0 : (Array.isArray(value) ? value.length === 0 : value === "");
      if (node.dataset.required === "true" && empty && !node.classList.contains("field-question-training_list")) {
        alertUser(`Complete required item ${$("small", node)?.textContent || "in this package"}.`, "warning");
        node.scrollIntoView({ behavior: "smooth", block: "center" });
        return false;
      }
    }
    const damage = Number(surveyValue(container, "b10_1_damage"));
    const severe = Number(surveyValue(container, "b10_2_severe"));
    if ($('[data-survey-question="b10_2_severe"]:not([hidden])', step) && severe > damage) {
      alertUser("B10.2 cannot be greater than the number of damaged plants in B10.1.", "warning");
      return false;
    }
    const visible = Number(surveyValue(container, "f2_visible"));
    const unhealthy = Number(surveyValue(container, "f2_1_unhealthy"));
    if ($('[data-survey-question="f2_1_unhealthy"]:not([hidden])', step) && unhealthy > visible) {
      alertUser("F2.1 cannot be greater than the number of visible birds in F2.", "warning");
      return false;
    }
    return true;
  };

  const validateFieldGuideQuestion = (node, container) => {
    if (!node || node.hidden) return true;
    if ($('[data-gps-tracker][data-tracking="true"]', node)) {
      alertUser("Stop and save the A7 GPS tracking before continuing.", "warning");
      return false;
    }
    const controls = $$('input:not([type="hidden"]), select, textarea', node);
    const invalid = controls.find((control) => !control.checkValidity());
    if (invalid) { invalid.reportValidity(); return false; }
    const value = surveyValue(container, node.dataset.surveyQuestion);
    const fileInput = $('input[type="file"]', node);
    const empty = fileInput ? fileInput.files.length === 0 : (Array.isArray(value) ? value.length === 0 : value === "");
    if (node.dataset.required === "true" && empty && !node.classList.contains("field-question-training_list")) {
      alertUser(`Complete required item ${$("small", node)?.textContent || "before continuing"}.`, "warning");
      return false;
    }
    return true;
  };

  const answersForSurvey = async (container) => {
    const answers = {};
    let firstMissing = null;
    for (const node of $$('[data-survey-question]', container)) {
      if (node.hidden || !node.dataset.surveyQuestion) continue;
      const questionId = node.dataset.surveyQuestion;
      const value = node.classList.contains("field-question-photos") ? await readPhotoFiles(node) : surveyValue(container, questionId);
      answers[questionId] = value;
      const empty = Array.isArray(value) ? value.length === 0 : value === "";
      if (!firstMissing && node.dataset.required === "true" && empty && !node.classList.contains("field-question-training_list")) firstMissing = node;
    }
    return { answers, firstMissing };
  };

  const localResultFor = (submission) => {
    let outcome = null;
    try {
      outcome = window.arfsaScoreFollowup(submission.answers, submission.topics);
    } catch (error) {
      // A scoring problem must never prevent the visit itself being saved.
    }
    return {
      id: submission.id, cbf: submission.cbf, environment: submission.environment || "live",
      recordId: submission.recordId, beneficiaryName: submission.beneficiaryName
        || fieldPackage?.farmers.find((farmer) => farmer.id === submission.recordId)?.name || submission.summary,
      eventDate: submission.eventDate, createdAt: submission.createdAt,
      topics: submission.topics || [],
      photoCount: Object.values(submission.answers || {}).flat().filter((value) => value && typeof value === "object" && value.data).length,
      hasLocation: Boolean(submission.geoLocation),
      status: submission.status || "pending", error: submission.error || "", syncError: submission.syncError || "", outcome,
    };
  };

  const queueSubmission = async (submission) => {
    if (submission.type === "followup") {
      const localResult = localResultFor(submission);
      const db = await dbPromise;
      // Save the visit and its result together so either both survive or neither
      // is reported as saved if storage is full or a transaction is interrupted.
      await new Promise((resolve, reject) => {
        const transaction = db.transaction(["outbox", "followupResults"], "readwrite");
        transaction.objectStore("outbox").put(submission);
        transaction.objectStore("followupResults").put(localResult);
        transaction.oncomplete = resolve;
        transaction.onabort = () => reject(transaction.error || new Error("Visit could not be saved."));
      });
      savedResults.push(localResult);
    } else await putStored("outbox", submission);
    // Once committed, do not let a subsequent read failure suggest saving again.
    outbox.push(submission);
    refreshCounts();
  };

  const applySynchronizedSubmission = (submission) => {
    if (!fieldPackage || !submission) return;
    if (submission.type === "centralized") {
      submission.entries.forEach((entry) => {
        const farmer = fieldPackage.farmers.find((item) => item.id === entry.recordId);
        if (farmer) farmer.ctTopics = farmer.ctTopics.filter((topic) => topic !== entry.topic);
      });
    } else if (submission.type === "followup") {
      const completedTopics = new Set(submission.topics || (submission.responses || []).map((entry) => entry.topic));
      const applyToFarmer = (recordId, topics) => {
        const farmer = fieldPackage.farmers.find((item) => item.id === recordId);
        if (farmer && topics.length) {
          farmer.followupTopics = farmer.followupTopics.filter((item) => !topics.includes(item.topic));
          farmer.visitedThisRound = true;
        }
      };
      applyToFarmer(submission.recordId, [...completedTopics]);
      [...(submission.sharedRecordIds || []), submission.sharedRecordId].filter(Boolean)
        .forEach((recordId) => applyToFarmer(recordId, matchingHouseholdTopics(recordId, [...completedTopics])));
    }
    fieldPackage.summary.centralizedTrainingDue = fieldPackage.farmers.reduce((total, farmer) => total + farmer.ctTopics.length, 0);
    fieldPackage.summary.followupsDue = fieldPackage.farmers.reduce((total, farmer) => total + farmer.followupTopics.length, 0);
  };

  const saveCentralized = async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const entries = $$("[data-ct-topics] input[type=checkbox]:checked").map((input) => ({
      recordId: Number(input.dataset.recordId),
      topic: input.dataset.topic,
    }));
    if (!entries.length) {
      alertUser("Select at least one attending beneficiary under a training topic.", "warning");
      return;
    }
    if (!form.reportValidity()) return;
    const topics = new Set(entries.map((entry) => entry.topic));
    await queueSubmission({
      id: submissionId(),
      type: "centralized",
      environment,
      cbf: currentCbf,
      eventDate: form.elements.eventDate.value,
      location: form.elements.location.value.trim(),
      entries,
      createdAt: new Date().toISOString(),
      status: "pending",
      summary: `${topics.size} ${topics.size === 1 ? "topic" : "topics"} · ${new Set(entries.map((entry) => entry.recordId)).size} beneficiaries`,
    });
    unfinishedForms.delete(form);
    renderCentralizedTopics();
    alertUser("Centralized training saved safely on this tablet.", "success");
    openPane("home");
    if (navigator.onLine) synchronize();
  };

  const saveFollowup = async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    if (!selectedFarmerId || !form.reportValidity()) return;
    const farmer = dueFarmers().find((item) => item.id === selectedFarmerId);
    if (!farmer) {
      alertUser("Select a beneficiary with a follow-up due.", "warning");
      return;
    }
    const container = $("[data-followup-questionnaires]");
    const topics = JSON.parse(container.dataset.topics || "[]");
    const surveyAnswers = await answersForSurvey(container);
    const { answers, firstMissing } = surveyAnswers;
    if (firstMissing) {
      firstMissing.closest("details")?.setAttribute("open", "");
      firstMissing.scrollIntoView({ behavior: "smooth", block: "center" });
      alertUser(`Complete required item ${$("small", firstMissing)?.textContent || "in the survey"}.`, "warning");
      return;
    }
    if (answers.b10_1_damage !== "" && answers.b10_2_severe !== "" && Number(answers.b10_2_severe) > Number(answers.b10_1_damage)) {
      alertUser("B10.2 cannot be greater than the number of damaged plants in B10.1.", "warning");
      return;
    }
    if (answers.f2_visible !== undefined && answers.f2_visible !== "" && Number(answers.f2_1_unhealthy) > Number(answers.f2_visible)) {
      alertUser("F2.1 cannot be greater than the number of visible birds in F2.", "warning");
      return;
    }
    const followupSubmission = {
      id: submissionId(),
      type: "followup",
      environment,
      cbf: currentCbf,
      eventDate: form.elements.eventDate.value,
      recordId: selectedFarmerId,
      sharedRecordIds: answers.a0_shared_plot === "yes"
        ? (answers.a0_shared_person || []).map(Number) : [],
      beneficiaryName: farmer.name,
      topics,
      surveyVersion: window.ARFSA_FOLLOWUP_RULES.version,
      answers,
      geoLocation: capturedLocation ? { ...capturedLocation } : null,
      createdAt: new Date().toISOString(),
      status: "pending",
      summary: `${farmer.name} · ${topics.length} ${topics.length === 1 ? "training type" : "training types"}`,
    };
    await queueSubmission(followupSubmission);
    unfinishedForms.delete(form);
    selectedFarmerId = null;
    capturedLocation = null;
    updateLocationStatus();
    $("[data-followup-questionnaires]").hidden = true;
    $("[data-followup-save]").hidden = true;
    renderFarmerList();
    selectedResultId = followupSubmission.id;
    focusVisit(farmer.name, true);
    alertUser(savedResults.find((item) => item.id === followupSubmission.id)?.outcome
      ? "Follow-up saved. The results are available on this tablet."
      : "Follow-up saved. Its results are not yet available on this tablet.", "success");
    openPane("results");
    const receipt = $("[data-save-receipt]");
    receipt?.focus({ preventScroll: true });
    receipt?.scrollIntoView({ behavior: "smooth", block: "start" });
    if (navigator.onLine) synchronize();
  };

  const uploadState = (item) => {
    if (item.status === "synchronized") return { tone: "complete", label: "Confirmed by server",
      detail: item.synchronizedAt ? `Confirmed ${formatDateTime(item.synchronizedAt)}. Nothing more to upload for this visit.` : "Received by the server. Nothing more to upload for this visit." };
    if (sendingIds.has(item.id)) return { tone: "pending", label: "Sending to server…",
      detail: "Waiting for the server to confirm receipt. Your saved copy stays on this tablet." };
    if (item.status === "error") return { tone: "attention", label: "Not accepted · needs attention",
      detail: item.error || "The server could not accept this entry. Open Pending for details." };
    if (item.syncError) return { tone: "attention", label: "Upload not confirmed",
      detail: item.syncError };
    return { tone: "pending", label: navigator.onLine ? "Waiting to send" : "Waiting for internet",
      detail: "Server receipt has not been confirmed. This entry is in Pending." };
  };

  const renderSaveReceipt = (visit) => {
    const state = uploadState(visit);
    const complete = visit.status === "synchronized";
    const receipt = create("section", `panel field-save-receipt field-save-${state.tone}`);
    receipt.dataset.saveReceipt = "";
    receipt.tabIndex = -1;
    receipt.setAttribute("aria-label", "Follow-up save receipt");
    const title = create("div", "field-save-title");
    const icon = create("span", "field-save-icon", "✓");
    icon.setAttribute("aria-hidden", "true");
    const copy = create("div");
    copy.append(create("p", "eyebrow", environment === "test" ? "Practice follow-up saved" : "Follow-up saved"),
      create("h2", "", complete ? "Saved on this tablet and server" : "Saved on this tablet"),
      create("p", "", `${visit.beneficiaryName} · Visit date ${visit.eventDate}`));
    title.append(icon, copy);
    receipt.append(title);
    const steps = create("ol", "field-save-steps");
    const local = create("li", "is-complete");
    local.append(create("strong", "", "1. Saved on this tablet"),
      create("p", "", `Saved ${formatDateTime(visit.createdAt)}. You can close the app and reopen this visit in Results.`));
    const server = create("li", complete ? "is-complete" : "");
    server.append(create("strong", "", `2. ${state.label}`), create("p", "", state.detail));
    steps.append(local, server);
    receipt.append(steps);
    const contents = ["Survey answers", ...(visit.photoCount ? [`${visit.photoCount} ${visit.photoCount === 1 ? "photo" : "photos"}`] : []),
      ...(visit.hasLocation ? ["GPS location"] : [])].join(" · ");
    receipt.append(create("p", "field-save-contents", `${complete ? "Sent to server" : "Saved and queued to send"}: ${contents}.`));
    if (visit.topics?.length) receipt.append(create("p", "field-save-contents", `Training types: ${visit.topics.join(" · ")}`));
    if (!complete) receipt.append(create("p", "field-save-next", state.tone === "attention"
      ? "You do not need to enter this visit again. Check the message above, then try Synchronize now when connected. Keep this saved entry until the server confirms it."
      : "Back at the office: connect and open this app to upload automatically, or press Synchronize now. Keep it open until the server confirms receipt."));
    if (!complete) receipt.append(create("p", "field-save-contents", "Until then, keep this browser's site data so your saved entries remain available."));
    const actions = create("div", "field-save-actions");
    if (!complete) {
      const sync = create("button", "button button-primary", syncing ? "Sending to server…" : "Synchronize now");
      sync.type = "button";
      sync.disabled = syncing || !navigator.onLine;
      sync.addEventListener("click", () => synchronize());
      const pending = create("button", "button button-secondary", `View pending entries (${currentOutbox().length})`);
      pending.type = "button";
      pending.addEventListener("click", () => openPane("pending"));
      actions.append(sync);
      if (!focusedVisit) actions.append(pending);
    }
    receipt.append(actions);
    return receipt;
  };

  const appendVisitFinish = (container, visit) => {
    if (!focusedVisit) return;
    const finish = create("section", "field-sticky-save field-visit-finish");
    const complete = visit.status === "synchronized";
    finish.append(create("span", "", complete ? "Saved on this device and confirmed by the server."
      : "Saved on this device. Your visit will stay in Pending until the server confirms it."));
    const button = create("button", "button button-primary", "Finish and return to Field App");
    button.type = "button";
    button.addEventListener("click", leaveVisit);
    finish.append(button);
    container.append(finish);
  };

  const renderResults = () => {
    const container = $("[data-followup-results]");
    const select = $("[data-result-select]");
    const visits = savedResults.filter((item) => item.cbf === currentCbf && item.environment === environment)
      .sort((a, b) => b.createdAt.localeCompare(a.createdAt));
    container.replaceChildren();
    select.replaceChildren(...visits.map((item) => new Option(`${item.eventDate} · ${item.beneficiaryName} · ${item.status === "synchronized" ? "On server" : item.status === "error" || item.syncError ? "Upload needs attention" : "Tablet only"}`, item.id)));
    select.closest("label").hidden = !visits.length;
    if (!visits.length) {
      container.append(create("p", "panel field-empty", "Save a follow-up to see its results here, even without internet."));
      return;
    }
    const visit = visits.find((item) => item.id === selectedResultId) || visits[0];
    selectedResultId = visit.id;
    select.value = visit.id;
    container.append(renderSaveReceipt(visit));
    if (visit.status !== "synchronized") container.append(create("p", "field-result-note", "The results below were calculated on this tablet. The server will check them after upload."));
    const result = visit.outcome;
    if (!result) {
      container.append(create("p", "panel field-empty", "This visit is saved, but its outcomes could not be calculated on this tablet. Update the app or synchronize to receive the results."));
      appendVisitFinish(container, visit);
      return;
    }
    const percent = (value) => `${Number(value).toFixed(1).replace(/\.0$/, "")}%`;
    const passed = (value) => value == null ? "—" : value ? "Passed" : "Not met";
    const hero = create("section", "panel result-hero");
    const household = create("div");
    household.append(create("span", "", "Household outcome"), create("strong", "", result.household_outcome
      ? `${result.household_outcome.code} — ${result.household_outcome.label}` : "Training-specific assessment"));
    if (result.household_outcome) household.append(create("p", "", result.household_outcome.action));
    const kpis = create("div", "result-kpis");
    for (const [label, value] of [["RVO", passed(result.rvo?.passed)], ["Project", passed(result.project_passed)],
      ["Breadth", result.breadth ? `${result.breadth.achieved}/${result.breadth.total}` : "—"],
      ["Depth", result.depth == null ? "—" : percent(result.depth)]]) {
      const card = create("article");
      card.append(create("span", "", label), create("strong", "", value));
      kpis.append(card);
    }
    hero.append(household, kpis);
    container.append(hero);
    if (result.failure_comments?.length) {
      const findings = create("section", "panel failure-comments-panel");
      findings.append(create("h2", "", "Findings to share with the beneficiary"));
      const list = create("div", "failure-comment-list");
      for (const failure of result.failure_comments) {
        const card = create("article", "failure-comment-card");
        const label = create("div");
        label.append(create("strong", "", failure.source_id), create("span", "", failure.packages.join(" · ")));
        card.append(label, create("p", "", failure.comment));
        list.append(card);
      }
      findings.append(list);
      container.append(findings);
    }
    const addScores = (title, items) => {
      if (!items.length) return;
      const section = create("section", "panel field-result-scores");
      section.append(create("h2", "", title));
      for (const [label, score] of items) {
        const card = create("article", "field-result-score");
        card.append(create("h3", "", label), create("strong", "", `${percent(score.score)} · ${score.status}${score.critical_failed ? " · critical" : ""}`),
          create("p", "", score.recommendation));
        section.append(card);
      }
      container.append(section);
    };
    addScores("Package results", Object.values(result.packages || {}).map((item) => [item.title, item]));
    addScores("Recommended next actions", Object.entries(result.trainings || {}));
    if (visit.eventId) {
      const actions = create("section", "panel field-result-heading");
      if (navigator.onLine) {
        const link = create("a", "button button-primary", "Review and confirm CBF decisions");
        link.href = `/data-entry/followup-results/${visit.eventId}`;
        if (focusedVisit) {
          // Keep the saved visit open while reviewing the online decision form.
          link.target = "_blank";
          link.rel = "noopener";
          link.textContent += " (opens a new tab)";
        }
        actions.append(link);
      } else actions.append(create("p", "", "Reconnect to review and confirm CBF decisions. These results remain available offline."));
      container.append(actions);
    }
    appendVisitFinish(container, visit);
  };

  const renderOutbox = () => {
    const list = $("[data-outbox-list]");
    const items = currentOutbox().sort((a, b) => b.createdAt.localeCompare(a.createdAt));
    const homeList = $("[data-home-outbox]");
    homeList.replaceChildren();
    $("[data-home-saved]").hidden = !items.length;
    list.replaceChildren();
    if (!items.length) {
      const empty = create("div", "panel field-outbox-empty");
      empty.append(create("div", "field-safety-icon", "✓"), create("h2", "", "No saved entries waiting to send"), create("p", "", `No pending entries for ${currentCbf || "the selected CBF"}. Follow-up save receipts are available in Results.`));
      list.append(empty);
      return;
    }
    items.forEach((item) => {
      const state = uploadState(item);
      const card = create("article", `panel field-outbox-card${state.tone === "attention" ? " has-error" : ""}`);
      const type = item.type === "centralized" ? "Centralized training" : "Follow-up";
      const title = create("div");
      title.append(create("span", "field-outbox-type", type), create("h3", "", item.summary), create("p", "", `${item.eventDate} · Saved ${formatDateTime(item.createdAt)}`));
      if (item.geoLocation) title.append(create("p", "field-outbox-location", `Location attached · accuracy approximately ${Math.round(item.geoLocation.accuracy)} m`));
      title.append(create("p", state.tone === "attention" ? "field-outbox-error" : "", state.detail));
      const actions = create("div", "field-outbox-actions");
      actions.append(create("span", "field-local-saved", "✓ Saved on tablet"),
        create("span", state.tone === "attention" ? "status-pill field-status-error" : "status-pill", state.label));
      if (item.type === "followup" && savedResults.some((result) => result.id === item.id)) {
        const view = create("button", "button button-secondary button-small", "View results");
        view.type = "button";
        view.addEventListener("click", () => { selectedResultId = item.id; openPane("results"); });
        actions.append(view);
      }
      const discard = create("button", "button button-ghost button-small", "Discard");
      discard.type = "button";
      discard.disabled = syncing;
      discard.addEventListener("click", async () => {
        if (syncing) return;
        if (!confirm("Discard this saved copy from the device? Server receipt has not been confirmed. If it has not reached the server, this entry will be lost.")) return;
        const db = await dbPromise;
        await new Promise((resolve, reject) => {
          const transaction = db.transaction(["outbox", "followupResults"], "readwrite");
          transaction.objectStore("outbox").delete(item.id);
          transaction.objectStore("followupResults").delete(item.id);
          transaction.oncomplete = resolve;
          transaction.onabort = () => reject(transaction.error);
        });
        savedResults = await getAllStored("followupResults");
        outbox = await getAllStored("outbox");
        refreshCounts();
        renderOutbox();
        renderCentralizedTopics();
        renderFarmerList();
      });
      actions.append(discard);
      card.append(title, actions);
      list.append(card);
      const homeCard = create("article", "panel field-outbox-card");
      const homeCopy = create("div");
      homeCopy.append(create("span", "field-outbox-type", type), create("h3", "", item.summary),
        create("p", "", `${item.eventDate} · Saved on device ${formatDateTime(item.createdAt)}`));
      const homeStatus = create("div", "field-outbox-actions");
      homeStatus.append(create("span", "field-local-saved", "✓ Saved on this device"),
        create("span", "status-pill", state.label));
      homeCard.append(homeCopy, homeStatus);
      homeList.append(homeCard);
    });
  };

  const deviceId = async () => {
    const stored = await getStored("meta", "deviceId");
    if (stored?.value) return stored.value;
    const value = submissionId();
    await putStored("meta", { key: "deviceId", value });
    return value;
  };

  const synchronize = async () => {
    if (syncing || root.inert) return;
    const pending = currentOutbox().slice(0, 100);
    if (!pending.length) {
      alertUser("No saved entries are waiting to send for this CBF.", "success");
      return;
    }
    if (!navigator.onLine) {
      alertUser("Entries remain safe on this tablet. Synchronize when internet is available.", "warning");
      return;
    }
    syncing = true;
    pending.forEach((item) => sendingIds.add(item.id));
    refreshCounts();
    renderResults();
    renderOutbox();
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 30000);
    try {
      const response = await fetch(config.syncUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": config.csrfToken, Accept: "application/json" },
        body: JSON.stringify({ cbf: currentCbf, environment, deviceId: await deviceId(), submissions: pending }),
        signal: controller.signal,
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || !Array.isArray(payload.results)) throw new Error(payload.error || "Synchronization could not be completed. Sign in again if your session expired.");
      let accepted = 0;
      let rejected = 0;
      for (const result of payload.results) {
        const item = pending.find((entry) => entry.id === result.id);
        if (!item || !sendingIds.has(result.id)) continue;
        const localResult = savedResults.find((item) => item.id === result.id);
        if (result.status === "accepted" || result.status === "duplicate") {
          const confirmed = localResult && { ...localResult, status: "synchronized", synchronizedAt: new Date().toISOString(),
            eventId: result.eventId, error: "", syncError: "", outcome: result.outcome || localResult.outcome };
          // Update the receipt and queue together: a server confirmation must
          // never leave a visit simultaneously marked as waiting to send.
          const db = await dbPromise;
          await new Promise((resolve, reject) => {
            const transaction = db.transaction(["outbox", "followupResults"], "readwrite");
            if (confirmed) transaction.objectStore("followupResults").put(confirmed);
            transaction.objectStore("outbox").delete(result.id);
            transaction.oncomplete = resolve;
            transaction.onabort = () => reject(transaction.error || new Error("Server confirmation could not be stored. Try synchronizing again."));
          });
          if (confirmed) savedResults = savedResults.map((visit) => visit.id === result.id ? confirmed : visit);
          outbox = outbox.filter((entry) => entry.id !== result.id);
          applySynchronizedSubmission(item);
          accepted += 1;
        } else {
          const error = result.message || "The server rejected this entry.";
          if (localResult) {
            const failed = { ...localResult, status: "error", error, syncError: "" };
            await putStored("followupResults", failed);
            savedResults = savedResults.map((visit) => visit.id === result.id ? failed : visit);
          }
          const failed = { ...item, status: "error", error, syncError: "" };
          await putStored("outbox", failed);
          outbox = outbox.map((entry) => entry.id === result.id ? failed : entry);
          rejected += 1;
        }
        sendingIds.delete(result.id);
      }
      if (accepted && fieldPackage) await putStored("packagesV2", { key: packageKey(currentCbf), package: fieldPackage });
      outbox = await getAllStored("outbox");
      savedResults = await getAllStored("followupResults");
      renderResults();
      refreshCounts();
      renderOutbox();
      if (accepted) await prepareTablet({ quiet: true });
      renderCentralizedTopics();
      renderFarmerList();
      const remaining = currentOutbox().length;
      if (remaining) alertUser(`${accepted} confirmed by server; ${remaining} still to send${rejected ? `, including ${rejected} needing attention` : ""}. Open Pending for details.`, "warning");
      else alertUser(`${accepted} ${accepted === 1 ? "entry" : "entries"} confirmed by server. No saved entries left to send for this CBF.`, "success");
    } catch (error) {
      const message = !navigator.onLine ? "Internet was disconnected before upload could be confirmed. Reconnect and try again."
        : error.name === "AbortError" ? "The server did not confirm the upload in time. Try Synchronize now again."
        : error instanceof TypeError ? "Could not reach the server. Check your connection and try Synchronize now again."
        : error.message || "Synchronization could not be completed. Try again when connected.";
      for (const item of outbox.filter((entry) => sendingIds.has(entry.id))) {
        item.syncError = message;
        const localResult = savedResults.find((visit) => visit.id === item.id);
        if (localResult) localResult.syncError = message;
        try {
          await putStored("outbox", item);
          if (localResult) await putStored("followupResults", localResult);
        } catch (_) { /* Preserve the original saved entry if status storage fails. */ }
      }
      alertUser(`${message} Unconfirmed entries remain saved on this tablet.`, "warning");
    } finally {
      clearTimeout(timeout);
      syncing = false;
      sendingIds.clear();
      refreshCounts();
      renderResults();
      renderOutbox();
    }
  };

  const start = async () => {
    updateConnection();
    updateLocationStatus();
    $$('input[type="date"]', root).forEach((input) => { if (!input.value) input.value = localDate(); });
    outbox = await getAllStored("outbox");
    const lastCbf = config.assignedCbf || (await getStored("meta", metaKey("lastCbf")))?.value || (environment === "live" ? (await getStored("meta", "lastCbf"))?.value : "");
    currentCbf = lastCbf;
    const cbfSelect = $("[data-cbf-select]");
    if (cbfSelect && lastCbf) cbfSelect.value = lastCbf;
    if (lastCbf) {
      const storedPackage = await getStored("packagesV2", packageKey(lastCbf));
      fieldPackage = storedPackage?.package || (environment === "live" ? await getStored("packages", lastCbf) : null);
    }
    if (fieldPackage) renderPackage();
    else refreshCounts();
    savedResults = await getAllStored("followupResults");
    for (const item of outbox) {
      const existing = savedResults.find((result) => result.id === item.id);
      if (item.type === "followup" && item.answers && !existing?.outcome) {
        const result = localResultFor(item);
        await putStored("followupResults", result);
        savedResults = savedResults.filter((stored) => stored.id !== item.id);
        savedResults.push(result);
      }
    }
    renderResults();
    renderOutbox();
    storageReady = true;
    if (navigator.onLine && fieldPackage && currentOutbox().length) setTimeout(synchronize, 900);
  };

  $("[data-prepare]").addEventListener("click", () => prepareTablet());
  $("[data-cancel-visit]").addEventListener("click", () => {
    if (saving || !unfinishedForms.has($("[data-followup-form]")) || !confirm("This visit has not been saved. Cancel it and discard the answers?")) return;
    unfinishedForms.delete($("[data-followup-form]"));
    selectedFarmerId = null;
    capturedLocation = null;
    $("[data-followup-questionnaires]").replaceChildren();
    $("[data-followup-questionnaires]").hidden = true;
    $("[data-followup-save]").hidden = true;
    const status = $("[data-save-status]", $("[data-followup-form]"));
    if (status) status.hidden = true;
    leaveVisit();
  });
  $("[data-result-select]").addEventListener("change", (event) => { selectedResultId = event.target.value; renderResults(); });
  $$('[data-sync-now]').forEach((button) => button.addEventListener("click", () => synchronize()));
  $$('[data-field-tab]').forEach((button) => button.addEventListener("click", () => openPane(button.dataset.fieldTab)));
  $$('[data-open-pane]').forEach((button) => button.addEventListener("click", () => openPane(button.dataset.openPane)));
  $("[data-ct-group]").addEventListener("change", renderCentralizedTopics);
  $("[data-fu-group]").addEventListener("change", () => { selectedFarmerId = null; renderFarmerList(); });
  $("[data-fu-unvisited]").addEventListener("change", () => { selectedFarmerId = null; renderFarmerList(); });
  $("[data-fu-search]").addEventListener("input", renderFarmerList);
  const saveEntry = (save) => async (event) => {
    event.preventDefault();
    if (saving || root.inert) return;
    saving = true;
    const form = event.currentTarget;
    const buttons = $$('button[type="submit"], [data-save-without-photo]', form);
    const labels = buttons.map((button) => button.textContent);
    let status = $("[data-save-status]", form);
    if (!status) {
      status = create("p", "field-save-feedback");
      status.dataset.saveStatus = "";
      status.setAttribute("role", "status");
      form.append(status);
    }
    status.hidden = false;
    status.classList.remove("has-error");
    status.textContent = "Saving on this tablet… Keep this form open until saving is confirmed.";
    form.setAttribute("aria-busy", "true");
    const photoPickers = $$("[data-photo-picker]", form);
    photoPickers.forEach((picker) => { picker.inert = true; });
    buttons.forEach((button) => { button.disabled = true; button.textContent = "Saving on tablet…"; });
    $("[data-cancel-visit]").disabled = true;
    try {
      await save(event);
      status.hidden = true;
    } catch (error) {
      status.classList.add("has-error");
      status.textContent = error.photoError
        ? `Not saved. ${error.message} Your questionnaire answers are still here.`
        : "Not saved. Keep this form open and try saving again. Do not close the app until you see the save confirmation.";
      status.scrollIntoView({ behavior: "smooth", block: "center" });
      alertUser(status.textContent, "error");
    } finally {
      saving = false;
      form.removeAttribute("aria-busy");
      photoPickers.forEach((picker) => { picker.inert = false; });
      $("[data-cancel-visit]").disabled = false;
      buttons.forEach((button, index) => { button.disabled = false; button.textContent = labels[index]; });
    }
  };
  $("[data-ct-form]").addEventListener("submit", saveEntry(saveCentralized));
  $("[data-followup-form]").addEventListener("submit", saveEntry(saveFollowup));
  const markUnfinished = (event) => {
    const form = event.target.closest("[data-ct-form], [data-followup-form]");
    if (form && !event.target.matches("[data-ct-group], [data-fu-group], [data-fu-search], [data-fu-unvisited]")) unfinishedForms.add(form);
  };
  root.addEventListener("input", markUnfinished);
  root.addEventListener("change", markUnfinished);
  root.addEventListener("field-app:before-update", (event) => {
    if (!storageReady || saving || syncing || preparing) {
      event.detail.reason = "Please wait for the app to finish loading, saving or synchronizing, then try the update again.";
      event.preventDefault();
    } else if (unfinishedForms.size) {
      event.detail.reason = "You have an unfinished entry. Save it on this tablet before updating the app.";
      event.preventDefault();
    }
  });
  window.addEventListener("online", () => { updateConnection(); if (fieldPackage && currentOutbox().length) synchronize(); });
  window.addEventListener("offline", updateConnection);
  window.addEventListener("beforeunload", (event) => {
    if (saving || unfinishedForms.size) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
  start().catch(() => alertUser("This browser could not open the tablet's offline storage.", "error"));
})();
