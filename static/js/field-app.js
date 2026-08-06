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
    .flatMap((item) => item.responses.map((entry) => `${item.recordId}::${entry.topic}`)));

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

  const questionInput = (question, topicIndex) => {
    const name = `fu__${topicIndex}__${question.id}`;
    const wrap = create("section", `field-question field-question-${question.type}`);
    const copy = create("div", "field-question-copy");
    copy.append(create("small", "", question.source_id), create("h3", "", question.label));
    if (question.help) copy.append(create("p", "", question.help));
    wrap.append(copy);
    const options = question.options || [];
    if (question.type === "textarea") {
      const input = document.createElement("textarea");
      input.name = name;
      input.rows = 3;
      input.placeholder = "Enter observations or advice";
      wrap.append(input);
    } else if (question.type === "number" || question.type === "adoption_rate") {
      const numberWrap = create("div", "field-number-input");
      const input = document.createElement("input");
      input.type = "number";
      input.name = name;
      input.min = "0";
      input.step = question.type === "adoption_rate" ? "0.1" : "any";
      if (question.type === "adoption_rate") input.max = "100";
      numberWrap.append(input);
      if (question.type === "adoption_rate") numberWrap.append(create("span", "", "%"));
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
    } else if ((question.type === "choice" || question.type === "next_action") && options.length <= 6) {
      const choices = create("div", "field-choice-grid");
      options.forEach((option) => {
        const label = create("label", "field-choice");
        const input = document.createElement("input");
        input.type = "radio";
        input.name = name;
        input.value = typeof option === "object" ? option.value : option;
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
    farmer.availableFollowups.forEach((status, topicIndex) => {
      const questionnaire = fieldPackage.questionnaires[status.topic];
      const details = create("details", "field-questionnaire");
      details.dataset.topic = status.topic;
      if (topicIndex === 0) details.open = true;
      const summary = create("summary");
      const title = create("span");
      title.append(create("strong", "", questionnaire.title), create("small", "", `${status.topic} · Last activity ${status.lastActivityDate || "unknown"}`));
      summary.append(title, create("b", "", `${questionnaire.questions.length} questions`));
      const questions = create("div", "field-question-list");
      questionnaire.questions.forEach((question) => questions.append(questionInput(question, topicIndex)));
      details.append(summary, questions);
      container.append(details);
    });
    container.hidden = false;
    $("[data-followup-save]").hidden = false;
    $("[data-followup-selection]").textContent = `${farmer.availableFollowups.length} ${farmer.availableFollowups.length === 1 ? "topic" : "topics"} due for ${farmer.name}`;
    container.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  const answersForTopic = (details, questionnaire, topicIndex) => {
    const answers = {};
    let hasAnswer = false;
    questionnaire.questions.forEach((question) => {
      const name = `fu__${topicIndex}__${question.id}`;
      let value;
      if (question.type === "multi") {
        value = $$(`input[name="${CSS.escape(name)}"]:checked`, details).map((input) => input.value);
      } else if (question.type === "choice" || question.type === "next_action") {
        value = $(`input[name="${CSS.escape(name)}"]:checked`, details)?.value
          ?? $(`select[name="${CSS.escape(name)}"]`, details)?.value ?? "";
      } else {
        value = $(`[name="${CSS.escape(name)}"]`, details)?.value?.trim() ?? "";
      }
      answers[question.id] = value;
      hasAnswer = hasAnswer || (Array.isArray(value) ? value.length > 0 : value !== "");
    });
    return { answers, hasAnswer };
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
        const completedTopics = new Set(submission.responses.map((entry) => entry.topic));
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
    const responses = [];
    const detailsNodes = $$("[data-followup-questionnaires] details[data-topic]");
    for (const [topicIndex, details] of detailsNodes.entries()) {
      const topic = details.dataset.topic;
      const questionnaire = fieldPackage.questionnaires[topic];
      const { answers, hasAnswer } = answersForTopic(details, questionnaire, topicIndex);
      if (!hasAnswer) continue;
      const adoption = Number(answers.adoption_rate);
      if (answers.adoption_rate === "" || !Number.isFinite(adoption) || adoption < 0 || adoption > 100) {
        details.open = true;
        alertUser(`Enter an adoption rate between 0 and 100 for ${topic}.`, "warning");
        return;
      }
      if (!answers.next_action) {
        details.open = true;
        alertUser(`Choose what should happen next for ${topic}.`, "warning");
        return;
      }
      responses.push({ topic, answers });
    }
    if (!responses.length) {
      alertUser("Complete at least one follow-up questionnaire.", "warning");
      return;
    }
    await queueSubmission({
      id: submissionId(),
      type: "followup",
      environment,
      cbf: currentCbf,
      eventDate: form.elements.eventDate.value,
      recordId: selectedFarmerId,
      responses,
      geoLocation: capturedLocation ? { ...capturedLocation } : null,
      createdAt: new Date().toISOString(),
      status: "pending",
      summary: `${farmer.name} · ${responses.length} ${responses.length === 1 ? "topic" : "topics"}`,
    });
    selectedFarmerId = null;
    capturedLocation = null;
    updateLocationStatus();
    $("[data-followup-questionnaires]").hidden = true;
    $("[data-followup-save]").hidden = true;
    renderFarmerList();
    alertUser("Follow-up saved safely on this tablet.", "success");
    openPane("home");
    if (navigator.onLine) synchronize();
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

  const synchronize = async () => {
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
  $$('[data-sync-now]').forEach((button) => button.addEventListener("click", synchronize));
  $$('[data-field-tab]').forEach((button) => button.addEventListener("click", () => openPane(button.dataset.fieldTab)));
  $$('[data-open-pane]').forEach((button) => button.addEventListener("click", () => openPane(button.dataset.openPane)));
  $("[data-ct-group]").addEventListener("change", renderCentralizedTopics);
  $("[data-fu-group]").addEventListener("change", () => { selectedFarmerId = null; renderFarmerList(); });
  $("[data-fu-search]").addEventListener("input", renderFarmerList);
  $("[data-ct-form]").addEventListener("submit", saveCentralized);
  $("[data-followup-form]").addEventListener("submit", saveFollowup);
  $("[data-capture-location]").addEventListener("click", captureCurrentLocation);
  $("[data-clear-location]").addEventListener("click", () => {
    capturedLocation = null;
    updateLocationStatus();
  });
  window.addEventListener("online", () => { updateConnection(); if (fieldPackage && currentOutbox().length) synchronize(); });
  window.addEventListener("offline", updateConnection);
  start().catch(() => alertUser("This browser could not open the tablet's offline storage.", "error"));
})();
