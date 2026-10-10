// Conversation history: list past conversations and continue one. UI-independent logic; the
// page supplies a `view` (see history-panel.js and app.js) and tests supply a fake one.
// All text from the server (titles, messages) is only ever handed to the view as strings.
import { MISSING_MESSAGE, fetchHistory, isConversationId } from "./chat-restore.js";
import { sessionEnded } from "./session.js";

export const LIST_URL = "/api/chat/conversations";
export const RESUMED_MESSAGE = "会話を再開しました。";
export const LIST_FAILED_MESSAGE = "履歴を読み込めませんでした。";
export const OPEN_FAILED_MESSAGE = "会話を読み込めませんでした。";
export const OLDER_FAILED_MESSAGE = "以前のメッセージを読み込めませんでした。";
export const LINK_INVALID_MESSAGE = "指定された会話は見つかりませんでした。";
export const BUSY_MESSAGE = "応答中は会話を切り替えられません。完了するか「停止」してください。";
export const EMPTY_LIST_MESSAGE = "保存された会話はまだありません。";
const CURSOR = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z_[0-9a-f-]{36}$/;

// `?c=<uuid>` from location.search: { kind: "none" | "ok" | "invalid", id? }. Only the canonical
// lowercase UUID is accepted; a repeated or malformed parameter is invalid.
export function conversationParam(search) {
  let values;
  try {
    values = new URLSearchParams(search ?? "").getAll("c");
  } catch {
    return { kind: "invalid" };
  }
  if (values.length === 0) return { kind: "none" };
  if (values.length === 1 && isConversationId(values[0])) return { kind: "ok", id: values[0] };
  return { kind: "invalid" };
}

// "たった今" / "5分前" / "3時間前" / "2日前" / a plain date. Empty for an unreadable timestamp.
export function formatRelative(iso, now = Date.now()) {
  const time = typeof iso === "string" ? Date.parse(iso) : Number.NaN;
  if (Number.isNaN(time)) return "";
  const seconds = Math.max(0, Math.floor((now - time) / 1000));
  if (seconds < 60) return "たった今";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}分前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}時間前`;
  if (seconds < 7 * 86400) return `${Math.floor(seconds / 86400)}日前`;
  const date = new Date(time);
  return `${date.getFullYear()}/${date.getMonth() + 1}/${date.getDate()}`;
}

// Resolves { status: "ok", items, nextCursor } | { status: "failed" }.
export async function fetchConversations(fetchImpl = fetch, { before } = {}) {
  try {
    const url = before ? `${LIST_URL}?before=${encodeURIComponent(before)}` : LIST_URL;
    const response = await fetchImpl(url, { headers: { Accept: "application/json" } });
    if (response.status === 401) {
      sessionEnded();
      return { status: "failed" };
    }
    if (!response.ok) return { status: "failed" };
    const data = await response.json();
    if (!Array.isArray(data?.conversations)) return { status: "failed" };
    const items = data.conversations
      .filter(
        (item) =>
          isConversationId(item?.id) &&
          typeof item.title === "string" &&
          typeof item.updated_at === "string" &&
          Number.isInteger(item.message_count) &&
          item.message_count >= 0,
      )
      .map(({ id, title, updated_at: updatedAt, message_count: count }) => ({
        id,
        title,
        updatedAt,
        count,
      }));
    const nextCursor =
      typeof data.next_cursor === "string" && CURSOR.test(data.next_cursor)
        ? data.next_cursor
        : null;
    return { status: "ok", items, nextCursor };
  } catch {
    return { status: "failed" };
  }
}

// view: setStatus(text, error), showListLoading(), showList({ items, append, hasMore }),
// showListError(text), showConversation(messages, { hasMore }), prependOlder(messages,
// { hasMore }), closePanel().
// session: reset(), busy, conversationId.   memory: set(id).
export function createHistoryController({ session, memory, view, fetchImpl }) {
  let listCursor = null;
  let listToken = 0;
  let openToken = 0;
  let currentId = null;
  let olderBefore = null;
  let loadingOlder = false;

  async function loadList(before) {
    const token = ++listToken;
    if (!before) view.showListLoading();
    const result = await fetchConversations(fetchImpl, { before });
    if (token !== listToken) return "stale";
    if (result.status !== "ok") {
      view.showListError(LIST_FAILED_MESSAGE);
      return "failed";
    }
    listCursor = result.nextCursor;
    view.showList({ items: result.items, append: Boolean(before), hasMore: listCursor !== null });
    return "ok";
  }

  async function select(id, { fromPanel = true } = {}) {
    if (!isConversationId(id)) {
      view.setStatus(LINK_INVALID_MESSAGE, true);
      return "invalid";
    }
    if (session.busy) {
      view.setStatus(BUSY_MESSAGE, true);
      return "busy";
    }
    const token = ++openToken;
    const result = await fetchHistory(id, fetchImpl);
    if (token !== openToken) return "stale";
    if (session.busy) {
      view.setStatus(BUSY_MESSAGE, true);
      return "busy";
    }
    if (result.status === "missing" || (result.status === "ok" && result.messages.length === 0)) {
      view.setStatus(fromPanel ? MISSING_MESSAGE : LINK_INVALID_MESSAGE, true);
      return "missing";
    }
    if (result.status !== "ok") {
      view.setStatus(OPEN_FAILED_MESSAGE, true);
      return "failed";
    }
    if (!session.reset()) {
      view.setStatus(BUSY_MESSAGE, true);
      return "busy";
    }
    session.conversationId = id;
    memory.set(id);
    currentId = id;
    olderBefore = result.nextBefore;
    view.showConversation(result.messages, {
      hasMore: result.hasMore,
      nextBefore: result.nextBefore,
    });
    view.closePanel();
    view.setStatus(RESUMED_MESSAGE);
    return "resumed";
  }

  return {
    openList: () => loadList(null),
    moreList: () => (listCursor ? loadList(listCursor) : Promise.resolve("none")),
    select,
    // Called by 『新しい会話』 and by the restore path so older-page state follows the screen.
    adopt(id, nextBefore) {
      currentId = id;
      olderBefore = nextBefore ?? null;
    },
    forget() {
      currentId = null;
      olderBefore = null;
      openToken += 1;
    },
    async loadOlder() {
      if (!currentId || !olderBefore || loadingOlder) return "none";
      loadingOlder = true;
      const id = currentId;
      try {
        const result = await fetchHistory(id, fetchImpl, { before: olderBefore });
        if (id !== currentId) return "stale";
        if (result.status !== "ok") {
          view.setStatus(OLDER_FAILED_MESSAGE, true);
          return "failed";
        }
        olderBefore = result.nextBefore;
        view.prependOlder(result.messages, { hasMore: result.hasMore });
        return "ok";
      } finally {
        loadingOlder = false;
      }
    },
    // Handles `/?c=<uuid>`. Returns "none" when the parameter is absent, else the outcome.
    async openFromParam(search) {
      const param = conversationParam(search);
      if (param.kind === "none") return "none";
      if (param.kind === "invalid") {
        view.setStatus(LINK_INVALID_MESSAGE, true);
        return "invalid";
      }
      return select(param.id, { fromPanel: false });
    },
  };
}
