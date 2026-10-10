import assert from "node:assert/strict";
import test from "node:test";
import {
  CASUAL_SKIP_TEXT, ERROR_CODES, FALLBACK_TEXT, NOT_CONNECTED_TITLE, NOT_WIRED_TEXT, RESEARCH_STARTED_TEXT, SKIPS, SKIP_TEXT, begin, initialState, parseActivity, reduce, reset, settle, viewModel,
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
  assert.match(failed.explain, /通信/);
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

// ---- routing (opt-in on the server) ----

const routed = (decided, fallback = false) => [
  { stage: "received" },
  { stage: "routing" },
  { stage: "route_selected", route: "main", decided, fallback },
];

test("without routing events nothing about the router changes", () => {
  const model = viewModel(feed(begin(), { stage: "received" }, { stage: "generating" }));
  assert.equal(node(model, "router").connected, false);
  assert.equal(model.routeNote, null);
  assert.equal(edge(model, "input-main").active, true);
  assert.equal(edge(model, "input-router").active, false);
});

test("routing connects and lights the router, then the path to MAIN AGENT", () => {
  const routing = viewModel(feed(begin(), { stage: "received" }, { stage: "routing" }));
  assert.equal(routing.caption, "ROUTING");
  assert.equal(node(routing, "router").connected, true);
  assert.equal(node(routing, "router").active, true);
  assert.equal(edge(routing, "input-router").active, true);
  assert.equal(edge(routing, "input-router").connected, true);
  assert.equal(edge(routing, "input-main").active, false);

  const selected = viewModel(feed(begin(), ...routed("memory")));
  assert.equal(selected.caption, "ROUTE SELECTED · MAIN AGENT");
  assert.equal(selected.routeNote, null);
  assert.equal(node(selected, "router").active, true);
  assert.equal(node(selected, "main").active, true);
  assert.equal(edge(selected, "router-main").active, true);
  assert.equal(edge(selected, "router-main").connected, true);
  assert.equal(edge(selected, "input-main").active, false);

  // The path to MAIN AGENT stays lit while it works; the router stays connected, no longer active.
  const generating = viewModel(feed(begin(), ...routed("memory"), { stage: "memory_lookup", count: 1 }, { stage: "generating" }));
  assert.equal(node(generating, "router").connected, true);
  assert.equal(node(generating, "router").active, false);
  assert.equal(edge(generating, "input-router").active, true);
  assert.equal(edge(generating, "router-main").active, true);
  assert.equal(edge(generating, "input-main").active, false);
  for (const id of ["realtime", "researcher"]) {
    assert.equal(node(generating, id).connected, false);
    assert.equal(node(generating, id).active, false);
  }
  assert.match(generating.diagramLabel, /ROUTER/);
  const done = viewModel(feed(begin(), ...routed("memory"), { stage: "done" }));
  assert.ok(done.edges.every((e) => !e.active));
  assert.equal(node(done, "router").connected, true);
  // A fresh turn without routing events forgets the router.
  assert.equal(node(viewModel(begin()), "router").connected, false);
});

test("casual and research are shown as routed but run on the main agent", () => {
  for (const [decided, caption] of [["casual", "ROUTED: CASUAL"], ["research", "ROUTED: RESEARCH"]]) {
    const selected = viewModel(feed(begin(), ...routed(decided)));
    assert.equal(selected.caption, caption);
    assert.equal(selected.explain, NOT_WIRED_TEXT);
    assert.equal(selected.explain, "この経路はまだ接続されていないため、メインのエージェントで処理します");
    assert.equal(selected.live, selected.explain);
    assert.equal(node(selected, "main").active, true);
    assert.equal(edge(selected, "router-main").active, true);
    for (const id of ["realtime", "researcher"]) {
      const dimmed = node(selected, id);
      assert.equal(dimmed.connected, false);
      assert.equal(dimmed.active, false);
      assert.match(dimmed.title, /not connected/);
    }
    assert.equal(edge(selected, "router-realtime").active, false);
    assert.equal(edge(selected, "router-researcher").active, false);
    // The note outlasts the moment, so it can be read while the answer is generated.
    const later = viewModel(feed(begin(), ...routed(decided), { stage: "generating" }));
    assert.equal(later.caption, "SYNTHESIZING RESPONSE");
    assert.equal(later.live, `回答を生成しています。${NOT_WIRED_TEXT}。`);
    assert.equal(later.routeNote.caption, caption);
    assert.equal(later.routeNote.text, NOT_WIRED_TEXT);
    assert.equal(viewModel(feed(begin(), ...routed(decided), { stage: "done" })).routeNote.decided, decided);
    // ...and goes away with the turn.
    assert.equal(viewModel(reset()).routeNote, null);
    assert.equal(viewModel(begin(feed(begin(), ...routed(decided)))).routeNote, null);
  }
});

test("a router fallback is shown as such, never as a chosen route", () => {
  const model = viewModel(feed(begin(), ...routed("memory", true)));
  assert.equal(model.caption, "ROUTE SELECTED · MAIN AGENT (FALLBACK)");
  assert.equal(model.explain, FALLBACK_TEXT);
  assert.equal(model.routeNote, null);
  assert.equal(node(model, "main").active, true);
});

test("routing fields are validated and unknown ones dropped", () => {
  assert.deepEqual(
    parseActivity({ stage: "route_selected", route: "main", decided: "casual", fallback: false, text: "x" }),
    { stage: "route_selected", route: "main", decided: "casual", fallback: false },
  );
  // `main` is never a router choice; a bad optional field is dropped, not the whole event.
  assert.deepEqual(
    parseActivity({ stage: "route_selected", route: "main", decided: "main", fallback: "yes" }),
    { stage: "route_selected", route: "main" },
  );
  assert.deepEqual(parseActivity({ stage: "routing", decided: "casual" }), { stage: "routing" });
  assert.equal(parseActivity({ stage: "route_selected", decided: "casual" }), null);
  // Without `decided` (an older server) the route is taken as sent and no note is invented.
  const legacy = viewModel(feed(begin(), { stage: "routing" }, { stage: "route_selected", route: "main" }));
  assert.equal(legacy.routeNote, null);
  assert.equal(legacy.caption, "ROUTE SELECTED · MAIN AGENT");
});

// ---- a research started from the chat ----

const researchTurn = [
  { stage: "received" },
  { stage: "routing" },
  { stage: "route_selected", route: "research", decided: "research", fallback: false },
  { stage: "researching", step: "started" },
];

test("a started research lights the researcher and says it runs in the background", () => {
  const selected = viewModel(feed(begin(), ...researchTurn.slice(0, 3)));
  assert.equal(selected.caption, "ROUTED: RESEARCH");
  assert.equal(selected.explain, RESEARCH_STARTED_TEXT);
  assert.match(selected.explain, /バックグラウンド/);
  assert.match(selected.explain, /リサーチ/);
  assert.equal(node(selected, "researcher").connected, true);
  assert.equal(node(selected, "researcher").active, true);
  assert.equal(edge(selected, "router-researcher").active, true);
  assert.equal(edge(selected, "router-researcher").connected, true);
  assert.equal(node(selected, "main").active, false);
  assert.equal(edge(selected, "router-main").active, false);
  // REALTIME stays dimmed.
  assert.equal(node(selected, "realtime").connected, false);
  assert.match(node(selected, "realtime").title, /not connected/);

  const started = viewModel(feed(begin(), ...researchTurn));
  assert.equal(started.caption, "ROUTED: RESEARCH");
  assert.equal(started.explain, RESEARCH_STARTED_TEXT);
  assert.equal(started.mode, "research");
  assert.equal(node(started, "researcher").active, true);
  assert.equal(started.live, started.explain);
  assert.deepEqual(started.routeNote, { decided: "research", caption: "ROUTED: RESEARCH", text: RESEARCH_STARTED_TEXT });
  assert.match(started.diagramLabel, /RESEARCHER/);

  // The turn ends with the Researcher, not the Main Agent, as its result node.
  const done = viewModel(feed(begin(), ...researchTurn, { stage: "done" }));
  assert.equal(done.phase, "done");
  assert.equal(done.routeNote.caption, "ROUTED: RESEARCH");
  assert.equal(node(done, "researcher").connected, true);
  assert.equal(node(done, "researcher").status, "done");
  assert.equal(node(done, "main").status, "idle");
  assert.equal(node(done, "realtime").connected, false);
  assert.ok(done.edges.every((e) => !e.active));
  // ...and the next turn forgets it.
  assert.equal(viewModel(begin()).routeNote, null);
  assert.equal(node(viewModel(begin()), "researcher").connected, false);
});

test("a research that was not started says why and runs on the main agent", () => {
  assert.deepEqual([...SKIPS].sort(), Object.keys(SKIP_TEXT).sort());
  for (const skip of SKIPS) {
    const turn = [
      { stage: "received" },
      { stage: "routing" },
      { stage: "route_selected", route: "main", decided: "research", fallback: false, research_skip: skip },
    ];
    const selected = viewModel(feed(begin(), ...turn));
    assert.equal(selected.caption, "RESEARCH NOT STARTED");
    assert.equal(selected.explain, SKIP_TEXT[skip]);
    assert.notEqual(selected.explain, NOT_WIRED_TEXT);
    assert.equal(node(selected, "main").active, true);
    assert.equal(node(selected, "researcher").connected, false);
    assert.equal(node(selected, "researcher").active, false);
    assert.equal(edge(selected, "router-researcher").active, false);
    const later = viewModel(feed(begin(), ...turn, { stage: "generating" }));
    assert.equal(later.live, `回答を生成しています。${SKIP_TEXT[skip]}。`);
    assert.equal(later.routeNote.text, SKIP_TEXT[skip]);
  }
});

test("research fields are validated and the legacy shapes are unchanged", () => {
  assert.deepEqual(
    parseActivity({ stage: "route_selected", route: "main", decided: "research", fallback: false, research_skip: "busy", x: 1 }),
    { stage: "route_selected", route: "main", decided: "research", fallback: false, research_skip: "busy" },
  );
  assert.deepEqual(
    parseActivity({ stage: "route_selected", route: "main", decided: "research", fallback: false, research_skip: "<b>" }),
    { stage: "route_selected", route: "main", decided: "research", fallback: false },
  );
  assert.deepEqual(parseActivity({ stage: "researching", step: "started" }), { stage: "researching", step: "started" });
  // research without a "research" decision (an older server) is not read as a started research
  const legacy = viewModel(feed(begin(), { stage: "routing" }, { stage: "route_selected", route: "research" }));
  assert.equal(legacy.caption, "ROUTE SELECTED · RESEARCHER");
  assert.equal(legacy.routeNote, null);
  // a hostile skip value never changes the wording of a non-research route
  const memory = viewModel(feed(begin(), { stage: "routing" },
    { stage: "route_selected", route: "main", decided: "memory", fallback: false, research_skip: "busy" }));
  assert.equal(memory.caption, "ROUTE SELECTED · MAIN AGENT");
});

// ---- DOM adapter, with a tiny stand-in DOM ----

class El {
  constructor(tag) { this.tag = tag; this.attrs = {}; this.children = []; this.textContent = ""; this.hidden = false; this.listeners = {}; }
  setAttribute(n, v) { this.attrs[n] = String(v); }
  getAttribute(n) { return this.attrs[n] ?? null; }
  removeAttribute(n) { delete this.attrs[n]; }
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

test("the panel shows the routing stages and the not-wired note, mirrored in the live text", () => {
  const mount = new El("div");
  const view = createActivityView(fakeDoc(), mount, fakeEnv());
  const root = mount.children[0];
  const live = byClass(root, "activity-live");
  const caption = byClass(root, "activity-caption");
  const note = byClass(root, "activity-route");
  const router = root.find((n) => n.attrs["data-node"] === "router");
  assert.equal(note.hidden, true);
  view.begin();
  view.handle({ stage: "received" });
  view.handle({ stage: "routing" });
  assert.equal(caption.textContent, "ROUTING");
  assert.equal(router.attrs["data-connected"], "true");
  assert.equal(router.attrs["data-status"], "active");
  assert.equal(root.find((n) => n.attrs["data-edge"] === "input-router").attrs["data-active"], "true");
  view.handle({ stage: "route_selected", route: "main", decided: "research", fallback: false });
  assert.equal(caption.textContent, "ROUTED: RESEARCH");
  assert.equal(live.textContent, NOT_WIRED_TEXT);
  assert.equal(note.hidden, false);
  assert.equal(note.attrs["data-decided"], "research");
  assert.match(note.textContent, /ROUTED: RESEARCH/);
  assert.match(note.textContent, /まだ接続されていない/);
  assert.equal(root.find((n) => n.attrs["data-edge"] === "router-main").attrs["data-active"], "true");
  for (const id of ["realtime", "researcher"]) {
    const dimmed = root.find((n) => n.attrs["data-node"] === id);
    assert.equal(dimmed.attrs["data-connected"], "false");
    assert.equal(dimmed.attrs["data-status"], "idle");
  }
  view.handle({ stage: "generating" });
  assert.equal(live.textContent, `回答を生成しています。${NOT_WIRED_TEXT}。`);
  assert.equal(note.hidden, false);
  view.begin();
  assert.equal(note.hidden, true);
  assert.equal(note.textContent, "");
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

// ---- the casual path (JARVIS_CASUAL on) ----

const casualTurn = [
  { stage: "received" },
  { stage: "routing" },
  { stage: "route_selected", route: "casual", decided: "casual", fallback: false },
  { stage: "generating" },
];

test("a casual turn lights REALTIME, never the main agent, and has no memory step", () => {
  const generating = viewModel(feed(begin(), ...casualTurn));
  assert.equal(generating.caption, "SYNTHESIZING RESPONSE");
  assert.equal(node(generating, "realtime").connected, true);
  assert.equal(node(generating, "realtime").active, true);
  assert.equal(node(generating, "main").active, false);
  assert.equal(edge(generating, "router-realtime").active, true);
  assert.equal(edge(generating, "router-main").active, false);
  assert.equal(generating.routeNote, null);
  const selected = viewModel(feed(begin(), ...casualTurn.slice(0, 3)));
  assert.equal(selected.caption, "ROUTE SELECTED · REALTIME");
  const done = viewModel(feed(begin(), ...casualTurn, { stage: "done" }));
  assert.equal(node(done, "realtime").status, "done");
  assert.equal(node(done, "realtime").connected, true);
  assert.equal(node(done, "main").status, "idle");
  assert.ok(done.edges.every((e) => !e.active));
  assert.equal(node(viewModel(begin()), "realtime").connected, false);
});

test("a casual decision that fell back says why and runs on the main agent", () => {
  for (const skip of ["over_budget", "provider", "low_confidence"]) {
    const turn = [
      { stage: "routing" },
      { stage: "route_selected", route: "main", decided: "casual", fallback: false, casual_skip: skip },
      { stage: "generating" },
    ];
    const selected = viewModel(feed(begin(), ...turn.slice(0, 2)));
    assert.equal(selected.caption, "CASUAL NOT USED");
    assert.equal(selected.explain, CASUAL_SKIP_TEXT[skip]);
    assert.notEqual(selected.explain, NOT_WIRED_TEXT);
    assert.equal(node(selected, "realtime").connected, false);
    const later = viewModel(feed(begin(), ...turn));
    assert.equal(later.routeNote.caption, "CASUAL NOT USED");
    assert.equal(node(later, "main").active, true);
    assert.equal(node(later, "realtime").active, false);
  }
  assert.deepEqual(
    parseActivity({ stage: "route_selected", route: "main", decided: "casual", casual_skip: "x", text: "t" }),
    { stage: "route_selected", route: "main", decided: "casual" },
  );
});

test("a research-backed turn shows each research step, then generating, then done", () => {
  let state = feed(
    begin(),
    { stage: "received" },
    { stage: "routing" },
    { stage: "route_selected", route: "research", decided: "research", fallback: false },
    { stage: "researching", step: "started" },
  );
  assert.equal(viewModel(state).caption, "ROUTED: RESEARCH");
  const captions = [];
  for (const step of ["planning", "searching", "reading", "verifying", "writing"]) {
    state = feed(state, { stage: "researching", step });
    captions.push(viewModel(state).caption);
  }
  assert.deepEqual(captions, [
    "RESEARCHING · PLANNING",
    "RESEARCHING · SEARCHING",
    "RESEARCHING · READING",
    "RESEARCHING · VERIFYING",
    "RESEARCHING · WRITING",
  ]);
  state = feed(state, { stage: "generating" });
  assert.equal(viewModel(state).caption, "SYNTHESIZING RESPONSE");
  state = feed(state, { stage: "done" });
  assert.equal(viewModel(state).caption, "RESPONSE COMPLETE");
});
