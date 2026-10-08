// Pure view-model helpers for the read-only Tasks screen. No DOM access, so Node can test them.
//
// Everything shown comes from what the API returned. Nothing is estimated: there is no
// percentage, no ETA, and a task is "running" only when its stored status says so.
// Goals, step descriptions, notes and summaries are untrusted data; this module only passes
// them through as strings and the DOM layer renders them with textContent.

export const TASK_STATUSES = ["pending", "running", "waiting", "failed", "completed", "cancelled"];
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

const STEP_INFO = {
  pending: { label: "未着手", glyph: "○", tone: "pending" },
  running: { label: "実行中", glyph: "▶", tone: "running" },
  completed: { label: "完了", glyph: "✓", tone: "completed" },
  failed: { label: "失敗", glyph: "✕", tone: "failed" },
  skipped: { label: "スキップ", glyph: "↷", tone: "skipped" },
  cancelled: { label: "取消", glyph: "⊘", tone: "cancelled" },
};

// Fixed enums from docs/tasks.md. Unknown codes fall back to OTHER_LABEL.
const FAILURE_LABELS = {
  execution_failed: "実行に失敗しました",
  verification_failed: "結果の検証に失敗しました",
  timeout: "制限時間を超えました",
  interrupted: "中断されました（再起動など）",
  internal_error: "内部エラー",
};
const WAITING_LABELS = {
  needs_confirmation: "確認が必要です",
  needs_input: "入力が必要です",
  dependency: "ほかの処理を待っています",
};
const VERIFICATION_LABELS = {
  not_verified: "未検証",
  verified: "検証済み",
  verification_failed: "検証に失敗",
};

// Object.hasOwn keeps keys such as "constructor" or "__proto__" from reaching a prototype.
function lookup(table, key) {
  return typeof key === "string" && Object.hasOwn(table, key) ? table[key] : null;
}

export function statusInfo(status) {
  const info = lookup(STATUS_INFO, status);
  return { key: info ? status : "other", ...(info ?? OTHER_INFO) };
}

export function stepStatusInfo(status) {
  const info = lookup(STEP_INFO, status);
  return { key: info ? status : "other", ...(info ?? OTHER_INFO) };
}

export const failureLabel = (code) => lookup(FAILURE_LABELS, code) ?? OTHER_LABEL;
export const waitingLabel = (code) => lookup(WAITING_LABELS, code) ?? OTHER_LABEL;
export const verificationLabel = (state) => lookup(VERIFICATION_LABELS, state) ?? OTHER_LABEL;

export const isTerminal = (task) => Boolean(task) && TERMINAL.has(task.status);

const isRecord = (value) => typeof value === "object" && value !== null && !Array.isArray(value);
const text = (value) => (typeof value === "string" ? value : null);
const count = (value) => (Number.isInteger(value) && value >= 0 ? value : null);

function normalizeStep(raw, position) {
  const step = isRecord(raw) ? raw : {};
  return {
    index: Number.isInteger(step.index) ? step.index : position,
    description: text(step.description) ?? "",
    status: text(step.status) ?? "",
    started_at: text(step.started_at),
    finished_at: text(step.finished_at),
    note: text(step.note),
  };
}

// Accepts a decoded API object and returns a task with only the fields this screen reads,
// or null when it is not a task. Unknown extra fields are dropped, never interpreted.
export function normalizeTask(raw) {
  if (!isRecord(raw) || typeof raw.id !== "string" || !raw.id || typeof raw.status !== "string") {
    return null;
  }
  const task = {
    id: raw.id,
    goal: text(raw.goal) ?? "",
    status: raw.status,
    current_step: count(raw.current_step),
    steps_total: count(raw.steps_total),
    steps_completed: count(raw.steps_completed),
    target_device: text(raw.target_device),
    attempt: count(raw.attempt),
    retry_of: text(raw.retry_of),
    verified: text(raw.verified),
    failure_code: text(raw.failure_code),
    waiting_reason: text(raw.waiting_reason),
    created_at: text(raw.created_at),
    updated_at: text(raw.updated_at),
    started_at: text(raw.started_at),
    finished_at: text(raw.finished_at),
  };
  if ("steps" in raw) {
    task.result_summary = text(raw.result_summary);
    task.steps = Array.isArray(raw.steps) ? raw.steps.map(normalizeStep) : [];
  }
  return task;
}

