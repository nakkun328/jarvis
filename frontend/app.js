import { sendChat } from "./chat-api.js";
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

const PENDING_TEXT = "考えています…";
const COUNTER_FROM = 3600;
let limitNoticeShown = false;

function setStatus(message, error = false) {
  status.textContent = message;
  status.classList.toggle("error", error);
}

function scrollToLatest() {
  window.scrollTo(0, document.documentElement.scrollHeight);
}

function createMessage(role, text) {
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
  scrollToLatest();
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
      assistant.item.classList.remove("message-pending");
      assistant.content.textContent = full;
      scrollToLatest();
    },
    complete({ provider, model }) {
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
  send: sendChat,
  view: {
    setStatus,
    clearInput() {
      input.value = "";
      resizeInput();
    },
    beginTurn: (text) => beginTurn(text, () => session.retry()),
    setBusy(value) {
      sendButton.disabled = value;
      newChatButton.disabled = value;
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

stopButton.addEventListener("click", () => session.stop());

newChatButton.addEventListener("click", () => {
  if (!session.reset()) return;
  conversation.replaceChildren(welcome);
  setStatus("");
  input.focus();
});
