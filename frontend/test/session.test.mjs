import assert from "node:assert/strict";
import test from "node:test";
import { ChatError, sendChat } from "../chat-api.js";
import { loadMemory, loadMemoryDetail } from "../memory-api.js";
import { loadSession, loadSessionList } from "../research-api.js";
import {
  LOGIN_PATH,
  LOGOUT_BUSY_LABEL,
  LOGOUT_FAILED_LABEL,
  LOGOUT_LABEL,
  LOGOUT_URL,
  SESSION_ENDED_MESSAGE,
  STATUS_URL,
  createSessionGuard,
  fetchAuthenticated,
  mountLogout,
  pageGuard,
  refusedAsUnauthorized,
  requestLogout,
  sessionEnded,
} from "../session.js";
import { followTask } from "../tasks-stream.js";
import { loadTask, loadTaskList } from "../tasks-api.js";

const ID = "11111111-1111-4111-8111-111111111111";
const json = (body, status = 200) => new Response(JSON.stringify(body), { status });
const unauthorized = () => json({ detail: "unauthorized" }, 401);

function fakeLocation(pathname = "/memory") {
  const replaced = [];
  return { pathname, replaced, replace: (url) => replaced.push(url) };
}

// Installs a fake page location for the shared guard and restores it afterwards.
async function onPage(pathname, body) {
  const previous = Object.getOwnPropertyDescriptor(globalThis, "location");
  const location = fakeLocation(pathname);
  Object.defineProperty(globalThis, "location", { value: location, configurable: true, writable: true });
  pageGuard.reset();
  try {
    await body(location);
  } finally {
    if (previous) Object.defineProperty(globalThis, "location", previous);
    else delete globalThis.location;
    pageGuard.reset();
  }
}

// ----- the redirect guard -----

test("a refused session redirects to /login exactly once", () => {
  const location = fakeLocation("/tasks");
  const guard = createSessionGuard(() => location);
  assert.equal(guard.leaving, false);
  assert.equal(guard.sessionEnded(), true);
  assert.equal(guard.sessionEnded(), false);
  assert.equal(guard.sessionEnded(), false);
  assert.deepEqual(location.replaced, [LOGIN_PATH]);
  assert.equal(LOGIN_PATH, "/login");
  assert.equal(guard.leaving, true);
});

test("the guard never redirects from the login page and survives a missing location", () => {
  for (const pathname of ["/login", "/login/"]) {
    const location = fakeLocation(pathname);
    assert.equal(createSessionGuard(() => location).sessionEnded(), false);
    assert.deepEqual(location.replaced, []);
  }
  assert.equal(createSessionGuard(() => undefined).sessionEnded(), false);
  assert.equal(createSessionGuard(() => ({ pathname: "/" })).sessionEnded(), false);
});

test("only a 401 counts as a refused session", () => {
  const location = fakeLocation();
  assert.equal(refusedAsUnauthorized({ status: 403 }), false);
  assert.equal(refusedAsUnauthorized({ status: 500 }), false);
  assert.equal(refusedAsUnauthorized(null), false);
  assert.deepEqual(location.replaced, []);
});

test("refusedAsUnauthorized and sessionEnded share the page's single redirect", async () => {
  await onPage("/memory", (location) => {
    assert.equal(refusedAsUnauthorized({ status: 401 }), true);
    assert.equal(refusedAsUnauthorized({ status: 401 }), true);
    assert.equal(sessionEnded(), false);
    assert.deepEqual(location.replaced, ["/login"]);
  });
});

// ----- background requests -----

test("memory requests that get 401 send the user to /login once, with a fixed error kind", async () => {
  await onPage("/memory", async (location) => {
    const fetchImpl = async () => unauthorized();
    await assert.rejects(loadMemory({ fetchImpl }), (e) => e.kind === "unauthorized" && e.status === 401);
    await assert.rejects(loadMemoryDetail(ID, { fetchImpl }), (e) => e.kind === "unauthorized");
    assert.deepEqual(location.replaced, ["/login"]);
  });
});

test("task and research requests that get 401 redirect once", async () => {
  await onPage("/tasks", async (location) => {
    const fetchImpl = async () => unauthorized();
    await assert.rejects(loadTaskList({ fetchImpl }), (e) => e.kind === "unauthorized");
    await assert.rejects(loadTask(ID, { fetchImpl }), (e) => e.kind === "unauthorized");
    await assert.rejects(loadSessionList({ fetchImpl }), (e) => e.kind === "unauthorized");
    await assert.rejects(loadSession(ID, { fetchImpl }), (e) => e.kind === "unauthorized");
    assert.deepEqual(location.replaced, ["/login"]);
  });
});

