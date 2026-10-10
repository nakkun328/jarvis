import { MemoryApiError, loadMemory, loadMemoryDetail } from "./memory-api.js";
import { WITHDRAW_CONFIRM, WITHDRAW_LABEL, withdrawMemory, withdrawMessage } from "./withdraw-memory.js";
import {
  TABS,
  TAB_LABELS,
  apiErrorMessage,
  cleanQuery,
  createDebouncer,
  detailModel,
  emptyMessage,
  isMemoryId,
  listViewModel,
  queryMessage,
  resultSummary,
} from "./memory-view.js";

// Read-only screen: every request is a GET and the DOM is built with textContent only. Memory
// content, sources, tags and projects are untrusted data and are never parsed as HTML. The search
// text is only ever sent as a URL-encoded query value and shown back with textContent.

const SEARCH_DELAY_MS = 300;

const $ = (selector) => document.querySelector(selector);
const tabsBox = $("#tabs");
const listStatus = $("#list-status");
const listBox = $("#memory-list");
const listNote = $("#list-note");
const reloadButton = $("#reload");
const listView = $("#list-view");
const listHeading = $("#list-heading");
const searchInput = $("#search");
const searchClear = $("#search-clear");
const detailView = $("#detail-view");
const detailHeading = $("#detail-heading");
const detailStatus = $("#detail-status");
const detailBody = $("#detail-body");
const backButton = $("#detail-back");

const state = {
  tab: "notes",
  notes: [],
  candidates: [],
  truncated: { notes: false, candidates: false },
  loaded: false,
  error: null,
  lastUpdated: null,
  abort: null,
  query: "", // the text the shown results are for ("" = no search)
  queryError: null,
  detailId: null,
  detail: null,
  detailError: null,
  detailAbort: null,
  returnFocusId: null,
};

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function badge(info) {
  const node = el("span", `badge tone-${info.tone}`);
  const glyph = el("span", "badge-glyph", info.glyph);
  glyph.setAttribute("aria-hidden", "true");
  node.append(glyph, el("span", "badge-label", info.label));
  return node;
}

const clock = () => new Date().toLocaleTimeString("ja-JP");

function metaList(fields) {
  const meta = el("dl", "meta");
  for (const [term, value] of fields) {
    const wrap = el("div", "meta-item");
    wrap.append(el("dt", "", term), el("dd", "", value));
    meta.append(wrap);
  }
  return meta;
}

function errorKind(error) {
  if (navigator.onLine === false) return "offline";
  return error instanceof MemoryApiError ? error.kind : "server";
}

// ----- tabs -----

const tabButtons = new Map();

function buildTabs() {
  for (const key of TABS) {
    const button = el("button", "filter");
    button.type = "button";
    button.dataset.tab = key;
    const number = el("span", "filter-count", "–");
    button.append(el("span", "filter-label", TAB_LABELS[key]), number);
    button.addEventListener("click", () => {
      state.tab = key;
      render();
    });
    tabButtons.set(key, { button, number });
    tabsBox.append(button);
  }
}

// ----- list -----

function autoNote(auto) {
  const line = [auto.label];
  if (auto.source) line.push(`出典: ${auto.source}`);
  if (auto.date) line.push(`取得日: ${auto.date}`);
  return el("p", "inference-note", line.join(" / "));
}

function itemNode(item) {
  const li = el("li", "memory-card");
  const head = el("div", "card-head");
  head.append(badge(item.status), badge(item.origin), el("span", "card-category", item.categoryText));
  li.append(head);
  if (item.isInference) {
    li.append(el("p", "inference-note", "AI の推測です。本人が言ったことではありません。"));
  }
  if (item.auto) li.append(autoNote(item.auto));
  li.append(el("p", "memory-content", item.content), metaList(item.fields));
  const open = el("button", "card-open", "詳細を見る");
  open.type = "button";
  open.dataset.open = item.id;
  open.addEventListener("click", () => openDetail(item.id));
  li.append(open);
  return li;
}

