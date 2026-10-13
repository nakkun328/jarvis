// The group discussion screen's behaviour. Everything the browser provides is injected (`env`),
// so Node tests drive it with a fake document, fake requests and a fake clock.
import { conversationParam } from "./chat-history.js";
import * as realApi from "./discussion-api.js";
import * as realPersonaApi from "./discussion-personas-api.js";
import { createPersonaPanel } from "./discussion-personas-panel.js";
import { createDiscussionPoller } from "./discussion-poll.js";
import { chipNode, messageNode } from "./discussion-render.js";
import {
  FEATURE_OFF_MESSAGE,
  buildStartBody,
  cancelErrorMessage,
  colorMap,
  errorMessage,
  availableModels,
  initialModel,
  isActiveStatus,
  messageViewModel,
  progressText,
  startBlockers,
  topicLength,
  withListing,
} from "./discussion-view.js";

export const PAGE_PATH = "/discussion";
export const INVALID_LINK_MESSAGE = "指定された議論は見つかりませんでした。";
export const LOAD_FAILED_MESSAGE = "読み込めませんでした。接続を確認して、もう一度お試しください。";
export const GAVE_UP_MESSAGE = "自動更新を止めました。再読み込みすると続きを確認できます。";

export function createScreen(env) {
  const { doc } = env;
  const api = env.api ?? realApi;
  const personaApi = env.personaApi ?? realPersonaApi;
  const $ = (id) => doc.getElementById(id);
  const el = {};
  for (const id of [
    "discussion-heading", "status", "retry", "start-section", "start-form", "topic", "topic-counter",
    "participants-hint", "participant-list", "rounds", "remaining", "blockers", "form-error", "start",
    "run-section", "run-title", "progress", "stop", "again", "readonly-note", "run-error", "transcript",
  ]) el[id] = $(id);

  const panel = createPersonaPanel({
    doc,
    api: personaApi,
    onChange: (listing, info) => applyListing(listing, info),
  });

  const state = {
    config: null,
    topic: "",
    participants: [], // { name, model, color } in the order chosen
    models: new Map(), // preset name -> model chosen
    rounds: 2,
    starting: false,
    colors: new Map(),
    poller: null,
    taskId: null,
    conversationId: null,
    stopping: false,
  };

  const setStatus = (text, error = false) => {
    el.status.textContent = text;
    el.status.className = error ? "list-status error" : "list-status";
  };

  // ----- start form -----

  function formValue() {
    return { topic: state.topic, participants: state.participants, rounds: state.rounds };
  }

  function refreshForm() {
    const config = state.config;
    if (config === null) return;
    const length = topicLength(state.topic);
    el["topic-counter"].textContent = `${length} / ${config.topic_max_chars}`;
    el["topic-counter"].className = length > config.topic_max_chars ? "counter over" : "counter";
    el.remaining.textContent = `今日の残り: ${config.remaining_today} / ${config.per_day_limit} 回（使用済み ${config.used_today} 回）`;
    const full = state.participants.length >= config.participants_max;
    for (const row of state.rows ?? []) {
      row.checkbox.disabled = row.preset.available === false || (full && !row.checkbox.checked);
      row.select.disabled = !row.checkbox.checked;
      row.node.className = row.checkbox.checked ? "participant selected" : "participant";
    }
    el["participants-hint"].textContent =
      `${config.participants_min}〜${config.participants_max}人を選びます（現在 ${state.participants.length}人）。` +
      (full ? "上限に達したため、ほかの参加者は選べません。" : "");
    const blockers = startBlockers(config, formValue());
    el.blockers.replaceChildren(...blockers.map((text) => {
      const item = doc.createElement("li");
      item.textContent = text;
      return item;
    }));
    el.start.disabled = state.starting || blockers.length > 0;
    el.start.setAttribute("aria-disabled", String(el.start.disabled));
  }

  function syncParticipants() {
    state.participants = (state.rows ?? [])
      .filter((row) => row.checkbox.checked)
      .map((row) => ({ name: row.preset.name, model: row.select.value, color: row.preset.color }));
  }

  // `previous`: the choices before a rebuild (name -> { checked, model }); null on the first build.
  function buildForm(config, previous = null) {
    state.rows = [];
    const usable = availableModels(config);
    el["participant-list"].replaceChildren();
    config.personas.forEach((preset, index) => {
      const selectable = preset.available !== false;
      const node = doc.createElement("div");
      node.className = "participant";
      const label = doc.createElement("label");
      label.className = "participant-pick";
      const checkbox = doc.createElement("input");
      checkbox.type = "checkbox";
      checkbox.id = `participant-${index}`;
      const before = previous?.get(preset.name);
      checkbox.checked = selectable && (previous === null ? index < 3 : before?.checked === true);
      checkbox.disabled = !selectable;
      const name = doc.createElement("span");
      name.className = "participant-name";
      name.textContent = preset.name;
      label.append(checkbox, chipNode(doc, preset.color), name);
      const role = doc.createElement("p");
      role.className = "participant-role";
      role.textContent = preset.custom ? `${preset.role}（自分のペルソナ）` : preset.role;
      const modelBox = doc.createElement("div");
      modelBox.className = "participant-model";
      const modelLabel = doc.createElement("label");
      modelLabel.textContent = "モデル";
      const select = doc.createElement("select");
      select.id = `participant-model-${index}`;
      modelLabel.setAttribute("for", select.id);
      select.setAttribute("aria-label", `${preset.name}のモデル`);
      for (const model of usable) {
        const option = doc.createElement("option");
        option.value = model.id;
        option.textContent = model.id;
        select.append(option);
      }
      select.value = before && usable.some((m) => m.id === before.model) ? before.model : initialModel(preset, config);
      modelBox.append(modelLabel, select);
      node.append(label, role);
      if (!selectable) {
        node.append(make("p", "participant-flag", "モデルが使えないため選べません。ペルソナの編集で別のモデルを選んでください。"));
      }
      node.append(modelBox);
      el["participant-list"].append(node);
      const row = { preset, checkbox, select, node };
      state.rows.push(row);
      const changed = () => {
        syncParticipants();
        refreshForm();
      };
      checkbox.addEventListener("change", changed);
      select.addEventListener("change", changed);
    });
    el.rounds.replaceChildren();
    for (let n = 1; n <= config.max_rounds; n += 1) {
      const option = doc.createElement("option");
      option.value = String(n);
      option.textContent = `${n} ラウンド`;
      el.rounds.append(option);
    }
    state.rounds = config.default_rounds;
    el.rounds.value = String(state.rounds);
    syncParticipants();
  }

  function make(tag, className, text) {
    const node = doc.createElement(tag);
    node.className = className;
    node.textContent = text;
    return node;
  }

  // A fresh persona listing after an edit: rebuild the picker, keep the choices that still exist
  // (a renamed persona keeps its place, a deleted one drops out).
  function applyListing(listing, info = {}) {
    if (state.config === null) return;
    const previous = new Map();
    for (const row of state.rows ?? []) {
      const name = info.renamed?.from === row.preset.name ? info.renamed.to : row.preset.name;
      previous.set(name, { checked: row.checkbox.checked, model: row.select.value });
    }
    if (info.deleted) previous.delete(info.deleted);
    state.config = withListing(state.config, listing);
    buildForm(state.config, previous);
    refreshForm();
  }

  function showPanel() {
    panel.setListing(
      { presets: state.config.presets, custom: state.config.custom, max_custom: state.config.max_custom, personas_store: state.config.personas_store },
      { models: state.config.models, palette: state.config.palette },
    );
  }

  function showStart() {
    el["start-section"].hidden = false;
    showPanel();
    el["run-section"].hidden = true;
    refreshForm();
  }

  async function submit() {
    if (state.starting || state.config === null) return;
    if (startBlockers(state.config, formValue()).length > 0) return;
    state.starting = true;
    el["form-error"].textContent = "";
    refreshForm();
    const participants = state.participants.map((p) => ({ ...p }));
    try {
      const body = buildStartBody(state.config, formValue());
      const started = await api.startDiscussion(body);
      state.colors = colorMap(participants, state.config.personas);
      env.replaceUrl(`${PAGE_PATH}?c=${started.conversation_id}`);
      state.starting = false;
      beginRun(started.conversation_id, started.task_id, { live: true });
    } catch (error) {
      state.starting = false;
      el["form-error"].textContent = errorMessage(error?.code ?? error?.kind, error?.status ?? null);
      // The counters and the busy state may have changed on the server.
      if (error?.code === "DISCUSSION_BUSY" || error?.code === "DISCUSSION_DAILY_LIMIT") {
        try {
          const fresh = await api.loadConfig();
          if (fresh !== null) state.config = fresh;
        } catch {
          // Keep the old numbers; the error above already explains.
        }
      }
      refreshForm();
    }
  }

  // ----- run view -----

  function setLive(live) {
    el.stop.hidden = !live;
    el.again.hidden = live;
    el["run-section"].setAttribute("aria-busy", String(live));
  }

  function appendMessages(messages) {
    for (const message of messages) {
      el.transcript.append(messageNode(doc, messageViewModel(message, state.colors)));
    }
  }

  function beginRun(conversationId, taskId, { live }) {
    stopPoller();
    state.conversationId = conversationId;
    state.taskId = taskId;
    state.stopping = false;
    el["start-section"].hidden = true;
    panel.hide();
    el["run-section"].hidden = false;
    el.transcript.replaceChildren();
    el["run-error"].textContent = "";
    el["readonly-note"].hidden = live;
    el.stop.disabled = false;
    setLive(live);
    el.progress.textContent = live ? progressText(null) : "";
    setStatus("");
    if (!live) return;
    state.poller = createDiscussionPoller({
      conversationId,
      taskId,
      api,
      schedule: env.schedule,
      cancel: env.cancel,
      isHidden: env.isHidden,
      callbacks: {
        onMessages: appendMessages,
        onStatus: (task) => {
          el["run-error"].textContent = "";
          el.progress.textContent = state.stopping && isActiveStatus(task.status) ? "停止しています…" : progressText(task);
        },
        onFinished: (task) => {
          el.progress.textContent = progressText(task);
          setLive(false);
          el.stop.disabled = false;
          el["readonly-note"].hidden = true;
        },
        onError: (error) => {
          el["run-error"].textContent = errorMessage(error?.code ?? error?.kind, error?.status ?? null);
        },
        onGiveUp: () => {
          el["run-error"].textContent = GAVE_UP_MESSAGE;
          setLive(false);
        },
      },
    });
    state.poller.start();
  }

  function stopPoller() {
    if (state.poller !== null) state.poller.stop();
    state.poller = null;
  }

  async function stop() {
    if (state.taskId === null || state.stopping) return;
    state.stopping = true;
    el.stop.disabled = true;
    el["run-error"].textContent = "";
    const result = await api.cancelDiscussion(state.taskId);
    if (!result.ok) {
      state.stopping = false;
      el.stop.disabled = false;
      el["run-error"].textContent = cancelErrorMessage(result.status);
      return;
    }
    el.progress.textContent = "停止しています…";
    if (state.poller !== null) await state.poller.nudge();
  }

  async function showExisting(conversationId, config) {
    el["start-section"].hidden = true;
    try {
      const { messages } = await api.loadMessages(conversationId, 0);
      if (messages.length === 0) {
        setStatus(INVALID_LINK_MESSAGE, true);
        return;
      }
      state.colors = colorMap([], config?.personas ?? []);
      beginRun(conversationId, null, { live: false });
      appendMessages(messages);
      el["run-title"].focus?.();
    } catch (error) {
      const missing = error?.kind === "http" && error.status === 404;
      setStatus(missing ? INVALID_LINK_MESSAGE : errorMessage(error?.code ?? error?.kind, error?.status ?? null), true);
      el.retry.hidden = missing;
    }
  }

  // ----- start-up -----

  async function init() {
    el.retry.hidden = true;
    const link = conversationParam(env.search);
    setStatus("読み込み中…");
    let config;
    try {
      config = await api.loadConfig();
    } catch (error) {
      setStatus(errorMessage(error?.code ?? error?.kind, error?.status ?? null), true);
      el.retry.hidden = false;
      return;
    }
    state.config = config;
    if (link.kind === "invalid") {
      setStatus(INVALID_LINK_MESSAGE, true);
      if (config !== null) {
        buildForm(config);
        showStart();
      }
      return;
    }
    if (link.kind === "ok") {
      if (config?.active?.conversation_id === link.id) {
        state.colors = colorMap([], config.personas);
        beginRun(link.id, config.active.task_id, { live: true });
        return;
      }
      await showExisting(link.id, config);
      return;
    }
    if (config === null) {
      setStatus(FEATURE_OFF_MESSAGE, true);
      return;
    }
    if (config.active !== null) {
      state.colors = colorMap([], config.personas);
      env.replaceUrl(`${PAGE_PATH}?c=${config.active.conversation_id}`);
      beginRun(config.active.conversation_id, config.active.task_id, { live: true });
      return;
    }
    setStatus("");
    buildForm(config);
    showStart();
  }

  el.topic.addEventListener("input", () => {
    state.topic = el.topic.value;
    refreshForm();
  });
  el.rounds.addEventListener("change", () => {
    state.rounds = Number.parseInt(el.rounds.value, 10);
    refreshForm();
  });
  el["start-form"].addEventListener("submit", (event) => {
    event.preventDefault?.();
    void submit();
  });
  el.stop.addEventListener("click", () => void stop());
  el.again.addEventListener("click", () => env.navigate(PAGE_PATH));
  el.retry.addEventListener("click", () => void init());

  return { init, state, el, submit, stop, destroy: stopPoller };
}
