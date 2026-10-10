// Pure view logic for starting a research and watching it (no DOM, so Node can test it).
//
// Everything shown comes from the API. Questions, titles, quotes and result text are untrusted
// data: this module only passes them through as strings and the DOM layer renders them with
// textContent. Error messages are looked up from fixed codes; nothing the server returned is
// shown as an error.

export const QUESTION_MAX = 2000;
export const ACTIVE_STATUSES = new Set(["pending", "running", "waiting"]);

// Shown next to the start button and required by the owner's decision on the search service.
export const SEARCH_NOTICE =
  "調べるときは、質問文が検索サービス(Tavily)へ送信されます。サービス側で保持・利用される可能性があります。記憶の内容は送信しません。";

export const CITATION_NOTE =
  "引用は、出典の本文に引用文がそのまま含まれていることを確認したものです（内容が正しいことの保証ではありません）。出典の評価値（0〜1）は簡易な目安です。";

export const LEVELS = [
  {
    value: "quick",
    label: "クイック",
    hint: "検索は最大2回、読むページは最大3件。1〜2分ほどで終わります。",
  },
  {
    value: "standard",
    label: "標準",
    hint: "検索は最大5回、読むページは最大8件。複数の出典を照らし合わせます。最大5分ほどかかります。",
  },
  {
    value: "deep",
    label: "詳細",
    hint: "質問を2〜5個の調べ項目に分け、検索は最大10回、読むページは最大20件。検索の利用量が多く、最大10分ほどかかります。",
  },
];

const OWN = (table, key) => (typeof key === "string" && Object.hasOwn(table, key) ? table[key] : null);

export function levelHint(value) {
  return LEVELS.find((level) => level.value === value)?.hint ?? "";
}

export function isRequestableLevel(value) {
  return LEVELS.some((level) => level.value === value);
}

// ----- the question field -----

const CONTROL = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/;

// Counts code points like the server does. `valid` is what enables the start button.
export function questionState(value) {
  const text = typeof value === "string" ? value : "";
  const length = Array.from(text).length;
  const blank = text.trim().length === 0;
  const tooLong = length > QUESTION_MAX;
  const hasControl = CONTROL.test(text);
  let problem = null;
  if (tooLong) problem = "question_too_long";
  else if (hasControl) problem = "question_invalid_characters";
  return {
    length,
    remaining: QUESTION_MAX - length,
    blank,
    tooLong,
    hasControl,
    problem,
    valid: !blank && problem === null,
    counterText: `${length} / ${QUESTION_MAX}`,
  };
}

// ----- availability -----

const UNAVAILABLE_REASONS = {
  disabled: "調査の開始は設定でオフになっています。使うには JARVIS_RESEARCH_ENABLED を有効にして再起動してください。",
  no_search_provider: "検索サービスが設定されていないため、調査を始められません。",
  no_chat_provider: "チャットのモデルが設定されていないため、調査を始められません。",
};
const UNAVAILABLE_FALLBACK = "調査を始められる設定になっていません。";

// `body` is the decoded GET /api/research/status. Anything unexpected reads as unavailable.
export function availability(body) {
  if (body && typeof body === "object" && body.enabled === true) {
    return { enabled: true, reasonText: "" };
  }
  const reason = body && typeof body === "object" ? body.reason : null;
  return { enabled: false, reasonText: OWN(UNAVAILABLE_REASONS, reason) ?? UNAVAILABLE_FALLBACK };
}

// ----- messages for fixed error codes -----

const RUN_ERRORS = {
  busy: "別の調査を実行中です。終わるか取り消してから、もう一度お試しください。",
  search_budget_exhausted: "今月の検索の上限に達しているため、調査を始められません。",
  research_not_configured: "調査を始められる設定になっていません。",
  forbidden: "このページ以外からの要求は受け付けられません。ページを開き直してください。",
  unauthorized: "ログインの有効期限が切れました。ログイン画面に移動します。",
  question_required: "調べたいことを入力してください。",
  question_too_long: `質問は${QUESTION_MAX}文字以内にしてください。`,
  question_invalid_characters: "質問に使えない制御文字が含まれています。",
  invalid_level: "調べ方を選び直してください。",
  invalid_body: "リクエストが正しくありませんでした。",
  unsupported_media_type: "リクエストが正しくありませんでした。",
  session_not_found: "この調査は見つかりませんでした。",
  not_cancellable: "この調査はすでに終わっているため、取り消せません。",
  storage_unavailable: "調査データの保存先を読み取れません。しばらくしてからもう一度お試しください。",
  network: "サーバーに接続できません。JARVIS が起動しているか確認してください。",
  offline: "オフラインです。ネットワーク接続を確認してください。",
  format: "サーバーの応答を読み取れませんでした。",
  server: "サーバーでエラーが発生しました。",
};

