// Requests of the group discussion screen. The unsafe ones (start, stop) carry the fixed confirm
// header the server requires; none of them sends anything but the discussion's own fields.
import { sessionEnded } from "./session.js";
import { isUuid, normalizeConfig } from "./discussion-view.js";

export const CONFIG_URL = "/api/discussions/config";
export const START_URL = "/api/discussions";
export const messagesUrl = (id, after) =>
  `/api/chat/conversations/${encodeURIComponent(id)}/messages?after=${after}`;
export const cancelUrl = (taskId) => `/api/tasks/${encodeURIComponent(taskId)}/cancel`;
export const taskUrl = (taskId) => `/api/tasks/${encodeURIComponent(taskId)}`;

// kind: network | http | format. `code` is the `detail` string of an error answer, when it has one.
export class DiscussionApiError extends Error {
  constructor(kind, status = null, code = null) {
    super(kind);
    this.kind = kind;
    this.status = status;
    this.code = code;
  }
}

async function request(url, { fetchImpl = (u, i) => fetch(u, i), method = "GET", body, signal } = {}) {
  const headers = { Accept: "application/json" };
  const init = { method, headers, signal };
  if (method === "POST") {
    headers["X-Jarvis-Confirm"] = "1";
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
  }
  let response;
  try {
    response = await fetchImpl(url, init);
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw new DiscussionApiError("network");
  }
  if (response.status === 401) sessionEnded();
  if (!response.ok) {
    let code = null;
    try {
      const data = await response.json();
      if (typeof data?.detail === "string") code = data.detail;
    } catch {
      // An answer without a JSON body has no code.
    }
    throw new DiscussionApiError("http", response.status, code);
  }
  try {
    return await response.json();
  } catch {
    throw new DiscussionApiError("format", response.status);
  }
}

// null: the feature is off (the route does not exist).
export async function loadConfig(options = {}) {
  try {
    const config = normalizeConfig(await request(CONFIG_URL, options));
    if (config === null) throw new DiscussionApiError("format");
    return config;
  } catch (error) {
    if (error instanceof DiscussionApiError && error.kind === "http" && error.status === 404) return null;
    throw error;
  }
}

export async function startDiscussion(body, options = {}) {
  const data = await request(START_URL, { ...options, method: "POST", body });
  if (!isUuid(data?.conversation_id) || typeof data?.task_id !== "string") {
    throw new DiscussionApiError("format");
  }
  return { conversation_id: data.conversation_id, task_id: data.task_id };
}

// Resolves { ok: true } or { ok: false, status } (status "network" when nothing answered).
export async function cancelDiscussion(taskId, options = {}) {
  try {
    await request(cancelUrl(taskId), { ...options, method: "POST" }).catch((error) => {
      if (error instanceof DiscussionApiError && error.kind === "format") return null;
      throw error;
    });
    return { ok: true };
  } catch (error) {
    if (error instanceof DiscussionApiError) return { ok: false, status: error.kind === "network" ? "network" : error.status };
    return { ok: false, status: "network" };
  }
}

// All messages after the first `after` ones, oldest first, following has_more.
export async function loadMessages(id, after, options = {}) {
  const messages = [];
  let total = after;
  for (let guard = 0; guard < 20; guard += 1) {
    const data = await request(messagesUrl(id, after + messages.length), options);
    const page = Array.isArray(data?.messages)
      ? data.messages.filter((m) => typeof m?.content === "string" && (m.role === "user" || m.role === "assistant"))
      : [];
    if (!Number.isInteger(data?.total)) throw new DiscussionApiError("format");
    messages.push(...page.map(({ role, content }) => ({ role, content })));
    total = data.total;
    if (data.has_more !== true || page.length === 0) break;
  }
  return { messages, total };
}

export async function loadTaskStatus(taskId, options = {}) {
  const data = await request(taskUrl(taskId), options);
  if (typeof data?.status !== "string") throw new DiscussionApiError("format");
  return {
    status: data.status,
    steps_total: Number.isInteger(data.steps_total) ? data.steps_total : null,
    steps_completed: Number.isInteger(data.steps_completed) ? data.steps_completed : null,
  };
}
