import { TasksApiError, loadTask, loadTaskList } from "./tasks-api.js";
import { backoffDelay, followTask } from "./tasks-stream.js";
import {
  TASK_STATUSES,
  apiErrorMessage,
  connectionMessage,
  detailViewModel,
  isTerminal,
  listViewModel,
  mergeIntoList,
  statusChangeMessage,
  statusInfo,
} from "./tasks-view.js";

// Read-only screen: every request is a GET and the DOM is built with textContent only. Goals,
// step descriptions, notes and summaries are untrusted data and are never parsed as HTML.

const POLL_MS = 5000;
const POLL_MAX_MS = 30000;
const ID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

const $ = (selector) => document.querySelector(selector);
const filtersBox = $("#filters");
const listStatus = $("#list-status");
const listBox = $("#task-list");
const listNote = $("#list-note");
const layout = $("#layout");
const detailPane = $("#detail");
const detailHeading = $("#detail-heading");
const detailBody = $("#detail-body");
const connectionLine = $("#connection");
const reconnectButton = $("#reconnect");
const backButton = $("#back");
const announce = $("#announce");

const state = {
  filter: "all",
  all: [],
  shown: [],
  truncated: false,
  loaded: false,
  listError: null,
  lastUpdated: null,
  failures: 0,
  selectedId: null,
  detail: null,
  detailError: null,
  detailLoading: false,
  connection: { phase: "idle" },
  follow: null,
  pollTimer: null,
  listAbort: null,
  detailAbort: null,
  renderedList: "",
};

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function badge(info) {
  const node = el("span", `badge tone-${info.tone}`);
  node.append(el("span", "badge-glyph", info.glyph), el("span", "badge-label", info.label));
  node.firstChild.setAttribute("aria-hidden", "true");
  return node;
}

const clock = () => new Date().toLocaleTimeString("ja-JP");

// ----- filters -----

const filterButtons = new Map();

function buildFilters() {
  const entries = [["all", "すべて", null], ...TASK_STATUSES.map((s) => [s, statusInfo(s).label, statusInfo(s)])];
  for (const [key, label, info] of entries) {
    const button = el("button", "filter");
    button.type = "button";
    button.dataset.filter = key;
    if (info) button.classList.add(`tone-${info.tone}`);
    const glyph = el("span", "badge-glyph", info ? info.glyph : "");
    glyph.setAttribute("aria-hidden", "true");
    const name = el("span", "filter-label", label);
    const number = el("span", "filter-count", "–");
    button.append(glyph, name, number);
    button.addEventListener("click", () => {
      state.filter = key;
      renderFilters();
      renderList();
      void refreshList();
    });
    filterButtons.set(key, { button, number });
    filtersBox.append(button);
  }
}

function renderFilters() {
  const model = listViewModel(state.all, state.shown, { filter: state.filter });
  for (const [key, { button, number }] of filterButtons) {
    button.setAttribute("aria-pressed", String(key === state.filter));
    number.textContent = state.loaded ? String(model.counts[key]) : "–";
  }
}

// ----- list -----

function renderListStatus() {
  let message = "";
  let isError = false;
  if (!state.loaded && !state.listError) message = "読み込み中…";
  else if (state.listError) {
    isError = true;
    message = apiErrorMessage(state.listError);
    if (state.lastUpdated) message += `（最後に取得した内容を表示中・${state.lastUpdated}）`;
  } else {
    message = `最終取得 ${state.lastUpdated}`;
  }
  listStatus.textContent = message;
  listStatus.classList.toggle("error", isError);
}

function renderList() {
  const model = listViewModel(state.all, state.shown, {
    filter: state.filter,
    selectedId: state.selectedId,
    truncated: state.truncated,
  });
  const key = JSON.stringify([model.items, model.empty, model.truncated, state.loaded]);
  if (key !== state.renderedList) {
    state.renderedList = key;
    const focusedId = document.activeElement?.dataset?.taskId;
    const nodes = model.items.map((item) => {
      const li = el("li");
      const button = el("button", "task-item");
      button.type = "button";
      button.dataset.taskId = item.id;
      if (item.selected) button.setAttribute("aria-current", "true");
      const head = el("span", "task-item-head");
      head.append(badge(item.status), el("span", "task-count", item.countText));
      button.append(head, el("span", "task-goal", item.goal), el("span", "task-time", `更新 ${item.updatedText}`));
      button.addEventListener("click", () => navigateTo(item.id));
      li.append(button);
      return li;
    });
    if (model.empty && state.loaded) {
      const message = model.empty === "none"
        ? "タスクはまだありません。タスクはバックエンド側で作成され、ここには表示だけが行われます。"
        : "この状態のタスクはありません。";
      nodes.push(el("li", "empty", message));
    }
    listBox.replaceChildren(...nodes);
    if (focusedId) {
      for (const button of listBox.querySelectorAll("button[data-task-id]")) {
        if (button.dataset.taskId === focusedId) button.focus({ preventScroll: true });
      }
    }
  } else {
    // Only the selection changed or nothing did; keep nodes (and focus) in place.
    for (const button of listBox.querySelectorAll("button[data-task-id]")) {
      if (button.dataset.taskId === state.selectedId) button.setAttribute("aria-current", "true");
      else button.removeAttribute("aria-current");
    }
  }
  listNote.textContent = state.truncated
    ? `サーバーが返した新しい順の先頭 ${state.all.length} 件から集計しています。状態を選ぶと、その状態のタスクをサーバーから取得します。`
    : "取得できたタスクだけを新しい順に表示しています。";
  renderListStatus();
}

