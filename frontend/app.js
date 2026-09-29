import { ChatError, sendChat } from "./chat-api.js";

const conversation = document.querySelector("#conversation");
const welcome = document.querySelector("#welcome");
const form = document.querySelector("#chat-form");
const input = document.querySelector("#message");
const sendButton = document.querySelector("#send");
const newChatButton = document.querySelector("#new-chat");
const status = document.querySelector("#status");

let conversationId = null;
let busy = false;

function setStatus(message, error = false) {
  status.textContent = message;
  status.classList.toggle("error", error);
}

function scrollToLatest() {
  window.scrollTo(0, document.documentElement.scrollHeight);
}

function addMessage(role, text = "") {
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

function setBusy(value) {
  busy = value;
  sendButton.disabled = value;
  newChatButton.disabled = value;
  conversation.setAttribute("aria-busy", String(value));
}

async function submitMessage() {
  if (busy) return;
  const message = input.value.trim();
  if (!message) return;

  input.value = "";
  input.style.height = "";
  addMessage("user", message);
  const assistant = addMessage("assistant", "考えています…");
  let reply = "";
  setBusy(true);
  setStatus("JARVIS が応答しています…");

  try {
    const result = await sendChat({
      message,
      conversationId,
      onDelta(text) {
        reply += text;
        assistant.content.textContent = reply;
        scrollToLatest();
      },
    });
    conversationId = result.conversation_id;
    if (!reply) assistant.content.textContent = "応答がありませんでした。";
    if (typeof result.provider === "string" && typeof result.model === "string") {
      const meta = document.createElement("div");
      meta.className = "message-meta";
      meta.textContent = `${result.provider} · ${result.model}`;
      assistant.body.append(meta);
    }
    setStatus("");
  } catch (error) {
    assistant.item.classList.add("message-error");
    const explanation = error instanceof ChatError ? error.message : "通信に失敗しました。接続を確認してください。";
    assistant.content.textContent = reply ? `${reply}\n\n${explanation}` : explanation;
    setStatus("応答を完了できませんでした。", true);
  } finally {
    setBusy(false);
    input.focus();
    scrollToLatest();
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  void submitMessage();
});

input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    form.requestSubmit();
  }
});

input.addEventListener("input", () => {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 176)}px`;
});

newChatButton.addEventListener("click", () => {
  conversationId = null;
  conversation.replaceChildren(welcome);
  setStatus("");
  input.focus();
});
