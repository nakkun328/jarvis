// Pure model of the chat page's Activity View. No DOM access, so Node can test it.
//
// Input is only what the server's `activity` SSE events say (a fixed vocabulary, see
// docs/chat.md "Activity events"), plus three local turn boundaries the page already knows:
// the request was sent (begin), the turn ended (settle) and the user went back to idle (reset).
// Nothing is estimated or invented: a node shows as active only because an event said so, and
// every node that is not connected to anything today says so. Unknown stages, routes, steps and
// fields are ignored, so a newer server never breaks an older page.

export const STAGES = [
  "received",
  "routing",
  "route_selected",
  "memory_lookup",
  "researching",
  "generating",
  "speaking",
  "done",
  "error",
];
export const ROUTES = ["casual", "memory", "research", "main"];
export const STEPS = ["planning", "searching", "reading", "verifying", "writing"];
export const ERROR_CODES = [
  "conversation_not_found",
  "capacity",
  "storage",
  "memory",
  "provider",
  "cancelled",
  "internal",
];
export const MAX_COUNT = 99;
export const NOT_CONNECTED_TITLE = "未接続（not connected）";

// idle: nothing is happening. connecting: the request is sent, the server has not answered yet.
// active: the server reported a stage. done / error / cancelled: the turn's visible final state.
export const PHASES = ["idle", "connecting", "active", "done", "error", "cancelled"];
const FINAL = new Set(["done", "error", "cancelled"]);

const ROUTE_LABELS = {
  casual: "REALTIME",
  memory: "MAIN AGENT",
  research: "RESEARCHER",
  main: "MAIN AGENT",
};
const ROUTE_NODE = { casual: "realtime", memory: "main", research: "researcher", main: "main" };
const STEP_TEXT = {
  planning: "調べ方を計画しています。",
  searching: "情報源を検索しています。",
  reading: "取得したページを読んでいます。",
  verifying: "出典を検証しています。",
  writing: "調査結果をまとめています。",
};
const ERROR_TEXT = {
  conversation_not_found: "会話が見つかりませんでした。",
  capacity: "同時に処理できる会話の上限に達しています。",
  storage: "会話の保存先が利用できません。",
  memory: "記憶の参照が利用できませんでした。",
  provider: "LLM プロバイダーから応答を得られませんでした。",
  cancelled: "処理が取り消されました。",
  internal: "内部エラーが発生しました。",
  // Client-side only: the connection failed or the stream ended without a final event.
  interrupted: "サーバーとの通信が完了しませんでした。",
};

export function initialState() {
  return { phase: "idle", stage: null, route: null, count: null, step: null, code: null };
}

function isOneOf(list, value) {
  return typeof value === "string" && list.includes(value);
}

// Turns one SSE `data` string (or an already parsed object) into a clean payload, or null.
// Only the allowlisted field for the stage is read; everything else is dropped.
export function parseActivity(data) {
  let payload = data;
  if (typeof data === "string") {
    try {
      payload = JSON.parse(data);
    } catch {
      return null;
    }
  }
  if (payload === null || typeof payload !== "object" || Array.isArray(payload)) return null;
  const { stage } = payload;
  if (!isOneOf(STAGES, stage)) return null;
  const clean = { stage };
  if (stage === "route_selected") {
    if (!isOneOf(ROUTES, payload.route)) return null;
    clean.route = payload.route;
  } else if (stage === "memory_lookup") {
    const { count } = payload;
    if (!Number.isInteger(count) || count < 0 || count > MAX_COUNT) return null;
    clean.count = count;
  } else if (stage === "researching") {
    if (isOneOf(STEPS, payload.step)) clean.step = payload.step;
  } else if (stage === "error") {
    clean.code = isOneOf(ERROR_CODES, payload.code) ? payload.code : "internal";
  }
  return clean;
}

// The request was just sent. Starts a fresh turn from any state.
export function begin() {
  return { ...initialState(), phase: "connecting" };
}

