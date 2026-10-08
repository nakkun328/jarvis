import assert from "node:assert/strict";
import test from "node:test";
import {
  ERROR_CODES, NOT_CONNECTED_TITLE, begin, initialState, parseActivity, reduce, reset, settle, viewModel,
} from "../activity-view.js";
import { createActivityView } from "../activity.js";
import { sendChat } from "../chat-api.js";

const feed = (state, ...events) => events.reduce((s, e) => reduce(s, e), state);
const node = (model, id) => model.nodes.find((n) => n.id === id);
const edge = (model, id) => model.edges.find((e) => e.id === id);

test("a normal turn walks received -> memory -> generating -> done", () => {
  let state = begin();
  assert.equal(viewModel(state).caption, "SENDING REQUEST");
  state = feed(state, { stage: "received" });
  assert.equal(viewModel(state).caption, "REQUEST RECEIVED");
  state = feed(state, { stage: "memory_lookup", count: 2 });
  const recall = viewModel(state);
  assert.equal(recall.mode, "recall");
  assert.match(recall.explain, /2 件/);
  state = feed(state, { stage: "generating" });
  const generating = viewModel(state);
  assert.equal(generating.caption, "SYNTHESIZING RESPONSE");
  assert.equal(generating.explain, "回答を生成しています。");
  assert.equal(generating.live, generating.explain);
  assert.equal(generating.final, false);
  state = feed(state, { stage: "done" });
  const done = viewModel(state);
  assert.equal(done.phase, "done");
  assert.equal(done.final, true);
  assert.equal(done.mode, "done");
  assert.equal(viewModel(reset()).phase, "idle");
});

test("zero memory notes is reported honestly, not hidden", () => {
  const model = viewModel(feed(begin(), { stage: "memory_lookup", count: 0 }));
  assert.match(model.explain, /ありませんでした/);
});

test("only the main path is connected today; the rest say so", () => {
  const model = viewModel(feed(begin(), { stage: "generating" }));
  for (const id of ["router", "realtime", "researcher"]) {
    assert.equal(node(model, id).connected, false);
    assert.equal(node(model, id).active, false);
    assert.match(node(model, id).title, new RegExp(NOT_CONNECTED_TITLE.replace(/[()]/g, "\\$&")));
  }
  assert.equal(node(model, "main").connected, true);
  assert.equal(node(model, "main").active, true);
  assert.equal(node(model, "input").active, true);
  assert.equal(edge(model, "input-main").active, true);
  for (const id of ["input-router", "router-realtime", "router-main", "router-researcher"]) {
    assert.equal(edge(model, id).active, false);
    assert.equal(edge(model, id).connected, false);
  }
  assert.equal(viewModel(initialState()).nodes.filter((n) => n.active).length, 0);
});

test("nothing is animated for a stage that did not happen", () => {
  const idle = viewModel(initialState());
  assert.ok(idle.edges.every((e) => !e.active));
  const done = viewModel(feed(begin(), { stage: "received" }, { stage: "done" }));
  assert.ok(done.edges.every((e) => !e.active));
  assert.equal(node(done, "main").status, "done");
});

test("reserved stages render only when the server actually sends them", () => {
  const routed = viewModel(feed(begin(), { stage: "routing" }, { stage: "route_selected", route: "research" }));
  assert.equal(routed.caption, "ROUTE SELECTED · RESEARCHER");
  assert.equal(node(routed, "researcher").active, true);
  assert.equal(node(routed, "researcher").connected, true);
  assert.equal(edge(routed, "router-researcher").active, true);
  assert.equal(edge(routed, "input-main").active, false);
  const researching = viewModel(feed(begin(), { stage: "researching", step: "searching" }));
  assert.equal(researching.caption, "RESEARCHING · SEARCHING");
  assert.equal(viewModel(feed(begin(), { stage: "speaking" })).mode, "speak");
});

test("unknown events and fields are ignored", () => {
  const start = feed(begin(), { stage: "generating" });
  assert.equal(reduce(start, { stage: "teleporting" }), start);
  assert.equal(reduce(start, "not json"), start);
  assert.equal(reduce(start, null), start);
  assert.equal(reduce(start, [1]), start);
  assert.equal(reduce(start, { stage: "memory_lookup", count: "3" }), start);
  assert.equal(reduce(start, { stage: "memory_lookup", count: -1 }), start);
  assert.equal(reduce(start, { stage: "route_selected", route: "mars" }), start);
  assert.deepEqual(parseActivity('{"stage":"generating","message":"secret","extra":1}'), { stage: "generating" });
  assert.deepEqual(parseActivity({ stage: "done", count: 5 }), { stage: "done" });
  assert.deepEqual(parseActivity({ stage: "researching", step: "dancing" }), { stage: "researching" });
});

