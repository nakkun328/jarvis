// Read-only client for the memory API. Only GET requests are made here.
import { normalizeMemory } from "./memory-view.js";

export const LIST_LIMIT = 100;

// kind: network | offline | unavailable | bad_request | server | format
export class MemoryApiError extends Error {
  constructor(kind, status = null) {
    super(kind);
    this.kind = kind;
    this.status = status;
  }
}

export function classifyStatus(status) {
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
  if (!response.ok) throw new MemoryApiError(classifyStatus(response.status), response.status);
  try {
    return await response.json();
  } catch {
    throw new MemoryApiError("format");
  }
}

export const notesUrl = (limit = LIST_LIMIT) => `/api/memory/notes?limit=${limit}`;
export const candidatesUrl = (limit = LIST_LIMIT) => `/api/memory/candidates?limit=${limit}`;

async function fetchList(url, key, options) {
  const body = await getJson(url, options);
  if (!Array.isArray(body?.[key])) throw new MemoryApiError("format");
  const items = body[key].map(normalizeMemory);
  if (items.includes(null)) throw new MemoryApiError("format");
  return items;
}

// Loads both lists (newest first, as the API returns them). `truncated` says a list hit the
// limit, so older records exist that this screen does not show.
export async function loadMemory({ limit = LIST_LIMIT, fetchImpl, signal } = {}) {
  const options = { fetchImpl, signal };
  const [notes, candidates] = await Promise.all([
    fetchList(notesUrl(limit), "notes", options),
    fetchList(candidatesUrl(limit), "candidates", options),
  ]);
  return {
    notes,
    candidates,
    truncated: { notes: notes.length >= limit, candidates: candidates.length >= limit },
  };
}