// Back to the resting state (a finished turn timing out, or a new conversation).
export function reset() {
  return initialState();
}

// Applies one server event. Invalid events, and events after the turn already ended, change
// nothing.
export function reduce(state, rawEvent) {
  if (FINAL.has(state.phase)) return state;
  const event = parseActivity(rawEvent);
  if (!event) return state;
  if (event.stage === "done") return { ...state, phase: "done", stage: "done" };
  if (event.stage === "error") {
    return { ...state, phase: "error", stage: "error", code: event.code };
  }
  return {
    ...state,
    phase: "active",
    stage: event.stage,
    route: event.route ?? (event.stage === "route_selected" ? null : state.route),
    count: event.count ?? state.count,
    step: event.stage === "researching" ? (event.step ?? null) : state.step,
  };
}

// The turn ended on the client side. A final state the server already reported wins, so a
// provider error keeps its specific code. Otherwise the outcome is shown as it was:
//   "done"      the reply was saved (also covers a server that sends no activity events)
//   "cancelled" the user pressed Stop
//   "error"     anything else (network failure, a stream that ended early, ...)
export function settle(state, outcome) {
  if (FINAL.has(state.phase)) return state;
  if (outcome === "done") return { ...state, phase: "done", stage: "done" };
  if (outcome === "cancelled") return { ...state, phase: "cancelled", stage: null };
  return { ...state, phase: "error", stage: "error", code: "interrupted" };
}

// A stable machine-readable name of what the orb is doing. CSS keys its colour and ring motion
// on it, so every value here must have a rule in activity.css.
function orbMode(state) {
  if (state.phase === "idle") return "idle";
  if (state.phase === "connecting") return "listen";
  if (state.phase === "done") return "done";
  if (state.phase === "error") return "error";
  if (state.phase === "cancelled") return "stopped";
  switch (state.stage) {
    case "memory_lookup":
      return "recall";
    case "researching":
      return "research";
    case "generating":
      return "synth";
    case "speaking":
      return "speak";
    default:
      return "listen"; // received, routing, route_selected
  }
}

function describe(state) {
  switch (state.phase) {
    case "idle":
      return { caption: "STANDBY", explain: "待機中です。メッセージを送ると、JARVIS の動きがここに表示されます。" };
    case "connecting":
      return { caption: "SENDING REQUEST", explain: "メッセージを送信しています。" };
    case "done":
      return { caption: "RESPONSE COMPLETE", explain: "応答が完了しました。" };
    case "cancelled":
      return { caption: "STOPPED", explain: "応答を停止しました。この応答は保存されていません。" };
    case "error":
      return {
        caption: "ERROR",
        explain: `${ERROR_TEXT[state.code] ?? ERROR_TEXT.internal}この応答は保存されていません。`,
      };
    default:
  }
  switch (state.stage) {
    case "received":
      return { caption: "REQUEST RECEIVED", explain: "メッセージを受け取りました。" };
    case "routing":
      return { caption: "ROUTING", explain: "どの経路で答えるかを判断しています。" };
    case "route_selected": {
      const label = ROUTE_LABELS[state.route] ?? "ROUTE";
      return { caption: `ROUTE SELECTED · ${label}`, explain: `${label} の経路を選びました。` };
    }
    case "memory_lookup":
      return {
        caption: "MEMORY LOOKUP",
        explain:
          state.count > 0
            ? `承認済みの記憶 ${state.count} 件を参照しています。`
            : "参照できる承認済みの記憶はありませんでした。",
      };
    case "researching":
      return {
        caption: state.step ? `RESEARCHING · ${state.step.toUpperCase()}` : "RESEARCHING",
        explain: STEP_TEXT[state.step] ?? "調査を進めています。",
      };
    case "generating":
      return { caption: "SYNTHESIZING RESPONSE", explain: "回答を生成しています。" };
    case "speaking":
      return { caption: "SPEAKING", explain: "音声で応答しています。" };
    default:
      return { caption: "WORKING", explain: "処理中です。" };
  }
}

