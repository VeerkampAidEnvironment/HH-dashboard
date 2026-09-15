(() => {
  "use strict";

  const root = document.querySelector("[data-field-app]");
  const checkButton = root?.querySelector("[data-check-app-update]");
  const applyButton = root?.querySelector("[data-apply-app-update]");
  const status = root?.querySelector("[data-app-update-status]");
  if (!checkButton || !applyButton || !status) return;

  if (!("serviceWorker" in navigator) || !window.isSecureContext) {
    checkButton.disabled = true;
    status.textContent = "App updates require a supported browser and an HTTPS connection.";
    return;
  }

  let registration;
  let registering;
  let checking = false;
  let restarting = false;
  let controllerChanged = false;
  let lastCheck = 0;
  let restartTimer;
  let previousController = navigator.serviceWorker.controller;
  const watched = new WeakSet();

  const showReady = () => {
    applyButton.hidden = false;
    status.textContent = "An update is ready. Save your current entry, then restart. Saved entries and prepared field data will stay on this tablet.";
  };

  const watchInstallation = (worker) => {
    if (!worker || watched.has(worker)) return;
    watched.add(worker);
    const changed = () => {
      if (worker.state === "installed") {
        if (registration.active) showReady();
        else status.textContent = "App ready for offline use. Updates will be checked when you are online.";
      } else if (worker.state === "redundant") {
        status.textContent = "The update could not be downloaded. Your existing app is still available. Check your connection, sign in if needed, and try again.";
      } else if (worker.state === "installing") {
        status.textContent = "Downloading app files. You can keep working.";
      }
    };
    worker.addEventListener("statechange", changed);
    changed();
  };

  const getRegistration = async () => {
    if (registration) return registration;
    if (!registering) {
      registering = navigator.serviceWorker.register("/field-app/service-worker.js", {
        scope: "/field-app/", updateViaCache: "none",
      }).then((result) => {
        registration = result;
        registration.addEventListener("updatefound", () => watchInstallation(registration.installing));
        watchInstallation(registration.installing);
        if (registration.waiting) showReady();
        return registration;
      }).finally(() => { registering = null; });
    }
    return registering;
  };

  const checkForUpdates = async (manual = false) => {
    if (checking || restarting) return;
    if (!navigator.onLine) {
      if (manual) status.textContent = "You are offline. Connect to the internet to check for updates. Saved entries remain on this tablet.";
      return;
    }
    if (!manual && lastCheck && Date.now() - lastCheck < 5 * 60 * 1000) return;
    lastCheck = Date.now();
    checking = true;
    checkButton.disabled = true;
    checkButton.textContent = "Checking…";
    status.textContent = "Checking for app updates…";
    try {
      const current = await getRegistration();
      await current.update();
      if (current.waiting || controllerChanged) showReady();
      else if (current.installing) watchInstallation(current.installing);
      else status.textContent = "Your app is up to date. Saved entries and prepared field data stay on this tablet.";
    } catch (error) {
      status.textContent = "Could not check for updates. Check your connection, sign in if needed, and try again. You can keep using the app.";
    } finally {
      checking = false;
      checkButton.disabled = false;
      checkButton.textContent = "Check for updates";
    }
  };

  const unlock = () => {
    restarting = false;
    root.inert = false;
    applyButton.disabled = false;
    applyButton.textContent = "Update and restart";
    clearTimeout(restartTimer);
  };

  applyButton.addEventListener("click", () => {
    if (restarting) return;
    const guard = new CustomEvent("field-app:before-update", { cancelable: true, detail: { reason: "" } });
    if (!root.dispatchEvent(guard)) {
      status.textContent = guard.detail.reason;
      return;
    }
    if (!registration?.waiting && !controllerChanged) {
      applyButton.hidden = true;
      checkForUpdates(true);
      return;
    }
    restarting = true;
    root.inert = true;
    applyButton.disabled = true;
    applyButton.textContent = "Restarting…";
    status.textContent = "Restarting the app. Your saved entries will stay on this tablet.";
    if (controllerChanged && !registration?.waiting) {
      window.location.reload();
      return;
    }
    restartTimer = setTimeout(() => {
      unlock();
      status.textContent = "The app has not restarted yet. Please try Update and restart again.";
    }, 15000);
    try {
      registration.waiting.postMessage({ type: "SKIP_WAITING" });
    } catch (error) {
      unlock();
      status.textContent = "The update could not start. Please check for updates again.";
    }
  });

  navigator.serviceWorker.addEventListener("controllerchange", () => {
    const wasControlled = Boolean(previousController);
    previousController = navigator.serviceWorker.controller;
    if (restarting) {
      clearTimeout(restartTimer);
      window.location.reload();
    } else if (wasControlled) {
      // Another window may apply an update. Never reload this user's open form.
      controllerChanged = true;
      showReady();
    }
  });
  checkButton.addEventListener("click", () => checkForUpdates(true));
  window.addEventListener("online", () => { lastCheck = 0; checkForUpdates(); });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") checkForUpdates();
  });
  // Register even when offline so an already downloaded update can be offered.
  getRegistration().then(() => checkForUpdates()).catch(() => {
    status.textContent = "Connect to the internet and sign in to enable app updates. Saved entries remain on this tablet.";
  });
})();
