// Pure helpers for the group discussion screen (docs/discussion.md). No DOM access, so Node can
// test them. Every string that came from the server (topic, turns, names, notices) is untrusted
// data: this module only passes it through and the DOM layer shows it as text nodes.

export const SUMMARY_SPEAKER = "まとめ";
export const NOTICE_SPEAKER = "お知らせ";
export const PALETTE = ["blue", "green", "orange", "purple", "red", "teal"];
export const NEUTRAL_COLOR = "neutral";

// The same rules as backend/discussion/format.py: a fixed 【name】 prefix of 1 to 20 characters
// without brackets or line breaks; a message without it has no speaker.
const PREFIX = /^【([^【】\n]{1,20})】/;
const NOTICE_CODE = /（([A-Z][A-Z_]{1,40})）$/;

export function parseMessage(content) {
  if (typeof content !== "string") return { speaker: null, text: "" };
  const match = PREFIX.exec(content);
  if (match === null) return { speaker: null, text: content };
  return { speaker: match[1], text: content.slice(match[0].length) };
}

export function noticeCode(text) {
  const match = typeof text === "string" ? NOTICE_CODE.exec(text) : null;
  return match ? match[1] : null;
}

const NOTICE_MEANINGS = {
  PERSONA_SKIPPED: "応答できなかった参加者の発言を飛ばして続けました。",
  PROVIDER_FAILURES: "モデルの応答が3回続けて失敗したため、議論を終了しました。",
  CALL_CAP: "モデル呼び出しの上限に達したため、議論を終了しました。",
  SUMMARY_FAILED: "まとめを作成できませんでした。",
  CONVERGED: "これ以上新しい論点が出なかったため、まとめに進みました。",
  CANCELLED: "停止されたため、議論を終了しました。",
  INTERRUPTED: "サーバーの再起動などで議論が中断されました。",
  EXECUTION_FAILED: "議論を実行できませんでした。",
  TIMEOUT: "制限時間を超えたため、議論を終了しました。",
  INTERNAL_ERROR: "内部エラーのため、議論を終了しました。",
  VERIFICATION_FAILED: "結果の検証に失敗したため、議論を終了しました。",
};
export const UNKNOWN_NOTICE_MEANING = "議論についてのお知らせです。";

const own = (table, key) => typeof key === "string" && Object.hasOwn(table, key);

export const noticeMeaning = (code) =>
  own(NOTICE_MEANINGS, code) ? NOTICE_MEANINGS[code] : UNKNOWN_NOTICE_MEANING;

