import assert from "node:assert/strict";
import test from "node:test";
import {
  FEED_URL, LINK_TEXT, POLL_INTERVAL_MS, POLL_WINDOW_MS, RECENT_LIMIT, SUMMARY_MAX,
  cleanSummary, createMemoryPoller, eventLine, memoryLink, mergeRecent, noticeText, parseFeed,
} from "../activity-memory.js";
import { begin, viewModel } from "../activity-view.js";
import { createActivityView } from "../activity.js";

const ID = "0b9f2c4e-1d3a-4f5b-8c6d-7e8f9a0b1c2d";
const raw = (seq, over = {}) => ({
  seq, at: "2026-10-11T00:00:00+00:00", kind: "approved", origin: "chat", memory_id: ID,
  summary: `fact ${seq}`, ...over,
});
const body = (latest, events, extra = {}) => ({ latest, events, configured: true, ...extra });

// ---- parsing and wording ----

test("parseFeed keeps valid events in order and drops malformed ones", () => {
  const feed = parseFeed(body(5, [
    raw(3), raw(2, { kind: "bogus" }), raw(4, { origin: "x" }), raw(5, { memory_id: "nope" }),
    null, "str", raw(0), raw(1.5), raw(1),
  ]));
  assert.deepEqual(feed.events.map((e) => e.seq), [1, 3]);
  assert.equal(feed.latest, 5);
  assert.equal(feed.configured, true);
  assert.deepEqual(Object.keys(feed.events[0]).sort(), ["kind", "memoryId", "origin", "seq", "summary"]);
});

test("parseFeed rejects a payload that is not the contract", () => {
  for (const bad of [null, undefined, 1, "x", [], {}, { latest: -1, events: [] }, { latest: "1", events: [] },
    { latest: 1 }, { latest: 1, events: {} }, { latest: 1.5, events: [] }]) {
    assert.equal(parseFeed(bad), null);
  }
  assert.equal(parseFeed(body(0, [], { configured: false })).configured, false);
});

test("summaries are cleaned of control and bidi characters and cut", () => {
  assert.equal(cleanSummary("a‮b​c\u0000d\n  e"), "a b c d e");
  assert.equal([...cleanSummary("あ".repeat(500))].length, SUMMARY_MAX);
  assert.equal(cleanSummary(42), "");
  assert.equal(cleanSummary(null), "");
});

test("wording says what happened and where", () => {
  const ev = (kind, origin, summary = "好きな色は青") => ({ seq: 1, kind, origin, summary });
  assert.equal(eventLine(ev("approved", "chat")), "記憶しました(自動承認・会話): 好きな色は青");
  assert.equal(eventLine(ev("approved", "research")), "記憶しました(自動承認・調査): 好きな色は青");
  assert.equal(eventLine(ev("staged", "chat")), "記憶の候補を作りました(確認待ち・会話): 好きな色は青");
  assert.equal(eventLine(ev("staged", "research")), "記憶の候補を作りました(確認待ち・調査): 好きな色は青");
  assert.equal(eventLine(ev("withdrawn", "chat")), "記憶を取り下げました(会話): 好きな色は青");
  assert.equal(eventLine(ev("staged", "chat", "")), "記憶の候補を作りました(確認待ち・会話)");
  assert.equal(noticeText([]), "");
  assert.equal(noticeText([ev("staged", "chat")]), eventLine(ev("staged", "chat")));
  const two = noticeText([{ ...ev("staged", "chat", "first") }, { ...ev("approved", "research", "second") }]);
  assert.match(two, /^記憶しました\(自動承認・調査\): second\(ほか 1 件\)$/);
});

test("mergeRecent keeps the newest five, newest first, without duplicates", () => {
  const evs = Array.from({ length: 8 }, (_, i) => ({ seq: i + 1, kind: "staged", origin: "chat", summary: "" }));
  let recent = mergeRecent([], evs.slice(0, 4));
  assert.deepEqual(recent.map((e) => e.seq), [4, 3, 2, 1]);
  recent = mergeRecent(recent, evs.slice(2));
  assert.equal(recent.length, RECENT_LIMIT);
  assert.deepEqual(recent.map((e) => e.seq), [8, 7, 6, 5, 4]);
});

// ---- the poller, with fake timers and fetch ----

