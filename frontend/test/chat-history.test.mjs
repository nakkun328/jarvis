import assert from "node:assert/strict";
import test from "node:test";
import {
  BUSY_MESSAGE, LINK_INVALID_MESSAGE, LIST_FAILED_MESSAGE, OLDER_FAILED_MESSAGE, OPEN_FAILED_MESSAGE,
  RESUMED_MESSAGE, conversationParam, createHistoryController, fetchConversations, formatRelative,
} from "../chat-history.js";
import { STORAGE_KEY, createConversationMemory, historyUrl } from "../chat-restore.js";
import { createHistoryPanel } from "../history-panel.js";

const A = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d";
const B = "1a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d";
const CURSOR = `2026-01-01T00:00:00.000Z_${B}`;

const reply = (status, body) => ({ status, ok: status >= 200 && status < 300, json: async () => body });
const row = (id, title = "t", count = 2) => ({
  id, title, updated_at: "2026-01-01T00:00:00.000Z", message_count: count,
});
const page = (id, messages, extra = {}) => ({
  conversation_id: id, messages, has_more: false, next_before: null, ...extra,
});

function fakeStorage(initial = {}) {
  const data = new Map(Object.entries(initial));
  return {
    getItem: (k) => (data.has(k) ? data.get(k) : null),
    setItem: (k, v) => data.set(k, String(v)),
    removeItem: (k) => data.delete(k),
    data,
  };
}
const throwing = {
  getItem() { throw new Error("blocked"); },
  setItem() { throw new Error("blocked"); },
  removeItem() { throw new Error("blocked"); },
};

function setup({ fetchImpl, storage = fakeStorage(), busy = false } = {}) {
  const log = { statuses: [], lists: [], conversations: [], older: [], closed: 0, loading: 0, errors: [] };
  const session = {
    busy,
    conversationId: null,
    resets: 0,
    reset() {
      if (this.busy) return false;
      this.resets += 1;
      this.conversationId = null;
      memory.clear();
      return true;
    },
  };
  const memory = createConversationMemory(storage);
  const controller = createHistoryController({
    session, memory, fetchImpl,
    view: {
      setStatus: (text, error = false) => log.statuses.push({ text, error }),
      showListLoading: () => { log.loading += 1; },
      showList: (p) => log.lists.push(p),
      showListError: (text) => log.errors.push(text),
      closePanel: () => { log.closed += 1; },
      showConversation: (messages, p) => log.conversations.push({ messages, ...p }),
      prependOlder: (messages, p) => log.older.push({ messages, ...p }),
    },
  });
  return { controller, session, log, storage };
}

test("conversationParam accepts only one canonical lowercase uuid", () => {
  assert.deepEqual(conversationParam(""), { kind: "none" });
  assert.deepEqual(conversationParam("?x=1"), { kind: "none" });
  assert.deepEqual(conversationParam(`?c=${A}`), { kind: "ok", id: A });
  for (const bad of ["?c=", "?c=abc", `?c=${A.toUpperCase()}`, `?c=${A}&c=${B}`, `?c=${A}x`,
    "?c=%3Cscript%3E", `?c=${A.replaceAll("-", "")}`]) {
    assert.deepEqual(conversationParam(bad), { kind: "invalid" }, bad);
  }
});

test("formatRelative", () => {
  const now = Date.parse("2026-01-10T12:00:00.000Z");
  const at = (ms) => new Date(now - ms).toISOString();
  assert.equal(formatRelative(at(5_000), now), "たった今");
  assert.equal(formatRelative(at(5 * 60_000), now), "5分前");
  assert.equal(formatRelative(at(3 * 3_600_000), now), "3時間前");
  assert.equal(formatRelative(at(2 * 86_400_000), now), "2日前");
  assert.match(formatRelative(at(30 * 86_400_000), now), /^\d{4}\/\d{1,2}\/\d{1,2}$/);
  assert.equal(formatRelative(new Date(now + 99_000).toISOString(), now), "たった今");
  assert.equal(formatRelative("nonsense", now), "");
  assert.equal(formatRelative(null, now), "");
});

