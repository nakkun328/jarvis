// Pure helpers for the Approvals screen: validate what the API sent and decide what to say.
// Nothing here touches the DOM. Every string from the server is untrusted and only ever becomes
// a text node in approvals.js.

export const MAX_FIELDS = 8;

const ID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function text(value, limit) {
  return typeof value === "string" ? value.slice(0, limit) : null;
}

// Returns a clean approval or null when the shape is wrong (the screen then shows a format error).
export function normalizeApproval(raw) {
  if (raw === null || typeof raw !== "object") return null;
  const id = typeof raw.id === "string" && ID_PATTERN.test(raw.id) ? raw.id : null;
  const toolName = text(raw.tool_name, 64);
  const requestedAt = Date.parse(raw.requested_at);
  const expiresAt = Date.parse(raw.expires_at);
  const summary = raw.summary;
  if (!id || !toolName || Number.isNaN(requestedAt) || Number.isNaN(expiresAt)) return null;
  if (summary === null || typeof summary !== "object" || !Array.isArray(summary.fields)) return null;
  const fields = [];
  for (const field of summary.fields.slice(0, MAX_FIELDS)) {
    const name = text(field?.name, 40);
    const preview = text(field?.preview, 80);
    if (name === null || preview === null) return null;
    fields.push({ name, preview });
  }
  return {
    id,
    toolName,
    fields,
    moreFields: Number.isInteger(summary.more_fields) && summary.more_fields > 0 ? summary.more_fields : 0,
    digestPrefix: text(summary.digest_prefix, 12) ?? "",
    requestedAt,
    expiresAt,
  };
}

export function secondsLeft(approval, now) {
  return Math.max(0, Math.ceil((approval.expiresAt - now) / 1000));
}

export function expiryText(approval, now) {
  const left = secondsLeft(approval, now);
  if (left <= 0) return "期限切れ(拒否扱い)";
  const minutes = Math.floor(left / 60);
  const seconds = left % 60;
  return minutes > 0 ? `あと ${minutes} 分 ${seconds} 秒で期限切れ` : `あと ${seconds} 秒で期限切れ`;
}

export const isUrgent = (approval, now) => secondsLeft(approval, now) <= 30;

export function listStatusText({ loaded, error, count }) {
  if (error === "unauthorized") return "ログインの有効期限が切れました。";
  if (error === "network") return "サーバーに接続できません。再試行します。";
  if (error === "unavailable") return "承認の保存先が利用できません。再試行します。";
  if (error === "format") return "サーバーの応答を読み取れませんでした。";
  if (error) return "承認待ちを読み込めませんでした。";
  if (!loaded) return "読み込み中…";
  return count === 0 ? "承認待ちはありません。" : `承認待ち ${count} 件`;
}

export function decisionMessage(kind, toolName, outcome) {
  const verb = kind === "approve" ? "承認" : "拒否";
  switch (outcome) {
    case "ok":
      return `${toolName} を${verb}しました。`;
    case "expired":
      return `${toolName} は期限切れです。操作は実行されません。`;
    case "not_pending":
    case "not_found":
      return `${toolName} は既に処理済みか存在しません。`;
    case "unauthorized":
      return "ログインの有効期限が切れました。";
    default:
      return `${toolName} を${verb}できませんでした。もう一度お試しください。`;
  }
}
