// Client for the approvals API. The two POSTs send the fixed confirmation header (a cross-site page
// cannot add it) and no body; errors are reduced to fixed outcome words.
import { normalizeApproval } from "./approvals-view.js";
import { sessionEnded } from "./session.js";

export const LIST_URL = "/api/approvals";
export const CONFIRM_HEADER = "X-Jarvis-Confirm";

export class ApprovalsApiError extends Error {
  // kind: unauthorized | network | unavailable | format | server
  constructor(kind, status = null) {
    super(kind);
    this.kind = kind;
    this.status = status;
  }
}

export function decisionUrl(id, kind) {
  return `${LIST_URL}/${encodeURIComponent(id)}/${kind === "approve" ? "approve" : "deny"}`;
}

export async function loadApprovals({ fetchImpl = fetch, signal } = {}) {
  let response;
  try {
    response = await fetchImpl(LIST_URL, { headers: { Accept: "application/json" }, signal });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw new ApprovalsApiError("network");
  }
  if (response.status === 401) {
    sessionEnded();
    throw new ApprovalsApiError("unauthorized", 401);
  }
  if (!response.ok) {
    throw new ApprovalsApiError(response.status === 503 ? "unavailable" : "server", response.status);
  }
  let body;
  try {
    body = await response.json();
  } catch {
    throw new ApprovalsApiError("format");
  }
  if (!Array.isArray(body?.approvals)) throw new ApprovalsApiError("format");
  const approvals = body.approvals.map(normalizeApproval);
  if (approvals.includes(null)) throw new ApprovalsApiError("format");
  return approvals;
}

// kind: "approve" | "deny". Resolves to ok | expired | not_pending | not_found | unauthorized |
// forbidden | network | error. It never throws for server answers.
export async function decide(id, kind, { fetchImpl = fetch, signal } = {}) {
  let response;
  try {
    response = await fetchImpl(decisionUrl(id, kind), {
      method: "POST",
      headers: { Accept: "application/json", [CONFIRM_HEADER]: "1" },
      signal,
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    return "network";
  }
  if (response.ok) return "ok";
  if (response.status === 401) {
    sessionEnded();
    return "unauthorized";
  }
  if (response.status === 410) return "expired";
  if (response.status === 409) return "not_pending";
  if (response.status === 404) return "not_found";
  if (response.status === 403) return "forbidden";
  return "error";
}