test("an error ends visibly, keeps its code, and later events change nothing", () => {
  let state = feed(begin(), { stage: "received" }, { stage: "generating" }, { stage: "error", code: "provider" });
  const model = viewModel(state);
  assert.equal(model.phase, "error");
  assert.equal(model.final, true);
  assert.equal(model.caption, "ERROR");
  assert.match(model.explain, /プロバイダー/);
  assert.match(model.explain, /保存されていません/);
  assert.equal(node(model, "main").status, "error");
  assert.equal(reduce(state, { stage: "generating" }), state);
  assert.equal(settle(state, "cancelled"), state, "the server's specific error wins");
  for (const code of ERROR_CODES) {
    assert.ok(viewModel(feed(begin(), { stage: "error", code })).explain.length > 0);
  }
  assert.match(viewModel(feed(begin(), { stage: "error", code: "from-the-future" })).explain, /内部エラー/);
});

test("settle covers stop, network failure and a server without activity events", () => {
  const generating = feed(begin(), { stage: "generating" });
  const stopped = viewModel(settle(generating, "cancelled"));
  assert.equal(stopped.phase, "cancelled");
  assert.equal(stopped.caption, "STOPPED");
  assert.equal(stopped.mode, "stopped");
  assert.ok(stopped.edges.every((e) => !e.active));
  const failed = viewModel(settle(begin(), "error"));
  assert.equal(failed.phase, "error");
  assert.ok(failed.final);
  assert.equal(viewModel(settle(begin(), "done")).phase, "done");
  // An error before any event (connect failure) is still a visible final state.
  assert.equal(viewModel(settle(initialState(), "error")).final, true);
});

test("begin starts a fresh turn from any final state", () => {
  const ended = feed(begin(), { stage: "error", code: "storage" });
  assert.equal(begin(ended).phase, "connecting");
  assert.equal(begin(ended).code, null);
});

test("reduced motion is carried into the view model", () => {
  assert.equal(viewModel(begin()).reducedMotion, false);
  assert.equal(viewModel(begin(), { reducedMotion: true }).reducedMotion, true);
});

test("the diagram label names what is connected", () => {
  const { diagramLabel } = viewModel(initialState());
  assert.match(diagramLabel, /INPUT/);
  assert.match(diagramLabel, /MAIN AGENT/);
  assert.doesNotMatch(diagramLabel, /ROUTER、|RESEARCHER、/);
});

// ---- DOM adapter, with a tiny stand-in DOM ----

class El {
  constructor(tag) { this.tag = tag; this.attrs = {}; this.children = []; this.textContent = ""; this.hidden = false; this.listeners = {}; }
  setAttribute(n, v) { this.attrs[n] = String(v); }
  getAttribute(n) { return this.attrs[n] ?? null; }
  append(...n) { this.children.push(...n); }
  addEventListener(type, fn) { (this.listeners[type] ??= []).push(fn); }
  click() { for (const fn of this.listeners.click ?? []) fn(); }
  *walk() { yield this; for (const c of this.children) yield* c.walk(); }
  find(pred) { for (const n of this.walk()) if (pred(n)) return n; return null; }
}
function fakeDoc(reduceClass = false) {
  return {
    documentElement: { classList: { contains: (c) => reduceClass && c === "reduce-motion" } },
    createElement: (t) => new El(t),
    createElementNS: (_ns, t) => new El(t),
  };
}
function fakeEnv({ narrow = false, reduced = false } = {}) {
  const timers = [];
  return {
    timers,
    matchMedia: (q) => ({ matches: q.includes("reduced-motion") ? reduced : narrow, addEventListener() {} }),
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: (id) => { timers[id - 1] = null; },
  };
}
const byClass = (root, cls) => root.find((n) => (n.attrs.class ?? "").split(" ").includes(cls));

test("the panel is built from text nodes and attributes only", () => {
  const mount = new El("div");
  createActivityView(fakeDoc(), mount, fakeEnv());
  const all = [...mount.walk()];
  assert.ok(all.length > 20);
  assert.ok(all.every((n) => !("innerHTML" in n) && !("style" in n)));
  const root = mount.children[0];
  assert.equal(root.attrs["aria-label"], "JARVIS の動作状況");
  const live = byClass(root, "activity-live");
  assert.equal(live.attrs["aria-live"], "polite");
  assert.equal(live.attrs.role, "status");
});

test("events update the live text, caption, orb mode and nodes", () => {
  const mount = new El("div");
  const view = createActivityView(fakeDoc(), mount, fakeEnv());
  const root = mount.children[0];
  const live = byClass(root, "activity-live");
  assert.equal(root.attrs["data-mode"], "idle");
  view.begin();
  view.handle('{"stage":"received"}');
  view.handle({ stage: "generating" });
  assert.equal(live.textContent, "回答を生成しています。");
  assert.equal(byClass(root, "activity-caption").textContent, "SYNTHESIZING RESPONSE");
  assert.equal(root.attrs["data-mode"], "synth");
  const main = root.find((n) => n.attrs["data-node"] === "main");
  assert.equal(main.attrs["data-status"], "active");
  const router = root.find((n) => n.attrs["data-node"] === "router");
  assert.equal(router.attrs["data-connected"], "false");
  assert.match(router.children[0].textContent, /not connected/);
  assert.equal(root.find((n) => n.attrs["data-edge"] === "input-main").attrs["data-active"], "true");
  view.handle("garbage");
  assert.equal(live.textContent, "回答を生成しています。");
});

