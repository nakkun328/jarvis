import { MemoryApiError, loadMemory } from "./memory-api.js";
import { TABS, TAB_LABELS, apiErrorMessage, listViewModel } from "./memory-view.js";

// Read-only screen: every request is a GET and the DOM is built with textContent only. Memory
// content, sources, tags and projects are untrusted data and are never parsed as HTML.

const $ = (selector) => document.querySelector(selector);
const tabsBox = $("#tabs");
const listStatus = $("#list-status");
const listBox = $("#memory-list");
const listNote = $("#list-note");
const reloadButton = $("#reload");

const state = {
  tab: "notes",
  notes: [],
  candidates: [],
  truncated: { notes: false, candidates: false },
  loaded: false,
  error: null,
  lastUpdated: null,
  abort: null,
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

function itemNode(item) {
  const li = el("li", "memory-card");
  const head = el("div", "card-head");
  head.append(badge(item.status), badge(item.origin), el("span", "card-category", item.categoryText));
  li.append(head);
  if (item.isInference) {
    li.append(el("p", "inference-note", "AI の推測です。本人が言ったことではありません。"));
  }
  li.append(el("p", "memory-content", item.content));
  const meta = el("dl", "meta");
  for (const [term, value] of item.fields) {
    const wrap = el("div", "meta-item");
    wrap.append(el("dt", "", term), el("dd", "", value));
    meta.append(wrap);
  }
  li.append(meta);
  return li;
}

function renderStatus() {
  let message = "";
  let isError = false;
  if (!state.loaded && !state.error) message = "読み込み中…";
  else if (state.error) {
    isError = true;
    message = apiErrorMessage(state.error);
    if (state.lastUpdated) message += `（最後に取得した内容を表示中・${state.lastUpdated}）`;
  } else {
    message = `最終取得 ${state.lastUpdated}`;
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
  if (model.empty && state.loaded) {
    nodes.push(el("li", "empty", model.tab === "notes"
      ? "承認済みのノートはまだありません。"
      : "確認待ちの候補はありません。"));
  }
  listBox.replaceChildren(...nodes);
  listNote.textContent = model.truncated
    ? "新しい順の最新 100 件のみ表示しています。これより古いものはこの画面には出ません。"
    : "新しい順に表示しています。";
  renderStatus();
}

async function refresh() {
  state.abort?.abort();
  const controller = new AbortController();
  state.abort = controller;
  reloadButton.disabled = true;
  try {
    const result = await loadMemory({ signal: controller.signal });
    if (state.abort !== controller) return;
    state.notes = result.notes;
    state.candidates = result.candidates;
    state.truncated = result.truncated;
    state.loaded = true;
    state.error = null;
    state.lastUpdated = clock();
  } catch (error) {
    if (error?.name === "AbortError" || state.abort !== controller) return;
    state.error = navigator.onLine === false ? "offline" : (error instanceof MemoryApiError ? error.kind : "server");
  }
  reloadButton.disabled = false;
  render();
}

reloadButton.addEventListener("click", () => void refresh());
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) void refresh();
});
window.addEventListener("online", () => void refresh());

buildTabs();
render();
void refresh();
