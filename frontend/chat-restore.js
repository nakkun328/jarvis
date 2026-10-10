// Remembers the current conversation id in this browser and restores the conversation after the
// chat page is reloaded or left and re-entered. Only the id is stored (never message text); the
// messages come from the server. Every storage access is guarded: the page works without storage.
import { sessionEnded } from "./session.js";

export const STORAGE_KEY = "jarvis.chat.conversation";
export const RESTORED_MESSAGE = "前回の会話を復元しました。";
export const MISSING_MESSAGE = "前回の会話は見つかりませんでした。新しい会話を始めます。";
export const FAILED_MESSAGE = "前回の会話を読み込めませんでした。";

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export function isConversationId(value) {
  return typeof value === "string" && UUID_PATTERN.test(value);
}

// `storage` may be null (unavailable) or throw on any access; both are treated as "nothing".
export function createConversationMemory(storage) {
  return {
    get() {
      try {
        const value = storage?.getItem(STORAGE_KEY);
        return isConversationId(value) ? value : null;
      } catch {
        return null;
      }
    },
    set(id) {
      try {
        if (isConversationId(id)) storage?.setItem(STORAGE_KEY, id);
      } catch {
        // Storage is optional.
      }
    },
    clear() {
      try {
        storage?.removeItem(STORAGE_KEY);
      } catch {
        // Storage is optional.
      }
    },
  };
}

export function historyUrl(id) {
  return `/api/chat/conversations/${id}/messages`;
}

// Resolves { status: "ok", messages } | { status: "missing" } | { status: "failed" }.
export async function fetchHistory(id, fetchImpl = fetch) {
  try {
    const response = await fetchImpl(historyUrl(id), { headers: { Accept: "application/json" } });
    if (response.status === 401) {
      sessionEnded();
      return { status: "failed" };
    }
    if (response.status === 404) return { status: "missing" };
    if (!response.ok) return { status: "failed" };
    const data = await response.json();
    if (data?.conversation_id !== id || !Array.isArray(data.messages)) return { status: "failed" };
    const messages = data.messages.filter(
      (item) =>
        (item?.role === "user" || item?.role === "assistant") && typeof item.content === "string",
    );
    return { status: "ok", messages: messages.map(({ role, content }) => ({ role, content })) };
  } catch {
    return { status: "failed" };
  }
}

// Restores the remembered conversation, if any. `view` has showHistory(messages) and
// setStatus(text, error); `session` has conversationId and busy. Nothing is applied once the
// user has already started typing into a conversation. Returns the outcome name.
export async function restoreConversation({ memory, session, view, fetchImpl }) {
  const id = memory.get();
  if (!id) return "none";
  const result = await fetchHistory(id, fetchImpl);
  if (session.busy || session.conversationId) return "skipped";
  if (result.status === "missing") {
    memory.clear();
    view.setStatus(MISSING_MESSAGE);
    return "missing";
  }
  if (result.status === "failed") {
    // Kept: a temporary failure should not forget a conversation that still exists.
    view.setStatus(FAILED_MESSAGE, true);
    return "failed";
  }
  if (result.messages.length === 0) {
    memory.clear();
    return "empty";
  }
  session.conversationId = id;
  view.showHistory(result.messages);
  view.setStatus(RESTORED_MESSAGE);
  return "restored";
}