function renderStatus() {
  let message = "";
  let isError = false;
  if (state.queryError) {
    isError = true;
    message = queryMessage(state.queryError);
  } else if (!state.loaded && !state.error) message = "読み込み中…";
  else if (state.error) {
    isError = true;
    message = apiErrorMessage(state.error);
    if (state.lastUpdated) message += `（最後に取得した内容を表示中・${state.lastUpdated}）`;
  } else {
    const summary = resultSummary(state.query, {
      notes: state.notes.length,
      candidates: state.candidates.length,
    });
    message = summary ? `${summary}（${state.lastUpdated}）` : `最終取得 ${state.lastUpdated}`;
  }
  listStatus.textContent = message;
  listStatus.classList.toggle("error", isError);
}

function render() {
  const model = listViewModel(state.notes, state.candidates, {
    tab: state.tab,
    truncated: state.truncated,
  });
  for (const [key, { button, number }] of tabButtons) {
    button.setAttribute("aria-pressed", String(key === model.tab));
    number.textContent = state.loaded ? String(model.counts[key]) : "–";
  }
  const nodes = model.items.map(itemNode);
  if (model.empty && state.loaded) nodes.push(el("li", "empty", emptyMessage(model.tab, state.query)));
  listBox.replaceChildren(...nodes);
  listNote.textContent = model.truncated
    ? "新しい順の最新 100 件のみ表示しています。これより古いものはこの画面には出ません。"
    : "新しい順に表示しています。";
  searchClear.hidden = searchInput.value === "";
  renderStatus();
}

async function refresh() {
  const cleaned = cleanQuery(searchInput.value);
  if (!cleaned.ok) {
    // A query the API would refuse is explained here and not sent; the shown results stay.
    state.queryError = cleaned.reason;
    render();
    return;
  }
  state.queryError = null;
  state.abort?.abort();
  const controller = new AbortController();
  state.abort = controller;
  reloadButton.disabled = true;
  try {
    const result = await loadMemory({ q: cleaned.value, signal: controller.signal });
    if (state.abort !== controller) return;
    state.notes = result.notes;
    state.candidates = result.candidates;
    state.truncated = result.truncated;
    state.loaded = true;
    state.error = null;
    state.query = cleaned.value;
    state.lastUpdated = clock();
  } catch (error) {
    if (error?.name === "AbortError" || state.abort !== controller) return;
    state.error = errorKind(error);
  }
  reloadButton.disabled = false;
  render();
}

// ----- search -----

const debouncedRefresh = createDebouncer(() => void refresh(), SEARCH_DELAY_MS);

function clearSearch() {
  searchInput.value = "";
  debouncedRefresh.cancel();
  searchClear.hidden = true;
  searchInput.focus();
  void refresh();
}

searchInput.addEventListener("input", () => {
  searchClear.hidden = searchInput.value === "";
  debouncedRefresh.schedule();
});
searchInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    debouncedRefresh.flush();
  } else if (event.key === "Escape" && searchInput.value !== "") {
    event.preventDefault();
    clearSearch();
  }
});
searchClear.addEventListener("click", clearSearch);

// ----- detail -----

function relatedNode(link) {
  const wrap = el("div", "detail-link");
  wrap.append(el("span", "detail-link-label", link.label), el("code", "detail-link-id", link.id));
  const open = el("button", "card-open", "そのノートの詳細を見る");
  open.type = "button";
  open.addEventListener("click", () => {
    location.hash = link.id;
  });
  wrap.append(open);
  return wrap;
}

function withdrawControl(id) {
  const box = el("div", "detail-section");
  const button = el("button", "card-open", WITHDRAW_LABEL);
  button.type = "button";
  const message = el("p", "list-note", "");
  message.setAttribute("role", "status");
  button.addEventListener("click", async () => {
    if (!window.confirm(WITHDRAW_CONFIRM)) return;
    button.disabled = true;
    const result = await withdrawMemory(id);
    if (result.kind === "ok") {
      void loadDetail(id);
      void refresh();
      return;
    }
    button.disabled = false;
    message.textContent = withdrawMessage(result.kind);
  });
  box.append(button, message);
  return box;
}

