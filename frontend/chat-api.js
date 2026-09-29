import { readEventStream } from "./sse.js";

export class ChatError extends Error {}

function requestBody(message, conversationId) {
  return JSON.stringify({
    message,
    ...(conversationId ? { conversation_id: conversationId } : {}),
  });
}

function parseEventData(data) {
  try {
    return JSON.parse(data);
  } catch {
    throw new ChatError("サーバーの応答を読み取れませんでした。");
  }
}

function requireText(value) {
  if (typeof value !== "string") throw new ChatError("サーバーの応答形式が正しくありません。");
  return value;
}

async function sendNonStreaming(body, onDelta, fetchImpl) {
  const response = await fetchImpl("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body,
  });
  if (!response.ok) throw new ChatError(`送信できませんでした (HTTP ${response.status})。`);

  let result;
  try {
    result = await response.json();
  } catch {
    throw new ChatError("サーバーの応答を読み取れませんでした。");
  }
  const reply = requireText(result?.reply);
  const conversationId = requireText(result?.conversation_id);
  if (!conversationId) throw new ChatError("会話 ID が返されませんでした。");
  onDelta(reply);
  return { conversation_id: conversationId, provider: result.provider, model: result.model };
}

export async function sendChat({ message, conversationId, onDelta, fetchImpl = fetch }) {
  const body = requestBody(message, conversationId);
  const response = await fetchImpl("/api/chat/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body,
  });

  if ([404, 405, 501].includes(response.status) || (response.ok && !response.body)) {
    return sendNonStreaming(body, onDelta, fetchImpl);
  }
  if (!response.ok) throw new ChatError(`送信できませんでした (HTTP ${response.status})。`);

  let completion;
  await readEventStream(response.body, ({ event, data }) => {
    if (event === "delta") {
      onDelta(requireText(parseEventData(data)?.text));
    } else if (event === "done") {
      completion = parseEventData(data);
    } else if (event === "error") {
      const message = requireText(parseEventData(data)?.message);
      throw new ChatError(message);
    }
  });
  const completedId = requireText(completion?.conversation_id);
  if (!completedId) throw new ChatError("応答が途中で終了しました。もう一度お試しください。");
  return { conversation_id: completedId, provider: completion.provider, model: completion.model };
}