// "2/5 ステップ完了": a plain count, never a percentage.
export function stepCountText(task) {
  const { steps_completed: done, steps_total: total } = task;
  if (done === null || total === null || done === undefined || total === undefined) {
    return "ステップ数 —";
  }
  return `${done}/${total} ステップ完了`;
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

// ----- list -----

// Counts come only from the tasks the API returned; statuses it does not know go to "other".
export function countByStatus(tasks) {
  const counts = { all: tasks.length, other: 0 };
  for (const status of TASK_STATUSES) counts[status] = 0;
  for (const task of tasks) {
    if (TASK_STATUSES.includes(task.status)) counts[task.status] += 1;
    else counts.other += 1;
  }
  return counts;
}

export function filterTasks(tasks, filter) {
  return TASK_STATUSES.includes(filter) ? tasks.filter((task) => task.status === filter) : tasks;
}

export function listItemModel(task, { selectedId = null, timeZone } = {}) {
  const info = statusInfo(task.status);
  return {
    id: task.id,
    goal: task.goal,
    status: info,
    countText: stepCountText(task),
    updatedText: formatTimestamp(task.updated_at, { timeZone }),
    selected: task.id === selectedId,
  };
}

// `all` is every task the API returned (it feeds the counts); `shown` is the part the screen
// lists. They differ only when the server was asked for one status because `all` was cut off.
// The API returns the newest tasks first and the screen keeps that order.
export function listViewModel(
  all,
  shown,
  { filter = "all", selectedId = null, truncated = false, timeZone } = {},
) {
  const rows = filterTasks(shown, filter);
  return {
    counts: countByStatus(all),
    items: rows.map((task) => listItemModel(task, { selectedId, timeZone })),
    empty: rows.length === 0 ? (all.length === 0 ? "none" : "filtered") : null,
    truncated,
  };
}

// Replaces one task's list row with newer data from the stream without inventing fields.
export function mergeIntoList(tasks, detail) {
  return tasks.map((task) => {
    if (task.id !== detail.id) return task;
    const summary = { ...detail };
    delete summary.steps;
    delete summary.result_summary;
    return summary;
  });
}

// ----- detail -----

// One segment per step, taken from the step's own stored status. This is a step indicator,
// not a progress estimate: segments are not weighted and have no time basis.
export function stepSegments(steps) {
  return steps.map((step, position) => {
    const info = stepStatusInfo(step.status);
    return {
      key: info.key,
      tone: info.tone,
      label: `手順 ${position + 1}: ${info.label}`,
    };
  });
}

export function detailViewModel(task, { timeZone } = {}) {
  const steps = (task.steps ?? []).map((step, position) => {
    const info = stepStatusInfo(step.status);
    return {
      number: position + 1,
      description: step.description,
      status: info,
      startedText: step.started_at ? formatTimestamp(step.started_at, { timeZone }) : null,
      finishedText: step.finished_at ? formatTimestamp(step.finished_at, { timeZone }) : null,
      note: step.note,
    };
  });
  return {
    id: task.id,
    goal: task.goal,
    status: statusInfo(task.status),
    countText: stepCountText(task),
    segments: stepSegments(task.steps ?? []),
    steps,
    failure: task.failure_code === null ? null : failureLabel(task.failure_code),
    waiting: task.waiting_reason === null ? null : waitingLabel(task.waiting_reason),
    verification: task.verified === null ? null : verificationLabel(task.verified),
    resultSummary: task.result_summary ?? null,
    targetDevice: task.target_device,
    attempt: task.attempt,
    retryOf: task.retry_of,
    times: [
      ["作成", formatTimestamp(task.created_at, { timeZone })],
      ["開始", task.started_at ? formatTimestamp(task.started_at, { timeZone }) : "—"],
      ["更新", formatTimestamp(task.updated_at, { timeZone })],
      ["終了", task.finished_at ? formatTimestamp(task.finished_at, { timeZone }) : "—"],
    ],
    terminal: isTerminal(task),
  };
}

// Text for the polite live region when the selected task changes state.
export function statusChangeMessage(previous, next) {
  if (!previous || !next || previous.id !== next.id || previous.status === next.status) return null;
  return `タスクの状態が「${statusInfo(previous.status).label}」から「${statusInfo(next.status).label}」に変わりました。`;
}

// ----- error and connection messages -----

const API_ERROR_MESSAGES = {
  network: "サーバーに接続できません。JARVIS が起動しているか確認してください。",
  offline: "オフラインです。ネットワーク接続を確認してください。",
  unavailable: "タスクの保存先を読み取れません。しばらくしてからもう一度お試しください。",
  busy: "接続が混み合っています。しばらくしてからもう一度お試しください。",
  not_found: "このタスクは見つかりませんでした。",
  server: "サーバーでエラーが発生しました。",
  format: "サーバーの応答を読み取れませんでした。",
};

export function apiErrorMessage(kind) {
  return lookup(API_ERROR_MESSAGES, kind) ?? API_ERROR_MESSAGES.server;
}

const CONNECTION_MESSAGES = {
  connecting: "ライブ更新に接続しています…",
  live: "ライブ更新中（サーバーが保存した状態を表示しています）",
  reconnecting: "ライブ更新を再接続しています",
  gave_up: "自動更新を停止しました。「再接続」で再開できます。",
  done: "このタスクは終了しました。これ以上更新されません。",
  not_found: "このタスクは見つかりませんでした。",
  paused: "タブが非表示のため更新を止めています。",
  idle: "",
};

export function connectionMessage(connection) {
  const base = lookup(CONNECTION_MESSAGES, connection?.phase) ?? "";
  if (connection?.phase === "reconnecting" && Number.isInteger(connection.attempt)) {
    return `${base}（${connection.attempt}回目）…`;
  }
  return base;
}
