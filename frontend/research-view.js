// Pure view-model helpers for the read-only Research screen. No DOM access, so Node can test them.
//
// Everything shown comes from what the API returned. Nothing is searched, fetched, scored or
// estimated here. Questions, queries, titles, quotes and results are untrusted data; this module
// only passes them through as strings and the DOM layer renders them with textContent.

export const SESSION_STATUSES = [
  "pending", "running", "waiting", "failed", "completed", "cancelled",
];
export const TERMINAL = new Set(["failed", "completed", "cancelled"]);

export const OTHER_LABEL = "その他";

// Each status has a text label AND a glyph (shape); colour is a third, redundant cue.
const STATUS_INFO = {
  pending: { label: "実行待ち", glyph: "○", tone: "pending" },
  running: { label: "実行中", glyph: "▶", tone: "running" },
  waiting: { label: "応答待ち", glyph: "‖", tone: "waiting" },
  failed: { label: "失敗", glyph: "✕", tone: "failed" },
  completed: { label: "完了", glyph: "✓", tone: "completed" },
  cancelled: { label: "取消済み", glyph: "⊘", tone: "cancelled" },
};
const OTHER_INFO = { label: OTHER_LABEL, glyph: "?", tone: "other" };

const LEVEL_LABELS = {
  memory: "記憶のみ",
  quick: "クイック",
  standard: "標準",
  deep: "詳細",
  extensive: "徹底",
};

const SOURCE_TYPE_LABELS = {
  official: "公式",
  docs: "ドキュメント",
  academic: "学術",
  news: "ニュース",
  community: "コミュニティ",
  blog: "ブログ",
  forum: "フォーラム",
  unknown: "種別不明",
};

// Fixed enum from backend/research/models.py. Unknown codes fall back to OTHER_LABEL.
const FAILURE_LABELS = {
  search_failed: "検索に失敗しました",
  no_results: "検索結果がありませんでした",
  reader_failed: "ページの読み取りに失敗しました",
  synthesis_failed: "回答のまとめに失敗しました",
  timeout: "制限時間を超えました",
  budget_exceeded: "上限を超えました",
  internal_error: "内部エラー",
};

// The five ratings are heuristic hints in the 0..1 range, not facts.
export const RATING_KEYS = ["authority", "freshness", "primary", "relevance", "agreement"];
const RATING_LABELS = {
  authority: "権威性",
  freshness: "新しさ",
  primary: "一次性",
  relevance: "関連性",
  agreement: "一致度",
};

// Object.hasOwn keeps keys such as "constructor" or "__proto__" from reaching a prototype.
function lookup(table, key) {
  return typeof key === "string" && Object.hasOwn(table, key) ? table[key] : null;
}

export function statusInfo(status) {
  const info = lookup(STATUS_INFO, status);
  return { key: info ? status : "other", ...(info ?? OTHER_INFO) };
}

export const levelLabel = (level) => lookup(LEVEL_LABELS, level) ?? OTHER_LABEL;
export const sourceTypeLabel = (type) => lookup(SOURCE_TYPE_LABELS, type) ?? OTHER_LABEL;
export const failureLabel = (code) => lookup(FAILURE_LABELS, code) ?? OTHER_LABEL;
export const isTerminal = (session) => Boolean(session) && TERMINAL.has(session.status);

const isRecord = (value) => typeof value === "object" && value !== null && !Array.isArray(value);
const text = (value) => (typeof value === "string" ? value : null);
const integer = (value) => (Number.isInteger(value) && value >= 0 ? value : null);
const rating = (value) =>
  typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1 ? value : null;

// ----- links -----

// A stored URL becomes a link only when it parses as an absolute http or https URL. Anything
// else (javascript:, data:, relative, malformed) is shown as plain text and gets no href.
export function safeHref(value) {
  if (typeof value !== "string" || value.length === 0 || value.length > 2048) return null;
  if (/[\u0000- \u007f]/.test(value)) return null;
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    return null;
  }
  if (parsed.protocol !== "https:" && parsed.protocol !== "http:") return null;
  if (!parsed.hostname || parsed.username || parsed.password) return null;
  return parsed.href;
}

