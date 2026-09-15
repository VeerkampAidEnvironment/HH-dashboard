/* Same-name identity review. All changes remain a draft until Save identity split. */
(() => {
  "use strict";
  const dirtyWorkspaces = new Set();
  const node = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  const button = (text, action, className = "button button-ghost") => {
    const element = node("button", text, className);
    element.type = "button";
    element.addEventListener("click", action);
    return element;
  };
  const describe = (field, records) => {
    const values = records.map(record => field.normalized[record.id] || "");
    const distinct = new Set(values.filter(Boolean));
    return {conflict: distinct.size > 1, missing: values.includes("") && distinct.size > 0,
      populated: distinct.size > 0};
  };

  function initialize(root) {
    const data = JSON.parse(root.querySelector("[data-identity-data]").textContent);
    const find = name => root.querySelector(`[data-${name}]`);
    const records = data.records;
    const groups = [];
    const assignments = new Map(records.map(record => [record.id, null]));
    let active = null;
    let sequence = 0;
    let previewVersion = 0;
    let previewTimer;
    let previewController;
    let pending = false;
    let previewError = "";
    let saving = false;
    let draggedRecord = null;
    const members = group => records.filter(record => assignments.get(record.id) === group.id);
    const currentGroup = () => groups.find(group => group.id === active);
    const occupied = () => groups.filter(group => members(group).length);
    const groupName = group => `Person ${group.id}`;
    const profileConflictCount = group => data.fields.filter(field => !field.history && describe(field, members(group)).conflict).length;
    const newGroup = () => {
      const group = {id: ++sequence, survivor_id: null, choices: {}, plan: null};
      groups.push(group);
      return group;
    };
    records.forEach(record => {
      const group = newGroup();
      group.survivor_id = record.id;
      assignments.set(record.id, group.id);
    });
    const payload = () => ({revision: data.revision, record_ids: records.map(record => record.id),
      groups: occupied().map(group => ({record_ids: members(group).map(record => record.id),
        survivor_id: group.survivor_id, choices: group.choices}))});

    function assign(record, group) {
      const previous = groups.find(item => item.id === assignments.get(record.id));
      assignments.set(record.id, group?.id ?? null);
      for (const changed of new Set([previous, group].filter(Boolean))) {
        changed.choices = {};
        changed.plan = null;
        const ids = members(changed).map(item => item.id);
        if (!ids.includes(changed.survivor_id)) changed.survivor_id = ids[0] ?? null;
      }
      if (previous && previous !== group && !members(previous).length) groups.splice(groups.indexOf(previous), 1);
      active = group?.id ?? null;
    }

    function assignToOwnPerson(record) {
      const group = groups.find(item => item.id === assignments.get(record.id));
      if (!group || members(group).length < 2 || saving) return false;
      const individual = newGroup();
      assign(record, individual);
      edit();
      return true;
    }

    function doubleClickToSeparate(element, record) {
      element.addEventListener("dblclick", event => {
        if (event.target.closest("a, button, select, option, label")) return;
        if (!assignToOwnPerson(record)) return;
        event.preventDefault();
        event.stopPropagation();
      });
    }

    function draggable(element, record) {
      element.draggable = true;
      element.title = `Drag record ${records.indexOf(record) + 1} to a person group`;
      element.addEventListener("dragstart", event => {
        if (saving) { event.preventDefault(); return; }
        draggedRecord = record;
        event.dataTransfer.effectAllowed = "move";
        event.dataTransfer.setData("text/plain", String(record.id));
        element.classList.add("is-dragging");
        root.classList.add("is-dragging-record");
      });
      element.addEventListener("dragend", () => {
        draggedRecord = null;
        element.classList.remove("is-dragging");
        root.classList.remove("is-dragging-record");
        root.querySelectorAll(".is-drop-target").forEach(target => target.classList.remove("is-drop-target"));
      });
    }

    function dropTarget(element, group) {
      element.addEventListener("dragover", event => {
        if (!draggedRecord || saving || assignments.get(draggedRecord.id) === group.id) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = "move";
        element.classList.add("is-drop-target");
      });
      element.addEventListener("dragleave", event => {
        if (!element.contains(event.relatedTarget)) element.classList.remove("is-drop-target");
      });
      element.addEventListener("drop", event => {
        event.preventDefault();
        event.stopPropagation();
        if (!draggedRecord || saving || assignments.get(draggedRecord.id) === group.id) return;
        assign(draggedRecord, group);
        draggedRecord = null;
        root.classList.remove("is-dragging-record");
        edit();
      });
    }

    function edit() {
      dirtyWorkspaces.add(root);
      find("confirm-panel").hidden = true;
      find("save-error").hidden = true;
      previewError = "";
      pending = true;
      for (const group of groups) group.plan = null;
      previewVersion++;
      previewController?.abort();
      clearTimeout(previewTimer);
      render(); // Highlight the selected group's differences before the server round trip.
      previewTimer = setTimeout(validate, 120);
    }

    async function request(url, body, signal) {
      const response = await fetch(url, {method: "POST", credentials: "same-origin", signal,
        headers: {"Content-Type": "application/json", "X-CSRF-Token": root.dataset.csrf},
        body: JSON.stringify(body)});
      const result = await response.json().catch(() => null);
      if (!response.ok || !result?.ok) throw new Error(result?.error || "Could not check this split. Check your connection and sign-in, then retry.");
      return result;
    }

    async function validate() {
      const version = previewVersion;
      const submitted = occupied();
      previewController = new AbortController();
      try {
        const result = await request(root.dataset.previewUrl, payload(), previewController.signal);
        if (version !== previewVersion) return;
        submitted.forEach((group, index) => { group.plan = result.groups[index]; });
      } catch (error) {
        if (version !== previewVersion || error.name === "AbortError") return;
        previewError = error.message;
      }
      if (version !== previewVersion) return;
      pending = false;
      renderTabs();
      renderComparison();
      renderSummary();
    }

    function renderRecords() {
      const list = find("record-list");
      const scrollTop = list.scrollTop;
      list.replaceChildren();
      records.forEach((record, index) => {
        const group = groups.find(item => item.id === assignments.get(record.id));
        const card = node("div", undefined, "identity-record");
        card.dataset.recordId = record.id;
        draggable(card, record);
        doubleClickToSeparate(card, record);
        if (group) dropTarget(card, group);
        card.classList.toggle("is-current", Boolean(group && group.id === active));
        const heading = node("div", undefined, "identity-record-heading");
        heading.append(node("span", "⠿", "identity-drag-handle"), node("b", `#${index + 1}`), node("strong", record.uid));
        const link = node("a", "Open");
        link.href = record.url;
        link.target = "_blank";
        link.rel = "noopener";
        link.setAttribute("aria-label", `Open full record ${record.uid}, source row ${record.source_row || "manual"}`);
        heading.append(link);
        card.append(heading, node("small", `Source row ${record.source_row || "manual"} · ${record.village || "Village missing"}`),
          node("p", `${record.group_name || "Farmer group missing"} · ${record.cbf_name || "CBF missing"}`));
        const strongest = data.pairs.find(pair => pair.record_ids.includes(record.id) && pair.score > 0);
        if (strongest) {
          const otherId = strongest.record_ids.find(id => id !== record.id);
          const match = node("small", `Best match: #${records.findIndex(item => item.id === otherId) + 1} · ${strongest.score}%`, "identity-record-match");
          match.title = `Matches on: ${strongest.reasons.join(", ")}${strongest.warnings.length ? `. ${strongest.warnings.join("; ")}` : ""}`;
          card.append(match);
        }
        if (group && members(group).length > 1) {
          card.append(node("small", "Double-click this record to make it an individual", "identity-separate-hint"));
        }
        const label = node("label", "Person group");
        const select = node("select");
        select.setAttribute("aria-label", `Person group for record ${index + 1}, ${record.uid}`);
        groups.forEach(item => {
          const count = members(item).length;
          select.append(new Option(`${groupName(item)} (${count} record${count === 1 ? "" : "s"})`, String(item.id)));
        });
        select.append(new Option("+ New person / individual", "new"));
        select.value = group ? String(group.id) : "";
        select.addEventListener("change", () => {
          assign(record, select.value === "new" ? newGroup() : groups.find(item => item.id === Number(select.value)));
          edit();
          find("record-list").querySelectorAll("select")[index]?.focus({preventScroll: true});
        });
        label.append(select);
        card.append(label);
        list.append(card);
      });
      find("assignment-count").textContent = `${occupied().length} people`;
      list.scrollTop = scrollTop;
    }

    function renderTabs() {
      const tabs = find("group-tabs");
      const scrollTop = tabs.scrollTop;
      tabs.replaceChildren();
      const tab = (text, id, warning, group) => {
        const card = node("div", undefined, "identity-group-card");
        const control = button(text, () => { active = id; render(); }, "identity-group-tab");
        control.classList.toggle("is-active", active === id);
        control.classList.toggle("has-conflicts", Boolean(warning));
        control.setAttribute("aria-pressed", String(active === id));
        card.append(control);
        if (group) {
          card.dataset.personGroup = group.id;
          card.setAttribute("aria-label", `${groupName(group)} drop area`);
          dropTarget(card, group);
          members(group).forEach(record => {
            const chip = node("span", `⠿ #${records.indexOf(record) + 1} · Row ${record.source_row || "manual"}`, "identity-group-member");
            chip.dataset.recordId = record.id;
            draggable(chip, record);
            doubleClickToSeparate(chip, record);
            if (members(group).length > 1) chip.title += ". Double-click to make this record an individual";
            card.append(chip);
          });
          const differences = data.fields.filter(field => !field.history && describe(field, members(group)).conflict);
          if (differences.length) {
            card.append(node("small", `Conflicts: ${differences.map(field => field.label).join(", ")}`, "identity-group-conflicts"));
          }
          if (group.plan?.conflicts.some(conflict => conflict.history)) {
            card.append(node("small", "Conflicting training event values", "identity-group-conflicts"));
          }
          card.append(node("small", "Drop a record here", "identity-drop-hint"));
        } else {
          card.classList.add("identity-all-card");
        }
        tabs.append(card);
      };
      tab(`All records (${records.length})`, null);
      groups.forEach(group => {
        const count = members(group).length;
        const conflicts = group.plan ? group.plan.unresolved : profileConflictCount(group);
        tab(`${groupName(group)} · ${count}${conflicts ? ` · ${conflicts} to resolve` : count === 1 ? " · Individual" : ""}`,
          group.id, conflicts || group.plan?.errors.length, group);
      });
      tabs.scrollTop = scrollTop;
    }

    function choose(group, key, token) {
      group.choices[key] = token;
      edit();
    }

    function renderComparison() {
      const group = currentGroup();
      const selected = group ? members(group) : records;
      const summary = find("group-summary");
      summary.replaceChildren();
      const title = node("div");
      title.append(node("h4", group ? `${groupName(group)} · ${selected.length} record${selected.length === 1 ? "" : "s"}` : "Compare everyone with this name"));
      title.append(node("p", group ? selected.length > 1 ? "Choose which value to keep in each highlighted profile conflict." : "One record represents one individual. Drop another record onto this group to compare them." : "Drag matching records into the same person group. Differences here compare all records; they do not need resolving across different people."));
      summary.append(title);
      if (group && !selected.length) summary.append(button("Remove empty group", () => {
        groups.splice(groups.indexOf(group), 1); active = null; render();
      }));
      if (group && selected.length > 1) {
        const label = node("label", "Retained beneficiary ID");
        const select = node("select");
        selected.forEach(record => select.append(new Option(`${record.uid} · Row ${record.source_row || "manual"}`, String(record.id))));
        select.value = String(group.survivor_id);
        select.addEventListener("change", () => { group.survivor_id = Number(select.value); edit(); });
        label.append(select);
        summary.append(label);
      }
      const wrap = find("comparison-table");
      const scrollLeft = wrap.scrollLeft;
      const scrollTop = wrap.scrollTop;
      wrap.replaceChildren();
      if (!selected.length) {
        wrap.append(node("p", "Assign a record to this person using the selectors on the left.", "identity-empty"));
      } else {
        const table = node("table", undefined, "identity-matrix");
        const caption = node("caption", group ? `${groupName(group)} record comparison` : "All same-name records");
        caption.className = "identity-sr-only";
        table.append(caption);
        const head = node("thead");
        const header = node("tr");
        const fieldHead = node("th", "Field"); fieldHead.scope = "col"; header.append(fieldHead);
        selected.forEach(record => {
          const cell = node("th"); cell.scope = "col";
          cell.append(node("strong", `#${records.indexOf(record) + 1} · ${record.uid}`), node("small", `Source row ${record.source_row || "manual"}`));
          if (group && record.id === group.survivor_id) cell.append(node("small", "Retained ID", "identity-retained"));
          header.append(cell);
        });
        head.append(header); table.append(head);
        const body = node("tbody");
        let visible = 0;
        data.fields.forEach(field => {
          const state = describe(field, selected);
          const filter = find("field-filter").value;
          if (!state.populated || (filter === "differences" && !state.conflict && !state.missing) ||
            (filter === "conflicts" && (!state.conflict || field.history))) return;
          visible++;
          const row = node("tr");
          const label = node("th", field.label); label.scope = "row";
          if (state.conflict) label.append(node("small", field.history ? "History differs" : "Conflict", field.history ? "identity-history-label" : "identity-conflict-label"));
          else if (state.missing) label.append(node("small", "Missing information", "identity-missing-label"));
          row.append(label);
          selected.forEach(record => {
            const cell = node("td");
            const populated = Boolean(field.normalized[record.id]);
            cell.append(node("span", populated ? String(field.values[record.id]) : "Missing"));
            cell.className = !populated ? "identity-cell-missing" : state.conflict ? field.history ? "identity-cell-history" : "identity-cell-conflict" : "";
            if (group && state.conflict && !field.history && populated) {
              const token = JSON.stringify([record.id, field.key]);
              const chosen = group.choices[field.key] === token;
              const control = button(chosen ? "✓ Keep this value" : "Use this value", () => choose(group, field.key, token), "identity-value-choice");
              control.setAttribute("aria-label", `${chosen ? "Keeping" : "Keep"} ${field.label}: ${field.values[record.id]} from ${record.uid}`);
              control.setAttribute("aria-pressed", String(chosen));
              cell.classList.toggle("is-chosen", chosen);
              cell.append(control);
            }
            row.append(cell);
          });
          body.append(row);
        });
        table.append(body);
        wrap.append(table);
        if (!visible) wrap.append(node("p", "No differences in this view. Choose All populated fields to inspect the full profiles.", "identity-empty"));
      }
      wrap.scrollLeft = scrollLeft;
      wrap.scrollTop = scrollTop;
      const resolutions = find("conflict-choices");
      resolutions.replaceChildren();
      if (selected.length > 1 && data.fields.some(field => field.history)) {
        resolutions.append(node("p", "History differences are shown in blue. Distinct training events are combined; conflicting values for the same event must be resolved below.", "identity-history-note"));
      }
      if (group?.plan) {
        group.plan.errors.forEach(error => resolutions.append(node("p", error, "notice error")));
        group.plan.conflicts.filter(conflict => conflict.history).forEach(conflict => {
          const fieldset = node("fieldset", undefined, "identity-history-conflict");
          fieldset.append(node("legend", conflict.label), node("p", "These values refer to the same event. Choose the value to keep."));
          conflict.options.forEach(option => {
            const chosen = group.choices[conflict.key] === option.token;
            const control = button(`${chosen ? "✓ " : ""}${option.value} · ${option.uid}`, () => choose(group, conflict.key, option.token), "identity-value-choice");
            control.setAttribute("aria-pressed", String(chosen));
            fieldset.append(control);
          });
          resolutions.append(fieldset);
        });
      }
    }

    function renderSummary() {
      const populated = occupied();
      const unassigned = records.filter(record => assignments.get(record.id) === null).length;
      const multi = populated.filter(group => members(group).length > 1).length;
      const single = populated.length - multi;
      find("split-summary").textContent = `${multi} group${multi === 1 ? "" : "s"} to merge · ${single} individual${single === 1 ? "" : "s"} · ${unassigned} unassigned`;
      const unresolved = populated.reduce((total, group) => total + (group.plan?.unresolved ?? 0), 0);
      const errors = populated.reduce((total, group) => total + (group.plan?.errors.length ?? 0), 0);
      const ready = !pending && !previewError && !unassigned && !unresolved && !errors && populated.every(group => group.plan);
      find("review-split").disabled = !ready || saving;
      const status = find("live-status");
      status.replaceChildren();
      status.append(node("span", previewError || (pending ? "Checking group conflicts and programme history…" : unresolved ? `${unresolved} conflict${unresolved === 1 ? " still needs" : "s still need"} a value. Open the marked person groups.` : errors ? "Review the training-history issues in the marked groups." : unassigned ? "Assign all records, or keep the remaining records as individuals." : "All groups checked. Ready to review your split.")));
      if (previewError) status.append(button("Retry check", edit));
      return ready;
    }

    function render() { renderRecords(); renderTabs(); renderComparison(); renderSummary(); }
    find("new-group").addEventListener("click", () => { active = newGroup().id; render(); });
    find("field-filter").addEventListener("change", renderComparison);
    find("review-split").addEventListener("click", () => {
      if (!renderSummary()) return;
      const review = find("confirm-groups"); review.replaceChildren();
      occupied().forEach(group => {
        const items = members(group);
        const retained = items.find(record => record.id === group.survivor_id);
        const item = node("div", undefined, "identity-confirm-group");
        item.append(node("strong", `${groupName(group)} · ${items.length === 1 ? "Individual" : `${items.length} records combined`}`),
          node("span", items.map(record => `${record.uid} (row ${record.source_row || "manual"})`).join(" + ")),
          node("small", `Retain ${retained.uid}`));
        review.append(item);
      });
      find("confirm-panel").hidden = false;
      find("confirm-panel").scrollIntoView({block: "nearest", behavior: "smooth"});
      find("save-split").focus({preventScroll: true});
    });
    find("back-edit").addEventListener("click", () => { find("confirm-panel").hidden = true; find("review-split").focus(); });
    find("save-split").addEventListener("click", async () => {
      if (saving || !renderSummary()) return;
      saving = true;
      root.querySelectorAll("button, select").forEach(control => { control.disabled = true; });
      find("save-split").textContent = "Saving…";
      try {
        const result = await request(root.dataset.saveUrl, payload());
        dirtyWorkspaces.delete(root);
        root.replaceChildren(node("div", `Saved: ${records.length} records split into ${result.people} people. ${result.archived} duplicate records archived. The groups will stay separate.`, "notice success"));
        root.closest("details").querySelector(".identity-case-state").textContent = "Saved";
      } catch (error) {
        saving = false;
        root.querySelectorAll("button, select").forEach(control => { control.disabled = false; });
        find("save-split").textContent = "Save identity split";
        find("save-error").textContent = `${error.message} Your draft is still on this page. If the connection failed during saving, reload to check whether it was saved before trying again.`;
        find("save-error").hidden = false;
        renderSummary();
      }
    });
    const suggestedIds = new Set();
    const strongestPairs = data.pairs.filter(pair => {
      if (pair.score <= 0 || pair.record_ids.some(id => suggestedIds.has(id))) return false;
      pair.record_ids.forEach(id => suggestedIds.add(id));
      return true;
    }).slice(0, 3);
    strongestPairs.forEach(pair => {
      const numbers = pair.record_ids.map(id => `#${records.findIndex(record => record.id === id) + 1}`);
      const suggestion = node("span", `${numbers.join(" + ")} · ${pair.score}% similarity`, "identity-match-pair");
      suggestion.title = `Matches on: ${pair.reasons.join(", ")}${pair.warnings.length ? `. ${pair.warnings.join("; ")}` : ""}`;
      find("match-suggestions").append(suggestion);
    });
    if (strongestPairs.length) find("match-suggestions").prepend(node("strong", "Strongest suggested pairs"));
    pending = true;
    render();
    validate();
  }
  document.querySelectorAll("[data-identity-workspace]").forEach(root => {
    const panel = root.closest("details");
    let initialized = false;
    const open = () => {
      if (!panel.open || initialized) return;
      initialized = true;
      initialize(root);
    };
    panel.addEventListener("toggle", open);
    open();
  });
  window.addEventListener("beforeunload", event => {
    if (dirtyWorkspaces.size) { event.preventDefault(); event.returnValue = ""; }
  });
})();
