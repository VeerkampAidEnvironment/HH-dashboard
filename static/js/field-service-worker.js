const CACHE_NAME = "arfsa-field-shell-v24";
const APP_SHELL = [
  "/field-app/",
  "/field-app/manifest.webmanifest",
  "/static/css/app.css",
  "/static/js/app.js",
  "/static/js/field-app.js",
  "/static/img/field-app-icon-192.png",
  "/static/img/field-app-icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin || url.pathname.startsWith("/field-app/api/")) return;

  if (request.mode === "navigate" && url.pathname.startsWith("/field-app/")) {
    event.respondWith(
      fetch(request)
        .then((response) => {
          if (response.ok && !response.redirected && new URL(response.url).pathname === "/field-app/") {
            caches.open(CACHE_NAME).then((cache) => cache.put("/field-app/", response.clone()));
          }
          return response;
        })
        .catch(() => caches.match("/field-app/")),
    );
    return;
  }

  if (APP_SHELL.includes(url.pathname)) {
    event.respondWith(
      caches.match(request).then((cached) => cached || fetch(request).then((response) => {
        if (response.ok) caches.open(CACHE_NAME).then((cache) => cache.put(request, response.clone()));
        return response;
      })),
    );
  }
});