// ----- normalisation -----

function normalizeSource(raw) {
  if (!isRecord(raw) || typeof raw.id !== "string" || !raw.id) return null;
  const evaluation = isRecord(raw.evaluation) ? raw.evaluation : {};
  return {
    id: raw.id,
    url: text(raw.url) ?? "",
    final_url: text(raw.final_url) ?? "",
    title: text(raw.title),
    publisher: text(raw.publisher),
    published_at: text(raw.published_at),
    retrieved_at: text(raw.retrieved_at),
    source_type: text(raw.source_type) ?? "",
    evaluation: Object.fromEntries(RATING_KEYS.map((key) => [key, rating(evaluation[key])])),
  };
}

function normalizeClaim(raw) {
  if (!isRecord(raw) || typeof raw.id !== "string" || !raw.id) return null;
  return {
    id: raw.id,
    claim_text: text(raw.claim_text) ?? "",
    source_id: text(raw.source_id) ?? "",
    quote: text(raw.quote) ?? "",
    quote_start: integer(raw.quote_start),
    quote_end: integer(raw.quote_end),
  };
}

function normalizeQuery(raw, position) {
  const query = isRecord(raw) ? raw : {};
  return {
    position: Number.isInteger(query.position) ? query.position : position,
    text: text(query.text) ?? "",
  };
}

