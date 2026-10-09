import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";
import {
  PAGES,
  NAV_LABEL,
  SKIP_LABEL,
  buildNav,
  navModel,
  normalizePath,
  renderShell,
} from "../nav.js";
import { WORKER_SCOPE, WORKER_URL, canRegister, registerServiceWorker } from "../pwa.js";

// A tiny stand-in for the DOM: just enough for nav.js, and it records textContent only.
class FakeNode {
  constructor(tag) {
    this.tag = tag;
    this.className = "";
    this.textContent = "";
    this.id = "";
    this.attributes = {};
    this.children = [];
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  append(...nodes) { this.children.push(...nodes); }
  prepend(...nodes) { this.children.unshift(...nodes); }
  *walk() { yield this; for (const child of this.children) yield* child.walk(); }
  querySelector(selector) {
    const wanted = selector.startsWith(".") ? (n) => n.className.split(" ").includes(selector.slice(1)) : (n) => n.tag === selector;
    for (const node of this.walk()) if (node !== this && wanted(node)) return node;
    return null;
  }
}

function fakeDocument({ withMain = true } = {}) {
  const header = new FakeNode("header");
  const action = new FakeNode("button");
  header.append(action);
  const main = new FakeNode("main");
  const body = new FakeNode("body");
  body.append(header);
  if (withMain) body.append(main);
  return {
    header, action, main, body,
    createElement: (tag) => new FakeNode(tag),
    querySelector: (selector) => (selector === "[data-app-header]" ? header : selector === "main" && withMain ? main : null),
  };
}

test("one page list defines every screen, with unique root-relative paths", () => {
  assert.deepEqual(PAGES.map((page) => page.path), ["/", "/tasks", "/approvals", "/research", "/memory", "/devices"]);
  assert.equal(new Set(PAGES.map((page) => page.path)).size, PAGES.length);
  for (const page of PAGES) {
    assert.match(page.path, /^\/[a-z]*$/);
    assert.ok(page.label.length > 0);
  }
});

test("paths are normalised and exactly one page is current for a known path", () => {
  assert.equal(normalizePath("/tasks/"), "/tasks");
  assert.equal(normalizePath(""), "/");
  assert.equal(normalizePath(undefined), "/");
  assert.equal(normalizePath("///"), "/");
  for (const page of PAGES) {
    const current = navModel(page.path).filter((item) => item.current);
    assert.deepEqual(current.map((item) => item.path), [page.path]);
  }
  assert.equal(navModel("/tasks/").find((item) => item.current).path, "/tasks");
  assert.equal(navModel("/unknown").some((item) => item.current), false);
  // A prefix is not a match: /memory-old must not highlight /memory.
  assert.equal(navModel("/memory-old").some((item) => item.current), false);
});

test("adding a page to the list is the only change needed for a new nav link", () => {
  const pages = [...PAGES, { path: "/extra", label: "追加" }];
  const nav = buildNav(fakeDocument(), "/extra", pages);
  const links = nav.children;
  assert.equal(links.length, PAGES.length + 1);
  assert.equal(links.at(-1).getAttribute("href"), "/extra");
  assert.equal(links.at(-1).getAttribute("aria-current"), "page");
});

test("the shell builds brand, nav and skip link while keeping the page's own actions", () => {
  const doc = fakeDocument();
  assert.equal(renderShell(doc, "/research"), true);
  const [brand, nav, action] = doc.header.children;
  assert.equal(brand.className, "brand");
  assert.equal(nav.className, "app-nav");
  assert.equal(nav.getAttribute("aria-label"), NAV_LABEL);
  assert.equal(action, doc.action);
  assert.deepEqual(nav.children.map((link) => link.textContent), PAGES.map((page) => page.label));
  assert.deepEqual(nav.children.map((link) => link.getAttribute("aria-current")), [null, null, null, "page", null]);
  const skip = doc.body.children[0];
  assert.equal(skip.className, "skip-link");
  assert.equal(skip.textContent, SKIP_LABEL);
  assert.equal(skip.getAttribute("href"), `#${doc.main.id}`);
  assert.equal(doc.main.getAttribute("tabindex"), "-1");
});

test("the shell renders the same navigation on every page and is idempotent", () => {
  const signatures = new Set();
  for (const page of PAGES) {
    const doc = fakeDocument();
    renderShell(doc, page.path);
    signatures.add(JSON.stringify(doc.header.children[1].children.map((l) => [l.textContent, l.getAttribute("href")])));
    assert.equal(renderShell(doc, page.path), false);
    assert.equal(doc.header.children.length, 3);
  }
  assert.equal(signatures.size, 1);
  assert.equal(renderShell(fakeDocument(), "/") && renderShell({ querySelector: () => null }, "/"), false);
});

test("text is set with textContent and never parsed as markup", () => {
  const nav = buildNav(fakeDocument(), "/", [{ path: "/", label: "<img src=x onerror=alert(1)>" }]);
  assert.equal(nav.children[0].textContent, "<img src=x onerror=alert(1)>");
});

// ----- service worker registration -----

test("registration is allowed only in secure contexts or on localhost", () => {
  const base = { hasServiceWorker: true };
  assert.equal(canRegister({ ...base, isSecureContext: true, hostname: "jarvis.example" }), true);
  for (const hostname of ["localhost", "127.0.0.1", "[::1]", "LOCALHOST"]) {
    assert.equal(canRegister({ ...base, isSecureContext: false, hostname }), true, hostname);
  }
  assert.equal(canRegister({ ...base, isSecureContext: false, hostname: "192.168.0.10" }), false);
  assert.equal(canRegister({ ...base, isSecureContext: false, hostname: "localhost.evil.example" }), false);
  assert.equal(canRegister({ isSecureContext: true, hostname: "localhost", hasServiceWorker: false }), false);
});

test("registerServiceWorker registers /sw.js at scope / once the page has loaded", async () => {
  const calls = [];
  let onLoad = null;
  const env = {
    isSecureContext: true,
    location: { hostname: "localhost" },
    document: { readyState: "loading" },
    navigator: { serviceWorker: { register: async (url, options) => { calls.push([url, options]); return "reg"; } } },
    addEventListener: (type, handler) => { if (type === "load") onLoad = handler; },
  };
  const pending = registerServiceWorker(env);
  assert.equal(calls.length, 0);
  onLoad();
  assert.equal(await pending, "reg");
  assert.deepEqual(calls, [[WORKER_URL, { scope: WORKER_SCOPE }]]);
  assert.equal(WORKER_URL, "/sw.js");
  assert.equal(WORKER_SCOPE, "/");
});

test("registerServiceWorker does nothing on an insecure origin or without support", async () => {
  const insecure = { isSecureContext: false, location: { hostname: "192.0.2.5" }, navigator: { serviceWorker: {} } };
  assert.equal(await registerServiceWorker(insecure), null);
  assert.equal(await registerServiceWorker({ isSecureContext: true, location: { hostname: "x" }, navigator: {} }), null);
  const failing = {
    isSecureContext: true,
    location: { hostname: "localhost" },
    document: { readyState: "complete" },
    navigator: { serviceWorker: { register: async () => { throw new Error("denied"); } } },
  };
  assert.equal(await registerServiceWorker(failing), null);
});

// ----- the service worker's request policy -----

async function loadWorker() {
  const source = await readFile(new URL("../sw.js", import.meta.url), "utf8");
  const listeners = {};
  const stores = new Map();
  const cache = (name) => {
    if (!stores.has(name)) stores.set(name, new Map());
    const store = stores.get(name);
    return {
      put: async (request, response) => { store.set(request.url, response); },
      match: async (request) => store.get(request.url),
      addAll: async (requests) => { for (const request of requests) store.set(request.url, new Response("shell")); },
    };
  };
  const context = {
    self: {
      location: { origin: "http://localhost:8000" },
      addEventListener: (type, handler) => { listeners[type] = handler; },
      skipWaiting: async () => {},
      clients: { claim: async () => {} },
    },
    caches: {
      open: async (name) => cache(name),
      keys: async () => [...stores.keys()],
      delete: async (name) => stores.delete(name),
    },
    // A service worker resolves relative request URLs against its origin; Node needs that spelled out.
    Request: class extends Request {
      constructor(input, init) { super(new URL(input, "http://localhost:8000"), init); }
    },
    Response, URL,
    fetch: async () => { throw new Error("fetch not stubbed"); },
  };
  vm.createContext(context);
  vm.runInContext(source, context);
  return { context, listeners, stores };
}

function fetchEvent(url, { method = "GET", accept = "*/*" } = {}) {
  const request = { url, method, headers: new Headers({ accept }) };
  const event = { request, response: undefined, respondWith(value) { this.response = value; } };
  return event;
}

const SHELL_FILE = "http://localhost:8000/static/shell.css";

test("the worker never intercepts api, streams, pages, writes or other origins", async () => {
  const { listeners } = await loadWorker();
  const untouched = [
    fetchEvent("http://localhost:8000/api/memory/notes"),
    fetchEvent("http://localhost:8000/api/tasks/abc/events", { accept: "text/event-stream" }),
    fetchEvent("http://localhost:8000/api/chat/stream", { method: "POST" }),
    fetchEvent("http://localhost:8000/"),
    fetchEvent("http://localhost:8000/memory"),
    fetchEvent("http://localhost:8000/tasks"),
    fetchEvent("http://localhost:8000/manifest.webmanifest"),
    fetchEvent("http://localhost:8000/sw.js"),
    fetchEvent("http://localhost:8000/static/memory.html"),
    fetchEvent("http://localhost:8000/static/shell.css?x=1"),
    fetchEvent("http://localhost:8000/static/shell.css", { method: "POST" }),
    fetchEvent("https://evil.example/static/shell.css"),
    fetchEvent("http://localhost:8000/static/../api/memory/notes"),
  ];
  for (const event of untouched) {
    listeners.fetch(event);
    assert.equal(event.response, undefined, `${event.request.method} ${event.request.url}`);
  }
});

test("a shell file goes to the network first and is kept as a fallback", async () => {
  const { context, listeners, stores } = await loadWorker();
  context.fetch = async () => new Response("body{}", { headers: { "content-type": "text/css" } });
  // Responses made in Node have type "default"; the worker only stores same-origin "basic" ones.
  const basic = Object.defineProperty(new Response("body{}"), "type", { value: "basic" });
  context.fetch = async () => basic;
  const event = fetchEvent(SHELL_FILE);
  listeners.fetch(event);
  assert.equal(await event.response, basic);
  assert.equal([...stores.values()][0].has(SHELL_FILE), true);
  // The network now fails: the cached copy answers.
  context.fetch = async () => { throw new TypeError("offline"); };
  const offline = fetchEvent(SHELL_FILE);
  listeners.fetch(offline);
  assert.equal(await (await offline.response).text(), "body{}");
});

test("no-store responses and error responses are not cached", async () => {
  const { context, listeners, stores } = await loadWorker();
  const noStore = Object.defineProperty(new Response("x", { headers: { "cache-control": "no-store" } }), "type", { value: "basic" });
  context.fetch = async () => noStore;
  let event = fetchEvent(SHELL_FILE);
  listeners.fetch(event);
  await event.response;
  const failed = Object.defineProperty(new Response("x", { status: 500 }), "type", { value: "basic" });
  context.fetch = async () => failed;
  event = fetchEvent(SHELL_FILE);
  listeners.fetch(event);
  await event.response;
  assert.equal([...stores.values()].flatMap((store) => [...store.keys()]).length, 0);
});

test("a shell file that is neither reachable nor cached is an error, not a made-up page", async () => {
  const { context, listeners } = await loadWorker();
  context.fetch = async () => { throw new TypeError("offline"); };
  const event = fetchEvent(SHELL_FILE);
  listeners.fetch(event);
  await assert.rejects(event.response, TypeError);
});

const basic = (body, init) => Object.defineProperty(new Response(body, init), "type", { value: "basic" });

test("install precaches only the shell files and activate drops older versions", async () => {
  const { context, listeners, stores } = await loadWorker();
  context.fetch = async (request) =>
    basic("x", { headers: { "content-type": request.url.endsWith(".css") ? "text/css" : "text/javascript" } });
  stores.set("jarvis-shell-v0", new Map());
  stores.set("unrelated-cache", new Map());
  let installed;
  listeners.install({ waitUntil: (promise) => { installed = promise; } });
  await installed;
  const [current] = [...stores.keys()].filter((name) => name !== "jarvis-shell-v0" && name !== "unrelated-cache");
  assert.match(current, /^jarvis-shell-v\d+$/);
  const urls = [...stores.get(current).keys()];
  assert.ok(urls.length > 5);
  for (const url of urls) {
    const { pathname } = new URL(url);
    assert.match(pathname, /^\/static\/.+\.(css|js|png|svg)$/);
  }
  assert.ok(urls.includes("http://localhost:8000/static/session.js"));
  let activated;
  listeners.activate({ waitUntil: (promise) => { activated = promise; } });
  await activated;
  assert.equal(stores.has("jarvis-shell-v0"), false);
  assert.equal(stores.has("unrelated-cache"), true);
  assert.equal(stores.has(current), true);
  assert.ok(context.self);
});

// ----- login enabled: a refused or redirected answer is never stored or replaced by the cache -----

async function installFailure(respond) {
  const { context, listeners, stores } = await loadWorker();
  context.fetch = async () => respond();
  let installed;
  listeners.install({ waitUntil: (promise) => { installed = promise; } });
  await assert.rejects(installed);
  return [...stores.values()].flatMap((store) => [...store.keys()]);
}

test("install fails, and caches nothing, while the session is refused or redirected", async () => {
  assert.deepEqual(await installFailure(() => basic('{"detail":"unauthorized"}', { status: 401 })), []);
  const redirected = () => {
    const response = basic("<html>login</html>", { headers: { "content-type": "text/html" } });
    return Object.defineProperty(response, "redirected", { value: true });
  };
  assert.deepEqual(await installFailure(redirected), []);
  assert.deepEqual(
    await installFailure(() => basic("<html>login</html>", { headers: { "content-type": "text/html; charset=utf-8" } })),
    [],
  );
});

test("a refusal after logout is passed through and the cached copy does not answer it", async () => {
  const { context, listeners, stores } = await loadWorker();
  context.fetch = async () => basic("body{}", { headers: { "content-type": "text/css" } });
  const first = fetchEvent(SHELL_FILE);
  listeners.fetch(first);
  await first.response;
  assert.equal([...stores.values()][0].has(SHELL_FILE), true);
  // Logged out: the server answers 401 (network reachable). That answer is returned as is.
  const refusal = basic('{"detail":"unauthorized"}', { status: 401 });
  context.fetch = async () => refusal;
  const second = fetchEvent(SHELL_FILE);
  listeners.fetch(second);
  const answered = await second.response;
  assert.equal(answered, refusal);
  assert.equal(answered.status, 401);
  // And the 401 did not replace the stored copy.
  assert.equal(await (await [...stores.values()][0].get(SHELL_FILE)).text(), "body{}");
});

test("redirects and HTML answers are not stored under a shell file's address", async () => {
  const { context, listeners, stores } = await loadWorker();
  const login = Object.defineProperty(
    basic("<html>login</html>", { headers: { "content-type": "text/html" } }),
    "redirected",
    { value: true },
  );
  for (const answer of [login, basic("<html></html>", { headers: { "content-type": "text/html" } })]) {
    context.fetch = async () => answer;
    const event = fetchEvent(SHELL_FILE);
    listeners.fetch(event);
    await event.response;
  }
  assert.equal([...stores.values()].flatMap((store) => [...store.keys()]).length, 0);
});

test("the shell files are static code only: no pages, login files or user data endpoints", async () => {
  const source = await readFile(new URL("../sw.js", import.meta.url), "utf8");
  const block = source.split("const SHELL_FILES = [")[1].split("];")[0];
  const files = [...block.matchAll(/"([^"]+)"/g)].map((match) => match[1]);
  for (const file of files) {
    assert.match(file, /^\/static\/[A-Za-z0-9_./-]+\.(css|js|png|svg)$/);
    assert.doesNotMatch(file, /login|\/api\//);
  }
  assert.ok(files.includes("/static/session.js"));
});