test("fetchConversations validates items, cursor and statuses", async () => {
  const ok = await fetchConversations(async (url) => {
    assert.equal(url, "/api/chat/conversations");
    return reply(200, {
      conversations: [row(A, "hello", 4), { id: "bad" }, { ...row(B), message_count: -1 }, row(B, 5)],
      next_cursor: CURSOR,
    });
  });
  assert.equal(ok.status, "ok");
  assert.deepEqual(ok.items.map((i) => i.id), [A]);
  assert.equal(ok.nextCursor, CURSOR);
  const junkCursor = await fetchConversations(async () => reply(200, { conversations: [], next_cursor: "x" }));
  assert.equal(junkCursor.nextCursor, null);
  let seen;
  await fetchConversations(async (url) => { seen = url; return reply(200, { conversations: [] }); }, { before: CURSOR });
  assert.equal(seen, `/api/chat/conversations?before=${encodeURIComponent(CURSOR)}`);
  for (const f of [async () => reply(500, {}), async () => reply(200, {}), async () => { throw new Error("x"); }]) {
    assert.equal((await fetchConversations(f)).status, "failed");
  }
});

test("openList shows an empty list, a list with more, and an error", async () => {
  let body = { conversations: [] };
  const t = setup({ fetchImpl: async () => reply(200, body) });
  assert.equal(await t.controller.openList(), "ok");
  assert.equal(t.log.loading, 1);
  assert.deepEqual(t.log.lists[0], { items: [], append: false, hasMore: false });
  body = { conversations: [row(A)], next_cursor: CURSOR };
  await t.controller.openList();
  assert.equal(t.log.lists[1].hasMore, true);
  const failing = setup({ fetchImpl: async () => reply(503, {}) });
  assert.equal(await failing.controller.openList(), "failed");
  assert.deepEqual(failing.log.errors, [LIST_FAILED_MESSAGE]);
});

test("moreList appends the next page using the cursor", async () => {
  const urls = [];
  const t = setup({
    fetchImpl: async (url) => {
      urls.push(url);
      return reply(200, urls.length === 1
        ? { conversations: [row(A)], next_cursor: CURSOR }
        : { conversations: [row(B)], next_cursor: null });
    },
  });
  assert.equal(await t.controller.moreList(), "none");
  await t.controller.openList();
  await t.controller.moreList();
  assert.equal(urls[1], `/api/chat/conversations?before=${encodeURIComponent(CURSOR)}`);
  assert.equal(t.log.lists[1].append, true);
  assert.equal(t.log.lists[1].hasMore, false);
});

test("select resumes a conversation: id, storage key, history, status", async () => {
  const messages = [{ role: "user", content: "hi" }, { role: "assistant", content: "yo" }];
  const t = setup({
    fetchImpl: async (url) => {
      assert.equal(url, historyUrl(A));
      return reply(200, page(A, messages, { has_more: true, next_before: "42" }));
    },
  });
  assert.equal(await t.controller.select(A), "resumed");
  assert.equal(t.session.conversationId, A);
  assert.equal(t.storage.data.get(STORAGE_KEY), A);
  assert.deepEqual(t.log.conversations[0], { messages, hasMore: true, nextBefore: "42" });
  assert.equal(t.log.closed, 1);
  assert.deepEqual(t.log.statuses.at(-1), { text: RESUMED_MESSAGE, error: false });
});

test("select works when storage is unavailable or throws", async () => {
  for (const storage of [null, throwing]) {
    const t = setup({
      storage,
      fetchImpl: async () => reply(200, page(A, [{ role: "user", content: "hi" }])),
    });
    assert.equal(await t.controller.select(A), "resumed");
    assert.equal(t.session.conversationId, A);
  }
});

test("select refuses while busy, on missing, on failure and on a bad id", async () => {
  const ok = async () => reply(200, page(A, [{ role: "user", content: "hi" }]));
  const busy = setup({ fetchImpl: ok, busy: true });
  assert.equal(await busy.controller.select(A), "busy");
  assert.equal(busy.log.statuses.at(-1).text, BUSY_MESSAGE);
  const missing = setup({ fetchImpl: async () => reply(404, {}) });
  assert.equal(await missing.controller.select(A), "missing");
  assert.equal(missing.session.conversationId, null);
  assert.equal(missing.log.statuses.at(-1).error, true);
  const failed = setup({ fetchImpl: async () => reply(500, {}) });
  assert.equal(await failed.controller.select(A), "failed");
  assert.equal(failed.log.statuses.at(-1).text, OPEN_FAILED_MESSAGE);
  assert.equal(await failed.controller.select("../x"), "invalid");
  assert.equal(failed.log.conversations.length, 0);
});

test("the current conversation is kept when opening another one fails", async () => {
  const t = setup({ fetchImpl: async () => reply(500, {}) });
  t.session.conversationId = B;
  t.storage.setItem(STORAGE_KEY, B);
  await t.controller.select(A);
  assert.equal(t.session.conversationId, B);
  assert.equal(t.storage.data.get(STORAGE_KEY), B);
});

