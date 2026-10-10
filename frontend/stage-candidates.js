// "記憶の候補にする": stages a completed session's verified claims as PENDING memory candidates.
// Only a person's button press calls stageCandidates. Nothing becomes approved memory here; a
// candidate still has to be reviewed. The POST sends the fixed confirmation header (a cross-site
// page cannot add it) and no body. Messages are fixed Japanese strings shown as text nodes.
import { sessionEnded } from "./session.js";

export const CONFIRM_HEADER = "X-Jarvis-Confirm";

export function candidatesUrl(sessionId) {
  return `/api/research/sessions/${encodeURIComponent(sessionId)}/memory-candidates`;
}

function reduce(body) {
  if (
    body === null ||
    typeof body !== "object" ||
    !Array.isArray(body.candidates) ||
    typeof body.eligible !== "boolean"
  ) {
    return null;
  }
  const omitted = Number.isInteger(body.omitted) && body.omitted > 0 ? body.omitted : 0;
  const created = Number.isInteger(body.created) && body.created > 0 ? body.created : 0;
  const approved = body.candidates.filter((item) => item?.status === "approved").length;
  return {
    eligible: body.eligible,
    count: body.candidates.length,
    omitted,
    created,
    autoApproval: body.auto_approval === true,
    approved,
  };
}

// Resolves to { kind: "ok", ...state } or { kind: <fixed failure word> }. It never throws for
// server answers.
async function request(sessionId, init, { fetchImpl = fetch, signal } = {}) {
  let response;
  try {
    response = await fetchImpl(candidatesUrl(sessionId), { ...init, signal });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    return { kind: "network" };
  }
  if (response.status === 401) {
    sessionEnded();
    return { kind: "unauthorized" };
  }
  if (response.status === 403) return { kind: "forbidden" };
  if (response.status === 404) return { kind: "not_found" };
  if (response.status === 409) return { kind: "refused" };
  if (!response.ok) return { kind: response.status === 503 ? "unavailable" : "error" };
  let body;
  try {
    body = reduce(await response.json());
  } catch {
    body = null;
  }
  return body === null ? { kind: "format" } : { kind: "ok", ...body };
}

export function loadCandidates(sessionId, options) {
  return request(sessionId, { headers: { Accept: "application/json" } }, options);
}

export function stageCandidates(sessionId, options) {
  return request(
    sessionId,
    { method: "POST", headers: { Accept: "application/json", [CONFIRM_HEADER]: "1" } },
    options,
  );
}

export const BUTTON_LABEL = "記憶の候補にする";

const FAILURES = {
  unauthorized: "ログインの有効期限が切れました。",
  forbidden: "この操作は許可されませんでした。",
  not_found: "この調査は見つかりませんでした。",
  refused: "この調査には候補にできる検証済みの主張がありません。",
  unavailable: "記憶の保存先を読み取れません。しばらくしてからもう一度お試しください。",
  network: "サーバーに接続できません。",
  format: "サーバーの応答を読み取れませんでした。",
  error: "サーバーでエラーが発生しました。",
};

// phase: "loading" | "idle" | "busy" | "added" | "error". Returns the texts and button state.
// `autoApproval` is the server's flag (the owner switched automatic approval on); the note says
// which of the two it is. `approved` counts candidates that are already approved memory.
export function memoryPanelModel({
  phase,
  count = 0,
  omitted = 0,
  created = 0,
  approved = 0,
  autoApproval = false,
  errorKind = null,
}) {
  const base = "検証済みの主張を、引用・出典・取得日つきで記憶の候補にします。";
  const note = autoApproval
    ? `${base}自動承認が有効です。条件を満たした主張は、そのまま記憶になります（信頼の低い出典などは確認待ち）。`
    : `${base}自動で記憶には入りません。「記憶」画面で確認・承認したものだけが記憶になります。`;
  const model = { note, status: "", error: "", buttonLabel: BUTTON_LABEL, disabled: false };
  if (phase === "loading") {
    model.disabled = true;
    model.status = "確認中…";
  } else if (phase === "busy") {
    model.disabled = true;
    model.status = "候補を作成しています…";
  } else if (phase === "added") {
    const pending = Math.max(count - approved, 0);
    model.disabled = true;
    model.buttonLabel = "追加済み";
    model.status =
      (created > 0 ? `新規 ${created} 件を作成しました。` : "新しい候補はありません（追加済み）。") +
      `合計 ${count} 件のうち、自動承認 ${approved} 件、確認待ち ${pending} 件。` +
      (pending > 0 ? "確認待ちは「記憶」画面で確認してください。" : "") +
      (omitted > 0 ? ` 上限のため ${omitted} 件は追加していません。` : "");
  } else if (phase === "error") {
    model.error = FAILURES[errorKind] ?? FAILURES.error;
  }
  return model;
}
