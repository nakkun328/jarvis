import { ResearchApiError, loadSession, loadSessionList } from "./research-api.js";
import { initRunPanel } from "./research-run.js";
import { loadCandidates, memoryPanelModel, stageCandidates } from "./stage-candidates.js";
import {
  SESSION_STATUSES,
  apiErrorMessage,
  backoffDelay,
  detailViewModel,
  isTerminal,
  listViewModel,
  statusChangeMessage,
  statusInfo,
} from "./research-view.js";

// Read-only screen: every request is a GET and the DOM is built with textContent only. Questions,
// queries, titles, quotes and results are untrusted data and are never parsed as HTML. Links are
// created only for http(s) URLs (see safeHref) and always open without referrer or opener.

const POLL_MS = 5000;
const POLL_MAX_MS = 30000;
const ID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

const $ = (selector) => document.querySelector(selector);
const filtersBox = $("#filters");
const listStatus = $("#list-status");
const listBox = $("#session-list");
const listNote = $("#list-note");
const layout = $("#layout");
const detailPane = $("#detail");
const mainBox = $(".research-main");
const detailHeading = $("#detail-heading");
const detailBody = $("#detail-body");
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
  pollTimer: null,
  listAbort: null,
  detailAbort: null,
  renderedList: "",
  renderedDetail: "",
  memory: { id: null, phase: "idle", count: 0, omitted: 0, created: 0, errorKind: null },
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

// A link only for an href that safeHref accepted; otherwise plain text. The URL text is
// always shown as text, never as markup.
function urlNode(url, href, className) {
  const wrap = el("p", className);
  if (href) {
    const link = el("a", "", url);
    link.href = href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    wrap.append(link);
  } else {
    wrap.textContent = url;
  }
  return wrap;
}

const runPanel = initRunPanel({
  onSessionsChanged: () => void refreshList(),
  onOpenDetail: (id) => navigateTo(id),
});

const clock = () => new Date().toLocaleTimeString("ja-JP");

// ----- filters -----

const filterButtons = new Map();

function buildFilters() {
  const entries = [
    ["all", "すべて", null],
    ...SESSION_STATUSES.map((s) => [s, statusInfo(s).label, statusInfo(s)]),
  ];
  for (const [key, label, info] of entries) {
    const button = el("button", "filter");
    button.type = "button";
    button.dataset.filter = key;
    if (info) button.classList.add(`tone-${info.tone}`);
    const glyph = el("span", "badge-glyph", info ? info.glyph : "");
    glyph.setAttribute("aria-hidden", "true");
    const number = el("span", "filter-count", "–");
    button.append(glyph, el("span", "filter-label", label), number);
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
    const focusedId = document.activeElement?.dataset?.sessionId;
    const nodes = model.items.map((item) => {
      const li = el("li");
      const button = el("button", "session-item");
      button.type = "button";
      button.dataset.sessionId = item.id;
      if (item.selected) button.setAttribute("aria-current", "true");
      const head = el("span", "session-item-head");
      head.append(badge(item.status), el("span", "session-meta", item.levelText));
      button.append(
        head,
        el("span", "session-question", item.question),
        el("span", "session-time", `更新 ${item.updatedText}`),
      );
      button.addEventListener("click", () => navigateTo(item.id));
      li.append(button);
      return li;
    });
    if (model.empty && state.loaded) {
      const message = model.empty === "none"
        ? "調査はまだありません。上のフォームから調べたいことを依頼できます。"
        : "この状態の調査はありません。";
      nodes.push(el("li", "empty", message));
    }
    listBox.replaceChildren(...nodes);
    if (focusedId) {
      for (const button of listBox.querySelectorAll("button[data-session-id]")) {
        if (button.dataset.sessionId === focusedId) button.focus({ preventScroll: true });
      }
    }
  } else {
    // Only the selection changed or nothing did; keep nodes (and focus) in place.
    for (const button of listBox.querySelectorAll("button[data-session-id]")) {
      if (button.dataset.sessionId === state.selectedId) button.setAttribute("aria-current", "true");
      else button.removeAttribute("aria-current");
    }
  }
  listNote.textContent = state.truncated
    ? `新しい順の最新 ${state.all.length} 件から集計しています。状態を選ぶと、その状態の最新の調査をサーバーから取得します。`
    : "調査を新しい順に表示しています。";
  renderListStatus();
}