function harness({ responses = [], hidden = false, overrides = {} } = {}) {
  const calls = [];
  const timers = [];
  let clock = 0;
  const queue = [...responses];
  const h = {
    calls, timers, reported: [], configs: [], hidden,
    advance(ms) { clock += ms; },
    // fires the oldest pending timer and lets its async body finish
    async fire() {
      const index = timers.findIndex(Boolean);
      assert.notEqual(index, -1, "a timer was pending");
      const timer = timers[index];
      timers[index] = null;
      clock += timer.ms;
      await timer.fn();
    },
    pending: () => timers.filter(Boolean).length,
  };
  h.poller = createMemoryPoller({
    onEvents: (events, options) => h.reported.push({ events, ...options }),
    onConfig: (value) => h.configs.push(value),
    fetchImpl: async (url) => {
      calls.push(url);
      const next = queue.length > 1 ? queue.shift() : queue[0];
      if (next instanceof Error) throw next;
      return next;
    },
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: (id) => { timers[id - 1] = null; },
    now: () => clock,
    isHidden: () => h.hidden,
    ...overrides,
  });
  return h;
}
const ok = (json) => ({ status: 200, ok: true, json: async () => json });

test("baseline records the cursor and reports the backlog as silent", async () => {
  const h = harness({ responses: [ok(body(3, [raw(2), raw(3)]))] });
  await h.poller.baseline();
  assert.equal(h.poller.cursor, 3);
  assert.equal(h.calls[0], `${FEED_URL}?after=0`);
  assert.equal(h.reported.length, 1);
  assert.equal(h.reported[0].silent, true);
  assert.deepEqual(h.configs, [true]);
});

test("after a reply the feed is polled and new events are reported once", async () => {
  const h = harness({
    responses: [ok(body(3, [])), ok(body(3, [])), ok(body(4, [raw(4)])), ok(body(4, []))],
  });
  await h.poller.baseline();
  h.poller.watch();
  assert.equal(h.timers[0].ms, POLL_INTERVAL_MS);
  assert.equal(h.poller.watching, true);
  await h.fire();
  assert.equal(h.reported.length, 0);
  assert.equal(h.calls.at(-1), `${FEED_URL}?after=3`);
  await h.fire();
  assert.equal(h.reported.length, 1);
  assert.equal(h.reported[0].silent, false);
  assert.deepEqual(h.reported[0].events.map((e) => e.seq), [4]);
  await h.fire();
  assert.equal(h.calls.at(-1), `${FEED_URL}?after=4`);
  assert.equal(h.reported.length, 1); // nothing is reported twice
});

test("polling stops after the window", async () => {
  const h = harness({ responses: [ok(body(0, []))] });
  await h.poller.baseline();
  h.poller.watch();
  let ticks = 0;
  while (h.pending() > 0) {
    await h.fire();
    ticks += 1;
    assert.ok(ticks < 100);
  }
  assert.equal(ticks, Math.ceil(POLL_WINDOW_MS / POLL_INTERVAL_MS));
  assert.equal(h.poller.watching, false);
});

test("a hidden tab stops the polling, and stop() cancels it for a new turn", async () => {
  const h = harness({ responses: [ok(body(0, []))] });
  await h.poller.baseline();
  h.poller.watch();
  h.hidden = true;
  const before = h.calls.length;
  await h.fire();
  assert.equal(h.calls.length, before, "no request while hidden");
  assert.equal(h.pending(), 0);

  h.hidden = false;
  h.poller.watch();
  assert.equal(h.pending(), 1);
  h.poller.stop();
  assert.equal(h.pending(), 0);
  assert.equal(h.poller.watching, false);
});

test("a newer watch replaces the running one, and a stale tick does nothing", async () => {
  const h = harness({ responses: [ok(body(0, []))] });
  await h.poller.baseline();
  h.poller.watch();
  const stale = h.timers[0];
  h.poller.watch();
  assert.equal(h.pending(), 1);
  const before = h.calls.length;
  await stale.fn();
  assert.equal(h.calls.length, before);
});

