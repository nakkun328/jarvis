// Pure view-model helpers for the read-only Memory screen. No DOM access, so Node can test them.
//
// Everything shown comes from what the API returned. Nothing is approved, scored or edited here.
// Memory text, sources, tags and projects are untrusted data; this module only passes them
// through as strings and the DOM layer renders them with textContent.

export const TABS = ["notes", "candidates"];
export const OTHER_LABEL = "その他";

// Status: a text label AND a glyph; colour is a redundant third cue. Shared tone classes live in shell.css.
const STATUS_INFO = {
  approved: { label: "承認済み", glyph: "✓", tone: "completed" },
  pending: { label: "確認待ち", glyph: "○", tone: "pending" },
  conflict: { label: "競合", glyph: "!", tone: "waiting" },
};
const OTHER_INFO = { label: OTHER_LABEL, glyph: "?", tone: "other" };

// An AI inference is visibly different from something the user said or a tool observed.
const ORIGIN_INFO = {
  user_explicit: { label: "ユーザー本人の発言", glyph: "●", tone: "completed" },
  ai_inference: { label: "AIの推測", glyph: "◇", tone: "waiting" },
  tool_observation: { label: "ツールの観測", glyph: "▣", tone: "running" },
};

const CATEGORY_LABELS = {
  user: "ユーザー",
  project: "プロジェクト",
  conversation: "会話",
  work_state: "作業状態",
  temporary: "一時",
  self: "自己（JARVIS）",
};

// Object.hasOwn keeps keys such as "constructor" or "__proto__" from reaching a prototype.
function lookup(table, key) {
  return typeof key === "string" && Object.hasOwn(table, key) ? table[key] : null;
}

export function statusInfo(status) {
  const info = lookup(STATUS_INFO, status);
  return { key: info ? status : "other", ...(info ?? OTHER_INFO) };
}

export function originInfo(origin) {
  const info = lookup(ORIGIN_INFO, origin);
  return { key: info ? origin : "other", ...(info ?? OTHER_INFO) };
}

export const categoryLabel = (category) => lookup(CATEGORY_LABELS, category) ?? OTHER_LABEL;

const isRecord = (value) => typeof value === "object" && value !== null && !Array.isArray(value);
const text = (value) => (typeof value === "string" ? value : null);
const score = (value) =>
  typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1 ? value : null;

// Returns null for anything that is not a usable record, so the caller can report a format error.
export function normalizeMemory(raw) {
  if (!isRecord(raw) || typeof raw.id !== "string" || !raw.id) return null;
  return {
    id: raw.id,
    status: text(raw.status) ?? "",
    category: text(raw.category) ?? "",
    content: text(raw.content) ?? "",
    source: text(raw.source) ?? "",
    origin: text(raw.origin) ?? "",
    importance: score(raw.importance),
    confidence: score(raw.confidence),
    tags: Array.isArray(raw.tags) ? raw.tags.filter((tag) => typeof tag === "string") : [],
    project: text(raw.project),
    revision: text(raw.revision),
    supersedes_id: text(raw.supersedes_id),
    created_at: text(raw.created_at),
    updated_at: text(raw.updated_at),
  };
}

const TIMESTAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/;

export function formatTimestamp(value, { locale = "ja-JP", timeZone } = {}) {
  if (typeof value !== "string" || !TIMESTAMP.test(value)) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat(locale, {
    dateStyle: "short",
    timeStyle: "medium",
    ...(timeZone ? { timeZone } : {}),
  }).format(date);
}

// "0.80", or "—" when the stored value was missing or out of range.
export function formatScore(value) {
  return typeof value === "number" ? value.toFixed(2) : "—";
}

// The note revision is a 64-character SHA-256; twelve characters identify it on screen.
export function shortRevision(value) {
  return typeof value === "string" && /^[0-9a-f]{64}$/.test(value) ? value.slice(0, 12) : null;
}

export function itemModel(memory, { timeZone } = {}) {
  const fields = [
    ["確信度", formatScore(memory.confidence)],
    ["重要度", formatScore(memory.importance)],
    ["出典", memory.source || "—"],
  ];
  const revision = shortRevision(memory.revision);
  fields.push(["リビジョン", revision ?? (memory.status === "approved" ? "—" : "未承認")]);
  fields.push(["作成", formatTimestamp(memory.created_at, { timeZone })]);
  fields.push(["更新", formatTimestamp(memory.updated_at, { timeZone })]);
  if (memory.project) fields.push(["プロジェクト", memory.project]);
  if (memory.tags.length) fields.push(["タグ", memory.tags.join(" / ")]);
  if (memory.supersedes_id) fields.push(["訂正の対象", memory.supersedes_id]);
  return {
    id: memory.id,
    status: statusInfo(memory.status),
    origin: originInfo(memory.origin),
    categoryText: categoryLabel(memory.category),
    isInference: memory.origin === "ai_inference",
    content: memory.content,
    fields,
  };
}

export function listViewModel(notes, candidates, { tab = "notes", truncated = {}, timeZone } = {}) {
  const active = tab === "candidates" ? candidates : notes;
  return {
    tab: tab === "candidates" ? "candidates" : "notes",
    counts: { notes: notes.length, candidates: candidates.length },
    items: active.map((memory) => itemModel(memory, { timeZone })),
    empty: active.length === 0,
    truncated: Boolean(tab === "candidates" ? truncated.candidates : truncated.notes),
  };
}

// ----- text -----

export const TAB_LABELS = { notes: "承認済みノート", candidates: "確認待ちの候補" };

// ----- error messages -----

const API_ERROR_MESSAGES = {
  network: "サーバーに接続できません。JARVIS が起動しているか確認してください。",
  offline: "オフラインです。ネットワーク接続を確認してください。",
  unavailable: "記憶データの保存先を読み取れません。しばらくしてからもう一度お試しください。",
  bad_request: "リクエストが正しくありませんでした。",
  server: "サーバーでエラーが発生しました。",
  format: "サーバーの応答を読み取れませんでした。",
};

export function apiErrorMessage(kind) {
  return lookup(API_ERROR_MESSAGES, kind) ?? API_ERROR_MESSAGES.server;
}