test("a 401 other than from the session layer does not redirect from other statuses", async () => {
  await onPage("/memory", async (location) => {
    await assert.rejects(loadMemory({ fetchImpl: async () => json({}, 403) }), (e) => e.kind === "server");
    await assert.rejects(loadMemory({ fetchImpl: async () => json({}, 500) }), (e) => e.kind === "server");
    assert.deepEqual(location.replaced, []);
  });
});

test("chat gets a clear message and no retry on 401, from both the stream and the fallback", async () => {
  await onPage("/", async (location) => {
    await assert.rejects(
      sendChat({ message: "hi", onDelta() {}, fetchImpl: async () => unauthorized() }),
      (error) =>
        error instanceof ChatError &&
        error.kind === "unauthorized" &&
        error.retryable === false &&
        error.message === SESSION_ENDED_MESSAGE,
    );
    assert.deepEqual(location.replaced, ["/login"]);
  });
  await onPage("/", async (location) => {
    // The stream endpoint is missing (404): the fallback request is refused too.
    const calls = [];
    await assert.rejects(
      sendChat({
        message: "hi",
        onDelta() {},
        fetchImpl: async (url) => {
          calls.push(url);
          return url.endsWith("/stream") ? new Response("", { status: 404 }) : unauthorized();
        },
      }),
      (error) => error.kind === "unauthorized",
    );
    assert.deepEqual(calls, ["/api/chat/stream", "/api/chat"]);
    assert.deepEqual(location.replaced, ["/login"]);
  });
});

test("a task stream that gets 401 stops at once: no reconnect loop", async () => {
  await onPage("/tasks", async (location) => {
    const urls = [];
    const sleeps = [];
    const reason = await followTask(ID, {
      fetchImpl: async (url) => {
        urls.push(url);
        return unauthorized();
      },
      sleep: async (ms) => sleeps.push(ms),
    });
    assert.equal(reason, "unauthorized");
    assert.equal(urls.length, 1);
    assert.deepEqual(sleeps, []);
    assert.deepEqual(location.replaced, ["/login"]);
  });
});

test("requests that succeed never redirect", async () => {
  await onPage("/memory", async (location) => {
    await loadMemory({ fetchImpl: async (url) => json(url.includes("/notes") ? { notes: [] } : { candidates: [] }) });
    assert.deepEqual(location.replaced, []);
  });
});

// ----- status and logout requests -----

test("fetchAuthenticated is true only for an explicit signed-in answer", async () => {
  const seen = [];
  const answer = (response) => async (url, options) => {
    seen.push([url, options]);
    return response instanceof Error ? Promise.reject(response) : response;
  };
  assert.equal(await fetchAuthenticated(answer(json({ authenticated: true }))), true);
  assert.equal(seen[0][0], STATUS_URL);
  assert.equal(seen[0][1].method, undefined);
  assert.equal(seen[0][1].credentials, "same-origin");
  assert.equal(await fetchAuthenticated(answer(json({ authenticated: false }))), false);
  assert.equal(await fetchAuthenticated(answer(json({ authenticated: "true" }))), false);
  assert.equal(await fetchAuthenticated(answer(json(null))), false);
  assert.equal(await fetchAuthenticated(answer(json({ detail: "Not Found" }, 404))), false); // login off
  assert.equal(await fetchAuthenticated(answer(new Response("not json"))), false);
  assert.equal(await fetchAuthenticated(answer(new TypeError("down"))), false);
});

test("requestLogout posts same-origin and treats an already ended session as logged out", async () => {
  const seen = [];
  const reply = (status) => async (url, options) => {
    seen.push([url, options]);
    return json({ authenticated: false }, status);
  };
  assert.equal(await requestLogout(reply(200)), true);
  assert.equal(seen[0][0], LOGOUT_URL);
  assert.equal(seen[0][1].method, "POST");
  assert.equal(seen[0][1].credentials, "same-origin");
  assert.equal(seen[0][1].body, undefined);
  assert.equal(await requestLogout(reply(401)), true);
  assert.equal(await requestLogout(reply(403)), false); // refused by the same-origin check
  assert.equal(await requestLogout(reply(500)), false);
  assert.equal(await requestLogout(async () => { throw new TypeError("down"); }), false);
});