test("malformed payloads, http errors and network errors are ignored and polling goes on", async () => {
  const h = harness({
    responses: [
      ok(body(1, [])),
      ok({ latest: "x" }),
      { status: 500, ok: false, json: async () => ({}) },
      new Error("offline"),
      { status: 200, ok: true, json: async () => { throw new SyntaxError("bad"); } },
      ok(body(2, [raw(2)])),
    ],
  });
  await h.poller.baseline();
  h.poller.watch();
  for (let i = 0; i < 5; i += 1) await h.fire();
  assert.equal(h.reported.length, 1);
  assert.deepEqual(h.reported[0].events.map((e) => e.seq), [2]);
  assert.equal(h.pending(), 1);
});

test("a restarted server (counter went back) is followed", async () => {
  const h = harness({ responses: [ok(body(40, [])), ok(body(1, [raw(1)]))] });
  await h.poller.baseline();
  h.poller.watch();
  await h.fire();
  assert.equal(h.poller.cursor, 1);
  assert.equal(h.reported.length, 1);
});

test("the filter limits events, and mark() moves the cursor silently", async () => {
  const h = harness({
    responses: [ok(body(5, [raw(5)])), ok(body(6, [raw(6, { origin: "chat" }), raw(7, { origin: "research" })]))],
    overrides: { filter: (e) => e.origin === "research" },
  });
  await h.poller.mark();
  assert.equal(h.poller.cursor, 5);
  assert.equal(h.reported.length, 0);
  h.poller.watch();
  await h.fire();
  assert.deepEqual(h.reported[0].events.map((e) => e.seq), [7]);
  assert.equal(h.poller.cursor, 7);
});

test("the first poll without a baseline never announces the backlog", async () => {
  const h = harness({ responses: [ok(body(2, [raw(1), raw(2)]))] });
  h.poller.watch();
  await h.fire();
  assert.equal(h.reported[0].silent, true);
  assert.equal(h.poller.cursor, 2);
});

// ---- the Activity View integration ----

class El {
  constructor(tag) { this.tag = tag; this.attrs = {}; this.children = []; this.textContent = ""; this.hidden = false; this.listeners = {}; }
  setAttribute(n, v) { this.attrs[n] = String(v); }
  getAttribute(n) { return this.attrs[n] ?? null; }
  removeAttribute(n) { delete this.attrs[n]; }
  append(...n) { this.children.push(...n); }
  replaceChildren(...n) { this.children = n; }
  addEventListener() {}
  *walk() { yield this; for (const c of this.children) yield* c.walk(); }
  find(pred) { for (const n of this.walk()) if (pred(n)) return n; return null; }
}
const fakeDoc = () => ({
  documentElement: { classList: { contains: () => false } },
  createElement: (t) => new El(t),
  createElementNS: (_ns, t) => new El(t),
});
function fakeEnv() {
  const timers = [];
  return {
    timers,
    matchMedia: () => ({ matches: false, addEventListener() {} }),
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: (id) => { timers[id - 1] = null; },
  };
}
const byClass = (root, cls) => root.find((n) => (n.attrs.class ?? "").split(" ").includes(cls));
const nodeOf = (root, id) => root.find((n) => n.attrs["data-node"] === id);
const parsed = (...events) => parseFeed(body(9, events)).events;

test("the diagram has a MEMORY node, connected unless the server says it is not configured", () => {
  const model = viewModel(begin());
  const memory = model.nodes.find((n) => n.id === "memory");
  assert.equal(memory.label, "MEMORY");
  assert.equal(memory.connected, true);
  assert.equal(memory.active, false);
  assert.ok(model.edges.some((e) => e.id === "main-memory"));
  const off = viewModel(begin(), { memory: { configured: false, lit: true } });
  const dim = off.nodes.find((n) => n.id === "memory");
  assert.equal(dim.connected, false);
  assert.equal(dim.active, false);
  assert.match(dim.title, /not connected/);
  assert.equal(off.edges.find((e) => e.id === "main-memory").connected, false);
  const lit = viewModel(begin(), { memory: { configured: true, lit: true } });
  assert.equal(lit.nodes.find((n) => n.id === "memory").status, "active");
  assert.equal(lit.edges.find((e) => e.id === "main-memory").active, true);
  assert.match(lit.diagramLabel, /MEMORY/);
});

