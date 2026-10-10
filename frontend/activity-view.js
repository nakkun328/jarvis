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
// What the router can choose. `main` is where a turn runs, never a router choice.
export const DECIDED = ["casual", "memory", "research"];
// Shown while the router chose a path that is not wired yet and the Main Agent answers instead.
export const NOT_WIRED_TEXT = "この経路はまだ接続されていないため、メインのエージェントで処理します";
export const FALLBACK_TEXT = "ルーターが経路を確定できなかったため、既定のメインのエージェントで処理します";
// The chat emits only `started` (a research was handed to the run service); the other steps are
// part of the vocabulary but the chat does not follow the run.
export const STEPS = ["started", "planning", "searching", "reading", "verifying", "writing"];
// Why a research the router asked for was not started (the Main Agent answers instead).
export const SKIPS = ["busy", "not_configured", "budget_exhausted", "refused", "low_confidence"];
// Why a casual decision ran on the Main Agent (only a server with the casual path switched on
// says so). `low_confidence` is shared with the research list above.
export const CASUAL_SKIPS = ["over_budget", "provider", "low_confidence"];
export const CASUAL_SKIP_TEXT = {
  over_budget: "雑談経路の1日の利用上限に達しているため、メインのエージェントで処理します",
  provider: "雑談経路で応答を得られなかったため、メインのエージェントで処理します",
  low_confidence: "経路の確信度が足りないため雑談経路を使わず、メインのエージェントで処理します",
};
// Shown for a turn in which a research really was started: it runs in the background.
export const RESEARCH_STARTED_TEXT =
  "調査をバックグラウンドで開始しました。進み具合と結果は「リサーチ」画面で見られます";
export const SKIP_TEXT = {
  busy: "別の調査が実行中のため調査を開始できず、メインのエージェントで処理します",
  not_configured: "調査を利用できないため、メインのエージェントで処理します",
  budget_exhausted: "検索の上限に達しているため調査を開始できず、メインのエージェントで処理します",
  refused: "調査を開始できなかったため、メインのエージェントで処理します",
  low_confidence: "経路の確信度が足りないため調査を開始せず、メインのエージェントで処理します",
};
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
  started: RESEARCH_STARTED_TEXT,
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
  return {
    phase: "idle", stage: null, route: null, decided: null, fallback: false, routed: false,
    skip: null, count: null, step: null, code: null,
  };
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
    // Optional: a server without a router sends only `route`.
    if (isOneOf(DECIDED, payload.decided)) clean.decided = payload.decided;
    if (typeof payload.fallback === "boolean") clean.fallback = payload.fallback;
    if (isOneOf(SKIPS, payload.research_skip)) clean.research_skip = payload.research_skip;
    if (isOneOf(CASUAL_SKIPS, payload.casual_skip)) clean.casual_skip = payload.casual_skip;
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
    // A router took part in this turn from the first routing event on.
    routed: state.routed || event.stage === "routing" || event.stage === "route_selected",
    decided: event.stage === "route_selected" ? (event.decided ?? null) : state.decided,
    fallback: event.stage === "route_selected" ? event.fallback === true : state.fallback,
    skip:
      event.stage === "route_selected"
        ? (event.research_skip ?? event.casual_skip ?? null)
        : state.skip,
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

// The router's choice when it is a path that is not wired (casual, research) and the Main Agent
// runs the turn instead; otherwise null. Never claims a route ran that did not.
function unwiredChoice(state) {
  if (state.route !== "main" || !state.routed || state.skip) return null;
  return state.decided === "casual" || state.decided === "research" ? state.decided : null;
}

// A research was really started for this turn (the server said the research path ran).
function researchStarted(state) {
  return state.route === "research" && state.decided === "research" && state.routed;
}

// The router chose research, none was started, and the server said why.
function researchSkipped(state) {
  return state.route === "main" && state.decided === "research" && state.routed && Boolean(state.skip);
}

