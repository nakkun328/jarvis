// Read-only client for the task state API. Only GET requests are made here.
import { TASK_STATUSES, filterTasks, normalizeTask } from "./tasks-view.js";
import { sessionEnded } from "./session.js";

export const LIST_LIMIT = 100;

// kind: unauthorized | network | offline | unavailable | busy | not_found | server | format
export class TasksApiError extends Error {
  constructor(kind, status = null) {
    super(kind);
    this.kind = kind;
    this.status = status;
  }
}

export function classifyStatus(status) {
  if (status === 401) return "unauthorized";
  if (status === 404 || status === 422) return "not_found";
  if (status === 429) return "busy";
  if (status === 503) return "unavailable";
  return "server";
}

async function getJson(url, { fetchImpl = fetch, signal } = {}) {
  let response;
  try {
    response = await fetchImpl(url, { headers: { Accept: "application/json" }, signal });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw new TasksApiError("network");
  }
  if (response.status === 401) sessionEnded();
  if (!response.ok) throw new TasksApiError(classifyStatus(response.status), response.status);
  try {
    return await response.json();
  } catch {
    throw new TasksApiError("format");
  }
}

export function listUrl({ status = null, limit = LIST_LIMIT } = {}) {
  const query = new URLSearchParams();
  if (TASK_STATUSES.includes(status)) query.set("status", status);
  query.set("limit", String(limit));
  return `/api/tasks?${query}`;
}

export function detailUrl(taskId) {
  return `/api/tasks/${encodeURIComponent(taskId)}`;
}

export function eventsUrl(taskId) {
  return `${detailUrl(taskId)}/events`;
}

async function fetchTasks(options) {
  const body = await getJson(listUrl(options), options);
  if (!Array.isArray(body?.tasks)) throw new TasksApiError("format");
  const tasks = body.tasks.map(normalizeTask);
  if (tasks.includes(null)) throw new TasksApiError("format");
  return tasks;
}

// Loads the list for the screen. `all` (up to LIST_LIMIT newest tasks, newest first) feeds the
// counts. The API has no pagination, so when `all` hit the limit the chosen filter is also
// asked of the server, otherwise older tasks of that status could be missing from the page.
// `truncated` says the counts cover only part of the stored tasks.
export async function loadTaskList({ filter = "all", limit = LIST_LIMIT, fetchImpl, signal } = {}) {
  const all = await fetchTasks({ limit, fetchImpl, signal });
  const truncated = all.length >= limit;
  let shown = filterTasks(all, filter);
  if (truncated && TASK_STATUSES.includes(filter)) {
    shown = await fetchTasks({ status: filter, limit, fetchImpl, signal });
  }
  return { all, shown, truncated };
}

export async function loadTask(taskId, { fetchImpl, signal } = {}) {
  const task = normalizeTask(await getJson(detailUrl(taskId), { fetchImpl, signal }));
  if (task === null || !Array.isArray(task.steps)) throw new TasksApiError("format");
  return task;
}