// Error codes of POST /api/discussions and /config, each in plain Japanese.
const ERROR_MESSAGES = {
  DISCUSSION_BUSY: "ほかの議論が実行中です。終わるか停止してから、もう一度開始してください。",
  DISCUSSION_DAILY_LIMIT: "今日の議論の回数上限に達しました。明日もう一度お試しください。",
  DISCUSSION_REQUEST_INVALID: "リクエストの形式が正しくありません。ページを読み込み直してください。",
  DISCUSSION_TOPIC_INVALID: "議題が空か、長すぎます。文字数を確認してください。",
  DISCUSSION_ROUNDS_INVALID: "ラウンド数が範囲外です。選び直してください。",
  DISCUSSION_PARTICIPANTS_INVALID: "参加者は2〜4人で、重複なく選んでください。",
  DISCUSSION_PRESET_UNKNOWN: "選んだ参加者が見つかりません。ページを読み込み直してください。",
  DISCUSSION_LIMITS_INVALID: "議論の上限設定に問題があります。サーバーの設定を確認してください。",
  DISCUSSION_MODEL_UNAVAILABLE: "選んだモデルは今使えません。別のモデルを選んでください。",
  DISCUSSION_STORAGE_UNAVAILABLE: "保存領域に接続できません。しばらくしてからもう一度お試しください。",
  PERSONA_NAME_INVALID: "参加者の名前が正しくありません。",
  PERSONA_ROLE_INVALID: "参加者の役割が正しくありません。",
  PERSONA_STANCE_INVALID: "参加者の立場の設定が正しくありません。",
  PERSONA_CHECKLIST_INVALID: "参加者のチェックリストが正しくありません。",
  PERSONA_MODEL_INVALID: "参加者のモデルの指定が正しくありません。",
  PERSONA_MODEL_NOT_ALLOWED: "許可されていないモデルが選ばれています。選び直してください。",
  PERSONA_COLOR_INVALID: "参加者の色の指定が正しくありません。",
  PERSONA_FIELD_UNKNOWN: "参加者の設定に未対応の項目があります。",
  PERSONA_INVALID: "参加者の設定が正しくありません。",
  PERSONA_DUPLICATE_NAME: "参加者の名前が重複しています。",
  PERSONA_NAME_RESERVED: "この名前は予約されているため使えません（プリセット名や「まとめ」「お知らせ」など）。",
  PERSONA_NOT_FOUND: "そのペルソナは見つかりません。一覧を更新しました。",
  PERSONA_LIMIT: "保存できるペルソナの数の上限に達しています。不要なものを削除してください。",
  PERSONA_STORE_UNAVAILABLE: "ペルソナの保存先を使えません。サーバーの設定を確認してください。",
  PERSONA_STORE_CORRUPT: "ペルソナの保存ファイルが壊れているため、変更できません。",
  PERSONA_STORE_TOO_LARGE: "ペルソナの保存データが大きすぎます。内容を短くするか、不要なものを削除してください。",
  PERSONA_FILE_UNREADABLE: "参加者の設定ファイルを読めません。",
  PERSONA_FILE_INVALID: "参加者の設定ファイルが正しくありません。",
  forbidden: "この操作は許可されていません（別のサイトからの操作は受け付けません）。",
  confirm_header_required: "確認ヘッダーがありません。ページを読み込み直してください。",
  not_found: "指定された議論は見つかりませんでした。",
};
export const GENERIC_ERROR = "操作を完了できませんでした。しばらくしてからもう一度お試しください。";
export const NETWORK_ERROR = "サーバーに接続できません。接続を確認してもう一度お試しください。";
export const FEATURE_OFF_MESSAGE =
  "グループ議論はこのサーバーで有効になっていません（JARVIS_DISCUSSION と JARVIS_MODEL_CHOICES の設定が必要です）。";
export const SESSION_MESSAGE = "ログインの有効期限が切れました。";

// kind: "network" | "http"; code is the `detail` string of an error answer when it has one.
export function errorMessage(code, status = null) {
  if (code === "network") return NETWORK_ERROR;
  if (own(ERROR_MESSAGES, code)) return ERROR_MESSAGES[code];
  if (status === 401) return SESSION_MESSAGE;
  if (status === 404) return FEATURE_OFF_MESSAGE;
  if (status === 429) return ERROR_MESSAGES.DISCUSSION_DAILY_LIMIT;
  if (status === 409) return ERROR_MESSAGES.DISCUSSION_BUSY;
  if (status === 503) return ERROR_MESSAGES.DISCUSSION_STORAGE_UNAVAILABLE;
  return GENERIC_ERROR;
}

export function cancelErrorMessage(status) {
  if (status === 409) return "この議論はすでに終了しているため、停止できません。";
  if (status === 404) return "停止する議論が見つかりません。";
  if (status === "network") return NETWORK_ERROR;
  if (status === 401) return SESSION_MESSAGE;
  if (status === 403) return ERROR_MESSAGES.confirm_header_required;
  return "停止できませんでした。もう一度お試しください。";
}

// ----- config -----

const isRecord = (v) => typeof v === "object" && v !== null && !Array.isArray(v);
const posInt = (v, fallback) => (Number.isInteger(v) && v >= 0 ? v : fallback);
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
export const isUuid = (v) => typeof v === "string" && UUID.test(v);

export function normalizePreset(raw) {
  if (!isRecord(raw) || typeof raw.name !== "string" || typeof raw.model !== "string") return null;
  return {
    name: raw.name,
    role: typeof raw.role === "string" ? raw.role : "",
    stance: typeof raw.stance === "string" ? raw.stance : "",
    checklist: Array.isArray(raw.checklist) ? raw.checklist.filter((x) => typeof x === "string") : [],
    model: raw.model,
    color: PALETTE.includes(raw.color) ? raw.color : NEUTRAL_COLOR,
  };
}

