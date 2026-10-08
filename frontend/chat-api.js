import { readEventStream } from "./sse.js";

// kind: why the request failed. retryable: whether sending the same text again can help.
// A failed or stopped turn is never saved by the server, so retrying is always safe.
export class ChatError extends Error {
  constructor(message, { kind = "unknown", retryable = true } = {}) {
    super(message);
    this.kind = kind;
    this.retryable = retryable;
  }
}

// The backend's English `detail` / SSE `message` strings are a contract (pinned by
// tests/test_chat_ui_contract.py). They are mapped here to actionable Japanese text; unknown
// server text is never shown verbatim.
const SERVER_MESSAGES = {
  "conversation not found": {
    kind: "conversation-missing",
    retryable: false,
    message:
      "この会話は見つかりません（期限切れ、またはサーバー側で削除されました）。内容は引き継がれていません。「新しい会話」から始め直してください。",
  },
  "chat provider is not configured": {
    kind: "not-configured",
    retryable: false,
    message:
      "チャット用の LLM プロバイダーが設定されていません。サーバー側で JARVIS_LLM_PROVIDER などを設定して再起動してください。",
  },
  "chat provider failed": {
    kind: "provider",
    retryable: true,
    message: "LLM プロバイダーからの応答に失敗しました。時間をおいて再試行してください。",
  },
  "conversation capacity reached": {
    kind: "capacity",
    retryable: true,
    message: "同時に処理できる会話の上限に達しています。少し待ってから再試行してください。",
  },
  "conversation storage unavailable": {
    kind: "unavailable",
    retryable: true,
    message: "会話の保存先が利用できません。サーバーの状態を確認して再試行してください。",
  },
  "memory context unavailable": {
    kind: "unavailable",
    retryable: true,
    message: "メモリ参照が利用できません。サーバーの状態を確認して再試行してください。",
  },
  "message must contain text": {
    kind: "invalid",
    retryable: false,
    message: "メッセージが空です。文字を入力してください。",
  },
};

function serverError(text, status) {
  const known = SERVER_MESSAGES[text];
  if (known) return new ChatError(known.message, known);
  if (status === 422) {
    return new ChatError("メッセージを受け付けられませんでした（空、または 4,000 文字を超えています）。", {
      kind: "invalid",
      retryable: false,
    });
  }
  if (status >= 500) {
    return new ChatError(`サーバーでエラーが発生しました (HTTP ${status})。時間をおいて再試行してください。`, {
      kind: "server",
    });
  }
  const suffix = status ? ` (HTTP ${status})` : "";
  return new ChatError(`送信できませんでした${suffix}。`, { kind: "http" });
}

async function httpError(response) {
  let detail;
  try {
    detail = (await response.json())?.detail;
  } catch {
    // Body is not JSON; fall back to the status code.
  }
  return serverError(typeof detail === "string" ? detail : "", response.status);
}

function parseEventData(data) {
  try {
    return JSON.parse(data);
  } catch {
    throw new ChatError("サーバーの応答を読み取れませんでした。", { kind: "protocol" });
  }
}

function requireText(value) {
  if (typeof value !== "string") {
    throw new ChatError("サーバーの応答形式が正しくありません。", { kind: "protocol" });
  }
  return value;
}

function requestBody(message, conversationId) {
  return JSON.stringify({
    message,
    ...(conversationId ? { conversation_id: conversationId } : {}),
  });
}

async function sendNonStreaming(body, onDelta, fetchImpl, signal) {
  const response = await fetchImpl("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body,
    signal,
  });
  if (!response.ok) throw await httpError(response);

  let result;
  try {
    result = await response.json();
  } catch {
    throw new ChatError("サーバーの応答を読み取れませんでした。", { kind: "protocol" });
  }
  const reply = requireText(result?.reply);
  const conversationId = requireText(result?.conversation_id);
  if (!conversationId) throw new ChatError("会話 ID が返されませんでした。", { kind: "protocol" });
  onDelta(reply);
  return { conversation_id: conversationId, provider: result.provider, model: result.model };
}

// Resolves only after the server's `done` event. Anything else (error event, stream ending
// early, network failure, abort via `signal`) rejects, and the caller must treat any text
// already delivered through onDelta as an unsaved partial reply.
//
// `onActivity(data)` is optional. When given, the request asks for the server's additive
// `activity` events and each one's raw JSON text is passed on (parsing and validation belong to
// activity-view.js). Anything wrong with an activity event is ignored: it can never fail a chat.
export async function sendChat({ message, conversationId, onDelta, onActivity, signal, fetchImpl = fetch }) {
  const body = requestBody(message, conversationId);
  const response = await fetchImpl("/api/chat/stream", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
      ...(onActivity ? { "X-Jarvis-Activity": "1" } : {}),
    },
    body,
    signal,
  });

  if ([404, 405, 501].includes(response.status) || (response.ok && !response.body)) {
    return sendNonStreaming(body, onDelta, fetchImpl, signal);
  }
  if (!response.ok) throw await httpError(response);

  let completion;
  await readEventStream(response.body, ({ event, data }) => {
    if (event === "delta") {
      onDelta(requireText(parseEventData(data)?.text));
    } else if (event === "activity") {
      try {
        onActivity?.(data);
      } catch {
        // A display problem must not break the reply.
      }
    } else if (event === "done") {
      completion = parseEventData(data);
    } else if (event === "error") {
      throw serverError(requireText(parseEventData(data)?.message), 0);
    }
  });
  if (!completion) {
    throw new ChatError("応答が途中で終了しました。もう一度お試しください。", { kind: "incomplete" });
  }
  const completedId = requireText(completion.conversation_id);
  if (!completedId) throw new ChatError("会話 ID が返されませんでした。", { kind: "protocol" });
  return { conversation_id: completedId, provider: completion.provider, model: completion.model };
}
