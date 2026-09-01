(() => {
  "use strict";

  const root = document.querySelector("[data-field-app]");
  const configNode = document.getElementById("field-app-config");
  if (!root || !configNode) return;

  const config = JSON.parse(configNode.textContent);
  const DB_NAME = "arfsa-ae-field-app";
  const DB_VERSION = 2;
  const environment = config.environment || "live";
  let fieldPackage = null;
  let currentCbf = config.assignedCbf || "";
  let outbox = [];
  let selectedFarmerId = null;
  let capturedLocation = null;
  let syncing = false;

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
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });

  const dbRequest = async (storeName, mode, operation) => {
    const db = await dbPromise;
    return new Promise((resolve, reject) => {
      const transaction = db.transaction(storeName, mode);
      const store = transaction.objectStore(storeName);
      const request = operation(store);
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
  };
  const getStored = (store, key) => dbRequest(store, "readonly", (target) => target.get(key));
  const getAllStored = (store) => dbRequest(store, "readonly", (target) => target.getAll());
  const putStored = (store, value) => dbRequest(store, "readwrite", (target) => target.put(value));
  const deleteStored = (store, key) => dbRequest(store, "readwrite", (target) => target.delete(key));
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
    $("span", status).textContent = online ? "Online" : "Offline — entries save locally";
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
  const queuedFuKeys = () => new Set(currentOutbox()
    .filter((item) => item.type === "followup")
    .flatMap((item) => (item.topics || (item.responses || []).map((entry) => entry.topic))
      .map((topic) => `${item.recordId}::${topic}`)));

  const refreshCounts = () => {
    const items = currentOutbox();
    $$('[data-summary-pending], [data-pending-badge]').forEach((node) => { node.textContent = items.length; });
    $("[data-summary-pending-label]").textContent = items.length === 1 ? "tablet submission" : "tablet submissions";
  };

  const openPane = (name) => {
    $$('[data-field-tab]').forEach((button) => button.classList.toggle("active", button.dataset.fieldTab === name));
    $$('[data-field-pane]').forEach((pane) => {
      const active = pane.dataset.fieldPane === name;
      pane.classList.toggle("active", active);
      pane.hidden = !active;
    });
    if (name === "pending") renderOutbox();
    if (name === "centralized") renderCentralizedTopics();
    if (name === "followup") renderFarmerList();
    window.scrollTo({ top: 0, behavior: "smooth" });
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
    populateFilters();
    renderCentralizedTopics();
    renderFarmerList();
    refreshCounts();
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
    button.disabled = true;
    button.textContent = "Preparing…";
    try {
      const response = await fetch(`${config.bootstrapUrl}?cbf=${encodeURIComponent(cbf)}`, {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
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
    const farmers = dueFarmers().filter((farmer) => {
      if (group && farmer.group !== group) return false;
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
    if (question.condition) wrap.dataset.condition = JSON.stringify(question.condition);
    if (question.skip_condition) wrap.dataset.skipCondition = JSON.stringify(question.skip_condition);
    wrap.dataset.required = question.required ? "true" : "false";
    if (question.max_selections) wrap.dataset.maxSelections = String(question.max_selections);
    if (question.exclusive_values) wrap.dataset.exclusiveValues = JSON.stringify(question.exclusive_values);
    const copy = create("div", "field-question-copy");
    copy.append(create("small", "", question.source_id), create("h3", "", question.label));
    if (question.help) copy.append(create("p", "", question.help));
    if (question.required) copy.append(create("b", "field-required", "Required"));
    wrap.append(copy);
    const options = question.options || [];
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
        const editorInput = document.createElement("input");
        editorInput.type = question.id === "a8_contact" ? "tel" : "text";
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
      const input = document.createElement("input");
      input.type = "file";
      input.name = name;
      input.accept = "image/jpeg,image/png,image/webp,image/heic,image/heif";
      input.multiple = true;
      const button = create("span", "button button-secondary", "Choose photos");
      const status = create("small", "", "Up to 6 photos · JPG, PNG, WebP or HEIC");
      input.addEventListener("change", () => {
        status.textContent = input.files.length ? `${input.files.length} ${input.files.length === 1 ? "photo" : "photos"} selected` : "Up to 6 photos · JPG, PNG, WebP or HEIC";
      });
      upload.append(input, button, status);
      wrap.append(upload);
    } else if (question.type === "textarea") {
      const input = document.createElement("textarea");
      input.name = name;
      input.rows = 3;
      input.placeholder = "Enter observations or advice";
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
    renderFarmerList();
    const farmer = dueFarmers().find((item) => item.id === farmerId);
    const container = $("[data-followup-questionnaires]");
    container.replaceChildren();
    if (!farmer) {
      container.hidden = true;
      $("[data-followup-save]").hidden = true;
      return;
    }
    const heading = create("div", "field-selected-farmer");
    heading.append(create("span", "field-farmer-avatar", farmer.name.slice(0, 1).toUpperCase()));
    const copy = create("div");
    copy.append(create("small", "", "Selected beneficiary"), create("h2", "", farmer.name), create("p", "", [farmer.uid, farmer.group || "No group", farmer.village].filter(Boolean).join(" · ")));
    heading.append(copy);
    container.append(heading);
    const dueTopics = farmer.availableFollowups.map((item) => item.topic);
    const dueSet = new Set(dueTopics);
    const sections = (fieldPackage.survey?.sections || []).filter((section) =>
      section.always || (section.topics || []).some((topic) => dueSet.has(topic)));
    const dueCard = create("div", "field-survey-due");
    dueCard.append(create("strong", "", "Training types due"));
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
        let position = active.findIndex((node) => node.dataset.surveyQuestion === currentQuestionId);
        if (position < 0) position = 0;
        const current = active[position];
        if (current) currentQuestionId = current.dataset.surveyQuestion;
        questionNodes.forEach((node) => node.classList.toggle("survey-guide-hidden", node !== current));
        previousQuestion.hidden = position <= 0;
        nextQuestion.hidden = !current || position >= active.length - 1;
        actions.hidden = !current || position < active.length - 1;
        questionProgress.textContent = current ? `Question ${position + 1} of ${active.length}` : "No applicable questions";
      };
      previousQuestion.addEventListener("click", () => {
        const active = questionNodes.filter((node) => !node.hidden);
        const position = active.findIndex((node) => node.dataset.surveyQuestion === currentQuestionId);
        if (position > 0) currentQuestionId = active[position - 1].dataset.surveyQuestion;
        refreshQuestionGuide();
      });
      nextQuestion.addEventListener("click", () => {
        const active = questionNodes.filter((node) => !node.hidden);
        const position = active.findIndex((node) => node.dataset.surveyQuestion === currentQuestionId);
        if (!validateFieldGuideQuestion(active[position], container)) return;
        if (position >= 0 && position < active.length - 1) currentQuestionId = active[position + 1].dataset.surveyQuestion;
        refreshQuestionGuide();
      });
      details.refreshQuestionGuide = refreshQuestionGuide;
      details.append(summary, questions, guide, actions);
      container.append(details);
    });
    const finalize = create("div", "field-survey-finalize");
    finalize.hidden = true;
    finalize.append(create("strong", "", "All packages completed"), create("p", "", "Save the visit. The server will calculate package scores, Breadth, Depth and recommended next actions during synchronization."));
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
    $$('[data-survey-question]', container).forEach((node) => {
      const condition = node.dataset.condition ? JSON.parse(node.dataset.condition) : null;
      const skipCondition = node.dataset.skipCondition ? JSON.parse(node.dataset.skipCondition) : null;
      const visible = surveyConditionMatches(container, condition) && !(skipCondition && surveyConditionMatches(container, skipCondition));
      node.hidden = !visible;
      $$('input, select, textarea', node).forEach((control) => { control.disabled = !visible; });
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
    const files = Array.from($('input[type="file"]', node)?.files || []);
    if (files.length > 6) throw new Error("Upload no more than 6 photos for one photo question.");
    if (files.some((file) => file.size > 8 * 1024 * 1024)) throw new Error("Each follow-up photo must be smaller than 8 MB.");
    return Promise.all(files.map((file) => new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve({ name: file.name, type: file.type, data: reader.result });
      reader.onerror = () => reject(new Error(`Could not read ${file.name}.`));
      reader.readAsDataURL(file);
    })));
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
      if (fileInput?.files.length > 6) {
        alertUser("Upload no more than 6 photos for one photo question.", "warning");
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
    const householdTotalValue = surveyValue(container, "a10_total");
    const householdMaleValue = surveyValue(container, "a10_male");
    const householdFemaleValue = surveyValue(container, "a10_female");
    if (householdTotalValue !== "") {
      const householdTotal = Number(householdTotalValue);
      const householdMale = householdMaleValue === "" ? 0 : Number(householdMaleValue);
      const householdFemale = householdFemaleValue === "" ? 0 : Number(householdFemaleValue);
      if (householdMale > householdTotal) {
        alertUser("A10.2 cannot be greater than the total household size in A10.1.", "warning");
        return false;
      }
      if (householdFemale > householdTotal) {
        alertUser("A10.3 cannot be greater than the total household size in A10.1.", "warning");
        return false;
      }
      if (householdMaleValue !== "" && householdFemaleValue !== "" && householdMale + householdFemale > householdTotal) {
        alertUser("A10.2 and A10.3 together cannot be greater than the total household size in A10.1.", "warning");
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

  const queueSubmission = async (submission) => {
    await putStored("outbox", submission);
    outbox = await getAllStored("outbox");
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
      const farmer = fieldPackage.farmers.find((item) => item.id === submission.recordId);
      if (farmer) {
        const completedTopics = new Set(submission.topics || (submission.responses || []).map((entry) => entry.topic));
        farmer.followupTopics = farmer.followupTopics.filter((item) => !completedTopics.has(item.topic));
      }
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
    let surveyAnswers;
    try {
      surveyAnswers = await answersForSurvey(container);
    } catch (error) {
      alertUser(error.message || "One of the selected photos could not be read.", "warning");
      return;
    }
    const { answers, firstMissing } = surveyAnswers;
    if (firstMissing) {
      firstMissing.closest("details")?.setAttribute("open", "");
      firstMissing.scrollIntoView({ behavior: "smooth", block: "center" });
      alertUser(`Complete required item ${$("small", firstMissing)?.textContent || "in the survey"}.`, "warning");
      return;
    }
    if ((answers.i1_helpful || []).length > 2) {
      alertUser("Select no more than two answers for I1.", "warning");
      return;
    }
    if (answers.a10_total !== undefined && answers.a10_total !== "") {
      const householdTotal = Number(answers.a10_total);
      const householdMale = answers.a10_male === "" ? 0 : Number(answers.a10_male);
      const householdFemale = answers.a10_female === "" ? 0 : Number(answers.a10_female);
      if (householdMale > householdTotal || householdFemale > householdTotal ||
          (answers.a10_male !== "" && answers.a10_female !== "" && householdMale + householdFemale > householdTotal)) {
        alertUser("Household male and female counts cannot exceed the total household size in A10.1.", "warning");
        return;
      }
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
      topics,
      answers,
      geoLocation: capturedLocation ? { ...capturedLocation } : null,
      createdAt: new Date().toISOString(),
      status: "pending",
      summary: `${farmer.name} · ${topics.length} ${topics.length === 1 ? "training type" : "training types"}`,
    };
    await queueSubmission(followupSubmission);
    selectedFarmerId = null;
    capturedLocation = null;
    updateLocationStatus();
    $("[data-followup-questionnaires]").hidden = true;
    $("[data-followup-save]").hidden = true;
    renderFarmerList();
    alertUser("Follow-up saved safely on this tablet.", "success");
    openPane("home");
    if (navigator.onLine) synchronize(followupSubmission.id);
  };

  const renderOutbox = () => {
    const list = $("[data-outbox-list]");
    const items = currentOutbox().sort((a, b) => b.createdAt.localeCompare(a.createdAt));
    list.replaceChildren();
    if (!items.length) {
      const empty = create("div", "panel field-outbox-empty");
      empty.append(create("div", "field-safety-icon", "✓"), create("h2", "", "Everything is synchronized"), create("p", "", "There are no field submissions waiting on this tablet."));
      list.append(empty);
      return;
    }
    items.forEach((item) => {
      const card = create("article", `panel field-outbox-card${item.status === "error" ? " has-error" : ""}`);
      const type = item.type === "centralized" ? "Centralized training" : "Follow-up";
      const title = create("div");
      title.append(create("span", "field-outbox-type", type), create("h3", "", item.summary), create("p", "", `${item.eventDate} · Saved ${formatDateTime(item.createdAt)}`));
      if (item.geoLocation) title.append(create("p", "field-outbox-location", `Location attached · accuracy approximately ${Math.round(item.geoLocation.accuracy)} m`));
      if (item.error) title.append(create("p", "field-outbox-error", item.error));
      const actions = create("div", "field-outbox-actions");
      actions.append(create("span", item.status === "error" ? "status-pill field-status-error" : "status-pill", item.status === "error" ? "Needs attention" : "Waiting to upload"));
      const discard = create("button", "button button-ghost button-small", "Discard");
      discard.type = "button";
      discard.addEventListener("click", async () => {
        if (!confirm("Discard this locally saved submission? It has not been added to the central database.")) return;
        await deleteStored("outbox", item.id);
        outbox = await getAllStored("outbox");
        refreshCounts();
        renderOutbox();
        renderCentralizedTopics();
        renderFarmerList();
      });
      actions.append(discard);
      card.append(title, actions);
      list.append(card);
    });
  };

  const deviceId = async () => {
    const stored = await getStored("meta", "deviceId");
    if (stored?.value) return stored.value;
    const value = submissionId();
    await putStored("meta", { key: "deviceId", value });
    return value;
  };

  const synchronize = async (resultSubmissionId = "") => {
    if (syncing) return;
    const pending = currentOutbox();
    if (!pending.length) {
      alertUser("Everything on this tablet is already synchronized.", "success");
      return;
    }
    if (!navigator.onLine) {
      alertUser("Entries remain safe on this tablet. Synchronize when internet is available.", "warning");
      return;
    }
    syncing = true;
    $$('[data-sync-now]').forEach((button) => { button.disabled = true; button.textContent = "Synchronizing…"; });
    try {
      const response = await fetch(config.syncUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": config.csrfToken, Accept: "application/json" },
        body: JSON.stringify({ cbf: currentCbf, deviceId: await deviceId(), submissions: pending.slice(0, 100) }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || !Array.isArray(payload.results)) throw new Error(payload.error || "Synchronization could not be completed. Sign in again if your session expired.");
      let accepted = 0;
      let rejected = 0;
      for (const result of payload.results) {
        if (result.status === "accepted" || result.status === "duplicate") {
          applySynchronizedSubmission(pending.find((entry) => entry.id === result.id));
          await deleteStored("outbox", result.id);
          accepted += 1;
        } else {
          const item = pending.find((entry) => entry.id === result.id);
          if (item) await putStored("outbox", { ...item, status: "error", error: result.message || "The server rejected this entry." });
          rejected += 1;
        }
      }
      if (accepted && fieldPackage) await putStored("packagesV2", { key: packageKey(currentCbf), package: fieldPackage });
      outbox = await getAllStored("outbox");
      refreshCounts();
      renderOutbox();
      if (accepted) await prepareTablet({ quiet: true });
      renderCentralizedTopics();
      renderFarmerList();
      const resultTargetId = typeof resultSubmissionId === "string" && resultSubmissionId
        ? resultSubmissionId
        : (pending.length === 1 && pending[0].type === "followup" ? pending[0].id : "");
      const resultTarget = payload.results.find((item) => item.id === resultTargetId && item.eventId && (item.status === "accepted" || item.status === "duplicate"));
      if (resultTarget) {
        window.location.assign(`/data-entry/followup-results/${resultTarget.eventId}`);
        return;
      }
      if (rejected) alertUser(`${accepted} synchronized; ${rejected} need attention. Open Pending for details.`, "warning");
      else alertUser(`${accepted} ${accepted === 1 ? "submission" : "submissions"} synchronized successfully.`, "success");
    } catch (error) {
      alertUser(error.message || "Synchronization could not be completed. Entries remain safe on this tablet.", "error");
    } finally {
      syncing = false;
      $$('[data-sync-now]').forEach((button) => { button.disabled = false; button.textContent = "Synchronize now"; });
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
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("/field-app/service-worker.js", { scope: "/field-app/" }).catch(() => {});
    }
    if (navigator.onLine && fieldPackage && currentOutbox().length) setTimeout(synchronize, 900);
  };

  $("[data-prepare]").addEventListener("click", () => prepareTablet());
  $$('[data-sync-now]').forEach((button) => button.addEventListener("click", () => synchronize()));
  $$('[data-field-tab]').forEach((button) => button.addEventListener("click", () => openPane(button.dataset.fieldTab)));
  $$('[data-open-pane]').forEach((button) => button.addEventListener("click", () => openPane(button.dataset.openPane)));
  $("[data-ct-group]").addEventListener("change", renderCentralizedTopics);
  $("[data-fu-group]").addEventListener("change", () => { selectedFarmerId = null; renderFarmerList(); });
  $("[data-fu-search]").addEventListener("input", renderFarmerList);
  $("[data-ct-form]").addEventListener("submit", saveCentralized);
  $("[data-followup-form]").addEventListener("submit", saveFollowup);
  window.addEventListener("online", () => { updateConnection(); if (fieldPackage && currentOutbox().length) synchronize(); });
  window.addEventListener("offline", updateConnection);
  start().catch(() => alertUser("This browser could not open the tablet's offline storage.", "error"));
})();
