// DOM adapter for the Activity View panel. All wording and logic live in activity-view.js; this
// file only builds the panel once and copies a view model into it. Text is placed with
// textContent and attributes only; nothing here parses markup, and no inline style is set
// (activity.css keys every colour and animation on the data-* attributes written below).
import { begin, initialState, reduce, reset, settle, viewModel } from "./activity-view.js";
import { eventLine, memoryLink, mergeRecent, noticeText } from "./activity-memory.js";

const SVG_NS = "http://www.w3.org/2000/svg";
const REDUCED_QUERY = "(prefers-reduced-motion: reduce)";
const NARROW_QUERY = "(max-width: 600px)";
// A finished turn shows its result for a while, then the panel returns to standby. A stopped or
// failed turn stays on screen until the next message.
export const DONE_RESET_MS = 6000;
// How long the MEMORY node stays lit after a memory was made.
export const MEMORY_LIT_MS = 30000;

const PANEL_LABEL = "JARVIS の動作状況";
const HIDE_LABEL = "図を隠す";
const SHOW_LABEL = "図を表示";

// Node and edge geometry for the 340x150 diagram. The ROUTER is drawn above the direct
// INPUT -> MAIN AGENT path that messages take when no router is configured.
const NODE_BOX = {
  input: { x: 8, y: 63, w: 60, h: 24 },
  router: { x: 108, y: 16, w: 64, h: 24 },
  realtime: { x: 225, y: 9, w: 100, h: 26 },
  main: { x: 225, y: 62, w: 100, h: 26 },
  researcher: { x: 225, y: 115, w: 100, h: 26 },
  memory: { x: 8, y: 115, w: 84, h: 26 },
};
const EDGE_PATH = {
  "input-router": "M38,63 C38,28 78,28 108,28",
  "router-realtime": "M172,28 C198,28 200,22 225,22",
  "router-main": "M172,28 C200,28 198,75 225,75",
  "router-researcher": "M156,40 C156,100 190,128 225,128",
  "input-main": "M68,75 L225,75",
  "main-memory": "M225,82 C160,86 110,92 70,115",
};

function svg(doc, tag, attributes = {}, text) {
  const node = doc.createElementNS(SVG_NS, tag);
  for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, String(value));
  if (text !== undefined) node.textContent = text;
  return node;
}

function html(doc, tag, className, text) {
  const node = doc.createElement(tag);
  if (className) node.setAttribute("class", className);
  if (text !== undefined) node.textContent = text;
  return node;
}

function buildOrb(doc) {
  const orb = svg(doc, "svg", { class: "orb", viewBox: "0 0 120 120", "aria-hidden": "true", focusable: "false" });
  orb.append(
    svg(doc, "circle", { class: "orb-glow", cx: 60, cy: 60, r: 54 }),
    svg(doc, "circle", { class: "orb-ring orb-ring-outer", cx: 60, cy: 60, r: 49 }),
    svg(doc, "circle", { class: "orb-ring orb-ring-inner", cx: 60, cy: 60, r: 40 }),
    svg(doc, "circle", { class: "orb-core", cx: 60, cy: 60, r: 29 }),
    svg(doc, "text", { class: "orb-letter", x: 60, y: 61, "text-anchor": "middle", "dominant-baseline": "central" }, "J"),
  );
  return orb;
}

function buildDiagram(doc, model) {
  const diagram = svg(doc, "svg", { class: "diagram", viewBox: "0 0 340 150", role: "img" });
  const edges = new Map();
  const nodes = new Map();
  for (const edge of model.edges) {
    const path = svg(doc, "path", { class: "edge", d: EDGE_PATH[edge.id], "data-edge": edge.id });
    edges.set(edge.id, path);
    diagram.append(path);
  }
  for (const node of model.nodes) {
    const box = NODE_BOX[node.id];
    const group = svg(doc, "g", { class: "node", "data-node": node.id });
    const title = svg(doc, "title", {}, node.title);
    group.append(
      title,
      svg(doc, "rect", { x: box.x, y: box.y, width: box.w, height: box.h, rx: 7 }),
      svg(doc, "text", { x: box.x + box.w / 2, y: box.y + box.h / 2 + 0.5, "text-anchor": "middle", "dominant-baseline": "central" }, node.label),
    );
    nodes.set(node.id, { group, title });
    diagram.append(group);
  }
  return { diagram, edges, nodes };
}