test("done returns to standby after a delay; stop and error stay visible", () => {
  const env = fakeEnv();
  const mount = new El("div");
  const view = createActivityView(fakeDoc(), mount, env);
  const root = mount.children[0];
  view.begin();
  view.handle({ stage: "done" });
  assert.equal(root.attrs["data-phase"], "done");
  const pending = env.timers.filter(Boolean);
  assert.equal(pending.length, 1);
  pending[0].fn();
  env.timers.fill(null); // a fired timer is gone
  assert.equal(root.attrs["data-phase"], "idle");

  view.begin();
  view.settle("cancelled");
  assert.equal(root.attrs["data-phase"], "cancelled");
  assert.equal(env.timers.filter(Boolean).length, 0);
  view.begin();
  view.settle("error");
  assert.equal(root.attrs["data-phase"], "error");
  assert.equal(env.timers.filter(Boolean).length, 0);
  assert.equal(view.state.phase, "error");
});

test("a new turn cancels the pending return to standby", () => {
  const env = fakeEnv();
  const mount = new El("div");
  const view = createActivityView(fakeDoc(), mount, env);
  view.begin();
  view.settle("done");
  view.begin();
  assert.equal(env.timers.filter(Boolean).length, 0);
  assert.equal(mount.children[0].attrs["data-phase"], "connecting");
});

test("reduced motion comes from the media query or the reduce-motion class", () => {
  for (const [doc, env] of [[fakeDoc(), fakeEnv({ reduced: true })], [fakeDoc(true), fakeEnv()]]) {
    const mount = new El("div");
    createActivityView(doc, mount, env);
    assert.equal(mount.children[0].attrs["data-reduced-motion"], "true");
  }
  const mount = new El("div");
  createActivityView(fakeDoc(), mount, fakeEnv());
  assert.equal(mount.children[0].attrs["data-reduced-motion"], "false");
});

test("the panel collapses, and starts collapsed on a narrow screen", () => {
  const wide = new El("div");
  createActivityView(fakeDoc(), wide, fakeEnv());
  const body = byClass(wide.children[0], "activity-body");
  const toggle = byClass(wide.children[0], "activity-toggle");
  assert.equal(body.hidden, false);
  assert.equal(toggle.attrs["aria-expanded"], "true");
  toggle.click();
  assert.equal(body.hidden, true);
  assert.equal(toggle.attrs["aria-expanded"], "false");
  toggle.click();
  assert.equal(body.hidden, false);

  const narrow = new El("div");
  createActivityView(fakeDoc(), narrow, fakeEnv({ narrow: true }));
  assert.equal(byClass(narrow.children[0], "activity-body").hidden, true);
  // The status text stays visible while collapsed.
  assert.ok(byClass(narrow.children[0], "activity-live"));
});

// ---- transport: sendChat hands activity events over without touching the chat ----

const sse = (...parts) =>
  new Response(new Blob([parts.join("")]).stream(), { headers: { "Content-Type": "text/event-stream" } });
const frame = (event, data) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;

test("sendChat opts in only with onActivity and forwards activity data", async () => {
  const calls = [];
  const seen = [];
  const fetchImpl = async (url, options) => {
    calls.push({ url, headers: options.headers });
    return sse(
      frame("activity", { stage: "received" }),
      frame("delta", { text: "hi" }),
      frame("activity", { stage: "done" }),
      frame("done", { conversation_id: "c1", provider: "p", model: "m" }),
    );
  };
  const text = [];
  const result = await sendChat({
    message: "x", onDelta: (t) => text.push(t), onActivity: (d) => seen.push(JSON.parse(d)), fetchImpl,
  });
  assert.deepEqual(text, ["hi"]);
  assert.deepEqual(seen, [{ stage: "received" }, { stage: "done" }]);
  assert.equal(result.conversation_id, "c1");
  assert.equal(calls[0].headers["X-Jarvis-Activity"], "1");

  await sendChat({ message: "x", onDelta() {}, fetchImpl });
  assert.equal(calls[1].headers["X-Jarvis-Activity"], undefined);
});

test("activity events are ignored when no handler is given, and a throwing handler cannot break chat", async () => {
  const fetchImpl = async () => sse(
    frame("activity", { stage: "received" }),
    frame("delta", { text: "ok" }),
    frame("done", { conversation_id: "c2" }),
  );
  await sendChat({ message: "x", onDelta() {}, fetchImpl });
  const result = await sendChat({
    message: "x", onDelta() {}, fetchImpl, onActivity() { throw new Error("display bug"); },
  });
  assert.equal(result.conversation_id, "c2");
});
