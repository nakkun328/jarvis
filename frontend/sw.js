// JARVIS service worker: deliberately minimal, and NOT an offline mode.
//
// What it does:
//   * installs a versioned cache that holds a fallback copy of the static app-shell files only
//     (stylesheets, scripts and icons listed in SHELL_FILES below);
//   * for exactly those files, asks the network first and uses the cached copy only when the
//     network fails;
//   * removes caches of older versions when a new worker activates.
//
// What it never does: it does not intercept, read or store /api/* responses, event streams
// (SSE), page navigations or any HTML, any non-GET request, or any other origin. Those requests
// are not touched at all and go straight to the network as if no worker existed. Pages embed
// stored data and a later login layer must never be cached, so no HTML is ever kept here.
//
// To change the shell files, edit SHELL_FILES and bump CACHE_VERSION.
"use strict";

const CACHE_PREFIX = "jarvis-shell-";
const CACHE_VERSION = "v2";
const CACHE_NAME = CACHE_PREFIX + CACHE_VERSION;

const SHELL_FILES = [
  "/static/shell.css",
  "/static/style.css",
  "/static/activity.css",
  "/static/tasks.css",
  "/static/research.css",
  "/static/memory.css",
  "/static/shell.js",
  "/static/nav.js",
  "/static/pwa.js",
  "/static/icons/icon.svg",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/icons/icon-maskable-512.png",
];

// True only for a plain GET of one exact, same-origin shell file.
function isShellRequest(request, origin) {
  if (request.method !== "GET") return false;
  let url;
  try {
    url = new URL(request.url);
  } catch (error) {
    return false;
  }
  if (url.origin !== origin || url.search !== "" || url.pathname.startsWith("/api/")) return false;
  if ((request.headers.get("accept") || "").includes("text/event-stream")) return false;
  return SHELL_FILES.includes(url.pathname);
}

async function networkFirst(request) {
  const cache = await caches.open(CACHE_NAME);
  try {
    const response = await fetch(request);
    const cacheControl = response.headers.get("cache-control") || "";
    if (response.ok && response.type === "basic" && !/no-store/i.test(cacheControl)) {
      await cache.put(request, response.clone());
    }
    return response;
  } catch (error) {
    const cached = await cache.match(request);
    if (cached) return cached;
    throw error;
  }
}

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then((cache) => cache.addAll(SHELL_FILES.map((path) => new Request(path, { cache: "reload" }))))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((names) =>
        Promise.all(
          names
            .filter((name) => name.startsWith(CACHE_PREFIX) && name !== CACHE_NAME)
            .map((name) => caches.delete(name)),
        ),
      )
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  // Not calling respondWith leaves the request entirely to the browser.
  if (!isShellRequest(event.request, self.location.origin)) return;
  event.respondWith(networkFirst(event.request));
});
