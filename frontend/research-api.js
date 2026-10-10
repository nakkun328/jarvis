// Read-only client for the research API. Only GET requests are made here.
import { SESSION_STATUSES, filterSessions, normalizeSession } from "./research-view.js";
import { sessionEnded } from "./session.js";

export const LIST_LIMIT = 100;

// kind: unauthorized | network | offline | unavailable | not_found | bad_request | server | format
export class ResearchApiError extends Error {
  constructor(kind, status = null) {
    super(kind);
    this.kind = kind;
    this.status = status;
  }
}

export function classifyStatus(status) {
  if (status === 401) return "unauthorized";
  if (status === 404) return "not_found";
  if (status === 422) return "bad_request";
  if (status === 503) return "unavailable";
  return "server";
}

async function getJson(url, { fetchImpl = fetch, signal } = {}) {
  let response;
  try {
    response = await fetchImpl(url, { headers: { Accept: "application/json" }, signal });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw new ResearchApiError("network");
  }
  if (response.status === 401) sessionEnded();
  if (!response.ok) throw new ResearchApiError(classifyStatus(response.status), response.status);
  try {
    return await response.json();
  } catch {
    throw new ResearchApiError("format");
  }
}

export function listUrl({ status = null, limit = LIST_LIMIT } = {}) {
  const query = new URLSearchParams();
  if (SESSION_STATUSES.includes(status)) query.set("status", status);
  query.set("limit", String(limit));
  return `/api/research/sessions?${query}`;
}

export function detailUrl(sessionId) {
  return `/api/research/sessions/${encodeURIComponent(sessionId)}`;
}

async function fetchSessions(options) {
  const body = await getJson(listUrl(options), options);
  if (!Array.isArray(body?.sessions)) throw new ResearchApiError("format");
  const sessions = body.sessions.map(normalizeSession);
  if (sessions.includes(null)) throw new ResearchApiError("format");
  return sessions;
}

// Loads the list for the screen. `all` (up to LIST_LIMIT newest sessions, newest first as the API
// returns them) feeds the counts. There is no pagination, so when `all` hit the limit the chosen
// filter is also asked of the server, otherwise older sessions of that status could be missing
// from the page. `truncated` says the counts cover only the newest part of the stored sessions.
export async function loadSessionList({ filter = "all", limit = LIST_LIMIT, fetchImpl, signal } = {}) {
  const all = await fetchSessions({ limit, fetchImpl, signal });
  const truncated = all.length >= limit;
  let shown = filterSessions(all, filter);
  if (truncated && SESSION_STATUSES.includes(filter)) {
    shown = await fetchSessions({ status: filter, limit, fetchImpl, signal });
  }
  return { all, shown, truncated };
}

export async function loadSession(sessionId, { fetchImpl, signal } = {}) {
  const session = normalizeSession(await getJson(detailUrl(sessionId), { fetchImpl, signal }));
  if (session === null || !Array.isArray(session.sources)) throw new ResearchApiError("format");
  return session;
}
