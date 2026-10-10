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
// stored data and the login layer must never be cached, so no HTML is ever kept here.
//
// With login enabled (docs/auth.md) the shell files need the session cookie, which the worker's
// requests carry. A 401, a redirect or an HTML answer is never stored, and the cached copy is used
// only when the network itself fails, never to answer a refusal. The files hold no user data.
//
// To change the shell files, edit SHELL_FILES and bump CACHE_VERSION.
"use strict";

const CACHE_PREFIX = "jarvis-shell-";
const CACHE_VERSION = "v5";
const CACHE_NAME = CACHE_PREFIX + CACHE_VERSION;

const SHELL_FILES = [
  "/static/shell.css",
  "/static/style.css",
  "/static/activity.css",
  "/static/tasks.css",
  "/static/approvals.css",
  "/static/devices.css",
  "/static/research.css",
  "/static/memory.css",
  "/static/shell.js",
  "/static/nav.js",
  "/static/pwa.js",
  "/static/session.js",
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

// Only a plain, successful, same-origin static answer is ever stored. A redirect (for example to
// the login page when the session has expired), an error, an HTML document or a no-store response
// is passed through and never kept, so nothing that belongs to a session can end up in the cache.
function isCacheable(response) {
  const cacheControl = response.headers.get("cache-control") || "";
  const contentType = response.headers.get("content-type") || "";
  return (
    response.ok &&
    response.type === "basic" &&
    !response.redirected &&
    !/no-store/i.test(cacheControl) &&
    !/text\/html/i.test(contentType)
  );
}

async function precache() {
  const fetched = [];
  for (const path of SHELL_FILES) {
    const response = await fetch(new Request(path, { cache: "reload" }));
    // An expired session answers 401 (or a redirect to /login): fail the install and keep the old
    // worker. Unlike the runtime path this accepts no-store, because with login enabled the server
    // marks every response no-store and the install-time copies are the fallback baseline.
    const contentType = response.headers.get("content-type") || "";
    if (!response.ok || response.type !== "basic" || response.redirected || /text\/html/i.test(contentType)) {
      throw new Error("shell file unavailable");
    }
    fetched.push([path, response]);
  }
  const cache = await caches.open(CACHE_NAME);
  for (const [path, response] of fetched) await cache.put(new Request(path), response);
}

async function networkFirst(request) {
  const cache = await caches.open(CACHE_NAME);
  try {
    const response = await fetch(request);
    if (isCacheable(response)) await cache.put(request, response.clone());
    return response;
  } catch (error) {
    const cached = await cache.match(request);
    if (cached) return cached;
    throw error;
  }
}

self.addEventListener("install", (event) => {
  event.waitUntil(precache().then(() => self.skipWaiting()));
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