export function runErrorMessage(code) {
  return OWN(RUN_ERRORS, code) ?? RUN_ERRORS.server;
}

// ----- progress -----

export const STAGES = [
  { key: "planning", label: "検索語を作る" },
  { key: "searching", label: "Webを検索" },
  { key: "reading", label: "ページを読む" },
  { key: "verifying", label: "引用を検証" },
  { key: "writing", label: "結果をまとめる" },
];

const COUNTER_LABELS = [
  ["queries", "検索"],
  ["pages", "読んだページ"],
  ["sources", "出典"],
  ["claims", "検証済みの主張"],
];

const count = (value) => (Number.isInteger(value) && value >= 0 ? value : 0);

// `session` is a normalised session detail (research-view.js). For a session that is not running
// in this server process there is no `progress`, so only counts from the stored rows are shown.
export function progressModel(session) {
  const status = session?.status;
  const live = session?.progress ?? null;
  const stageKey = live && STAGES.some((stage) => stage.key === live.stage) ? live.stage : null;
  const stageIndex = stageKey === null ? -1 : STAGES.findIndex((stage) => stage.key === stageKey);
  let label = "";
  if (status === "pending") label = "順番待ちです。まもなく始まります。";
  else if (status === "running") label = stageKey ? `${STAGES[stageIndex].label}…` : "準備しています…";
  const total = count(live?.sub_questions);
  const subQuestion = total > 0 ? { index: Math.min(count(live?.sub_question), total), total } : null;
  if (status === "running" && subQuestion && subQuestion.index > 0) {
    label += `（調べ項目 ${subQuestion.index}/${subQuestion.total}）`;
  }
  else if (status === "waiting") label = "応答待ちです。";
  const stored = {
    queries: session?.queries?.length ?? 0,
    sources: session?.sources?.length ?? 0,
    claims: session?.claims?.length ?? 0,
    pages: 0,
  };
  const numbers = {
    queries: Math.max(count(live?.queries), stored.queries),
    pages: count(live?.pages),
    sources: Math.max(count(live?.sources), stored.sources),
    claims: Math.max(count(live?.claims), stored.claims),
  };
  return {
    active: ACTIVE_STATUSES.has(status),
    label,
    stageKey,
    stages: STAGES.map((stage, index) => ({
      key: stage.key,
      label: stage.label,
      state: index < stageIndex ? "done" : index === stageIndex ? "current" : "todo",
    })),
    round: count(live?.round),
    subQuestion,
    counters: COUNTER_LABELS.map(([key, text]) => ({ key, label: text, value: numbers[key] })),
    cancellable: status === "pending" || status === "running",
  };
}

// ----- the result -----

const NO_VERIFIED_JA = "出典から、引用つきで検証できる主張は得られませんでした。";
const CAVEAT_JA = {
  "No source could be read.": "読み取れた出典がありませんでした。",
  "Fewer relevant sources were found than this level aims for.":
    "このレベルが目指す数より、関連する出典が少なかったです。",
  "Some sources disagree and the disagreement is not resolved.":
    "出典の間で食い違いがあり、解決されていません。",
  "No source from an authoritative kind of site was found.": "権威のある種類のサイトの出典は見つかりませんでした。",
  "Every relevant source with a known date is old.": "日付の分かる関連出典はすべて古いものです。",
  "The search stopped because the time budget was used up.": "時間の上限に達したため、検索を止めました。",
  "The search stopped because the page budget was used up.": "ページ数の上限に達したため、検索を止めました。",
  "The search stopped because the query budget was used up.": "検索回数の上限に達したため、検索を止めました。",
  "The search stopped because the extra rounds were used up.": "追加検索の回数の上限に達したため、検索を止めました。",
  "The last extra round found no new source, so the search stopped.":
    "追加検索で新しい出典が見つからなかったため、検索を止めました。",
  "No new follow-up query could be built for the open gaps.": "不足を補う追加の検索語を作れませんでした。",
  "Some searches of the first pass failed.": "最初の検索の一部が失敗しました。",
  "The searches of an extra round failed.": "追加検索が失敗しました。",
  "An extra round could not be completed; its sources are unused.":
    "追加検索を完了できなかったため、その出典は使っていません。",
  "Some pages could not be read.": "読み取れなかったページがあります。",
  "Some proposed claims were removed because they could not be verified.":
    "検証できなかった主張は取り除きました。",
  "The sources did not support any claim that could be verified.": NO_VERIFIED_JA,
  "All verified claims come from one website, so they are not independent.":
    "検証できた主張はすべて同じウェブサイトのものなので、独立した裏付けとは言えません。",
  "The question was split by a fixed rule because no usable plan was made.":
    "調査計画を作れなかったため、質問を固定の規則で分けました。",
  "Some sub-questions have no verified claim.": "検証できた主張のない調べ項目があります。",
  "Some sub-questions were not researched because a limit was reached.":
    "上限に達したため、調べていない調べ項目があります。",
  "The search stopped because the monthly search budget was used up.":
    "今月の検索の上限に達したため、検索を止めました。",
  "Some listed sources are different pages of the same website, so they are not independent confirmation of each other.":
    "掲載した出典の一部は同じウェブサイトの別ページなので、互いに独立した裏付けとは言えません。",
};
const QUICK_NO_CLAIM = "No claim could be verified against a source.";
const QUICK_NO_CLAIM_JA = NO_VERIFIED_JA;
const QUICK_REMOVED = /^Note: (\d+) proposed claim\(s\) were removed because their sources or quotes could not be verified\.$/;
const READS_FAILED = /^(\d+) page\(s\) could not be read\. Some pages could not be read\.$/;
const CLAIMS_REMOVED = /^(\d+) claim\(s\) removed\. Some proposed claims were removed because they could not be verified\.$/;