test("a slow earlier selection does not overwrite a later one", async () => {
  const gates = {};
  const t = setup({
    fetchImpl: (url) => new Promise((resolve) => { gates[url] = resolve; }),
  });
  const first = t.controller.select(A);
  const second = t.controller.select(B);
  gates[historyUrl(B)](reply(200, page(B, [{ role: "user", content: "b" }])));
  assert.equal(await second, "resumed");
  gates[historyUrl(A)](reply(200, page(A, [{ role: "user", content: "a" }])));
  assert.equal(await first, "stale");
  assert.equal(t.session.conversationId, B);
});

test("loadOlder pages back with the cursor and stops at the start", async () => {
  const urls = [];
  const t = setup({
    fetchImpl: async (url) => {
      urls.push(url);
      if (urls.length === 1) return reply(200, page(A, [{ role: "user", content: "new" }], { has_more: true, next_before: "50" }));
      if (urls.length === 2) return reply(200, page(A, [{ role: "user", content: "mid" }], { has_more: true, next_before: "30" }));
      return reply(200, page(A, [{ role: "user", content: "old" }]));
    },
  });
  assert.equal(await t.controller.loadOlder(), "none");
  await t.controller.select(A);
  assert.equal(await t.controller.loadOlder(), "ok");
  assert.equal(urls[1], historyUrl(A, { before: "50" }));
  assert.equal(await t.controller.loadOlder(), "ok");
  assert.equal(urls[2], historyUrl(A, { before: "30" }));
  assert.deepEqual(t.log.older.map((o) => o.hasMore), [true, false]);
  assert.equal(await t.controller.loadOlder(), "none");
});

test("loadOlder failure keeps the cursor so it can be retried", async () => {
  let calls = 0;
  const t = setup({
    fetchImpl: async () => {
      calls += 1;
      if (calls === 1) return reply(200, page(A, [{ role: "user", content: "n" }], { has_more: true, next_before: "9" }));
      if (calls === 2) return reply(500, {});
      return reply(200, page(A, [{ role: "user", content: "o" }]));
    },
  });
  await t.controller.select(A);
  assert.equal(await t.controller.loadOlder(), "failed");
  assert.equal(t.log.statuses.at(-1).text, OLDER_FAILED_MESSAGE);
  assert.equal(await t.controller.loadOlder(), "ok");
});

test("forget drops older-page state (new conversation)", async () => {
  const t = setup({
    fetchImpl: async () => reply(200, page(A, [{ role: "user", content: "n" }], { has_more: true, next_before: "9" })),
  });
  await t.controller.select(A);
  t.controller.forget();
  assert.equal(await t.controller.loadOlder(), "none");
});

test("openFromParam: none, valid, unknown, invalid", async () => {
  const ok = setup({ fetchImpl: async () => reply(200, page(A, [{ role: "user", content: "hi" }])) });
  assert.equal(await ok.controller.openFromParam(""), "none");
  assert.equal(await ok.controller.openFromParam(`?c=${A}`), "resumed");
  assert.equal(ok.session.conversationId, A);
  const unknown = setup({ fetchImpl: async () => reply(404, {}) });
  assert.equal(await unknown.controller.openFromParam(`?c=${A}`), "missing");
  assert.deepEqual(unknown.log.statuses.at(-1), { text: LINK_INVALID_MESSAGE, error: true });
  assert.equal(unknown.log.conversations.length, 0);
  let fetched = 0;
  const invalid = setup({ fetchImpl: async () => { fetched += 1; return reply(200, {}); } });
  assert.equal(await invalid.controller.openFromParam("?c=nope"), "invalid");
  assert.equal(fetched, 0);
  assert.deepEqual(invalid.log.statuses.at(-1), { text: LINK_INVALID_MESSAGE, error: true });
});

// --- panel (keyboard and text-node behaviour) with a minimal fake DOM -------------------------