// Returns a config with only the fields this screen reads, or null when it is not a config.
export function normalizeConfig(raw) {
  if (!isRecord(raw) || raw.enabled !== true) return null;
  const models = (Array.isArray(raw.models) ? raw.models : [])
    .filter((m) => isRecord(m) && typeof m.id === "string")
    .map((m) => ({
      id: m.id,
      provider: typeof m.provider === "string" ? m.provider : "",
      model: typeof m.model === "string" ? m.model : m.id,
      available: m.available === true,
      is_default: m.is_default === true,
    }));
  const presets = (Array.isArray(raw.presets) ? raw.presets : []).map(normalizePreset);
  const presetList = presets.filter((p) => p !== null);
  const listing = normalizeListing({
    presets: presetList,
    custom: raw.custom_personas,
    max_custom: raw.max_custom_personas,
    store: raw.personas_store,
  });
  const active =
    isRecord(raw.active) && isUuid(raw.active.conversation_id) && typeof raw.active.task_id === "string"
      ? { task_id: raw.active.task_id, conversation_id: raw.active.conversation_id }
      : null;
  const maxRounds = Math.max(1, posInt(raw.max_rounds, 3));
  return {
    max_rounds: maxRounds,
    default_rounds: Math.min(maxRounds, Math.max(1, posInt(raw.default_rounds, 2))),
    per_day_limit: posInt(raw.per_day_limit, 0),
    used_today: posInt(raw.used_today, 0),
    remaining_today: posInt(raw.remaining_today, 0),
    participants_min: Math.max(1, posInt(raw.participants_min, 2)),
    participants_max: Math.max(1, posInt(raw.participants_max, 4)),
    topic_max_chars: Math.max(1, posInt(raw.topic_max_chars, 2000)),
    models,
    presets: presetList,
    ...listing,
    personas: [...presetList, ...listing.custom],
    palette: Array.isArray(raw.palette) ? raw.palette.filter((c) => PALETTE.includes(c)) : [...PALETTE],
    active,
  };
}

// The persona listing of GET /api/discussions/personas (or the matching config fields):
// { custom: [persona + available], max_custom, store }. `store` is "ok", "unavailable" or "corrupt".
export function normalizeListing(raw) {
  const rec = isRecord(raw) ? raw : {};
  const custom = (Array.isArray(rec.custom) ? rec.custom : [])
    .map((item) => {
      const persona = normalizePreset(item);
      return persona === null ? null : { ...persona, available: item.available === true, custom: true };
    })
    .filter((p) => p !== null);
  const given = rec.store ?? rec.personas_store;
  const store = given === "ok" || given === "corrupt" ? given : "unavailable";
  return { custom: store === "ok" ? custom : [], max_custom: Math.max(1, posInt(rec.max_custom, 20)), personas_store: store };
}

// The same config with a fresh persona listing (after an edit); presets do not change.
export function withListing(config, raw) {
  const listing = normalizeListing(raw);
  return { ...config, ...listing, personas: [...config.presets, ...listing.custom] };
}

export const availableModels = (config) => config.models.filter((m) => m.available);

// The model a participant starts with: the preset's own when it is usable, else the default one.
export function initialModel(preset, config) {
  const usable = availableModels(config);
  if (usable.some((m) => m.id === preset.model)) return preset.model;
  return (usable.find((m) => m.is_default) ?? usable[0])?.id ?? "";
}

export function topicLength(topic) {
  return Array.from(typeof topic === "string" ? topic : "").length;
}