// mount: the element that receives the panel. env: window-like (matchMedia, setTimeout) and an
// optional document override for tests.
export function createActivityView(doc, mount, env = {}) {
  const matchMedia = env.matchMedia?.bind(env);
  const schedule = env.setTimeout ?? globalThis.setTimeout;
  const cancel = env.clearTimeout ?? globalThis.clearTimeout;
  const resetAfter = env.doneResetMs ?? DONE_RESET_MS;
  const litFor = env.memoryLitMs ?? MEMORY_LIT_MS;

  let state = initialState();
  let timer = null;
  let lastLive = null;
  // The memory part: whether memory is configured at all (null = not known yet), whether the
  // node is lit, and the last few events. It is independent of the turn state above.
  const memoryState = { configured: null, lit: false, recent: [], notice: "" };
  let litTimer = null;

  const reducedMedia = matchMedia?.(REDUCED_QUERY);
  const narrowMedia = matchMedia?.(NARROW_QUERY);
  // A page can force reduced motion with the `reduce-motion` class on <html>: the documented
  // override for browsers/tools that cannot emulate the media feature.
  const motionReduced = () =>
    Boolean(reducedMedia?.matches) || Boolean(doc.documentElement?.classList?.contains("reduce-motion"));

  const initial = viewModel(state);
  const root = html(doc, "section", "activity");
  root.setAttribute("aria-label", PANEL_LABEL);

  const bar = html(doc, "div", "activity-bar");
  const dot = html(doc, "span", "activity-dot");
  dot.setAttribute("aria-hidden", "true");
  const caption = html(doc, "span", "activity-caption", initial.caption);
  const toggle = html(doc, "button", "activity-toggle", HIDE_LABEL);
  toggle.setAttribute("type", "button");
  toggle.setAttribute("aria-controls", "activity-body");
  bar.append(dot, caption, toggle);

  const live = html(doc, "p", "activity-live", initial.live);
  live.setAttribute("role", "status");
  live.setAttribute("aria-live", "polite");
  lastLive = initial.live;

  // Present only while the router chose a path that is not wired and the Main Agent runs the
  // turn instead. It outlasts the single route_selected moment, for the rest of the turn.
  const routeNote = html(doc, "p", "activity-route");
  routeNote.hidden = true;

  // The MEMORY part: one polite sentence when a memory was made, a link to /memory, and the
  // last few events as plain text.
  const memoryBox = html(doc, "div", "activity-memory");
  memoryBox.hidden = true;
  const memoryStatus = html(doc, "p", "activity-memory-status");
  memoryStatus.setAttribute("role", "status");
  memoryStatus.setAttribute("aria-live", "polite");
  const memoryLinkNode = memoryLink(doc);
  memoryLinkNode.setAttribute("class", "activity-memory-link");
  const memoryHeading = html(doc, "p", "activity-memory-heading", "最近の記憶");
  const memoryList = html(doc, "ul", "activity-memory-recent");
  memoryBox.append(memoryStatus, memoryLinkNode, memoryHeading, memoryList);

  const body = html(doc, "div", "activity-body");
  body.setAttribute("id", "activity-body");
  const stage = html(doc, "div", "activity-stage");
  const orb = buildOrb(doc);
  const { diagram, edges, nodes } = buildDiagram(doc, initial);
  stage.append(orb, diagram);
  body.append(stage);
  root.append(bar, live, routeNote, memoryBox, body);
  mount.append(root);

  function setCollapsed(collapsed) {
    body.hidden = collapsed;
    toggle.setAttribute("aria-expanded", String(!collapsed));
    toggle.textContent = collapsed ? SHOW_LABEL : HIDE_LABEL;
  }
  setCollapsed(Boolean(narrowMedia?.matches));
  toggle.addEventListener("click", () => setCollapsed(!body.hidden));

  function render() {
    const model = viewModel(state, {
      reducedMotion: motionReduced(),
      memory: { configured: memoryState.configured, lit: memoryState.lit },
    });
    root.setAttribute("data-phase", model.phase);
    root.setAttribute("data-mode", model.mode);
    root.setAttribute("data-reduced-motion", String(model.reducedMotion));
    caption.textContent = model.caption;
    // Re-setting identical text would make a screen reader repeat it.
    if (model.live !== lastLive) {
      live.textContent = model.live;
      lastLive = model.live;
    }
    if (model.routeNote) {
      routeNote.textContent = `${model.routeNote.caption} · ${model.routeNote.text}`;
      routeNote.setAttribute("data-decided", model.routeNote.decided);
      routeNote.hidden = false;
    } else {
      routeNote.textContent = "";
      routeNote.removeAttribute("data-decided");
      routeNote.hidden = true;
    }
    renderMemory();
    diagram.setAttribute("aria-label", model.diagramLabel);
    for (const edge of model.edges) {
      const path = edges.get(edge.id);
      path.setAttribute("data-connected", String(edge.connected));
      path.setAttribute("data-active", String(edge.active));
    }
    for (const node of model.nodes) {
      const parts = nodes.get(node.id);
      parts.group.setAttribute("data-connected", String(node.connected));
      parts.group.setAttribute("data-status", node.status);
      parts.title.textContent = node.title;
    }
  }

  function renderMemory() {
    memoryBox.setAttribute("data-lit", String(memoryState.lit));
    // Re-setting identical text would make a screen reader repeat it.
    if (memoryStatus.textContent !== memoryState.notice) {
      memoryStatus.textContent = memoryState.notice;
    }
    memoryStatus.hidden = memoryState.notice === "";
    memoryBox.hidden = memoryState.recent.length === 0 && memoryState.notice === "";
    memoryList.replaceChildren(
      ...memoryState.recent.map((event) => {
        const item = html(doc, "li", "activity-memory-item", eventLine(event));
        item.setAttribute("data-kind", event.kind);
        return item;
      }),
    );
    memoryHeading.hidden = memoryState.recent.length === 0;
  }

  function clearTimer() {
    if (timer !== null) cancel(timer);
    timer = null;
  }

  function update(next) {
    state = next;
    clearTimer();
    if (state.phase === "done") {
      timer = schedule(() => {
        timer = null;
        if (state.phase === "done") update(reset());
      }, resetAfter);
    }
    render();
  }

  reducedMedia?.addEventListener?.("change", render);
  render();

  return {
    // Memory events from the feed. `silent` events (the backlog at page load) fill the recent
    // list without announcing anything or lighting the node.
    memoryEvents(events, { silent = false } = {}) {
      if (!Array.isArray(events) || events.length === 0) return;
      memoryState.recent = mergeRecent(memoryState.recent, events);
      if (!silent) {
        memoryState.notice = noticeText(events);
        memoryState.lit = true;
        if (litTimer !== null) cancel(litTimer);
        litTimer = schedule(() => {
          litTimer = null;
          memoryState.lit = false;
          render();
        }, litFor);
      }
      render();
    },
    // Whether the server has memory configured; only `false` dims the MEMORY node.
    memoryConfigured(configured) {
      memoryState.configured = configured !== false;
      render();
    },
    begin: () => update(begin()),
    // Accepts an SSE `data` string or a parsed object; anything that is not a known event is
    // ignored.
    handle: (data) => update(reduce(state, data)),
    settle: (outcome) => update(settle(state, outcome)),
    reset: () => update(reset()),
    get state() {
      return state;
    },
  };
}
