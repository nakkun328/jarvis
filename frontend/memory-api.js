// Read-only client for the memory API. Only GET requests are made here.
import { isMemoryId, normalizeDetail, normalizeMemory } from "./memory-view.js";
import { sessionEnded } from "./session.js";

export const LIST_LIMIT = 100;

// kind: unauthorized | network | offline | unavailable | not_found | bad_request | server | format
export class MemoryApiError extends Error {
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
    throw new MemoryApiError("network");
  }
  if (response.status === 401) sessionEnded();
  if (!response.ok) throw new MemoryApiError(classifyStatus(response.status), response.status);
  try {
    return await response.json();
  } catch {
    throw new MemoryApiError("format");
  }
}

// The query is a URL-encoded value only; a blank one is left out.
function listUrl(path, limit, q) {
  const query = new URLSearchParams({ limit: String(limit) });
  if (typeof q === "string" && q !== "") query.set("q", q);
  return `${path}?${query}`;
}

export const notesUrl = (limit = LIST_LIMIT, q = "") => listUrl("/api/memory/notes", limit, q);
export const candidatesUrl = (limit = LIST_LIMIT, q = "") => listUrl("/api/memory/candidates", limit, q);
export const noteDetailUrl = (id) => `/api/memory/notes/${encodeURIComponent(id)}`;
export const candidateDetailUrl = (id) => `/api/memory/candidates/${encodeURIComponent(id)}`;

async function fetchList(url, key, options) {
  const body = await getJson(url, options);
  if (!Array.isArray(body?.[key])) throw new MemoryApiError("format");
  const items = body[key].map(normalizeMemory);
  if (items.includes(null)) throw new MemoryApiError("format");
  return items;
}

// Loads both lists (newest first, as the API returns them). `truncated` says a list hit the
// limit, so older records exist that this screen does not show.
export async function loadMemory({ limit = LIST_LIMIT, q = "", fetchImpl, signal } = {}) {
  const options = { fetchImpl, signal };
  const [notes, candidates] = await Promise.all([
    fetchList(notesUrl(limit, q), "notes", options),
    fetchList(candidatesUrl(limit, q), "candidates", options),
  ]);
  return {
    notes,
    candidates,
    truncated: { notes: notes.length >= limit, candidates: candidates.length >= limit },
  };
}

// One record by id. The note endpoint also serves replaced and retired notes; a record that is
// not a note is looked up as a candidate. Both missing is "not_found". An id that is not a
// canonical UUID is not requested at all.
export async function loadMemoryDetail(id, { fetchImpl, signal } = {}) {
  if (!isMemoryId(id)) throw new MemoryApiError("not_found", 404);
  const options = { fetchImpl, signal };
  let body;
  try {
    body = await getJson(noteDetailUrl(id), options);
  } catch (error) {
    if (!(error instanceof MemoryApiError) || error.kind !== "not_found") throw error;
    body = await getJson(candidateDetailUrl(id), options);
  }
  const detail = normalizeDetail(body);
  if (detail === null || detail.id !== id) throw new MemoryApiError("format");
  return detail;
}
