import assert from "node:assert/strict";
import test from "node:test";
import { renderReply } from "../chat-links.js";
import {
  FAILED_MESSAGE,
  MISSING_MESSAGE,
  RESTORED_MESSAGE,
  STORAGE_KEY,
  createConversationMemory,
  fetchHistory,
  historyUrl,
  restoreConversation,
} from "../chat-restore.js";
import { ChatSession } from "../chat-session.js";

const ID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d";

function fakeStorage(initial = {}) {
  const data = new Map(Object.entries(initial));
  return {
    getItem: (key) => (data.has(key) ? data.get(key) : null),
    setItem: (key, value) => data.set(key, String(value)),
    removeItem: (key) => data.delete(key),
    data,
  };
}

const throwing = {
  getItem() { throw new Error("blocked"); },
  setItem() { throw new Error("blocked"); },
  removeItem() { throw new Error("blocked"); },
};

function json(status, body) {
  return async () => ({ status, ok: status >= 200 && status < 300, json: async () => body });
}

function fakeView() {
  const log = { statuses: [], shown: null };
  return {
    log,
    setStatus: (text, error = false) => log.statuses.push({ text, error }),
    showHistory: (messages) => { log.shown = messages; },
  };
}

test("memory works without storage or when storage throws", () => {
  for (const storage of [null, undefined, throwing]) {
    const memory = createConversationMemory(storage);
    assert.equal(memory.get(), null);
    memory.set(ID);
    memory.clear();
  }
});

test("memory stores only a valid id and ignores junk", () => {
  const storage = fakeStorage({ [STORAGE_KEY]: "<script>" });
  const memory = createConversationMemory(storage);
  assert.equal(memory.get(), null);
  memory.set("not-an-id");
  assert.equal(storage.data.get(STORAGE_KEY), "<script>");
  memory.set(ID);
  assert.equal(memory.get(), ID);
  memory.clear();
  assert.equal(memory.get(), null);
});

test("restore success shows history, sets the id and a fixed status", async () => {
  const memory = createConversationMemory(fakeStorage({ [STORAGE_KEY]: ID }));
  const requested = [];
  const fetchImpl = async (url) => {
    requested.push(url);
    return json(200, {
      conversation_id: ID,
      messages: [
        { role: "user", content: "q" },
        { role: "assistant", content: "a" },
        { role: "system", content: "dropped" },
      ],
    })();
  };
  const session = { conversationId: null, busy: false };
  const view = fakeView();
  assert.equal(await restoreConversation({ memory, session, view, fetchImpl }), "restored");
  assert.deepEqual(requested, [historyUrl(ID)]);
  assert.equal(session.conversationId, ID);
  assert.deepEqual(view.log.shown, [
    { role: "user", content: "q" },
    { role: "assistant", content: "a" },
  ]);
  assert.deepEqual(view.log.statuses, [{ text: RESTORED_MESSAGE, error: false }]);
});

test("nothing stored means no request", async () => {
  const memory = createConversationMemory(fakeStorage());
  const fetchImpl = async () => assert.fail("no request expected");
  const session = { conversationId: null, busy: false };
  assert.equal(await restoreConversation({ memory, session, view: fakeView(), fetchImpl }), "none");
});

test("404 clears the stored id and shows the welcome (no history)", async () => {
  const storage = fakeStorage({ [STORAGE_KEY]: ID });
  const memory = createConversationMemory(storage);
  const session = { conversationId: null, busy: false };
  const view = fakeView();
  const outcome = await restoreConversation({
    memory, session, view, fetchImpl: json(404, { detail: "conversation not found" }),
  });
  assert.equal(outcome, "missing");
  assert.equal(storage.data.has(STORAGE_KEY), false);
  assert.equal(session.conversationId, null);
  assert.equal(view.log.shown, null);
  assert.deepEqual(view.log.statuses, [{ text: MISSING_MESSAGE, error: false }]);
});

test("a failed fetch keeps the id, shows the welcome and a fixed error", async () => {
  const storage = fakeStorage({ [STORAGE_KEY]: ID });
  const memory = createConversationMemory(storage);
  const session = { conversationId: null, busy: false };
  for (const fetchImpl of [
    async () => { throw new TypeError("offline"); },
    json(500, {}),
    json(200, { conversation_id: "other", messages: [] }),
    json(200, { conversation_id: ID, messages: "x" }),
  ]) {
    const view = fakeView();
    assert.equal(await restoreConversation({ memory, session, view, fetchImpl }), "failed");
    assert.equal(view.log.shown, null);
    assert.deepEqual(view.log.statuses, [{ text: FAILED_MESSAGE, error: true }]);
  }
  assert.equal(storage.data.get(STORAGE_KEY), ID);
});

test("an empty history is treated as no conversation", async () => {
  const storage = fakeStorage({ [STORAGE_KEY]: ID });
  const outcome = await restoreConversation({
    memory: createConversationMemory(storage),
    session: { conversationId: null, busy: false },
    view: fakeView(),
    fetchImpl: json(200, { conversation_id: ID, messages: [] }),
  });
  assert.equal(outcome, "empty");
  assert.equal(storage.data.has(STORAGE_KEY), false);
});

test("a conversation started while loading is not overwritten", async () => {
  const memory = createConversationMemory(fakeStorage({ [STORAGE_KEY]: ID }));
  const session = { conversationId: null, busy: true };
  const view = fakeView();
  const outcome = await restoreConversation({
    memory, session, view,
    fetchImpl: json(200, { conversation_id: ID, messages: [{ role: "user", content: "q" }] }),
  });
  assert.equal(outcome, "skipped");
  assert.equal(view.log.shown, null);
});

test("fetchHistory returns missing/failed statuses", async () => {
  assert.deepEqual(await fetchHistory(ID, json(404, {})), { status: "missing" });
  assert.deepEqual(await fetchHistory(ID, json(503, {})), { status: "failed" });
});

test("session reports id changes: set on a completed reply, cleared on reset", async () => {
  const seen = [];
  const view = {
    setStatus() {}, clearInput() {}, setBusy() {},
    beginTurn: () => ({
      showText() {}, complete() {}, fail() {}, reset() {}, clearRetry() {},
    }),
  };
  const session = new ChatSession({
    send: async ({ onDelta }) => {
      onDelta("hi");
      return { conversation_id: ID, provider: "p", model: "m" };
    },
    view,
    onConversation: (id) => seen.push(id),
  });
  session.submit("hello");
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.deepEqual(seen, [ID]);
  assert.equal(session.reset(), true);
  assert.deepEqual(seen, [ID, null]);
});

test("restored assistant text keeps the research link safe and opens in a new tab", () => {
  const made = [];
  const doc = {
    createTextNode: (text) => ({ text }),
    createElement: (tag) => {
      const el = { tag, attrs: {}, setAttribute(k, v) { this.attrs[k] = v; }, textContent: "" };
      made.push(el);
      return el;
    },
  };
  const container = { replaceChildren(...nodes) { this.nodes = nodes; } };
  renderReply(doc, container, `see /research#${ID} now <b>x</b>`);
  const [anchor] = made;
  assert.equal(anchor.tag, "a");
  assert.equal(anchor.attrs.href, `/research#${ID}`);
  assert.equal(anchor.attrs.target, "_blank");
  assert.equal(anchor.attrs.rel, "noopener noreferrer");
  assert.equal(made.length, 1);
  assert.equal(container.nodes[2].text, " now <b>x</b>");
});
