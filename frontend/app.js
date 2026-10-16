import { sendChat } from "./chat-api.js";
import { createActivityView } from "./activity.js";
import { createMemoryPoller } from "./activity-memory.js";
import { mountModelSelect } from "./model-select.js";
import { renderReply } from "./chat-links.js";
import { createMessagePoller, isToolRequested } from "./chat-poll.js";
import { createConversationMemory, restoreConversation } from "./chat-restore.js";
import { createHistoryController } from "./chat-history.js";
import { createHistoryPanel } from "./history-panel.js";
import { createVoice, recordLatency, summarizeLatency } from "./voice.js";
import { ChatSession, MAX_MESSAGE_LENGTH, messageLength } from "./chat-session.js";

const conversation = document.querySelector("#conversation");
const welcome = document.querySelector("#welcome");
const form = document.querySelector("#chat-form");
const input = document.querySelector("#message");
const sendButton = document.querySelector("#send");
const stopButton = document.querySelector("#stop");
const newChatButton = document.querySelector("#new-chat");
const status = document.querySelector("#status");
const counter = document.querySelector("#counter");
const historyButton = document.querySelector("#open-history");

function safeStorage() {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

const micButton = document.querySelector("#mic");
const speakToggle = document.querySelector("#speak-toggle");
const voiceNote = document.querySelector("#voice-note");
const SPEAK_KEY = "jarvis.voice.speak";

function showVoiceNote(text) {
  voiceNote.textContent = text;
  voiceNote.hidden = !text;
}

// Browser voice: push-to-talk fills the message box (never sends); replies are read aloud
// chunk by chunk only when the person turned it on. No audio is stored (docs/voice.md).
const voice = createVoice({
  env: window,
  onNotice: showVoiceNote,
  onListening: (on) => micButton.setAttribute("aria-pressed", String(on)),
  onFirstSound: (ms) => {
    const stats = summarizeLatency(recordLatency(safeStorage(), ms));
    showVoiceNote(`最初の音まで ${Math.round(ms)} ms（直近${stats.count}回の中央値 ${stats.median} ms）`);
  },
});

const memory = createConversationMemory(safeStorage());
const activity = createActivityView(document, document.querySelector("#activity"), window);

// Memory made after a reply (chat auto-memory, research staging) arrives through the memory
// feed: read once at page load, then polled for a short while after each completed reply.
const memoryPoller = createMemoryPoller({
  onEvents: (events, options) => activity.memoryEvents(events, options),
  onConfig: (configured) => activity.memoryConfigured(configured),
  fetchImpl: (url, init) => fetch(url, init),
  isHidden: () => document.hidden,
});
void memoryPoller.baseline();

// Answers that a background tool task posts into the open conversation appear without a reload.
const poller = createMessagePoller({
  isHidden: () => document.hidden,
  onMessages: (messages) => {
    for (const message of messages) {
      const created = createMessage(message.role, message.content);
      if (message.role === "assistant") renderReply(document, created.content, message.content);
    }
  },
});

const modelMount = document.querySelector("#model-select");
let modelSelect = null;
void mountModelSelect(document, modelMount, {
  storage: safeStorage(),
}).then((control) => {
  modelSelect = control;
});

const PENDING_TEXT = "考えています…";
let lastReply = "";
const COUNTER_FROM = 3600;
let limitNoticeShown = false;

function setStatus(message, error = false) {
  status.textContent = message;
  status.classList.toggle("error", error);
}

function scrollToLatest() {
  window.scrollTo(0, document.documentElement.scrollHeight);
}

function createMessage(role, text, { scroll = true } = {}) {
  welcome.remove();
  const item = document.createElement("article");
  item.className = `message message-${role}`;
  const avatar = document.createElement("span");
  avatar.className = "message-avatar";
  avatar.setAttribute("aria-hidden", "true");
  avatar.textContent = role === "user" ? "U" : "J";
  const body = document.createElement("div");
  const label = document.createElement("span");
  label.className = "message-label";
  label.textContent = role === "user" ? "あなた" : "JARVIS";
  const content = document.createElement("div");
  content.className = "message-text";
  content.textContent = text;
  body.append(label, content);
  item.append(avatar, body);
  conversation.append(item);
  if (scroll) scrollToLatest();
  return { item, body, content };
}

// One user message plus the assistant reply slot that answers it. Retry reuses the same slot.
function beginTurn(text, onRetry) {
  createMessage("user", text);
  const assistant = createMessage("assistant", PENDING_TEXT);
  assistant.item.classList.add("message-pending");
  let extras = [];
  let retryButton = null;

  function clearExtras() {
    for (const element of extras) element.remove();
    extras = [];
    retryButton = null;
    assistant.item.classList.remove("message-error", "message-aborted");
  }

  return {
    showText(full) {
      lastReply = full;
      voice.feed(full);
      assistant.item.classList.remove("message-pending");
      renderReply(document, assistant.content, full);
      scrollToLatest();
    },
    complete({ provider, model }) {
      voice.finish(lastReply);
      activity.settle("done");
      poller.turnDone(session.conversationId);
      poller.watch();
      memoryPoller.watch();
      assistant.item.classList.remove("message-pending");
      if (typeof provider === "string" && typeof model === "string") {
        const meta = document.createElement("div");
        meta.className = "message-meta";
        meta.textContent = `${provider} · ${model}`;
        assistant.body.append(meta);
        extras.push(meta);
      }
      scrollToLatest();
    },
    // Text received before a failure is shown as an unsaved partial, never as the reply.
    fail({ message, partial, kind, retryable }) {
      voice.cancel();
      activity.settle(kind === "aborted" ? "cancelled" : "error");
      assistant.item.classList.remove("message-pending");
      assistant.item.classList.add(kind === "aborted" ? "message-aborted" : "message-error");
      if (partial) {
        assistant.content.textContent = partial;
        assistant.content.classList.add("message-partial");
        const tag = document.createElement("div");
        tag.className = "message-tag";
        tag.textContent = "途中まで受信した内容です（未完了・保存されていません）";
        assistant.body.insertBefore(tag, assistant.content);
        extras.push(tag);
      } else {
        assistant.content.textContent = "";
      }
      const note = document.createElement("div");
      note.className = "message-note";
      note.setAttribute("role", "alert");
      note.textContent = message;
      assistant.body.append(note);
      extras.push(note);
      if (retryable) {
        retryButton = document.createElement("button");
        retryButton.type = "button";
        retryButton.className = "retry";
        retryButton.textContent = "再試行";
        retryButton.addEventListener("click", onRetry);
        assistant.body.append(retryButton);
        extras.push(retryButton);
      }
      scrollToLatest();
    },
    reset() {
      memoryPoller.stop();
      activity.begin();
      voice.beginTurn();
      lastReply = "";
      clearExtras();
      assistant.content.classList.remove("message-partial");
      assistant.content.textContent = PENDING_TEXT;
      assistant.item.classList.add("message-pending");
    },
    clearRetry() {
      retryButton?.remove();
      extras = extras.filter((element) => element !== retryButton);
      retryButton = null;
    },
  };
}

const session = new ChatSession({
  onConversation: (id) => (id ? memory.set(id) : memory.clear()),
  send: (options) =>
    sendChat({
      ...options,
      modelChoice: modelSelect?.value ?? null,
      onActivity: (data) => {
        activity.handle(data);
        if (isToolRequested(data)) poller.noteTask();
      },
    }),
  view: {
    setStatus,
    clearInput() {
      input.value = "";
      resizeInput();
    },
    beginTurn: (text) => {
      poller.stop();
      memoryPoller.stop();
      activity.begin();
      voice.stopListening();
      voice.beginTurn();
      showVoiceNote("");
      lastReply = "";
      return beginTurn(text, () => session.retry());
    },
    setBusy(value) {
      sendButton.disabled = value;
      newChatButton.disabled = value;
      historyButton.disabled = value;
      modelSelect?.setDisabled(value);
      stopButton.hidden = !value;
      conversation.setAttribute("aria-busy", String(value));
      if (!value) input.focus();
    },
  },
});

function resizeInput() {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 176)}px`;
  if (!input.value) input.style.height = "";
  updateCounter();
}

function updateCounter() {
  const length = messageLength(input.value);
  counter.hidden = length < COUNTER_FROM;
  counter.textContent = `${length.toLocaleString("en-US")} / ${MAX_MESSAGE_LENGTH.toLocaleString("en-US")}`;
  counter.classList.toggle("limit", length >= MAX_MESSAGE_LENGTH);
  // Browsers truncate pasted text at maxlength without telling the user.
  if (input.value.length >= input.maxLength) {
    limitNoticeShown = true;
    setStatus(`${MAX_MESSAGE_LENGTH.toLocaleString("en-US")} 文字の上限です。超えた分は入力されません。`, true);
  } else if (limitNoticeShown) {
    limitNoticeShown = false;
    setStatus("");
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  session.submit(input.value);
});

input.addEventListener("keydown", (event) => {
  // keyCode 229 covers Safari, which reports IME confirmation Enter with isComposing false.
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
    event.preventDefault();
    form.requestSubmit();
  }
});

input.addEventListener("input", resizeInput);

// Stop: the request is aborted first, then the speech is cleared.
stopButton.addEventListener("click", () => {
  session.stop();
  voice.cancel();
});

if (voice.support.speak) {
  speakToggle.hidden = false;
  let saved = false;
  try {
    saved = safeStorage()?.getItem(SPEAK_KEY) === "1";
  } catch {
    saved = false;
  }
  const apply = (on) => {
    const value = voice.setSpeaking(on);
    speakToggle.setAttribute("aria-pressed", String(value));
    try {
      safeStorage()?.setItem(SPEAK_KEY, value ? "1" : "0");
    } catch {
      // The choice then lasts only for this page.
    }
  };
  apply(saved);
  speakToggle.addEventListener("click", () => apply(!voice.speaking));
}

if (voice.support.listen) {
  micButton.hidden = false;
  const down = () => {
    if (session.busy) return;
    voice.startListening((text) => {
      input.value = text;
      resizeInput();
    });
  };
  const up = () => voice.stopListening();
  micButton.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    down();
  });
  for (const type of ["pointerup", "pointerleave", "pointercancel"]) {
    micButton.addEventListener(type, up);
  }
  micButton.addEventListener("keydown", (event) => {
    if ((event.key === " " || event.key === "Enter") && !event.repeat) {
      event.preventDefault();
      down();
    }
  });
  micButton.addEventListener("keyup", (event) => {
    if (event.key === " " || event.key === "Enter") up();
  });
}

newChatButton.addEventListener("click", () => {
  if (!session.reset()) return;
  voice.cancel();
  showVoiceNote("");
  poller.sync(null, null);
  memory.clear();
  chatHistory.forget();
  conversation.replaceChildren(welcome);
  activity.reset();
  setStatus("");
  input.focus();
});

// History: the 履歴 panel, older pages and `/?c=<uuid>` (see chat-history.js).
const olderButton = document.createElement("button");
olderButton.type = "button";
olderButton.className = "history-older";
olderButton.textContent = "さらに前を読み込む";
olderButton.hidden = true;
olderButton.addEventListener("click", () => void chatHistory.loadOlder());

function showHistory(messages, { hasMore = false, nextBefore = null, total } = {}) {
  // Whatever selected this conversation (restore, ?c=, the history panel) is the one a tool
  // task will post into: poll that id, starting from how many messages it holds now.
  poller.sync(session.conversationId, total);
  poller.watchAlways();
  for (const message of messages) {
    const created = createMessage(message.role, message.content, { scroll: false });
    if (message.role === "assistant") renderReply(document, created.content, message.content);
  }
  conversation.prepend(olderButton);
  olderButton.hidden = !hasMore;
  chatHistory.adopt(session.conversationId, nextBefore);
  scrollToLatest();
}

const historyPanel = createHistoryPanel(
  document,
  {
    trigger: historyButton,
    backdrop: document.querySelector("#history-panel"),
    list: document.querySelector("#history-list"),
    state: document.querySelector("#history-state"),
    close: document.querySelector("#history-close"),
    more: document.querySelector("#history-more"),
  },
  {
    onOpen: () => void chatHistory.openList(),
    onSelect: (id) => void chatHistory.select(id),
    onMore: () => void chatHistory.moreList(),
  },
);

const chatHistory = createHistoryController({
  session,
  memory,
  fetchImpl: (url, init) => fetch(url, init),
  view: {
    setStatus,
    showListLoading: () => historyPanel.showListLoading(),
    showList: (page) => historyPanel.showList(page),
    showListError: (text) => historyPanel.showListError(text),
    closePanel: () => historyPanel.closePanel(),
    showConversation(messages, page) {
      memoryPoller.stop();
      activity.reset();
      conversation.replaceChildren();
      showHistory(messages, page);
    },
    prependOlder(messages, { hasMore }) {
      const items = messages.map((message) => {
        const created = createMessage(message.role, message.content, { scroll: false });
        if (message.role === "assistant") renderReply(document, created.content, message.content);
        return created.item;
      });
      olderButton.after(...items);
      olderButton.hidden = !hasMore;
    },
  },
});

function takeConversationParam() {
  const search = window.location.search;
  if (!new URLSearchParams(search).has("c")) return null;
  try {
    const url = new URL(window.location.href);
    url.searchParams.delete("c");
    window.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
  } catch {
    // The parameter then stays in the address bar; the page still works.
  }
  return search;
}

const linkedSearch = takeConversationParam();
if (linkedSearch !== null) {
  void chatHistory.openFromParam(linkedSearch);
} else {
  void restoreConversation({
    memory,
    session,
    view: { setStatus, showHistory },
  });
}