// Accepts a decoded API object and returns a session with only the fields this screen reads,
// or null when it is not a session. Unknown extra fields are dropped, never interpreted.
export function normalizeSession(raw) {
  if (!isRecord(raw) || typeof raw.id !== "string" || !raw.id || typeof raw.status !== "string") {
    return null;
  }
  const session = {
    id: raw.id,
    question: text(raw.question) ?? "",
    level: text(raw.level) ?? "",
    status: raw.status,
    failure_reason: text(raw.failure_reason),
    has_result: raw.has_result === true,
    created_at: text(raw.created_at),
    updated_at: text(raw.updated_at),
  };
  if ("sources" in raw) {
    session.result_text = text(raw.result_text);
    session.queries = Array.isArray(raw.queries) ? raw.queries.map(normalizeQuery) : [];
    session.sources = (Array.isArray(raw.sources) ? raw.sources : [])
      .map(normalizeSource)
      .filter((source) => source !== null);
    session.claims = (Array.isArray(raw.claims) ? raw.claims : [])
      .map(normalizeClaim)
      .filter((claim) => claim !== null);
  }
  return session;
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

// "0.90" for a rating, "未評価" when the stored value is null.
export function formatRating(value) {
  return typeof value === "number" ? value.toFixed(2) : "未評価";
}

// ----- list -----

export function countByStatus(sessions) {
  const counts = { all: sessions.length, other: 0 };
  for (const status of SESSION_STATUSES) counts[status] = 0;
  for (const session of sessions) {
    if (SESSION_STATUSES.includes(session.status)) counts[session.status] += 1;
    else counts.other += 1;
  }
  return counts;
}

export function filterSessions(sessions, filter) {
  return SESSION_STATUSES.includes(filter)
    ? sessions.filter((session) => session.status === filter)
    : sessions;
}

export function listItemModel(session, { selectedId = null, timeZone } = {}) {
  return {
    id: session.id,
    question: session.question,
    status: statusInfo(session.status),
    levelText: levelLabel(session.level),
    hasResult: session.has_result,
    updatedText: formatTimestamp(session.updated_at, { timeZone }),
    selected: session.id === selectedId,
  };
}

// `all` is every session the API returned (it feeds the counts); `shown` is the part the screen
// lists. They differ only when the server was asked for one status because `all` was cut off.
export function listViewModel(
  all,
  shown,
  { filter = "all", selectedId = null, truncated = false, timeZone } = {},
) {
  const rows = filterSessions(shown, filter);
  return {
    counts: countByStatus(all),
    items: rows.map((session) => listItemModel(session, { selectedId, timeZone })),
    empty: rows.length === 0 ? (all.length === 0 ? "none" : "filtered") : null,
    truncated,
  };
}

// ----- detail -----

// Joins each claim to the source it cites. A claim whose source is not in the response is
// shown as such instead of being dropped, so a broken citation is visible.
export function detailViewModel(session, { timeZone } = {}) {
  const sources = session.sources ?? [];
  const claims = session.claims ?? [];
  const byId = new Map(sources.map((source, position) => [source.id, position]));
  const claimCounts = new Map();
  for (const claim of claims) {
    claimCounts.set(claim.source_id, (claimCounts.get(claim.source_id) ?? 0) + 1);
  }
  const sourceModels = sources.map((source, position) => ({
    id: source.id,
    number: position + 1,
    title: source.title,
    publisher: source.publisher,
    typeText: sourceTypeLabel(source.source_type),
    url: source.url,
    href: safeHref(source.url),
    finalUrl: source.final_url && source.final_url !== source.url ? source.final_url : null,
    finalHref: source.final_url && source.final_url !== source.url ? safeHref(source.final_url) : null,
    publishedText: source.published_at ? formatTimestamp(source.published_at, { timeZone }) : null,
    retrievedText: formatTimestamp(source.retrieved_at, { timeZone }),
    claimCount: claimCounts.get(source.id) ?? 0,
    ratings: RATING_KEYS.map((key) => ({
      key,
      label: RATING_LABELS[key],
      value: source.evaluation[key],
      text: formatRating(source.evaluation[key]),
    })),
  }));
  const claimModels = claims.map((claim, position) => {
    const sourcePosition = byId.get(claim.source_id);
    const source = sourcePosition === undefined ? null : sourceModels[sourcePosition];
    return {
      id: claim.id,
      number: position + 1,
      claimText: claim.claim_text,
      quote: claim.quote,
      rangeText:
        claim.quote_start !== null && claim.quote_end !== null
          ? `本文の ${claim.quote_start}〜${claim.quote_end} 文字目`
          : null,
      source: source && {
        number: source.number,
        title: source.title,
        url: source.url,
        href: source.href,
      },
    };
  });
  return {
    id: session.id,
    question: session.question,
    status: statusInfo(session.status),
    levelText: levelLabel(session.level),
    failure: session.failure_reason === null ? null : failureLabel(session.failure_reason),
    resultText: session.result_text ?? null,
    hasResult: session.has_result,
    queries: (session.queries ?? []).map((query, position) => ({
      number: position + 1,
      text: query.text,
    })),
    sources: sourceModels,
    claims: claimModels,
    times: [
      ["作成", formatTimestamp(session.created_at, { timeZone })],
      ["更新", formatTimestamp(session.updated_at, { timeZone })],
    ],
    terminal: isTerminal(session),
  };
}

// Text for the polite live region when the selected session changes state.
export function statusChangeMessage(previous, next) {
  if (!previous || !next || previous.id !== next.id || previous.status === next.status) return null;
  return `調査の状態が「${statusInfo(previous.status).label}」から「${statusInfo(next.status).label}」に変わりました。`;
}

// ----- error messages -----

const API_ERROR_MESSAGES = {
  unauthorized: "ログインの有効期限が切れました。ログイン画面に移動します。",
  network: "サーバーに接続できません。JARVIS が起動しているか確認してください。",
  offline: "オフラインです。ネットワーク接続を確認してください。",
  unavailable: "調査データの保存先を読み取れません。しばらくしてからもう一度お試しください。",
  not_found: "この調査は見つかりませんでした。",
  bad_request: "リクエストが正しくありませんでした。",
  server: "サーバーでエラーが発生しました。",
  format: "サーバーの応答を読み取れませんでした。",
};

export function apiErrorMessage(kind) {
  return lookup(API_ERROR_MESSAGES, kind) ?? API_ERROR_MESSAGES.server;
}

// Doubles the wait after each failure, up to a ceiling. `failures` counts consecutive failures.
export function backoffDelay(failures, { baseMs = 5000, maxMs = 30000 } = {}) {
  const count = Math.max(1, Math.floor(failures));
  return Math.min(maxMs, baseMs * 2 ** (count - 1));
}
