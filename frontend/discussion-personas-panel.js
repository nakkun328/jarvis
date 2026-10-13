// The persona editor panel inside the /discussion screen. Every string from the server (names,
// roles, stances, checklist lines) is shown with textContent only. The DOM and requests are
// injected so Node tests can drive it with a fake document and fake requests.
import { chipNode } from "./discussion-render.js";
import {
  COLOR_LABELS,
  FIELD_ORDER,
  LIMITS,
  colorLabel,
  copyDraft,
  draftFromPersona,
  emptyDraft,
  newBlocker,
  storeNote,
  validateDraft,
} from "./discussion-personas-view.js";
import { availableModels, errorMessage } from "./discussion-view.js";

const IDS = [
  "persona-section", "persona-details", "persona-status", "persona-store-note", "persona-presets",
  "persona-count", "persona-custom", "persona-empty", "persona-new-note", "persona-new",
  "persona-confirm", "persona-confirm-text", "persona-confirm-yes", "persona-confirm-no",
  "persona-form", "pf-title", "pf-name", "pf-name-counter", "pf-name-error", "pf-role",
  "pf-role-counter", "pf-role-error", "pf-stance", "pf-stance-counter", "pf-stance-error",
  "pf-checklist", "pf-add-line", "pf-checklist-error", "pf-model", "pf-model-hint", "pf-model-error",
  "pf-colors", "pf-color-error", "pf-error", "pf-save", "pf-cancel",
];

export const PERSONA_IDS = IDS;
export const SAVED_MESSAGE = "保存しました。";
export const DELETED_MESSAGE = "削除しました。";
export const REFRESH_FAILED_MESSAGE = "変更は保存されましたが、一覧を更新できませんでした。ページを読み込み直してください。";

const errorOf = (error) => errorMessage(error?.code ?? error?.kind, error?.status ?? null);