test("a memory made lights the node, announces politely and keeps a short list", () => {
  const env = fakeEnv();
  const mount = new El("div");
  const view = createActivityView(fakeDoc(), mount, env);
  const root = mount.children[0];
  const box = byClass(root, "activity-memory");
  const status = byClass(root, "activity-memory-status");
  assert.equal(box.hidden, true);
  assert.equal(status.attrs["aria-live"], "polite");
  assert.equal(status.attrs.role, "status");

  view.memoryEvents(parsed(raw(1, { summary: "朝型です" })));
  assert.equal(box.hidden, false);
  assert.equal(status.textContent, "記憶しました(自動承認・会話): 朝型です");
  assert.equal(nodeOf(root, "memory").attrs["data-status"], "active");
  assert.equal(root.find((n) => n.attrs["data-edge"] === "main-memory").attrs["data-active"], "true");
  const link = byClass(root, "activity-memory-link");
  assert.equal(link.attrs.href, "/memory");
  assert.equal(link.attrs.target, "_blank");
  assert.equal(link.attrs.rel, "noopener noreferrer");
  assert.equal(link.textContent, LINK_TEXT);

  view.memoryEvents(parsed(raw(2, { kind: "staged" }), raw(3, { kind: "staged", summary: "last" })));
  assert.match(status.textContent, /候補を作りました.*last\(ほか 1 件\)/);
  const items = byClass(root, "activity-memory-recent").children;
  assert.equal(items.length, 3);
  assert.match(items[0].textContent, /last/);

  const lit = env.timers.filter(Boolean);
  assert.equal(lit.length, 1, "one expiry timer, replaced on each new event");
  lit[0].fn();
  assert.equal(nodeOf(root, "memory").attrs["data-status"], "idle");
});

test("silent (backlog) events fill the list without lighting or announcing", () => {
  const env = fakeEnv();
  const mount = new El("div");
  const view = createActivityView(fakeDoc(), mount, env);
  const root = mount.children[0];
  view.memoryEvents(parsed(raw(1)), { silent: true });
  assert.equal(byClass(root, "activity-memory-status").textContent, "");
  assert.equal(nodeOf(root, "memory").attrs["data-status"], "idle");
  assert.equal(byClass(root, "activity-memory-recent").children.length, 1);
  assert.equal(env.timers.filter(Boolean).length, 0);
  view.memoryEvents([]);
  view.memoryEvents(null);
  assert.equal(byClass(root, "activity-memory-recent").children.length, 1);
});

test("an unconfigured memory dims the node; the list keeps working", () => {
  const mount = new El("div");
  const view = createActivityView(fakeDoc(), mount, fakeEnv());
  const root = mount.children[0];
  assert.equal(nodeOf(root, "memory").attrs["data-connected"], "true");
  view.memoryConfigured(false);
  assert.equal(nodeOf(root, "memory").attrs["data-connected"], "false");
  assert.match(nodeOf(root, "memory").children[0].textContent, /not connected/);
  view.memoryConfigured(true);
  assert.equal(nodeOf(root, "memory").attrs["data-connected"], "true");
});

test("hostile summary text is rendered as text, never as markup", () => {
  const mount = new El("div");
  const view = createActivityView(fakeDoc(), mount, fakeEnv());
  const root = mount.children[0];
  const hostile = '<img src=x onerror=alert(1)>‮<script>x</script>';
  view.memoryEvents(parsed(raw(1, { summary: hostile })));
  const all = [...root.walk()];
  assert.ok(all.every((n) => !["img", "script"].includes(n.tag)));
  assert.ok(all.every((n) => !("innerHTML" in n)));
  const status = byClass(root, "activity-memory-status");
  assert.ok(status.textContent.includes("<img src=x onerror=alert(1)>"));
  assert.ok(!status.textContent.includes("‮"));
  const link = memoryLink(fakeDoc());
  assert.equal(link.tag, "a");
});

test("the poller drives the view end to end", async () => {
  const mount = new El("div");
  const view = createActivityView(fakeDoc(), mount, fakeEnv());
  const timers = [];
  const answers = [ok(body(0, [])), ok(body(1, [raw(1, { summary: "犬を飼っている" })]))];
  const poller = createMemoryPoller({
    onEvents: (events, options) => view.memoryEvents(events, options),
    onConfig: (configured) => view.memoryConfigured(configured),
    fetchImpl: async () => answers.shift(),
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: () => {},
    now: () => 0,
  });
  await poller.baseline();
  poller.watch();
  await timers.at(-1).fn();
  const status = byClass(mount.children[0], "activity-memory-status");
  assert.equal(status.textContent, "記憶しました(自動承認・会話): 犬を飼っている");
});
