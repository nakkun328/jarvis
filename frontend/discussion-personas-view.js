// Pure helpers of the persona editor (docs/discussion.md, "Persona editor"). No DOM access, so
// Node can test them. The limits mirror backend/discussion/personas.py; the server stays the
// authority, these only give an early, specific message.
import { PALETTE, availableModels, errorMessage } from "./discussion-view.js";

export const LIMITS = Object.freeze({
  name: 20,
  role: 100,
  stance: 500,
  checklistItems: 5,
  checklistItem: 100,
});
// The speaker names the server keeps for itself (store.py).
export const RESERVED_NAMES = Object.freeze(["まとめ", "お知らせ", "まとめ役", "システム"]);
export const COLOR_LABELS = Object.freeze({
  blue: "青",
  green: "緑",
  orange: "オレンジ",
  purple: "紫",
  red: "赤",
  teal: "ティール",
});
export const colorLabel = (color) => COLOR_LABELS[color] ?? "標準";

export const STORE_NOTES = Object.freeze({
  unavailable:
    "ペルソナの保存先が設定されていないため、追加や編集はできません（プリセットは使えます）。" +
    "サーバー側で JARVIS_DB_PATH か JARVIS_DISCUSSION_PERSONAS_PATH を設定すると保存できます。",
  corrupt:
    "ペルソナの保存ファイルを読み込めないため、追加・編集・削除を止めています。" +
    "ファイルは上書きしません。サーバー上でバックアップ（同じ名前に .bak が付いたファイル）から手動で戻すか、" +
    "壊れたファイルを取り除いてください。",
});

const NAME_FORBIDDEN = /[\u0000-\u001f\u007f【】[\]<>]/u;
const CONTROL = /[\u0000-\u0008\u000b-\u001f\u007f]/u;
const length = (text) => Array.from(text).length;
const ALLOWED_MESSAGE = { PERSONA_NAME_INVALID: "名前を入力してください（1〜20文字）。" };

export const emptyDraft = () => ({ name: "", role: "", stance: "", checklist: [""], model: "", color: "" });

export function draftFromPersona(persona, models) {
  const usable = availableModels({ models }).some((m) => m.id === persona.model);
  return {
    name: persona.name,
    role: persona.role,
    stance: persona.stance,
    checklist: persona.checklist.length > 0 ? [...persona.checklist] : [""],
    // A model that left the allowlist or is not ready is not offered: pick another one.
    model: usable ? persona.model : "",
    color: PALETTE.includes(persona.color) ? persona.color : "",
  };
}

// A new custom persona prefilled from a preset: a free name, the preset's other fields.
export function copyDraft(preset, taken, models) {
  const used = new Set(taken.map((n) => n.toLocaleLowerCase()));
  const suffix = "のコピー";
  const stem = Array.from(preset.name).slice(0, LIMITS.name - length(suffix)).join("");
  let name = stem + suffix;
  for (let n = 2; used.has(name.toLocaleLowerCase()) && n < 100; n += 1) {
    const tail = `${suffix}${n}`;
    name = Array.from(preset.name).slice(0, LIMITS.name - length(tail)).join("") + tail;
  }
  return { ...draftFromPersona(preset, models), name };
}

// ctx: { models, presets, custom, editing } where `editing` is the saved name being edited (or null).
// Returns { errors: { name?, role?, stance?, checklist?, model?, color? }, body } (body: null with errors).
export function validateDraft(draft, ctx) {
  const errors = {};
  const name = draft.name.trim();
  const role = draft.role.trim();
  const stance = draft.stance.trim();
  const checklist = draft.checklist.map((item) => item.trim()).filter((item) => item !== "");

  const nameLength = length(name);
  const folded = name.toLocaleLowerCase();
  const others = ctx.custom.filter((p) => p.name !== ctx.editing).map((p) => p.name);
  if (nameLength === 0) errors.name = ALLOWED_MESSAGE.PERSONA_NAME_INVALID;
  else if (nameLength > LIMITS.name) errors.name = `名前は${LIMITS.name}文字以内にしてください（現在${nameLength}文字）。`;
  else if (NAME_FORBIDDEN.test(name)) errors.name = "名前に使えない文字（【 】 [ ] < > や制御文字）が含まれています。";
  else if (/^\.+$/.test(name)) errors.name = "名前を「.」だけにはできません。";
  else if (RESERVED_NAMES.includes(name) || ctx.presets.some((p) => p.name.toLocaleLowerCase() === folded)) {
    errors.name = errorMessage("PERSONA_NAME_RESERVED");
  } else if (others.some((n) => n.toLocaleLowerCase() === folded)) {
    errors.name = errorMessage("PERSONA_DUPLICATE_NAME");
  }

  const roleLength = length(role);
  if (roleLength === 0) errors.role = "役割を入力してください。";
  else if (roleLength > LIMITS.role) errors.role = `役割は${LIMITS.role}文字以内にしてください（現在${roleLength}文字）。`;
  else if (CONTROL.test(role) || role.includes("\n")) errors.role = "役割に改行や制御文字は使えません。";

  const stanceLength = length(draft.stance);
  if (stanceLength > LIMITS.stance) errors.stance = `立場は${LIMITS.stance}文字以内にしてください（現在${stanceLength}文字）。`;
  else if (CONTROL.test(draft.stance)) errors.stance = "立場に使えない制御文字が含まれています。";

  if (checklist.length > LIMITS.checklistItems) {
    errors.checklist = `チェックリストは${LIMITS.checklistItems}件までです。`;
  } else {
    const long = checklist.findIndex((item) => length(item) > LIMITS.checklistItem);
    if (long >= 0) errors.checklist = `チェックリストの各項目は${LIMITS.checklistItem}文字以内にしてください（${long + 1}件目）。`;
    else if (checklist.some((item) => CONTROL.test(item))) errors.checklist = "チェックリストに使えない制御文字が含まれています。";
  }

  if (!availableModels({ models: ctx.models }).some((m) => m.id === draft.model)) {
    errors.model = "使えるモデルを選んでください。";
  }
  if (!PALETTE.includes(draft.color)) errors.color = "色を選んでください。";

  if (Object.keys(errors).length > 0) return { errors, body: null };
  return {
    errors,
    body: { name, role, stance, checklist, model: draft.model, color: draft.color },
  };
}

// The order the form shows fields in, for moving focus to the first problem.
export const FIELD_ORDER = Object.freeze(["name", "role", "stance", "checklist", "model", "color"]);

// Why the editor is read-only (null: it can be used) and why "new" is disabled.
export const storeNote = (store) => (store === "ok" ? null : STORE_NOTES[store] ?? STORE_NOTES.unavailable);

export function newBlocker(listing) {
  if (listing.personas_store !== "ok") return storeNote(listing.personas_store);
  if (listing.custom.length >= listing.max_custom) return errorMessage("PERSONA_LIMIT");
  return null;
}
