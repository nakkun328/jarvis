import {
  RunApiError,
  cancelResearch,
  loadAvailability,
  pollSession,
  startResearch,
} from "./research-run-api.js";
import { detailViewModel, isTerminal, statusInfo } from "./research-view.js";
import {
  LEVELS,
  SEARCH_NOTICE,
  activeSessionId,
  availability,
  levelHint,
  pollDelay,
  progressModel,
  questionState,
  resultModel,
  runErrorMessage,
} from "./research-run-view.js";

// The "new research" panel: a form, live progress, and the cited result. Every string from the
// server (question, claims, quotes, titles, result text) is placed with textContent; nothing is
// parsed as markup. A link is made only for an http(s) URL that safeHref accepted, and it opens
// with rel="noopener noreferrer". The two POSTs live in research-run-api.js.

const $ = (selector) => document.querySelector(selector);

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

export function initRunPanel({ onSessionsChanged = () => {}, onOpenDetail = () => {} } = {}) {
  const panel = $("#run-panel");
  if (!panel) return { notifySessions() {} };
  const form = $("#run-form");
  const question = $("#run-question");
  const counter = $("#run-counter");
  const level = $("#run-level");
  const levelHintNode = $("#run-level-hint");
  const submit = $("#run-submit");
  const formError = $("#run-error");
  const unavailable = $("#run-unavailable");
  const progressBox = $("#run-progress");
  const progressHeading = $("#run-progress-heading");
  const stagesList = $("#run-stages");
  const countersBox = $("#run-counters");
  const live = $("#run-live");
  const cancelButton = $("#run-cancel");
  const progressError = $("#run-progress-error");
  const progressNote = $("#run-progress-note");
  const resultBox = $("#run-result");

  const state = {
    mode: "loading", // loading | unavailable | form | submitting | watching | finished
    enabled: false,
    sessionId: null,
    session: null,
    failures: 0,
    cancelling: false,
    timer: null,
    abort: null,
    renderedProgress: "",
    serverError: null,
    notice: null,
  };

  // ----- form -----

  for (const item of LEVELS) {
    const option = el("option", "", item.label);
    option.value = item.value;
    level.append(option);
  }

  function refreshForm() {
    const q = questionState(question.value);
    counter.textContent = q.counterText;
    counter.classList.toggle("over", q.tooLong);
    levelHintNode.textContent = levelHint(level.value);
    const busy = state.mode === "submitting";
    const canEdit = state.enabled && !busy;
    question.disabled = !canEdit;
    level.disabled = !canEdit;
    submit.disabled = !(canEdit && q.valid);
    const code = state.enabled && q.problem ? q.problem : state.serverError;
    formError.textContent = code ? runErrorMessage(code) : "";
  }

  function showFormError(code) {
    state.serverError = code;
    refreshForm();
  }

  function show(mode) {
    state.mode = mode;
    form.hidden = !(mode === "form" || mode === "submitting" || mode === "unavailable");
    progressBox.hidden = mode !== "watching";
    resultBox.hidden = mode !== "finished";
    unavailable.hidden = mode !== "unavailable";
    refreshForm();
  }

  question.addEventListener("input", () => {
    state.serverError = null;
    refreshForm();
  });
  level.addEventListener("change", refreshForm);
  level.addEventListener("input", refreshForm);
  question.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
      event.preventDefault();
      form.requestSubmit();
    }
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const q = questionState(question.value);
    if (!state.enabled || state.mode === "submitting" || !q.valid) return;
    state.serverError = null;
    show("submitting");
    try {
      const id = await startResearch({ question: question.value, level: level.value });
      question.value = "";
      attach(id);
      onSessionsChanged();
    } catch (error) {
      show("form");
      const code = error instanceof RunApiError ? error.code : "server";
      showFormError(navigator.onLine === false ? "offline" : code);
      if (code === "busy") {
        // The running research is picked up from the list; say why the new one was not started.
        state.notice = runErrorMessage("busy");
        onSessionsChanged();
      }
    }
  });

  // ----- watching -----

  function stopPolling() {
    clearTimeout(state.timer);
    state.timer = null;
    state.abort?.abort();
    state.abort = null;
  }

  function attach(id) {
    stopPolling();
    state.sessionId = id;
    state.session = null;
    state.failures = 0;
    state.cancelling = false;
    state.renderedProgress = "";
    progressError.textContent = "";
    progressNote.textContent = state.notice ?? "";
    state.notice = null;
    live.textContent = "";
    show("watching");
    renderProgress();
    progressHeading.focus({ preventScroll: false });
    void poll();
  }

  function schedule() {
    clearTimeout(state.timer);
    state.timer = null;
    if (document.hidden || state.mode !== "watching") return;
    state.timer = setTimeout(() => void poll(), pollDelay(state.failures));
  }

  async function poll() {
    const id = state.sessionId;
    if (!id || state.mode !== "watching") return;
    state.abort?.abort();
    const controller = new AbortController();
    state.abort = controller;
    try {
      const session = await pollSession(id, { signal: controller.signal });
      if (state.abort !== controller || state.sessionId !== id) return;
      state.failures = 0;
      progressError.textContent = "";
      state.session = session;
      if (isTerminal(session)) {
        finish(session);
        return;
      }
      renderProgress();
    } catch (error) {
      if (error?.name === "AbortError" || state.abort !== controller) return;
      const code = error instanceof RunApiError ? error.code : "server";
      if (code === "session_not_found") {
        stopPolling();
        show("form");
        showFormError("session_not_found");
        return;
      }
      state.failures += 1;
      progressError.textContent = `${runErrorMessage(navigator.onLine === false ? "offline" : code)} 再試行しています。`;
    }
    schedule();
  }

  function renderProgress() {
    const model = progressModel(state.session ?? { status: "pending" });
    const key = JSON.stringify([model, state.cancelling]);
    if (key === state.renderedProgress) return;
    state.renderedProgress = key;
    stagesList.replaceChildren(
      ...model.stages.map((stage) => {
        const item = el("li", `stage stage-${stage.state}`);
        item.dataset.stage = stage.key;
        if (stage.state === "current") item.setAttribute("aria-current", "step");
        const mark = { done: "✓", current: "▶", todo: "○" }[stage.state];
        const glyph = el("span", "stage-glyph", mark);
        glyph.setAttribute("aria-hidden", "true");
        const text = { done: "（完了）", current: "（実行中）", todo: "（待機中）" }[stage.state];
        item.append(glyph, el("span", "stage-label", stage.label), el("span", "sr-only", text));
        return item;
      }),
    );
    countersBox.replaceChildren(
      ...model.counters.map((counter) => {
        const wrap = el("div", "counter-item");
        wrap.append(el("dt", "", counter.label), el("dd", "", String(counter.value)));
        return wrap;
      }),
    );
    const roundText = model.round > 0 ? `（追加検索 ${model.round} 回目）` : "";
    const message = model.label ? `${model.label}${roundText}` : "";
    if (live.textContent !== message) live.textContent = message;
    cancelButton.hidden = !model.cancellable;
    cancelButton.disabled = state.cancelling;
    cancelButton.textContent = state.cancelling ? "取り消しています…" : "調査を取り消す";
  }

  cancelButton.addEventListener("click", async () => {
    if (!state.sessionId || state.cancelling) return;
    state.cancelling = true;
    renderProgress();
    try {
      await cancelResearch(state.sessionId);
    } catch (error) {
      const code = error instanceof RunApiError ? error.code : "server";
      if (code !== "not_cancellable") {
        state.cancelling = false;
        progressError.textContent = runErrorMessage(navigator.onLine === false ? "offline" : code);
      }
    }
    renderProgress();
    void poll();
  });

  // ----- result -----

  function linkLine(source) {
    const line = el("p", "claim-source");
    line.append(`出典 ${source.number}: `);
    const label = source.title ? `${source.title}（${source.url}）` : source.url;
    if (source.href) {
      const link = el("a", "", label);
      link.href = source.href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      line.append(link);
    } else {
      line.append(label);
    }
    return line;
  }

  function finish(session) {
    stopPolling();
    state.session = session;
    const detail = detailViewModel(session);
    const model = resultModel(session, detail);
    const heading = el("h3", "run-result-heading", {
      completed: "調査の結果",
      cancelled: "調査は取り消されました",
      failed: "調査は失敗しました",
    }[model.kind]);
    heading.tabIndex = -1;
    const summary = el("div", "detail-summary");
    summary.append(badge(statusInfo(session.status)));
    const parts = [heading, summary];

    if (model.kind === "failed") {
      parts.push(el("p", "reason", `失敗の理由: ${model.failure ?? "その他"}`));
    }
    if (model.kind === "cancelled") {
      parts.push(el("p", "placeholder", "調べている途中で取り消したため、結果はありません。"));
    }
    if (model.kind === "completed") {
      if (model.noVerifiedClaims) {
        parts.push(
          el(
            "p",
            "run-callout",
            "出典から、引用つきで確認できる主張は得られませんでした。分かったことを推測では書いていません。",
          ),
        );
      } else {
        const list = el("ol", "claims run-claims");
        for (const claim of model.claims) {
          const item = el("li", "claim");
          item.append(el("p", "claim-text", claim.claimText), el("p", "quote", claim.quote));
          item.append(
            claim.source ? linkLine(claim.source) : el("p", "claim-source missing", "出典が見つかりません"),
          );
          list.append(item);
        }
        const section = el("section", "detail-section");
        section.append(el("h4", "run-subheading", `検証済みの主張（${model.claims.length}）`), list);
        parts.push(section);
      }
      if (model.conflicts.length > 0) {
        const list = el("ul", "run-notes conflicts");
        for (const text of model.conflicts) list.append(el("li", "", text));
        const section = el("section", "detail-section run-conflicts");
        section.append(el("h4", "run-subheading", "出典の間の食い違い"), list);
        parts.push(section);
      }
      if (model.caveats.length > 0) {
        const list = el("ul", "run-notes caveats");
        for (const text of model.caveats) list.append(el("li", "", text));
        const section = el("section", "detail-section run-caveats");
        section.append(el("h4", "run-subheading", "注意点"), list);
        parts.push(section);
      }
      parts.push(el("p", "run-citation-note", model.citationNote));
    }

    const actions = el("div", "run-actions");
    const open = el("button", "run-secondary", "詳細（出典と評価）を開く");
    open.type = "button";
    open.addEventListener("click", () => onOpenDetail(session.id));
    const again = el("button", "run-primary", "新しい調査を始める");
    again.type = "button";
    again.addEventListener("click", () => {
      show("form");
      question.focus();
    });
    actions.append(open, again);
    parts.push(actions);

    resultBox.replaceChildren(...parts);
    show("finished");
    live.textContent = "";
    heading.focus({ preventScroll: false });
    onSessionsChanged();
  }

  // ----- availability and re-attaching -----

  async function init() {
    document.querySelector("#run-notice").textContent = SEARCH_NOTICE;
    refreshForm();
    let body = null;
    try {
      body = await loadAvailability();
    } catch (error) {
      state.enabled = false;
      show("unavailable");
      unavailable.textContent = runErrorMessage(error instanceof RunApiError ? error.code : "server");
      return;
    }
    const info = availability(body);
    state.enabled = info.enabled;
    if (info.enabled) {
      unavailable.textContent = "";
      show("form");
    } else {
      unavailable.textContent = info.reasonText;
      show("unavailable");
    }
  }

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      clearTimeout(state.timer);
      state.timer = null;
    } else if (state.mode === "watching") {
      void poll();
    }
  });

  void init();

  return {
    // Called with every refreshed session list: a run that is still going (for example after a
    // page reload) is picked up so its progress shows again.
    notifySessions(sessions) {
      if (state.mode !== "form" || !state.enabled) return;
      const id = activeSessionId(sessions);
      if (id) attach(id);
    },
  };
}
