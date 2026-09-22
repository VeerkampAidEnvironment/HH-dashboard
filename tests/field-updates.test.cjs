const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const source = fs.readFileSync(path.join(__dirname, "../static/js/field-updates.js"), "utf8");
const workerSource = fs.readFileSync(path.join(__dirname, "../static/js/field-service-worker.js"), "utf8");
const tick = () => new Promise((resolve) => setImmediate(resolve));

class Node extends EventTarget {
  hidden = true;
  disabled = false;
  textContent = "";
  click() { this.dispatchEvent(new Event("click")); }
}

function ui({ waiting = null, online = true, controller = {}, installing = null, updateError = false } = {}) {
  const nodes = Object.fromEntries(["check-app-update", "apply-app-update", "app-update-status", "app-update-notice"].map((key) => [key, new Node()]));
  const root = new Node();
  root.querySelector = (selector) => nodes[selector.slice(6, -1)];
  const document = new Node();
  document.querySelector = () => root;
  document.visibilityState = "visible";
  const registration = Object.assign(new Node(), { waiting, installing, active: controller });
  let checks = 0;
  registration.update = async () => { checks++; if (updateError) throw new Error("Network unavailable"); };
  const serviceWorker = Object.assign(new Node(), { controller, register: async () => registration });
  const navigator = { onLine: online, serviceWorker };
  let reloads = 0;
  const window = Object.assign(new Node(), { isSecureContext: true, location: { reload: () => reloads++ } });
  let timeout;
  vm.runInNewContext(source, {
    document, navigator, window, CustomEvent,
    setTimeout: (fn) => { timeout = fn; return 1; }, clearTimeout: () => { timeout = null; },
  });
  return { root, nodes, registration, serviceWorker, navigator, window, document,
    get reloads() { return reloads; }, get checks() { return checks; }, expire: () => timeout?.() };
}

test("a downloaded update waits for an explicit restart, then reloads only after activation", async () => {
  const messages = [];
  const app = ui({ waiting: { postMessage: (message) => messages.push(message.type) } });
  await tick();
  assert.equal(app.nodes["apply-app-update"].hidden, false);
  assert.equal(app.nodes["app-update-notice"].hidden, false);
  assert.equal(app.reloads, 0);
  app.nodes["apply-app-update"].click();
  assert.deepEqual(messages, ["SKIP_WAITING"]);
  assert.equal(app.reloads, 0);
  assert.equal(app.root.inert, true);
  app.serviceWorker.dispatchEvent(new Event("controllerchange"));
  assert.equal(app.reloads, 1);
});

test("unfinished entries and in-flight writes can veto restart", async () => {
  let sent = false;
  const app = ui({ waiting: { postMessage: () => { sent = true; } } });
  app.root.addEventListener("field-app:before-update", (event) => {
    event.preventDefault(); event.detail.reason = "Save your unfinished entry first.";
  });
  await tick();
  app.nodes["apply-app-update"].click();
  assert.equal(sent, false);
  assert.equal(app.reloads, 0);
  assert.match(app.nodes["app-update-status"].textContent, /unfinished/);
});

test("offline users can apply an already downloaded update without a new download", async () => {
  let sent = false;
  const app = ui({ online: false, waiting: { postMessage: () => { sent = true; } } });
  await tick();
  assert.equal(app.checks, 0);
  assert.equal(app.nodes["app-update-notice"].hidden, false);
  app.nodes["check-app-update"].click();
  assert.match(app.nodes["app-update-status"].textContent, /offline/);
  assert.equal(app.nodes["app-update-notice"].hidden, false);
  app.nodes["apply-app-update"].click();
  assert.equal(sent, true);
});

test("another window's update never reloads an open form", async () => {
  const app = ui();
  await tick();
  app.serviceWorker.dispatchEvent(new Event("controllerchange"));
  assert.equal(app.reloads, 0);
  assert.equal(app.nodes["apply-app-update"].hidden, false);
  app.nodes["apply-app-update"].click();
  assert.equal(app.reloads, 1);
});

test("first install does not trigger an unnecessary reload", async () => {
  const app = ui({ controller: null });
  await tick();
  app.serviceWorker.controller = {};
  app.serviceWorker.dispatchEvent(new Event("controllerchange"));
  assert.equal(app.reloads, 0);
  assert.equal(app.nodes["apply-app-update"].hidden, true);
  assert.equal(app.nodes["app-update-notice"].hidden, true);
});

test("failed checks and stalled activation restore usable controls", async () => {
  const app = ui({ updateError: true, waiting: { postMessage() {} } });
  await tick();
  assert.equal(app.nodes["check-app-update"].disabled, false);
  assert.match(app.nodes["app-update-status"].textContent, /Could not check/);
  assert.equal(app.nodes["app-update-notice"].hidden, false);
  app.nodes["apply-app-update"].click();
  app.expire();
  assert.equal(app.root.inert, false);
  assert.equal(app.nodes["apply-app-update"].disabled, false);
  assert.equal(app.reloads, 0);
});