// The caption and line shared by the "started" turn and its `researching{started}` moment.
const STARTED_VIEW = { caption: "ROUTED: RESEARCH", explain: RESEARCH_STARTED_TEXT };

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
      if (researchStarted(state)) return STARTED_VIEW;
      if (researchSkipped(state)) {
        return { caption: "RESEARCH NOT STARTED", explain: SKIP_TEXT[state.skip] };
      }
      if (casualSkipped(state)) {
        return { caption: "CASUAL NOT USED", explain: CASUAL_SKIP_TEXT[state.skip] };
      }
      const unwired = unwiredChoice(state);
      if (unwired) {
        return { caption: `ROUTED: ${unwired.toUpperCase()}`, explain: NOT_WIRED_TEXT };
      }
      const label = ROUTE_LABELS[state.route] ?? "ROUTE";
      if (state.fallback) {
        return { caption: `ROUTE SELECTED · ${label} (FALLBACK)`, explain: FALLBACK_TEXT };
      }
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
      if (state.step === "started" && researchStarted(state)) return STARTED_VIEW;
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

// The node that shows the turn's end: the Researcher when a research was started (the Main Agent
// did not run), otherwise the Main Agent.
function resultNode(state) {
  if (casualRan(state)) return "realtime";
  return researchStarted(state) ? "researcher" : "main";
}

// The casual path really answered this turn (the server said the casual route ran).
function casualRan(state) {
  return state.route === "casual" && state.decided === "casual" && state.routed;
}

// The router chose casual, the casual path is on, and it did not answer; the server said why.
function casualSkipped(state) {
  return (
    state.route === "main" && state.decided === "casual" && state.routed &&
    Boolean(state.skip) && Object.hasOwn(CASUAL_SKIP_TEXT, state.skip)
  );
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
    case "generating":
      // The casual path has no Main Agent step: its answer is generated by REALTIME.
      active.add(casualRan(state) ? "realtime" : "main");
      break;
    case "received":
    case "memory_lookup":
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
  // Once the server reported routing, the turn runs through the router for its whole length.
  const routerInvolved = state.routed || nodes.get("router").active;
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
    // The router counts as connected only for a turn the server actually routed.
    const connected =
      CONNECTED_TODAY.has(def.id) ||
      isActive ||
      (def.id === "router" && state.routed) ||
      (def.id === "researcher" && researchStarted(state)) ||
      (def.id === "realtime" && casualRan(state));
    let status = "idle";
    if (isActive) status = "active";
    else if (def.id === resultNode(state) && state.phase === "done") status = "done";
    else if (def.id === resultNode(state) && state.phase === "error") status = "error";
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
  const unwired = unwiredChoice(state);
  const routeNote = researchStarted(state)
    ? { decided: "research", caption: STARTED_VIEW.caption, text: STARTED_VIEW.explain }
    : researchSkipped(state)
      ? { decided: "research", caption: "RESEARCH NOT STARTED", text: SKIP_TEXT[state.skip] }
      : casualSkipped(state)
        ? { decided: "casual", caption: "CASUAL NOT USED", text: CASUAL_SKIP_TEXT[state.skip] }
      : unwired
        ? { decided: unwired, caption: `ROUTED: ${unwired.toUpperCase()}`, text: NOT_WIRED_TEXT }
        : null;
  return {
    phase: state.phase,
    stage: state.stage,
    mode: orbMode(state),
    // A note that outlasts the single route_selected moment, for the rest of the turn.
    routeNote,
    caption,
    explain,
    // The accessible equivalent of the whole display: one polite sentence per change.
    // While the turn is still running the unwired-route reason is part of the spoken line too:
    // route_selected is usually followed by the next stage within milliseconds.
    live:
      routeNote &&
      state.phase === "active" &&
      state.stage !== "route_selected" &&
      explain !== routeNote.text
        ? `${explain}${routeNote.text}。`
        : explain,
    final: FINAL.has(state.phase),
    reducedMotion: Boolean(reducedMotion),
    nodes: [...nodes.values()],
    edges,
    diagramLabel: `処理経路の図。現在つながっているのは ${connectedNames.join("、")} です。ほかのノードは未接続です。`,
  };
}
