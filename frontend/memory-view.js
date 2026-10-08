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
  // Reachable only from a detail page: the note was replaced by a reviewed correction, or retired.
  superseded: { label: "置換済み", glyph: "→", tone: "skipped" },
  retired: { label: "撤回済み", glyph: "×", tone: "cancelled" },
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

// ----- search -----

export const MAX_QUERY_CHARS = 100;
export const MAX_QUERY_TERMS = 8;
// The same rule as the API: control, surrogate, line and paragraph separator characters.
const FORBIDDEN_IN_QUERY = /[\p{Cc}\p{Cs}\p{Zl}\p{Zp}]/u;

export const QUERY_MESSAGES = {
  too_long: `検索語は ${MAX_QUERY_CHARS} 文字までです。`,
  control: "検索語に使えない文字（制御文字や改行）が含まれています。",
  too_many_terms: `検索語は ${MAX_QUERY_TERMS} 語までです（スペースで区切ります）。`,
};

// Mirrors the API's rules so a bad query is explained here instead of being sent. A blank query
// means "no search". The text is only ever used as a query value and shown with textContent.
export function cleanQuery(raw) {
  const value = typeof raw === "string" ? raw.trim() : "";
  if (value === "") return { ok: true, value: "", terms: [] };
  if ([...value].length > MAX_QUERY_CHARS) return { ok: false, reason: "too_long" };
  if (FORBIDDEN_IN_QUERY.test(value)) return { ok: false, reason: "control" };
  const terms = value.split(/\s+/u);
  if (terms.length > MAX_QUERY_TERMS) return { ok: false, reason: "too_many_terms" };
  return { ok: true, value, terms };
}

export function queryMessage(reason) {
  return lookup(QUERY_MESSAGES, reason) ?? QUERY_MESSAGES.control;
}

// Calls `action` once, `delayMs` after the last `schedule`. `cancel` drops a pending call and
// `flush` runs it now. Timers are injectable for tests.
// The defaults are wrappers: calling setTimeout as a method of another object throws in browsers.
const DEFAULT_TIMERS = {
  set: (fn, ms) => setTimeout(fn, ms),
  clear: (handle) => clearTimeout(handle),
};

export function createDebouncer(action, delayMs, timers = DEFAULT_TIMERS) {
  let handle = null;
  const cancel = () => {
    if (handle !== null) timers.clear(handle);
    handle = null;
  };
  return {
    schedule(...args) {
      cancel();
      handle = timers.set(() => {
        handle = null;
        action(...args);
      }, delayMs);
    },
    flush(...args) {
      cancel();
      action(...args);
    },
    cancel,
    get pending() {
      return handle !== null;
    },
  };
}

export function resultSummary(query, counts) {
  if (!query) return "";
  return `検索結果: 承認済みノート ${counts.notes} 件・確認待ちの候補 ${counts.candidates} 件`;
}

export function emptyMessage(tab, query) {
  if (query) return "この検索語に一致する記録はありません。";
  return tab === "notes" ? "承認済みのノートはまだありません。" : "確認待ちの候補はありません。";
}

// ----- detail -----

// Records are addressed by their canonical lowercase UUID only.
export const ID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
export const isMemoryId = (value) => typeof value === "string" && ID_PATTERN.test(value);

const REVIEW_ACTIONS = { approve: "承認", reject: "却下", flag_conflict: "競合として記録" };
const LIFECYCLE_ACTIONS = { supersede: "訂正で置換", retire: "撤回" };
const FULL_REVISION = /^[0-9a-f]{64}$/;

const idOrNull = (value) => (isMemoryId(value) ? value : null);

function normalizeReview(raw) {
  if (!isRecord(raw)) return null;
  return {
    action: text(raw.action) ?? "",
    previous_status: text(raw.previous_status) ?? "",
    new_status: text(raw.new_status) ?? "",
    occurred_at: text(raw.occurred_at),
    revision: text(raw.revision),
  };
}

function normalizeLifecycle(raw) {
  if (!isRecord(raw)) return null;
  return {
    action: text(raw.action) ?? "",
    related_id: idOrNull(raw.related_id),
    occurred_at: text(raw.occurred_at),
    revision: text(raw.revision),
  };
}

// The list shape plus replaced_by_id and the two histories; null for anything unusable.
export function normalizeDetail(raw) {
  const memory = normalizeMemory(raw);
  if (memory === null || !Array.isArray(raw.reviews) || !Array.isArray(raw.lifecycle)) return null;
  const reviews = raw.reviews.map(normalizeReview);
  const lifecycle = raw.lifecycle.map(normalizeLifecycle);
  if (reviews.includes(null) || lifecycle.includes(null)) return null;
  return {
    ...memory,
    supersedes_id: idOrNull(memory.supersedes_id),
    replaced_by_id: idOrNull(raw.replaced_by_id),
    reviews,
    lifecycle,
  };
}

const fullRevision = (value) => (typeof value === "string" && FULL_REVISION.test(value) ? value : "—");

export function detailModel(detail, { timeZone } = {}) {
  const base = itemModel(detail, { timeZone });
  const fields = base.fields.filter(([term]) => term !== "訂正の対象");
  fields.push(["ID", detail.id]);
  fields.push(["リビジョン（SHA-256）", fullRevision(detail.revision)]);
  const links = [];
  if (detail.supersedes_id) links.push({ label: "この記録が訂正する元のノート", id: detail.supersedes_id });
  if (detail.replaced_by_id) links.push({ label: "この記録を置き換えたノート", id: detail.replaced_by_id });
  const history = [
    ...detail.reviews.map((event) => ({
      at: event.occurred_at,
      text: `${lookup(REVIEW_ACTIONS, event.action) ?? OTHER_LABEL}（${statusInfo(event.previous_status).label} → ${statusInfo(event.new_status).label}）`,
      revision: shortRevision(event.revision),
      relatedId: null,
    })),
    ...detail.lifecycle.map((event) => ({
      at: event.occurred_at,
      text: lookup(LIFECYCLE_ACTIONS, event.action) ?? OTHER_LABEL,
      revision: shortRevision(event.revision),
      relatedId: event.related_id,
    })),
  ]
    .sort((a, b) => String(a.at).localeCompare(String(b.at)))
    .map((entry) => ({ ...entry, time: formatTimestamp(entry.at, { timeZone }) }));
  return { ...base, fields, links, history };
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
  unauthorized: "ログインの有効期限が切れました。ログイン画面に移動します。",
  network: "サーバーに接続できません。JARVIS が起動しているか確認してください。",
  offline: "オフラインです。ネットワーク接続を確認してください。",
  unavailable: "記憶データの保存先を読み取れません。しばらくしてからもう一度お試しください。",
  bad_request: "リクエストが正しくありませんでした。",
  not_found: "この記録は見つかりませんでした。",
  server: "サーバーでエラーが発生しました。",
  format: "サーバーの応答を読み取れませんでした。",
};

export function apiErrorMessage(kind) {
  return lookup(API_ERROR_MESSAGES, kind) ?? API_ERROR_MESSAGES.server;
}