const CONFLICT_KIND_JA = {
  "different figures": "数値が違います",
  "different dates": "日付が違います",
  "one says the opposite": "一方が反対のことを述べています",
};

function caveatText(line) {
  const known = OWN(CAVEAT_JA, line);
  if (known) return known;
  let match = READS_FAILED.exec(line);
  if (match) return `${match[1]}件のページを読み取れませんでした。`;
  match = CLAIMS_REMOVED.exec(line);
  if (match) return `検証できなかった${match[1]}件の主張を取り除きました。`;
  return line;
}

function conflictText(line) {
  const split = line.indexOf(": ");
  if (split > 0) {
    const kind = OWN(CONFLICT_KIND_JA, line.slice(0, split));
    if (kind) return `${kind}: ${line.slice(split + 2)}`;
  }
  return line;
}

// The stored result text has a fixed outline (see backend/research/standard.py and citations.py).
// Only the conflict and caveat sections are read out of it; the claims and sources shown come from
// the structured API fields, never from this text.
export function parseResultText(text) {
  const out = { conflicts: [], caveats: [] };
  if (typeof text !== "string") return out;
  let section = null;
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (line === "") {
      section = null;
    } else if (line === "Caveats:") {
      section = "caveats";
    } else if (line.startsWith("Open conflicts (") && line.endsWith(":")) {
      section = "conflicts";
    } else if (line === "Verified claims:" || line === "Sources:") {
      section = "other";
    } else if (line === QUICK_NO_CLAIM) {
      out.caveats.push(QUICK_NO_CLAIM_JA);
    } else if (QUICK_REMOVED.test(line)) {
      out.caveats.push(`検証できなかった${QUICK_REMOVED.exec(line)[1]}件の主張を取り除きました。`);
    } else if (section === "caveats" && line.startsWith("- ")) {
      out.caveats.push(caveatText(line.slice(2)));
    } else if (section === "conflicts" && line.startsWith("- ")) {
      out.conflicts.push(conflictText(line.slice(2)));
    }
  }
  return out;
}

// The outcome shown when a session has ended. `detail` is the model from detailViewModel
// (research-view.js): claims already joined to their sources, links already checked by safeHref.
export function resultModel(session, detail) {
  const status = session.status;
  const parsed = parseResultText(session.result_text);
  const openConflicts = (session.conflicts ?? []).filter((c) => c.status === "open").length;
  const conflicts = parsed.conflicts.slice();
  if (conflicts.length === 0 && openConflicts > 0) {
    conflicts.push(`出典の間で食い違いが${openConflicts}件見つかっています。`);
  }
  const noVerifiedClaims = status === "completed" && detail.claims.length === 0;
  return {
    kind: status === "completed" ? "completed" : status === "cancelled" ? "cancelled" : "failed",
    failure: detail.failure,
    claims: detail.claims,
    sourceCount: detail.sources.length,
    conflicts,
    // The "nothing could be verified" notice is shown as its own callout, not twice.
    caveats: noVerifiedClaims ? parsed.caveats.filter((line) => line !== NO_VERIFIED_JA) : parsed.caveats,
    noVerifiedClaims,
    citationNote: CITATION_NOTE,
  };
}

// ----- polling -----

export const POLL_BASE_MS = 2000;
export const POLL_MAX_MS = 15000;

export function pollDelay(failures) {
  const n = Math.max(0, Math.floor(failures));
  return n === 0 ? POLL_BASE_MS : Math.min(POLL_MAX_MS, POLL_BASE_MS * 2 ** n);
}

// The first session of a list (newest first) that is still queued or running, if any.
export function activeSessionId(sessions) {
  if (!Array.isArray(sessions)) return null;
  return sessions.find((session) => ACTIVE_STATUSES.has(session?.status))?.id ?? null;
}
