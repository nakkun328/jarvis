// Client for starting, cancelling and polling a research. The only requests this app makes
// that change state on the Research screen are the two POSTs below; both are same-origin, send a
// small JSON body (the start request) and are answered with fixed error codes only.
import { classifyStatus, detailUrl } from "./research-api.js";
import { normalizeSession } from "./research-view.js";
import { sessionEnded } from "./session.js";

export const STATUS_URL = "/api/research/status";
export const CREATE_URL = "/api/research/sessions";

export class RunApiError extends Error {
  // code: a fixed server code (busy, search_budget_exhausted, ...) or network | format | server
  constructor(code, status = null) {
    super(code);
    this.code = code;
    this.status = status;
  }
}

// Only a code the server is known to send is passed on; anything else reads as a generic error.
const KNOWN_CODES = new Set([
  "busy", "search_budget_exhausted", "research_not_configured", "forbidden", "unauthorized",
  "question_required", "question_too_long", "question_invalid_characters", "invalid_level",
  "invalid_body", "unsupported_media_type", "session_not_found", "not_cancellable",
  "storage_unavailable",
]);

async function errorFrom(response) {
  if (response.status === 401) {
    sessionEnded();
    return new RunApiError("unauthorized", 401);
  }
  let code = null;
  try {
    const body = await response.json();
    code = typeof body?.detail === "string" ? body.detail : null;
  } catch {
    code = null;
  }
  return new RunApiError(KNOWN_CODES.has(code) ? code : "server", response.status);
}

async function send(url, init, { fetchImpl = fetch, signal } = {}) {
  let response;
  try {
    response = await fetchImpl(url, { ...init, signal });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw new RunApiError("network");
  }
  if (!response.ok) throw await errorFrom(response);
  try {
    return await response.json();
  } catch {
    throw new RunApiError("format");
  }
}

export async function loadAvailability(options) {
  return send(STATUS_URL, { headers: { Accept: "application/json" } }, options);
}

export async function startResearch({ question, level }, options) {
  const body = await send(
    CREATE_URL,
    {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify({ question, level }),
    },
    options,
  );
  if (typeof body?.id !== "string" || !body.id) throw new RunApiError("format");
  return body.id;
}

export async function cancelResearch(sessionId, options) {
  const body = await send(
    `${CREATE_URL}/${encodeURIComponent(sessionId)}/cancel`,
    { method: "POST", headers: { Accept: "application/json" } },
    options,
  );
  return body?.status === "cancelled" ? "cancelled" : "cancelling";
}

// The same detail the browse screen reads, as a normalised session (with `progress` while live).
export async function pollSession(sessionId, { fetchImpl = fetch, signal } = {}) {
  let response;
  try {
    response = await fetchImpl(detailUrl(sessionId), {
      headers: { Accept: "application/json" },
      signal,
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw new RunApiError("network");
  }
  if (response.status === 401) sessionEnded();
  if (!response.ok) {
    const kind = classifyStatus(response.status);
    throw new RunApiError(kind === "not_found" ? "session_not_found" : kind === "unavailable" ? "storage_unavailable" : "server", response.status);
  }
  let body;
  try {
    body = await response.json();
  } catch {
    throw new RunApiError("format");
  }
  const session = normalizeSession(body);
  if (session === null || !Array.isArray(session.sources)) throw new RunApiError("format");
  return session;
}