async function refreshList() {
  state.listAbort?.abort();
  const controller = new AbortController();
  state.listAbort = controller;
  try {
    const result = await loadTaskList({ filter: state.filter, signal: controller.signal });
    if (state.listAbort !== controller) return;
    state.all = result.all;
    state.shown = result.shown;
    state.truncated = result.truncated;
    state.loaded = true;
    state.listError = null;
    state.failures = 0;
    state.lastUpdated = clock();
    // Without a live stream the list is the only change signal for the selected task.
    const selected = result.all.find((task) => task.id === state.selectedId);
    if (selected && state.detail && selected.updated_at !== state.detail.updated_at
        && !["live", "connecting"].includes(state.connection.phase) && !isTerminal(state.detail)) {
      void refreshDetail();
    } else if (state.selectedId && !state.detail && state.detailError && state.detailError !== "not_found") {
      void refreshDetail().then(startFollow);
    }
  } catch (error) {
    if (error?.name === "AbortError" || state.listAbort !== controller) return;
    state.failures += 1;
    state.listError = navigator.onLine === false ? "offline" : (error instanceof TasksApiError ? error.kind : "server");
    state.lastUpdated = state.lastUpdated ?? null;
  }
  renderFilters();
  renderList();
  schedulePoll();
}

function schedulePoll() {
  clearTimeout(state.pollTimer);
  state.pollTimer = null;
  if (document.hidden) return;
  const delay = state.failures ? backoffDelay(state.failures, { baseMs: POLL_MS, maxMs: POLL_MAX_MS }) : POLL_MS;
  state.pollTimer = setTimeout(() => void refreshList(), delay);
}

// ----- detail -----

function field(term, value) {
  const wrap = el("div", "meta-item");
  wrap.append(el("dt", "", term), el("dd", "", value));
  return wrap;
}

function section(title, ...children) {
  const node = el("section", "detail-section");
  node.append(el("h3", "", title), ...children);
  return node;
}

function renderDetail() {
  detailPane.setAttribute("aria-busy", String(state.detailLoading));
  layout.dataset.view = state.selectedId ? "detail" : "list";
  if (!state.selectedId) {
    detailHeading.textContent = "詳細";
    detailBody.replaceChildren(el("p", "placeholder", "一覧からタスクを選ぶと、ここに詳細が表示されます。"));
    return;
  }
  if (!state.detail) {
    detailHeading.textContent = "詳細";
    const message = state.detailError
      ? el("p", "error-text", apiErrorMessage(state.detailError))
      : el("p", "placeholder", "読み込み中…");
    detailBody.replaceChildren(message);
    return;
  }
  const model = detailViewModel(state.detail);
  detailHeading.textContent = "タスクの詳細";

  const summary = el("div", "detail-summary");
  summary.append(badge(model.status), el("span", "task-count", model.countText));
  const segments = el("div", "segments");
  segments.setAttribute("aria-hidden", "true");
  for (const segment of model.segments) {
    const node = el("span", `segment tone-${segment.tone}`);
    node.title = segment.label;
    segments.append(node);
  }
  summary.append(segments);

  const parts = [summary];
  if (state.detailError) parts.push(el("p", "error-text", apiErrorMessage(state.detailError)));
  const goal = el("p", "goal", model.goal);
  parts.push(section("目的", goal));
  if (model.failure !== null) parts.push(el("p", "reason reason-failed", `失敗の理由: ${model.failure}`));
  if (model.waiting !== null) parts.push(el("p", "reason reason-waiting", `待機の理由: ${model.waiting}`));
  if (model.verification !== null) parts.push(el("p", "verification", `結果の検証: ${model.verification}`));
  if (model.resultSummary) parts.push(section("結果の要約", el("p", "result", model.resultSummary)));

  const steps = el("ol", "steps");
  for (const step of model.steps) {
    const li = el("li", "step");
    const head = el("div", "step-head");
    head.append(badge(step.status), el("span", "step-number", `手順 ${step.number}`));
    li.append(head, el("p", "step-text", step.description));
    const times = [];
    if (step.startedText) times.push(`開始 ${step.startedText}`);
    if (step.finishedText) times.push(`終了 ${step.finishedText}`);
    if (times.length) li.append(el("p", "step-time", times.join(" / ")));
    if (step.note) li.append(el("p", "step-note", `メモ: ${step.note}`));
    steps.append(li);
  }
  parts.push(section(`手順（${model.steps.length}）`, steps));

  const meta = el("dl", "meta");
  for (const [term, value] of model.times) meta.append(field(term, value));
  if (model.targetDevice) meta.append(field("対象デバイス", model.targetDevice));
  if (model.attempt !== null) meta.append(field("試行回数", String(model.attempt)));
  if (model.retryOf) meta.append(field("再試行元", model.retryOf));
  meta.append(field("ID", model.id));
  parts.push(section("情報", meta));
  detailBody.replaceChildren(...parts);
}

