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