class FakeEl {
  constructor(doc, tag = "div") {
    this.doc = doc; this.tag = tag; this.children = []; this.hidden = false; this.disabled = false;
    this.listeners = {}; this.attrs = {}; this.textContent = ""; this.className = "";
    this.classList = { toggle: () => {} };
  }
  addEventListener(type, fn) { (this.listeners[type] ??= []).push(fn); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  setAttribute(k, v) { this.attrs[k] = v; }
  focus() { this.doc.activeElement = this; }
  fire(type, event = {}) {
    const e = { target: this, preventDefault() { e.prevented = true; }, ...event };
    for (const fn of this.listeners[type] ?? []) fn(e);
    return e;
  }
}

function panelSetup(handlers = {}) {
  const doc = { activeElement: null, createElement: (tag) => new FakeEl(doc, tag) };
  const els = Object.fromEntries(["trigger", "backdrop", "list", "state", "close", "more"].map((k) => [k, new FakeEl(doc, k === "list" ? "ul" : "button")]));
  els.backdrop.hidden = true;
  els.more.hidden = true;
  const calls = { open: 0, selected: [], more: 0 };
  const panel = createHistoryPanel(doc, els, {
    onOpen: () => { calls.open += 1; },
    onSelect: (id) => calls.selected.push(id),
    onMore: () => { calls.more += 1; },
    ...handlers,
  }, () => Date.parse("2026-01-01T00:10:00.000Z"));
  return { doc, els, panel, calls };
}

const texts = (el) => el.children.map((li) => li.children[0].children.map((s) => s.textContent));

test("panel opens, focuses close, renders titles as text and closes on Escape with focus restored", () => {
  const { doc, els, panel, calls } = panelSetup();
  els.trigger.fire("click");
  assert.equal(els.backdrop.hidden, false);
  assert.equal(calls.open, 1);
  assert.equal(els.trigger.attrs["aria-expanded"], "true");
  assert.equal(doc.activeElement, els.close);
  panel.showList({ items: [{ id: A, title: "<img src=x onerror=1>", updatedAt: "2026-01-01T00:00:00.000Z", count: 4 }], append: false, hasMore: true });
  assert.deepEqual(texts(els.list), [["<img src=x onerror=1>", "10分前 ・ 4 件"]]);
  assert.equal(els.more.hidden, false);
  els.backdrop.fire("keydown", { key: "Escape" });
  assert.equal(els.backdrop.hidden, true);
  assert.equal(doc.activeElement, els.trigger);
  assert.equal(els.trigger.attrs["aria-expanded"], "false");
});

test("panel shows an empty and an error state", () => {
  const { els, panel } = panelSetup();
  panel.showListLoading();
  assert.match(els.state.textContent, /読み込み中/);
  panel.showList({ items: [], append: false, hasMore: false });
  assert.match(els.state.textContent, /まだありません/);
  panel.showListError("失敗");
  assert.equal(els.state.textContent, "失敗");
  assert.equal(els.more.hidden, true);
});

test("clicking a conversation selects it; backdrop click closes but panel click does not", () => {
  const { els, panel, calls } = panelSetup();
  els.trigger.fire("click");
  panel.showList({ items: [{ id: A, title: "x", updatedAt: "bad", count: 1 }], append: false, hasMore: false });
  els.list.children[0].children[0].fire("click");
  assert.deepEqual(calls.selected, [A]);
  els.backdrop.fire("click", { target: els.list });
  assert.equal(els.backdrop.hidden, false);
  els.backdrop.fire("click");
  assert.equal(els.backdrop.hidden, true);
});

test("Tab wraps inside the panel and arrows move between conversations", () => {
  const { doc, els, panel } = panelSetup();
  els.trigger.fire("click");
  panel.showList({ items: [{ id: A, title: "a", updatedAt: "", count: 1 }, { id: B, title: "b", updatedAt: "", count: 1 }], append: false, hasMore: true });
  const [first, second] = els.list.children.map((li) => li.children[0]);
  els.close.focus();
  els.backdrop.fire("keydown", { key: "ArrowDown" });
  assert.equal(doc.activeElement, first);
  els.backdrop.fire("keydown", { key: "ArrowDown" });
  assert.equal(doc.activeElement, second);
  els.backdrop.fire("keydown", { key: "ArrowDown" });
  assert.equal(doc.activeElement, second);
  els.backdrop.fire("keydown", { key: "ArrowUp" });
  assert.equal(doc.activeElement, first);
  els.more.focus();
  const forward = els.backdrop.fire("keydown", { key: "Tab" });
  assert.equal(forward.prevented, true);
  assert.equal(doc.activeElement, els.close);
  const backward = els.backdrop.fire("keydown", { key: "Tab", shiftKey: true });
  assert.equal(backward.prevented, true);
  assert.equal(doc.activeElement, els.more);
});

test("keys are ignored while the panel is closed", () => {
  const { els } = panelSetup();
  const event = els.backdrop.fire("keydown", { key: "Escape" });
  assert.equal(event.prevented, undefined);
});