function renderConnection() {
  connectionLine.textContent = connectionMessage(state.connection);
  reconnectButton.hidden = state.connection.phase !== "gave_up";
}

function applyDetail(task) {
  const message = statusChangeMessage(state.detail, task);
  state.detail = task;
  state.detailError = null;
  state.all = mergeIntoList(state.all, task);
  state.shown = mergeIntoList(state.shown, task);
  if (message) announce.textContent = message;
  renderDetail();
  renderFilters();
  renderList();
}

async function refreshDetail() {
  const id = state.selectedId;
  if (!id) return;
  state.detailAbort?.abort();
  const controller = new AbortController();
  state.detailAbort = controller;
  state.detailLoading = !state.detail;
  renderDetail();
  try {
    const task = await loadTask(id, { signal: controller.signal });
    if (state.selectedId !== id || state.detailAbort !== controller) return;
    state.detailLoading = false;
    applyDetail(task);
  } catch (error) {
    if (error?.name === "AbortError" || state.selectedId !== id || state.detailAbort !== controller) return;
    state.detailLoading = false;
    state.detailError = navigator.onLine === false ? "offline" : (error instanceof TasksApiError ? error.kind : "server");
    renderDetail();
  }
}

// ----- live progress -----

function stopFollow() {
  state.follow?.abort();
  state.follow = null;
}

function setConnection(info) {
  state.connection = info;
  renderConnection();
}

function startFollow() {
  stopFollow();
  const id = state.selectedId;
  if (!id || !state.detail) return setConnection({ phase: "idle" });
  if (isTerminal(state.detail)) return setConnection({ phase: "done" });
  if (document.hidden) return setConnection({ phase: "paused" });
  const controller = new AbortController();
  state.follow = controller;
  const current = () => state.follow === controller && state.selectedId === id;
  void followTask(id, {
    signal: controller.signal,
    onState(streamState) {
      if (current() && streamState.task) applyDetail(streamState.task);
    },
    onConnection(info) {
      if (current()) setConnection(info);
    },
  }).then((reason) => {
    if (!current()) return;
    if (reason === "done") setConnection({ phase: "done" });
    else if (reason === "unauthorized") setConnection({ phase: "unauthorized" });
    else if (reason === "not_found") {
      state.detailError = "not_found";
      setConnection({ phase: "not_found" });
      renderDetail();
    }
  });
}

// ----- selection -----

function idFromHash() {
  const id = decodeURIComponent(location.hash.slice(1));
  return ID_PATTERN.test(id) ? id : null;
}

function navigateTo(id) {
  if (id) {
    // Setting the hash fires hashchange, which applies the selection.
    if (location.hash.slice(1) === id) applySelection(id);
    else location.hash = id;
  } else {
    history.pushState(null, "", location.pathname);
    applySelection(null);
  }
}

function applySelection(id) {
  if (id === state.selectedId && (id === null || state.detail)) return;
  stopFollow();
  state.detailAbort?.abort();
  state.selectedId = id;
  state.detail = null;
  state.detailError = null;
  setConnection({ phase: "idle" });
  renderDetail();
  renderList();
  if (!id) return;
  void refreshDetail().then(() => {
    startFollow();
    if (matchMedia("(max-width: 800px)").matches) detailHeading.focus();
  });
}

backButton.addEventListener("click", () => {
  const previous = state.selectedId;
  navigateTo(null);
  const button = listBox.querySelector(`button[data-task-id="${previous}"]`);
  button?.focus();
});

reconnectButton.addEventListener("click", () => {
  void refreshDetail().then(startFollow);
});

window.addEventListener("hashchange", () => applySelection(idFromHash()));

// ----- visibility and connectivity -----

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    clearTimeout(state.pollTimer);
    state.pollTimer = null;
    stopFollow();
    if (state.detail && !isTerminal(state.detail)) setConnection({ phase: "paused" });
  } else {
    void refreshList();
    if (state.selectedId) void refreshDetail().then(startFollow);
  }
});

window.addEventListener("online", () => void refreshList());
window.addEventListener("offline", () => {
  state.listError = "offline";
  renderList();
});

buildFilters();
renderFilters();
renderList();
renderDetail();
renderConnection();
applySelection(idFromHash());
void refreshList();