async function refreshList() {
  state.listAbort?.abort();
  const controller = new AbortController();
  state.listAbort = controller;
  try {
    const result = await loadSessionList({ filter: state.filter, signal: controller.signal });
    if (state.listAbort !== controller) return;
    state.all = result.all;
    state.shown = result.shown;
    state.truncated = result.truncated;
    state.loaded = true;
    state.listError = null;
    state.failures = 0;
    state.lastUpdated = clock();
    runPanel.notifySessions(state.all);
  } catch (error) {
    if (error?.name === "AbortError" || state.listAbort !== controller) return;
    state.failures += 1;
    state.listError = navigator.onLine === false ? "offline" : (error instanceof ResearchApiError ? error.kind : "server");
  }
  renderFilters();
  renderList();
  // A running session gains queries and sources without its own row changing, so its detail
  // is re-read on every cycle. A finished session no longer changes and is not re-read.
  if (state.selectedId && (!state.detail || !isTerminal(state.detail))
      && state.detailError !== "not_found") {
    void refreshDetail();
  }
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

function sourceNode(source) {
  const li = el("li", "source");
  li.append(el("p", "item-number", `出典 ${source.number}`));
  li.append(el("p", "source-title", source.title ?? "（タイトルなし）"));
  const sub = [source.typeText, source.publisher].filter(Boolean).join(" ・ ");
  li.append(el("p", "source-sub", sub));
  li.append(urlNode(source.url, source.href, "source-url"));
  if (source.finalUrl) {
    const finalNode = urlNode(source.finalUrl, source.finalHref, "source-url");
    finalNode.prepend("リダイレクト先: ");
    li.append(finalNode);
  }
  const ratings = el("dl", "ratings");
  for (const rating of source.ratings) {
    const item = el("div", "rating");
    item.append(el("dt", "", rating.label), el("dd", "", rating.text));
    ratings.append(item);
  }
  li.append(ratings);
  const times = [];
  if (source.publishedText) times.push(`公開 ${source.publishedText}`);
  times.push(`取得 ${source.retrievedText}`);
  times.push(`引用された回数 ${source.claimCount}`);
  li.append(el("p", "source-times", times.join(" / ")));
  return li;
}

function claimNode(claim) {
  const li = el("li", "claim");
  li.append(el("p", "item-number", `主張 ${claim.number}`));
  li.append(el("p", "claim-text", claim.claimText));
  li.append(el("p", "quote", claim.quote));
  if (claim.rangeText) li.append(el("p", "quote-range", claim.rangeText));
  if (claim.source) {
    const line = el("p", "claim-source");
    line.append(`出典 ${claim.source.number}: `);
    if (claim.source.href) {
      const link = el("a", "", claim.source.url);
      link.href = claim.source.href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      line.append(link);
    } else {
      line.append(claim.source.url);
    }
    li.append(line);
  } else {
    li.append(el("p", "claim-source missing", "出典が見つかりません"));
  }
  return li;
}

function memorySection() {
  const view = memoryPanelModel(state.memory);
  const box = el("div", "memory-candidates");
  box.append(el("p", "memory-note", view.note));
  const button = el("button", "run-secondary", view.buttonLabel);
  button.type = "button";
  button.disabled = view.disabled;
  button.addEventListener("click", () => void stageMemory(state.detail.id));
  box.append(button);
  const status = el("p", "memory-status", view.status);
  status.setAttribute("role", "status");
  box.append(status);
  const error = el("p", "error-text", view.error);
  error.setAttribute("role", "alert");
  box.append(error);
  return section("記憶の候補", box);
}

function setMemory(id, patch) {
  if (state.selectedId !== id) return;
  state.memory = { ...state.memory, id, ...patch };
  renderDetail();
}

// Reads which candidates already exist, once per selected session, to show the added state.
async function ensureMemoryState(id) {
  if (state.memory.id === id) return;
  state.memory = { id, phase: "loading", count: 0, omitted: 0, created: 0, errorKind: null };
  const result = await loadCandidates(id);
  if (result.kind === "ok" && result.count > 0) {
    setMemory(id, { phase: "added", count: result.count, omitted: result.omitted, created: 0 });
  } else {
    setMemory(id, { phase: "idle" });
  }
}

async function stageMemory(id) {
  setMemory(id, { phase: "busy", errorKind: null });
  const result = await stageCandidates(id);
  if (result.kind === "ok") {
    setMemory(id, {
      phase: "added",
      count: result.count,
      omitted: result.omitted,
      created: result.created,
    });
    announce.textContent = "記憶の候補に追加しました（未承認）。";
  } else {
    setMemory(id, { phase: "error", errorKind: result.kind });
  }
}

function renderDetail() {
  detailPane.setAttribute("aria-busy", String(state.detailLoading));
  layout.dataset.view = state.selectedId ? "detail" : "list";
  mainBox.dataset.view = layout.dataset.view;
  if (!state.selectedId) {
    state.renderedDetail = "";
    detailHeading.textContent = "詳細";
    detailBody.replaceChildren(el("p", "placeholder", "一覧から調査を選ぶと、ここに詳細が表示されます。"));
    return;
  }
  if (!state.detail) {
    state.renderedDetail = "";
    detailHeading.textContent = "詳細";
    const message = state.detailError
      ? el("p", "error-text", apiErrorMessage(state.detailError))
      : el("p", "placeholder", "読み込み中…");
    detailBody.replaceChildren(message);
    return;
  }
  const model = detailViewModel(state.detail);
  // Re-render only when the content changed, so polling does not reset scroll or selection.
  const key = JSON.stringify([model, state.detailError, state.memory]);
  if (key === state.renderedDetail) return;
  state.renderedDetail = key;
  detailHeading.textContent = "調査の詳細";

  const summary = el("div", "detail-summary");
  summary.append(badge(model.status), el("span", "session-meta", `レベル: ${model.levelText}`));

  const parts = [summary];
  if (state.detailError) parts.push(el("p", "error-text", apiErrorMessage(state.detailError)));
  parts.push(section("質問", el("p", "question", model.question)));
  if (model.reuse !== null) {
    const reuse = el("p", "reuse", model.reuse.label);
    reuse.append(el("span", "reuse-note", ` — ${model.reuse.note}`));
    parts.push(reuse);
  }
  if (model.failure !== null) parts.push(el("p", "reason", `失敗の理由: ${model.failure}`));
  if (model.resultText) {
    parts.push(section("結果", el("p", "result", model.resultText)));
  } else if (!model.failure) {
    parts.push(section("結果", el("p", "placeholder", "まだ結果は保存されていません。")));
  }

  const queries = el("ol", "queries");
  for (const query of model.queries) {
    const li = el("li", "query");
    li.append(el("span", "item-number", `${query.number}.`), el("span", "query-text", query.text));
    queries.append(li);
  }
  parts.push(section(`検索クエリ（${model.queries.length}）`, queries));

  const sources = el("ul", "sources");
  for (const source of model.sources) sources.append(sourceNode(source));
  parts.push(section(`出典（${model.sources.length}）`, sources));

  const claims = el("ul", "claims");
  for (const claim of model.claims) claims.append(claimNode(claim));
  parts.push(section(`主張と引用（${model.claims.length}）`, claims));

  if (state.detail.status === "completed" && model.claims.length > 0) {
    parts.push(memorySection());
    void ensureMemoryState(state.detail.id);
  }

  const meta = el("dl", "meta");
  for (const [term, value] of model.times) meta.append(field(term, value));
  meta.append(field("ID", model.id));
  parts.push(section("情報", meta));
  detailBody.replaceChildren(...parts);
}

function applyDetail(session) {
  const message = statusChangeMessage(state.detail, session);
  state.detail = session;
  state.detailError = null;
  if (message) announce.textContent = message;
  renderDetail();
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
    const session = await loadSession(id, { signal: controller.signal });
    if (state.selectedId !== id || state.detailAbort !== controller) return;
    state.detailLoading = false;
    applyDetail(session);
  } catch (error) {
    if (error?.name === "AbortError" || state.selectedId !== id || state.detailAbort !== controller) return;
    state.detailLoading = false;
    state.detailError = navigator.onLine === false ? "offline" : (error instanceof ResearchApiError ? error.kind : "server");
    renderDetail();
  }
}

// ----- selection -----

function idFromHash() {
  let id = "";
  try {
    id = decodeURIComponent(location.hash.slice(1));
  } catch {
    return null;
  }
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
  state.detailAbort?.abort();
  state.selectedId = id;
  state.detail = null;
  state.detailError = null;
  state.renderedDetail = "";
  renderDetail();
  renderList();
  if (!id) return;
  void refreshDetail().then(() => {
    if (matchMedia("(max-width: 800px)").matches) detailHeading.focus();
  });
}

backButton.addEventListener("click", () => {
  const previous = state.selectedId;
  navigateTo(null);
  const button = [...listBox.querySelectorAll("button[data-session-id]")]
    .find((candidate) => candidate.dataset.sessionId === previous);
  button?.focus();
});

window.addEventListener("hashchange", () => applySelection(idFromHash()));

// ----- visibility and connectivity -----

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    clearTimeout(state.pollTimer);
    state.pollTimer = null;
  } else {
    void refreshList();
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
applySelection(idFromHash());
void refreshList();