// env: { doc, api, onChange(listing, info) }. `api` has listPersonas/createPersona/updatePersona/deletePersona.
export function createPersonaPanel(env) {
  const { doc, api } = env;
  const el = {};
  for (const id of IDS) el[id] = doc.getElementById(id);

  const state = {
    listing: { presets: [], custom: [], max_custom: 20, personas_store: "unavailable" },
    models: [],
    palette: [],
    editing: undefined, // undefined: form closed; null: a new persona; string: the saved name
    rows: [], // checklist rows: { input, counter }
    colorInputs: [],
    attempted: false,
    busy: false,
    pendingDelete: null,
  };

  const make = (tag, className, text) => {
    const node = doc.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const setStatus = (text, error = false) => {
    el["persona-status"].textContent = text;
    el["persona-status"].className = error ? "list-status error" : "list-status";
  };
  const usableStore = () => state.listing.personas_store === "ok";
  const ctx = () => ({
    models: state.models,
    presets: state.listing.presets,
    custom: state.listing.custom,
    editing: typeof state.editing === "string" ? state.editing : null,
  });

  // ----- lists -----

  function describe(persona) {
    const parts = [persona.role, `モデル: ${persona.model}`];
    return parts.join(" / ");
  }

  function personaItem(persona, actions) {
    const item = make("li", persona.custom && persona.available === false ? "persona-item flagged" : "persona-item");
    const head = make("div", "persona-head");
    head.append(chipNode(doc, persona.color), make("span", "persona-name", persona.name), make("span", "color-text", colorLabel(persona.color)));
    item.append(head, make("p", "persona-desc", describe(persona)));
    if (persona.custom && persona.available === false) {
      item.append(make("p", "persona-flag", "このモデルは今使えません。編集して別のモデルを選ぶまで、議論には参加できません。"));
    }
    const bar = make("div", "persona-actions");
    for (const action of actions) bar.append(action);
    item.append(bar);
    return item;
  }

  function button(text, label, onClick, disabled) {
    const node = make("button", "", text);
    node.type = "button";
    node.setAttribute("aria-label", label);
    node.disabled = disabled;
    node.addEventListener("click", onClick);
    return node;
  }

  function renderLists() {
    const { listing } = state;
    const locked = !usableStore();
    const atLimit = listing.custom.length >= listing.max_custom;
    el["persona-presets"].replaceChildren(
      ...listing.presets.map((preset) =>
        personaItem({ ...preset, custom: false }, [
          button("コピーして編集", `${preset.name}をコピーして編集`, () => openCopy(preset), locked || atLimit),
        ]),
      ),
    );
    el["persona-custom"].replaceChildren(
      ...listing.custom.map((persona) =>
        personaItem(persona, [
          button("編集", `${persona.name}を編集`, () => openEdit(persona), locked),
          button("削除", `${persona.name}を削除`, () => askDelete(persona), locked),
        ]),
      ),
    );
    el["persona-count"].textContent = usableStore() ? `${listing.custom.length} / ${listing.max_custom}` : "";
    el["persona-empty"].hidden = !(usableStore() && listing.custom.length === 0);
    const note = storeNote(listing.personas_store);
    el["persona-store-note"].hidden = note === null;
    el["persona-store-note"].textContent = note ?? "";
    const blocker = newBlocker(listing);
    // Not repeated under the store note.
    el["persona-new-note"].textContent = locked ? "" : blocker ?? "";
    el["persona-new"].disabled = blocker !== null || state.busy;
  }

  // ----- delete -----

  function askDelete(persona) {
    closeForm();
    state.pendingDelete = persona.name;
    el["persona-confirm-text"].textContent = `「${persona.name}」を削除します。元に戻せません。よろしいですか？`;
    el["persona-confirm"].hidden = false;
    el["persona-confirm-no"].focus?.();
  }

  function closeConfirm() {
    state.pendingDelete = null;
    el["persona-confirm"].hidden = true;
  }

  async function confirmDelete() {
    const name = state.pendingDelete;
    if (name === null || state.busy) return;
    await mutate(() => api.deletePersona(name), DELETED_MESSAGE, { deleted: name });
    closeConfirm();
  }

  // ----- form -----

  function addRow(value = "") {
    const wrap = make("div", "checklist-row");
    const input = make("input");
    input.type = "text";
    input.value = value;
    input.setAttribute("autocomplete", "off");
    const index = state.rows.length + 1;
    input.setAttribute("aria-label", `チェックリストの項目`);
    const counter = make("span", "counter-inline");
    const row = { wrap, input, counter };
    const remove = make("button", "", "削除");
    remove.type = "button";
    remove.addEventListener("click", () => {
      state.rows = state.rows.filter((r) => r !== row);
      if (state.rows.length === 0) addRow("");
      renderRows();
      (state.rows[Math.min(index - 1, state.rows.length - 1)] ?? row).input.focus?.();
    });
    row.remove = remove;
    input.addEventListener("input", () => {
      updateCounters();
      revalidate();
    });
    wrap.append(input, counter, remove);
    state.rows.push(row);
  }

  function renderRows() {
    state.rows.forEach((row, i) => {
      row.input.setAttribute("aria-label", `チェックリスト ${i + 1}件目`);
      row.remove.setAttribute("aria-label", `チェックリスト ${i + 1}件目を削除`);
    });
    el["pf-checklist"].replaceChildren(...state.rows.map((r) => r.wrap));
    el["pf-add-line"].disabled = state.rows.length >= LIMITS.checklistItems;
    updateCounters();
  }

  function count(text) {
    return Array.from(text).length;
  }

  function counterNode(node, used, max) {
    node.textContent = `${used} / ${max}`;
    node.className = used > max ? "counter over" : "counter";
  }

  function updateCounters() {
    counterNode(el["pf-name-counter"], count(el["pf-name"].value.trim()), LIMITS.name);
    counterNode(el["pf-role-counter"], count(el["pf-role"].value.trim()), LIMITS.role);
    counterNode(el["pf-stance-counter"], count(el["pf-stance"].value), LIMITS.stance);
    for (const row of state.rows) counterNode(row.counter, count(row.input.value.trim()), LIMITS.checklistItem);
  }

  function readDraft() {
    return {
      name: el["pf-name"].value,
      role: el["pf-role"].value,
      stance: el["pf-stance"].value,
      checklist: state.rows.map((r) => r.input.value),
      model: el["pf-model"].value,
      color: state.colorInputs.find((c) => c.checked)?.value ?? "",
    };
  }

  function showErrors(errors) {
    for (const field of FIELD_ORDER) {
      const node = el[`pf-${field}-error`];
      if (node) node.textContent = errors[field] ?? "";
      const control = field === "checklist" ? null : el[`pf-${field}`];
      if (control) control.setAttribute("aria-invalid", String(field in errors));
    }
  }

  function revalidate() {
    if (state.attempted) showErrors(validateDraft(readDraft(), ctx()).errors);
  }

  function renderModels(draft, flagged) {
    const select = el["pf-model"];
    select.replaceChildren();
    const placeholder = make("option", "", "選んでください");
    placeholder.value = "";
    select.append(placeholder);
    for (const model of availableModels({ models: state.models })) {
      const option = make("option", "", model.id);
      option.value = model.id;
      select.append(option);
    }
    select.value = draft.model;
    el["pf-model-hint"].textContent = flagged
      ? "元のモデルは今使えません。使えるモデルから選び直してください。"
      : "使えるモデルだけが表示されます。";
  }

  function renderColors(draft) {
    state.colorInputs = [];
    const items = state.palette.map((color) => {
      const label = make("label", "swatch");
      const input = make("input");
      input.type = "radio";
      input.setAttribute("name", "pf-color");
      input.value = color;
      input.checked = draft.color === color;
      input.addEventListener("change", revalidate);
      state.colorInputs.push(input);
      label.append(input, chipNode(doc, color), make("span", "swatch-text", COLOR_LABELS[color] ?? color));
      return label;
    });
    el["pf-colors"].setAttribute("role", "radiogroup");
    el["pf-colors"].setAttribute("aria-label", "色");
    el["pf-colors"].replaceChildren(...items);
  }

  function openForm(draft, { editing, title, flagged = false }) {
    closeConfirm();
    state.editing = editing;
    state.attempted = false;
    el["pf-title"].textContent = title;
    el["pf-name"].value = draft.name;
    el["pf-role"].value = draft.role;
    el["pf-stance"].value = draft.stance;
    state.rows = [];
    for (const line of draft.checklist) addRow(line);
    renderRows();
    renderModels(draft, flagged);
    renderColors(draft);
    showErrors({});
    el["pf-error"].textContent = "";
    updateCounters();
    el["persona-form"].hidden = false;
    el["pf-name"].focus?.();
  }

  function closeForm() {
    state.editing = undefined;
    el["persona-form"].hidden = true;
  }

  function openNew() {
    if (newBlocker(state.listing) !== null) return;
    openForm(emptyDraft(), { editing: null, title: "新しいペルソナ" });
  }

  function openCopy(preset) {
    if (newBlocker(state.listing) !== null) return;
    const taken = [...state.listing.presets, ...state.listing.custom].map((p) => p.name);
    openForm(copyDraft(preset, taken, state.models), { editing: null, title: `「${preset.name}」をコピーして新規作成` });
  }

  function openEdit(persona) {
    if (!usableStore()) return;
    const draft = draftFromPersona(persona, state.models);
    openForm(draft, {
      editing: persona.name,
      title: `「${persona.name}」を編集`,
      flagged: draft.model === "",
    });
  }

  async function save() {
    if (state.busy || state.editing === undefined) return;
    state.attempted = true;
    const { errors, body } = validateDraft(readDraft(), ctx());
    showErrors(errors);
    el["pf-error"].textContent = "";
    if (body === null) {
      const first = FIELD_ORDER.find((f) => f in errors);
      const target = first === "checklist" ? state.rows[0]?.input : first === "color" ? state.colorInputs[0] : el[`pf-${first}`];
      target?.focus?.();
      return;
    }
    const editing = state.editing;
    const ok = await mutate(
      () => (editing === null ? api.createPersona(body) : api.updatePersona(editing, body)),
      SAVED_MESSAGE,
      editing !== null && editing !== body.name ? { renamed: { from: editing, to: body.name } } : {},
      (message) => {
        el["pf-error"].textContent = message;
      },
    );
    if (ok) closeForm();
  }

  // Runs a write, then re-reads the list and tells the screen. Returns true when the write worked.
  async function mutate(write, doneMessage, info, onError) {
    state.busy = true;
    el["pf-save"].disabled = true;
    renderLists();
    setStatus("");
    let ok = false;
    try {
      await write();
      ok = true;
    } catch (error) {
      const message = errorOf(error);
      if (onError) onError(message);
      else setStatus(message, true);
      // The store may have changed under us (not found, corrupt, ...): show the truth.
      if (["PERSONA_NOT_FOUND", "PERSONA_STORE_CORRUPT", "PERSONA_STORE_UNAVAILABLE"].includes(error?.code)) {
        await refresh(info, false);
      }
    }
    if (ok) await refresh(info, true, doneMessage);
    state.busy = false;
    el["pf-save"].disabled = false;
    renderLists();
    return ok;
  }

  async function refresh(info, announce, doneMessage = "") {
    try {
      const listing = await api.listPersonas();
      state.listing = listing;
      renderLists();
      env.onChange?.(listing, announce ? info : {});
      if (announce) setStatus(doneMessage);
    } catch {
      setStatus(REFRESH_FAILED_MESSAGE, true);
    }
  }

  // ----- public -----

  // listing: { presets, custom, max_custom, personas_store }; models and palette come from the config.
  function setListing(listing, { models, palette }) {
    state.listing = listing;
    state.models = models;
    state.palette = palette;
    el["persona-section"].hidden = false;
    if (!usableStore()) {
      closeForm();
      closeConfirm();
    }
    renderLists();
  }

  function hide() {
    el["persona-section"].hidden = true;
    closeForm();
    closeConfirm();
  }

  el["persona-new"].addEventListener("click", openNew);
  el["persona-confirm-yes"].addEventListener("click", () => confirmDelete());
  el["persona-confirm-no"].addEventListener("click", closeConfirm);
  el["pf-add-line"].addEventListener("click", () => {
    if (state.rows.length >= LIMITS.checklistItems) return;
    addRow("");
    renderRows();
    state.rows[state.rows.length - 1].input.focus?.();
  });
  el["pf-cancel"].addEventListener("click", closeForm);
  el["persona-form"].addEventListener("submit", (event) => {
    event.preventDefault?.();
    return save();
  });
  for (const id of ["pf-name", "pf-role", "pf-stance"]) {
    el[id].addEventListener("input", () => {
      updateCounters();
      revalidate();
    });
  }
  el["pf-model"].addEventListener("change", revalidate);

  return { setListing, hide, state, el, save, openNew, openEdit, openCopy };
}