// ----- the header button -----

class FakeNode {
  constructor(tag) {
    this.tag = tag;
    this.className = "";
    this.textContent = "";
    this.disabled = false;
    this.attributes = {};
    this.children = [];
    this.listeners = {};
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener(type, handler) { this.listeners[type] = handler; }
  append(...nodes) { this.children.push(...nodes); }
  *walk() { yield this; for (const child of this.children) yield* child.walk(); }
  querySelector(selector) {
    const name = selector.startsWith(".") ? selector.slice(1) : null;
    for (const node of this.walk()) {
      if (node !== this && name && node.className.split(" ").includes(name)) return node;
    }
    return null;
  }
}

function pageWithHeader() {
  const header = new FakeNode("header");
  return {
    header,
    createElement: (tag) => new FakeNode(tag),
    querySelector: (selector) => (selector === "[data-app-header]" ? header : null),
  };
}

const signedIn = async () => json({ authenticated: true });

test("no logout button when login is off or the browser is signed out", async () => {
  for (const answer of [json({ detail: "Not Found" }, 404), json({ authenticated: false })]) {
    const doc = pageWithHeader();
    assert.equal(await mountLogout(doc, { fetchImpl: async () => answer.clone() }), false);
    assert.equal(doc.header.children.length, 0);
  }
  assert.equal(await mountLogout({ querySelector: () => null }, { fetchImpl: signedIn }), false);
});

test("a signed-in browser gets exactly one labelled logout button, built with textContent", async () => {
  const doc = pageWithHeader();
  const options = { fetchImpl: signedIn, location: fakeLocation(), guard: createSessionGuard(() => undefined) };
  assert.equal(await mountLogout(doc, options), true);
  assert.equal(await mountLogout(doc, options), true);
  assert.equal(doc.header.children.length, 1);
  const [button] = doc.header.children;
  assert.equal(button.tag, "button");
  assert.equal(button.className, "logout-button");
  assert.equal(button.textContent, LOGOUT_LABEL);
  assert.equal(button.attributes.type, "button");
});

test("pressing it posts the logout and goes to /login", async () => {
  const doc = pageWithHeader();
  const location = fakeLocation();
  const calls = [];
  const fetchImpl = async (url, options = {}) => {
    calls.push([url, options.method ?? "GET"]);
    return url === STATUS_URL ? json({ authenticated: true }) : json({ authenticated: false });
  };
  await mountLogout(doc, { fetchImpl, location, guard: createSessionGuard(() => undefined) });
  const [button] = doc.header.children;
  const pending = button.listeners.click();
  assert.equal(button.disabled, true);
  assert.equal(button.textContent, LOGOUT_BUSY_LABEL);
  await pending;
  assert.deepEqual(calls, [[STATUS_URL, "GET"], [LOGOUT_URL, "POST"]]);
  assert.deepEqual(location.replaced, [LOGIN_PATH]);
});

test("a failed logout stays on the page, says so, and can be tried again", async () => {
  const doc = pageWithHeader();
  const location = fakeLocation();
  let failing = true;
  const fetchImpl = async (url) => {
    if (url === STATUS_URL) return json({ authenticated: true });
    return failing ? json({ detail: "forbidden" }, 403) : json({ authenticated: false });
  };
  await mountLogout(doc, { fetchImpl, location, guard: createSessionGuard(() => undefined) });
  const [button] = doc.header.children;
  await button.listeners.click();
  assert.equal(button.disabled, false);
  assert.equal(button.textContent, LOGOUT_FAILED_LABEL);
  assert.deepEqual(location.replaced, []);
  failing = false;
  await button.listeners.click();
  assert.deepEqual(location.replaced, [LOGIN_PATH]);
});

test("no button is added once the page is already leaving for /login", async () => {
  const doc = pageWithHeader();
  const location = fakeLocation("/tasks");
  const guard = createSessionGuard(() => location);
  const pending = mountLogout(doc, {
    fetchImpl: async () => {
      guard.sessionEnded(); // a background 401 arrives while the status check is in flight
      return json({ authenticated: true });
    },
    location,
    guard,
  });
  assert.equal(await pending, false);
  assert.equal(doc.header.children.length, 0);
  assert.deepEqual(location.replaced, ["/login"]);
});
