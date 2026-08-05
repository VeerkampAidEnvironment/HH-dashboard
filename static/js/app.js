document.addEventListener("DOMContentLoaded", () => {
  const menuButton = document.querySelector("[data-menu-toggle]");
  const navigation = document.querySelector("[data-main-nav]");
  menuButton?.addEventListener("click", () => navigation?.classList.toggle("open"));

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
      const monthFarmers = new Map((timeline.months || []).map((month) => [month, new Set()]));
      const allFarmers = new Set();
      const filteredEvents = (timeline.events || []).filter((event) => {
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
        (timeline.months || []).forEach((month) => {
          monthCategories.set(month, new Map(categories.map((category) => [category, new Set()])));
        });
        filteredEvents.forEach((event) => monthCategories.get(event.month)?.get(categoryLabel(event))?.add(event.farmer));
      }
      const items = (timeline.months || []).map((month) => {
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
      if (stackMode !== "total") details.push(`stacked by ${stackMode === "topic" ? "training type" : stackMode}`);
      if (description) description.textContent = `Showing ${details.join(" · ")} by month.`;
      if (result) result.textContent = `${allFarmers.size} farmers`;
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
      cbfBoxes.forEach((box) => { box.checked = false; });
      updateCbfSummary();
      render();
    });
    updateCbfSummary();
    render();
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
});
