// The server fills this fingerprint from the deployed app files.
const APP_VERSION = "__FIELD_APP_VERSION__";
const CACHE_PREFIX = "arfsa-field-shell-";
const CACHE_NAME = `${CACHE_PREFIX}${APP_VERSION}`;
const APP_SHELL = [
  "/field-app/",
  "/field-app/manifest.webmanifest",
  "/field-app/scoring-rules.js",
  "/static/css/app.css",
  "/static/js/app.js",
  "/static/js/field-app.js",
  "/static/js/field-scoring.js",
  "/static/js/field-updates.js",
  "/static/vendor/html2canvas.min.js",
  "/static/img/field-app-icon-192.png",
  "/static/img/field-app-icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    // Download the complete shell before offering an update. Login redirects
    // and interrupted downloads must leave the existing offline app usable.
    const responses = await Promise.all(APP_SHELL.map(async (path) => {
      const response = await fetch(new Request(path, { cache: "reload", credentials: "same-origin" }));
      if (!response.ok || response.redirected) throw new Error("App update download failed.");
      if (path === "/field-app/") {
        const html = await response.clone().text();
        if (!html.includes('"appVersion": "' + APP_VERSION + '"')) throw new Error("App release changed during download.");
      }
      return response;
    }));
    const cache = await caches.open(CACHE_NAME);
    await Promise.all(APP_SHELL.map((path, index) => cache.put(path, responses[index])));
  })());
  // Wait for the user's restart, or for all app windows to close.
});

self.addEventListener("message", (event) => {
  if (event.data?.type === "SKIP_WAITING") event.waitUntil(self.skipWaiting());
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys
        .filter((key) => key.startsWith(CACHE_PREFIX) && key !== CACHE_NAME)
        .map((key) => caches.delete(key))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin || url.pathname.startsWith("/field-app/api/")) return;

  if (request.mode === "navigate" && url.pathname.startsWith("/field-app/")) {
    event.respondWith((async () => {
      const cache = await caches.open(CACHE_NAME);
      try {
        const response = await fetch(request);
        if (response.status >= 500) throw new Error("Server unavailable.");
        if (response.ok && !response.redirected && new URL(response.url).pathname === "/field-app/") {
          const html = await response.clone().text();
          // Keep fallback HTML compatible with this worker's cached assets.
          if (html.includes('"appVersion": "' + APP_VERSION + '"')) {
            await cache.put("/field-app/", response.clone());
          }
        }
        return response;
      } catch (error) {
        const cached = await cache.match("/field-app/");
        if (cached) return cached;
        throw error;
      }
    })());
    return;
  }

  if (APP_SHELL.includes(url.pathname)) {
    event.respondWith((async () => {
      // New online HTML can arrive while an older worker is still active.
      if (url.searchParams.has("v") && url.searchParams.get("v") !== APP_VERSION) {
        return fetch(new Request(request, { cache: "reload" }));
      }
      const cache = await caches.open(CACHE_NAME);
      return (await cache.match(url.pathname)) || fetch(request);
    })());
  }
});