// Why the start button is disabled (an empty list: it is enabled). Each reason is a sentence.
export function startBlockers(config, form) {
  if (config === null) return [FEATURE_OFF_MESSAGE];
  const reasons = [];
  if (config.active !== null) reasons.push(ERROR_MESSAGES.DISCUSSION_BUSY);
  if (config.remaining_today <= 0) reasons.push(ERROR_MESSAGES.DISCUSSION_DAILY_LIMIT);
  if (availableModels(config).length === 0) reasons.push("使えるモデルがありません。サーバーの設定を確認してください。");
  const length = topicLength(form.topic);
  if (form.topic.trim() === "") reasons.push("議題を入力してください。");
  else if (length > config.topic_max_chars) {
    reasons.push(`議題は${config.topic_max_chars}文字以内にしてください。`);
  }
  const count = form.participants.length;
  if (count < config.participants_min || count > config.participants_max) {
    reasons.push(`参加者を${config.participants_min}〜${config.participants_max}人選んでください。`);
  }
  if (!Number.isInteger(form.rounds) || form.rounds < 1 || form.rounds > config.max_rounds) {
    reasons.push("ラウンド数を選んでください。");
  }
  return reasons;
}

// The POST body. A participant whose model is unchanged is sent by preset name; a changed model
// sends the full persona object so the server checks the chosen model against its allowlist.
export function buildStartBody(config, form) {
  const personas = form.participants.map((p) => {
    const preset = config.personas.find((x) => x.name === p.name);
    if (preset && preset.model === p.model) return preset.name;
    return {
      name: preset.name,
      role: preset.role,
      stance: preset.stance,
      checklist: preset.checklist,
      model: p.model,
      color: preset.color === NEUTRAL_COLOR ? undefined : preset.color,
    };
  });
  return { topic: form.topic.trim(), personas, rounds: form.rounds };
}

// Speaker name -> colour name, from the participants the page sent (or the presets when a
// discussion is opened later). Unknown speakers fall back to neutral.
export function colorMap(participants, presets = []) {
  const map = new Map();
  for (const p of participants) if (p?.name) map.set(p.name, p.color);
  for (const p of presets) if (!map.has(p.name)) map.set(p.name, p.color);
  return map;
}

export const colorOf = (map, speaker) => {
  const color = map.get(speaker);
  return PALETTE.includes(color) ? color : NEUTRAL_COLOR;
};

// ----- messages -----

// One entry per stored message: { kind: "topic"|"turn"|"summary"|"notice"|"other", ... }.
export function messageViewModel(message, colors) {
  const role = message?.role === "user" ? "user" : "assistant";
  const { speaker, text } = parseMessage(message?.content);
  if (role === "user") return { kind: "topic", speaker: null, text: speaker === null ? text : message.content, color: NEUTRAL_COLOR };
  if (speaker === SUMMARY_SPEAKER) return { kind: "summary", speaker, text, color: NEUTRAL_COLOR };
  if (speaker === NOTICE_SPEAKER) {
    const code = noticeCode(text);
    const detail = code === null ? text : text.slice(0, text.length - code.length - 2).trim();
    return { kind: "notice", speaker, code, text: detail, meaning: noticeMeaning(code), color: NEUTRAL_COLOR };
  }
  if (speaker === null) return { kind: "other", speaker: null, text, color: NEUTRAL_COLOR };
  return { kind: "turn", speaker, text, color: colorOf(colors, speaker) };
}

// ----- task progress -----

export const ACTIVE_STATUSES = new Set(["pending", "running", "waiting"]);
export const isActiveStatus = (status) => ACTIVE_STATUSES.has(status);

const STATUS_TEXT = {
  pending: "順番待ちです…",
  running: "議論中です",
  waiting: "待機中です",
  completed: "議論が終了しました。",
  failed: "議論は失敗して終了しました。",
  cancelled: "議論は停止されました。",
};

export function progressText(task) {
  if (!isRecord(task) || typeof task.status !== "string") return "状態を確認しています…";
  const base = own(STATUS_TEXT, task.status) ? STATUS_TEXT[task.status] : "状態を確認しています…";
  if (task.status !== "running") return base;
  const total = posInt(task.steps_total, 0);
  const done = posInt(task.steps_completed, 0);
  if (total === 0) return base;
  // The last step is the summary; the others are turns.
  const turns = total - 1;
  const text = done >= turns ? "まとめを作成中" : `発言 ${Math.min(done + 1, turns)} / ${turns}`;
  return `${base}（${text}）`;
}