test("download completion offers restart and download failure reports an error", async () => {
  const installing = Object.assign(new Node(), { state: "installing" });
  const app = ui({ installing });
  await tick();
  assert.equal(app.nodes["apply-app-update"].hidden, true);
  assert.equal(app.nodes["app-update-notice"].hidden, false);
  installing.state = "installed";
  installing.dispatchEvent(new Event("statechange"));
  assert.equal(app.nodes["apply-app-update"].hidden, false);
  assert.equal(app.nodes["app-update-notice"].hidden, false);
  installing.state = "redundant";
  installing.dispatchEvent(new Event("statechange"));
  assert.match(app.nodes["app-update-status"].textContent, /could not be downloaded/);
  assert.equal(app.nodes["app-update-notice"].hidden, true);
  assert.equal(app.reloads, 0);
});

test("an up-to-date app keeps the new-version notice hidden", async () => {
  const app = ui();
  await tick();
  assert.match(app.nodes["app-update-status"].textContent, /up to date/);
  assert.equal(app.nodes["app-update-notice"].hidden, true);
  app.nodes["check-app-update"].click();
  await tick();
  assert.equal(app.nodes["app-update-notice"].hidden, true);
});

function worker({ failPath, redirectPath, offline = false, serverVersion = "test-release" } = {}) {
  const listeners = {};
  const stores = new Map([["other-app-cache", new Map()], ["arfsa-field-shell-v30", new Map([["/field-app/", "old-shell"]])]]);
  const fetched = [];
  const self = { location: { origin: "https://example.test" }, addEventListener: (name, fn) => { listeners[name] = fn; },
    skipWaiting: async () => { self.skipped = true; }, clients: { claim: async () => {} } };
  const caches = {
    keys: async () => [...stores.keys()], delete: async (key) => stores.delete(key),
    open: async (key) => {
      if (!stores.has(key)) stores.set(key, new Map());
      const cache = stores.get(key);
      return { put: async (key, value) => cache.set(key, value), match: async (key) => cache.get(key) };
    },
  };
  class WorkerRequest extends Request {
    constructor(input, options) { super(typeof input === "string" ? new URL(input, self.location.origin) : input, options); }
  }
  const fetch = async (request) => {
    const url = new URL(request.url);
    fetched.push(request);
    if (offline || url.pathname === failPath) throw new Error("Download failed");
    const response = new Response(url.pathname === "/field-app/" ? `{"appVersion": "${serverVersion}"}` : "fresh asset");
    Object.defineProperty(response, "url", { value: url.href });
    Object.defineProperty(response, "redirected", { value: url.pathname === redirectPath });
    return response;
  };
  vm.runInNewContext(workerSource.replaceAll("__FIELD_APP_VERSION__", "test-release"), { self, caches, fetch, Request: WorkerRequest, URL });
  const trigger = async (name, extra = {}) => {
    let result;
    listeners[name]({ waitUntil: (promise) => { result = promise; }, respondWith: (promise) => { result = promise; }, ...extra });
    return result;
  };
  return { self, stores, fetched, trigger };
}

test("a complete shell is fetched fresh, waits for consent and cleans only its own old caches", async () => {
  const app = worker();
  await app.trigger("install");
  assert.ok(app.fetched.every((request) => request.cache === "reload"));
  assert.equal(app.stores.get("arfsa-field-shell-test-release").size, 11);
  assert.equal(app.self.skipped, undefined);
  await app.trigger("message", { data: { type: "SKIP_WAITING" } });
  assert.equal(app.self.skipped, true);
  await app.trigger("activate");
  assert.ok(app.stores.has("other-app-cache"));
  assert.equal(app.stores.has("arfsa-field-shell-v30"), false);
});

test("interrupted downloads, login redirects and mixed releases keep the old offline shell", async () => {
  for (const options of [{ failPath: "/static/js/field-app.js" }, { redirectPath: "/field-app/" }, { serverVersion: "different-release" }]) {
    const app = worker(options);
    await assert.rejects(app.trigger("install"));
    assert.equal(app.stores.get("arfsa-field-shell-v30").get("/field-app/"), "old-shell");
    assert.equal(app.stores.has("arfsa-field-shell-test-release"), false);
    assert.equal(app.self.skipped, undefined);
  }
});

test("offline shell and versioned static assets work; APIs are never intercepted", async () => {
  const app = worker({ offline: true });
  const cached = new Map([["/field-app/", "offline-html"], ["/static/js/field-app.js", "offline-js"]]);
  app.stores.set("arfsa-field-shell-test-release", cached);
  const request = (path, mode = "cors") => ({ url: `https://example.test${path}`, method: "GET", mode });
  assert.equal(await app.trigger("fetch", { request: request("/field-app/", "navigate") }), "offline-html");
  assert.equal(await app.trigger("fetch", { request: request("/static/js/field-app.js?v=test-release") }), "offline-js");
  assert.equal(await app.trigger("fetch", { request: request("/field-app/api/bootstrap") }), undefined);
});

test("new HTML receives new assets while an older worker is still active", async () => {
  const app = worker();
  await app.trigger("install");
  app.stores.get("arfsa-field-shell-test-release").set("/static/js/field-app.js", "old-js");
  const request = new Request("https://example.test/static/js/field-app.js?v=new-release");
  const response = await app.trigger("fetch", { request });
  assert.equal(await response.text(), "fresh asset");
  assert.equal(app.fetched.at(-1).cache, "reload");
});