function renderDetail() {
  detailBody.replaceChildren();
  detailStatus.classList.toggle("error", Boolean(state.detailError));
  if (state.detailError) {
    detailStatus.textContent = apiErrorMessage(state.detailError);
    return;
  }
  if (!state.detail) {
    detailStatus.textContent = "読み込み中…";
    return;
  }
  detailStatus.textContent = "";
  const model = detailModel(state.detail);
  const head = el("div", "card-head");
  head.append(badge(model.status), badge(model.origin), el("span", "card-category", model.categoryText));
  detailBody.append(head);
  if (model.isInference) {
    detailBody.append(el("p", "inference-note", "AI の推測です。本人が言ったことではありません。"));
  }
  if (model.auto) detailBody.append(autoNote(model.auto));
  detailBody.append(el("p", "memory-content", model.content), metaList(model.fields));
  if (model.canWithdraw) detailBody.append(withdrawControl(state.detail.id));
  if (model.links.length) {
    const section = el("section", "detail-section");
    section.append(el("h2", "", "関連する記録"));
    for (const link of model.links) section.append(relatedNode(link));
    detailBody.append(section);
  }
  const history = el("section", "detail-section");
  history.append(el("h2", "", "履歴"));
  if (model.history.length === 0) {
    history.append(el("p", "list-note", "記録された履歴はありません。"));
  } else {
    const list = el("ol", "history");
    for (const entry of model.history) {
      const item = el("li", "history-item");
      item.append(el("span", "history-time", entry.time), el("span", "history-text", entry.text));
      if (entry.revision) item.append(el("span", "history-revision", `リビジョン ${entry.revision}`));
      if (entry.relatedId) item.append(el("code", "detail-link-id", entry.relatedId));
      list.append(item);
    }
    history.append(list);
  }
  detailBody.append(history);
}

async function loadDetail(id) {
  state.detailAbort?.abort();
  const controller = new AbortController();
  state.detailAbort = controller;
  state.detail = null;
  state.detailError = null;
  renderDetail();
  try {
    const detail = await loadMemoryDetail(id, { signal: controller.signal });
    if (state.detailAbort !== controller) return;
    state.detail = detail;
  } catch (error) {
    if (error?.name === "AbortError" || state.detailAbort !== controller) return;
    state.detailError = errorKind(error);
  }
  renderDetail();
}

function hashId() {
  let raw = "";
  try {
    raw = decodeURIComponent(location.hash.slice(1));
  } catch {
    // A malformed escape is simply not an id.
  }
  return isMemoryId(raw) ? raw : null;
}

// The address bar (#<id>) decides which view is shown, so Back and shared links work.
function applyHash() {
  const id = hashId();
  if (id === state.detailId) return;
  state.detailId = id;
  if (id === null) {
    state.detailAbort?.abort();
    detailView.hidden = true;
    listView.hidden = false;
    // Return focus to the button that opened the detail, else to the page heading.
    const opener = state.returnFocusId
      ? listBox.querySelector(`[data-open="${state.returnFocusId}"]`)
      : null;
    (opener ?? listHeading).focus();
    return;
  }
  listView.hidden = true;
  detailView.hidden = false;
  window.scrollTo(0, 0);
  detailHeading.focus();
  void loadDetail(id);
}

function openDetail(id) {
  state.returnFocusId = id;
  location.hash = id;
}

function closeDetail() {
  history.pushState(null, "", location.pathname + location.search);
  applyHash();
}

backButton.addEventListener("click", closeDetail);
window.addEventListener("hashchange", applyHash);

reloadButton.addEventListener("click", () => {
  debouncedRefresh.cancel();
  void refresh();
});
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) void refresh();
});
window.addEventListener("online", () => void refresh());

buildTabs();
render();
void refresh();
applyHash();