// The nodes the server's own events prove are in use right now.
function activeNodes(state) {
  const active = new Set();
  if (state.phase !== "active" && state.phase !== "connecting") return active;
  active.add("input");
  switch (state.stage) {
    case "routing":
      active.add("router");
      break;
    case "route_selected":
      active.add("router");
      if (state.route) active.add(ROUTE_NODE[state.route]);
      break;
    case "researching":
      active.add("researcher");
      break;
    case "speaking":
      active.add("realtime");
      break;
    case "received":
    case "memory_lookup":
    case "generating":
      active.add("main");
      break;
    default:
  }
  return active;
}

// Which nodes exist today. A node outside this set is drawn dimmed with NOT_CONNECTED_TITLE
// unless the server reports it in use (then it is clearly connected after all).
const CONNECTED_TODAY = new Set(["input", "main"]);

const NODE_DEFS = [
  { id: "input", label: "INPUT" },
  { id: "router", label: "ROUTER" },
  { id: "realtime", label: "REALTIME" },
  { id: "main", label: "MAIN AGENT" },
  { id: "researcher", label: "RESEARCHER" },
];
// `direct` is the path messages take today, around the router that does not exist yet.
const EDGE_DEFS = [
  { id: "input-router", from: "input", to: "router" },
  { id: "router-realtime", from: "router", to: "realtime" },
  { id: "router-main", from: "router", to: "main" },
  { id: "router-researcher", from: "router", to: "researcher" },
  { id: "input-main", from: "input", to: "main", direct: true },
];

// Which edges carry the moving light. The direct path is lit only while no router is involved.
function activeEdges(state, nodes) {
  const lit = new Set();
  if (state.phase !== "active" && state.phase !== "connecting") return lit;
  const routerInvolved = nodes.get("router").active;
  if (routerInvolved) lit.add("input-router");
  for (const target of ["realtime", "main", "researcher"]) {
    if (routerInvolved && nodes.get(target).active) lit.add(`router-${target}`);
  }
  if (!routerInvolved && nodes.get("main").active) lit.add("input-main");
  if (!routerInvolved && state.phase === "connecting") lit.add("input-main");
  return lit;
}

// Everything the DOM layer needs, with all wording decided here.
export function viewModel(state, { reducedMotion = false } = {}) {
  const { caption, explain } = describe(state);
  const active = activeNodes(state);
  const nodes = new Map();
  for (const def of NODE_DEFS) {
    const isActive = active.has(def.id);
    const connected = CONNECTED_TODAY.has(def.id) || isActive;
    let status = "idle";
    if (isActive) status = "active";
    else if (def.id === "main" && state.phase === "done") status = "done";
    else if (def.id === "main" && state.phase === "error") status = "error";
    nodes.set(def.id, {
      id: def.id,
      label: def.label,
      connected,
      active: isActive,
      status,
      title: connected ? def.label : `${def.label} · ${NOT_CONNECTED_TITLE}`,
    });
  }
  const lit = activeEdges(state, nodes);
  const edges = EDGE_DEFS.map((def) => {
    const connected = nodes.get(def.from).connected && nodes.get(def.to).connected;
    return { ...def, direct: Boolean(def.direct), connected, active: lit.has(def.id) };
  });
  const connectedNames = [...nodes.values()].filter((node) => node.connected).map((node) => node.label);
  return {
    phase: state.phase,
    stage: state.stage,
    mode: orbMode(state),
    caption,
    explain,
    // The accessible equivalent of the whole display: one polite sentence per change.
    live: explain,
    final: FINAL.has(state.phase),
    reducedMotion: Boolean(reducedMotion),
    nodes: [...nodes.values()],
    edges,
    diagramLabel: `処理経路の図。現在つながっているのは ${connectedNames.join("、")} です。ほかのノードは未接続です。`,
  };
}
