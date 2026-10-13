import assert from "node:assert/strict";
import test from "node:test";
import { DiscussionApiError, request } from "../discussion-api.js";
import { createPersonaPanel } from "../discussion-personas-panel.js";
import { createPersona, deletePersona, listPersonas, personaUrl, updatePersona } from "../discussion-personas-api.js";
import { createScreen } from "../discussion-screen.js";
import { copyDraft, emptyDraft, validateDraft } from "../discussion-personas-view.js";
import { errorMessage, normalizeConfig } from "../discussion-view.js";
import { fakeDocument, rawConfig } from "./fake-dom.mjs";

const MODELS = normalizeConfig(rawConfig()).models;
const PRESETS = normalizeConfig(rawConfig()).presets;
const ctx = (over = {}) => ({ models: MODELS, presets: PRESETS, custom: [], editing: null, ...over });
const draft = (over = {}) => ({ ...emptyDraft(), name: "新人", role: "若手の視点", model: "gemini:flash", color: "blue", ...over });
const saved = (over = {}) => ({
  name: "自作", role: "役割", stance: "立場", checklist: ["a"], model: "gemini:flash", color: "green", available: true, ...over,
});

// ----- pure validation -----

test("validation mirrors the server limits with a specific message per field", () => {
  assert.equal(validateDraft(draft(), ctx()).errors.name, undefined);
  assert.deepEqual(validateDraft(draft(), ctx()).body, {
    name: "新人", role: "若手の視点", stance: "", checklist: [], model: "gemini:flash", color: "blue",
  });
  const bad = (over, field, pattern) => {
    const { errors, body } = validateDraft(draft(over), ctx());
    assert.equal(body, null);
    assert.match(errors[field], pattern, field);
  };
  bad({ name: "  " }, "name", /名前を入力/);
  bad({ name: "あ".repeat(21) }, "name", /20文字以内.*21文字/);
  bad({ name: "a【b" }, "name", /使えない文字/);
  bad({ name: "x<y" }, "name", /使えない文字/);
  bad({ name: ".." }, "name", /「\.」だけ/);
  bad({ name: "まとめ" }, "name", /予約/);
  bad({ name: "設計者" }, "name", /予約/);
  bad({ role: "" }, "role", /役割を入力/);
  bad({ role: "あ".repeat(101) }, "role", /100文字以内/);
  bad({ role: "a\nb" }, "role", /改行/);
  bad({ stance: "あ".repeat(501) }, "stance", /500文字以内/);
  bad({ checklist: ["x".repeat(101)] }, "checklist", /100文字以内.*1件目/);
  bad({ checklist: ["1", "2", "3", "4", "5", "6"] }, "checklist", /5件まで/);
  bad({ model: "groq:down" }, "model", /使えるモデル/);
  bad({ model: "" }, "model", /使えるモデル/);
  bad({ color: "" }, "color", /色を選んで/);
  bad({ color: "pink" }, "color", /色を選んで/);
});

test("validation counts characters, trims, drops blank checklist lines and allows 20 / 5 / 100", () => {
  const ok = validateDraft(
    draft({ name: ` ${"あ".repeat(20)} `, checklist: [" a ", "", "  ", "x".repeat(100)], stance: "s".repeat(500) }),
    ctx(),
  );
  assert.deepEqual(ok.errors, {});
  assert.equal(ok.body.name.length, 20);
  assert.deepEqual(ok.body.checklist, ["a", "x".repeat(100)]);
  // Astral characters count once.
  assert.equal(validateDraft(draft({ name: "𠮷".repeat(20) }), ctx()).errors.name, undefined);
});

test("validation: duplicates are case-folded, and the persona being edited does not clash with itself", () => {
  const custom = [saved({ name: "Alpha" })];
  assert.match(validateDraft(draft({ name: "alpha" }), ctx({ custom })).errors.name, /重複/);
  assert.equal(validateDraft(draft({ name: "alpha" }), ctx({ custom, editing: "Alpha" })).errors.name, undefined);
});

test("copyDraft picks a free name within 20 characters and drops an unusable model", () => {
  const preset = { ...PRESETS[1], name: "あ".repeat(20) };
  const copy = copyDraft(preset, [preset.name], MODELS);
  assert.ok(Array.from(copy.name).length <= 20);
  assert.ok(copy.name.endsWith("のコピー"));
  assert.equal(copy.model, "", "groq:down is not available");
  const again = copyDraft(PRESETS[0], ["設計者", "設計者のコピー"], MODELS);
  assert.equal(again.name, "設計者のコピー2");
  assert.equal(again.model, "gemini:flash");
});

test("every persona-route error code has its own Japanese message", () => {
  for (const code of [
    "PERSONA_NAME_INVALID", "PERSONA_NAME_RESERVED", "PERSONA_ROLE_INVALID", "PERSONA_STANCE_INVALID",
    "PERSONA_CHECKLIST_INVALID", "PERSONA_MODEL_INVALID", "PERSONA_MODEL_NOT_ALLOWED", "PERSONA_COLOR_INVALID",
    "PERSONA_FIELD_UNKNOWN", "PERSONA_INVALID", "PERSONA_DUPLICATE_NAME", "PERSONA_NOT_FOUND", "PERSONA_LIMIT",
    "PERSONA_STORE_UNAVAILABLE", "PERSONA_STORE_CORRUPT", "PERSONA_STORE_TOO_LARGE", "DISCUSSION_REQUEST_INVALID",
    "forbidden", "confirm_header_required",
  ]) {
    const message = errorMessage(code);
    assert.match(message, /[぀-ヿ一-鿿]/, code);
    assert.notEqual(message, errorMessage("UNKNOWN_CODE_XYZ"), `${code} must not fall back to the generic message`);
  }
});

// ----- requests -----

function fakeFetch(responses) {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, init });
    const next = responses.shift();
    return { ok: next.status < 400, status: next.status, json: async () => next.body };
  };
  return { fetchImpl, calls };
}

test("persona requests: URLs, methods, confirm header on every write, no header on read", async () => {
  const { fetchImpl, calls } = fakeFetch([
    { status: 200, body: { presets: [PRESETS[0]], custom: [saved()], max_custom: 20, store: "ok" } },
    { status: 201, body: saved({ name: "新規" }) },
    { status: 200, body: saved({ name: "改名" }) },
    { status: 200, body: { deleted: true } },
  ]);
  const listing = await listPersonas({ fetchImpl });
  assert.equal(listing.personas_store, "ok");
  assert.equal(listing.custom[0].custom, true);
  assert.equal(listing.custom[0].available, true);
  await createPersona({ ...saved({ name: "新規" }), extra: "dropped" }, { fetchImpl });
  await updatePersona("a/b ?x", saved({ name: "改名" }), { fetchImpl });
  await deletePersona("自作", { fetchImpl });
  assert.deepEqual(calls.map((c) => [c.init.method, c.url]), [
    ["GET", "/api/discussions/personas"],
    ["POST", "/api/discussions/personas"],
    ["PUT", `/api/discussions/personas/${encodeURIComponent("a/b ?x")}`],
    ["DELETE", `/api/discussions/personas/${encodeURIComponent("自作")}`],
  ]);
  assert.equal(calls[0].init.headers["X-Jarvis-Confirm"], undefined);
  for (const call of calls.slice(1)) assert.equal(call.init.headers["X-Jarvis-Confirm"], "1");
  assert.deepEqual(Object.keys(JSON.parse(calls[1].init.body)).sort(), ["checklist", "color", "model", "name", "role", "stance"]);
  assert.equal(calls[3].init.body, undefined);
  assert.equal(personaUrl("x"), "/api/discussions/personas/x");
});

test("persona requests keep the error code and status; a bad answer is a format error", async () => {
  const { fetchImpl } = fakeFetch([
    { status: 409, body: { detail: "PERSONA_DUPLICATE_NAME" } },
    { status: 200, body: { deleted: false } },
    { status: 200, body: { nope: 1 } },
  ]);
  await assert.rejects(createPersona(saved(), { fetchImpl }), (e) => e.status === 409 && e.code === "PERSONA_DUPLICATE_NAME");
  await assert.rejects(deletePersona("x", { fetchImpl }), (e) => e.kind === "format");
  await assert.rejects(updatePersona("x", saved(), { fetchImpl }), (e) => e instanceof DiscussionApiError && e.kind === "format");
  await assert.rejects(request("/x", { fetchImpl: async () => { throw new TypeError("down"); } }), (e) => e.kind === "network");
});

// ----- panel with an in-memory fake server -----

function fakeServer({ store = "ok", custom = [], max = 20 } = {}) {
  const server = { store, custom: custom.map((p) => ({ ...p })), max, calls: [], fail: null };
  const find = (name) => server.custom.findIndex((p) => p.name.toLocaleLowerCase() === name.toLocaleLowerCase());
  const check = (body, ignore = null) => {
    if (server.fail) throw new DiscussionApiError("http", server.fail.status, server.fail.code);
    if (server.store === "corrupt") throw new DiscussionApiError("http", 503, "PERSONA_STORE_CORRUPT");
    if (server.store !== "ok") throw new DiscussionApiError("http", 503, "PERSONA_STORE_UNAVAILABLE");
    const clash = find(body.name);
    if (clash >= 0 && clash !== ignore) throw new DiscussionApiError("http", 409, "PERSONA_DUPLICATE_NAME");
  };
  server.api = {
    listPersonas: async () => ({
      presets: PRESETS, custom: server.store === "ok" ? server.custom.map((p) => ({ ...p, custom: true })) : [],
      max_custom: server.max, personas_store: server.store,
    }),
    createPersona: async (body) => {
      server.calls.push(["create", body]);
      check(body);
      if (server.custom.length >= server.max) throw new DiscussionApiError("http", 409, "PERSONA_LIMIT");
      server.custom.push({ ...body, available: true });
    },
    updatePersona: async (name, body) => {
      server.calls.push(["update", name, body]);
      const at = find(name);
      if (at < 0) throw new DiscussionApiError("http", 404, "PERSONA_NOT_FOUND");
      check(body, at);
      server.custom[at] = { ...body, available: true };
    },
    deletePersona: async (name) => {
      server.calls.push(["delete", name]);
      const at = find(name);
      if (at < 0) throw new DiscussionApiError("http", 404, "PERSONA_NOT_FOUND");
      server.custom.splice(at, 1);
    },
  };
  return server;
}

function panelSetup(options = {}) {
  const doc = fakeDocument();
  const server = fakeServer(options);
  const changes = [];
  const panel = createPersonaPanel({ doc, api: server.api, onChange: (listing, info) => changes.push({ listing, info }) });
  const n = (id) => doc.nodes.get(id);
  const open = async () => panel.setListing(await server.api.listPersonas(), { models: MODELS, palette: ["blue", "green", "orange"] });
  const buttons = (id) => n(id).find((x) => x.tag === "button");
  const button = (id, label) => buttons(id).find((b) => b.getAttribute("aria-label") === label);
  const fill = (values) => {
    for (const [id, value] of Object.entries(values)) n(id).value = value;
  };
  const pickColor = (color) => {
    for (const input of panel.state.colorInputs) input.checked = input.value === color;
  };
  const submit = () => n("persona-form").dispatch("submit");
  return { doc, server, panel, n, open, button, buttons, fill, pickColor, submit, changes };
}

test("panel: presets are read-only with a copy action; custom ones list edit and delete", async () => {
  const t = panelSetup({ custom: [saved()] });
  await t.open();
  assert.equal(t.n("persona-section").hidden, false);
  assert.equal(t.n("persona-presets").children.length, 3);
  assert.deepEqual(t.buttons("persona-presets").map((b) => b.textContent), ["コピーして編集", "コピーして編集", "コピーして編集"]);
  assert.deepEqual(t.buttons("persona-custom").map((b) => b.textContent), ["編集", "削除"]);
  assert.match(t.n("persona-custom").children[0].textContent, /自作.*緑/);
  assert.equal(t.n("persona-count").textContent, "1 / 20");
  assert.equal(t.n("persona-form").hidden, true);
});

test("panel: copy a preset prefills a new persona; an unavailable preset model must be re-chosen", async () => {
  const t = panelSetup();
  await t.open();
  await t.button("persona-presets", "批判役をコピーして編集").dispatch("click");
  assert.equal(t.n("persona-form").hidden, false);
  assert.equal(t.n("pf-name").value, "批判役のコピー");
  assert.equal(t.n("pf-role").value, "弱点を突く");
  assert.equal(t.n("pf-model").value, "");
  assert.equal(t.panel.state.colorInputs.find((c) => c.checked).value, "orange");
  await t.submit();
  assert.match(t.n("pf-model-error").textContent, /使えるモデル/);
  assert.equal(t.server.calls.length, 0, "nothing is sent while a field is invalid");
  t.fill({ "pf-model": "openai:mini" });
  await t.submit();
  assert.deepEqual(t.server.calls.map((c) => c[0]), ["create"]);
  assert.equal(t.server.calls[0][1].name, "批判役のコピー");
  assert.equal(t.n("persona-form").hidden, true);
  assert.equal(t.n("persona-status").textContent, "保存しました。");
  assert.equal(t.changes.length, 1);
  assert.equal(t.changes[0].listing.custom[0].name, "批判役のコピー");
});

test("panel: create with inline validation, counters and checklist rows", async () => {
  const t = panelSetup();
  await t.open();
  await t.n("persona-new").dispatch("click");
  assert.equal(t.n("pf-title").textContent, "新しいペルソナ");
  t.fill({ "pf-name": "あ".repeat(21), "pf-stance": "x".repeat(10) });
  await t.n("pf-name").dispatch("input");
  assert.equal(t.n("pf-name-counter").textContent, "21 / 20");
  assert.equal(t.n("pf-name-counter").className, "counter over");
  assert.equal(t.n("pf-stance-counter").textContent, "10 / 500");
  await t.submit();
  for (const field of ["name", "role", "model", "color"]) assert.notEqual(t.n(`pf-${field}-error`).textContent, "", field);
  assert.equal(t.n("pf-name").focused, true);
  assert.equal(t.n("pf-name").getAttribute("aria-invalid"), "true");
  assert.equal(t.server.calls.length, 0);
  // Checklist rows: add up to 5, remove, keep at least one.
  for (let i = 0; i < 6; i += 1) await t.n("pf-add-line").dispatch("click");
  assert.equal(t.n("pf-checklist").children.length, 5);
  assert.equal(t.n("pf-add-line").disabled, true);
  await t.buttons("pf-checklist")[0].dispatch("click");
  assert.equal(t.n("pf-checklist").children.length, 4);
  assert.equal(t.n("pf-add-line").disabled, false);
  // Fix and save.
  t.fill({ "pf-name": "新人", "pf-role": "若手", "pf-model": "gemini:flash" });
  t.panel.state.rows[0].input.value = "確認する";
  t.pickColor("green");
  await t.submit();
  assert.deepEqual(t.server.calls[0], ["create", {
    name: "新人", role: "若手", stance: "xxxxxxxxxx", checklist: ["確認する"], model: "gemini:flash", color: "green",
  }]);
  assert.equal(t.n("persona-custom").children.length, 1);
  assert.equal(t.n("persona-count").textContent, "1 / 20");
});

test("panel: the colour chooser is a radio group with a text label per colour", async () => {
  const t = panelSetup();
  await t.open();
  await t.n("persona-new").dispatch("click");
  const swatches = t.n("pf-colors").children;
  assert.deepEqual(swatches.map((s) => s.textContent), ["青", "緑", "オレンジ"]);
  assert.equal(t.n("pf-colors").getAttribute("role"), "radiogroup");
  assert.ok(t.panel.state.colorInputs.every((i) => i.type === "radio" && i.getAttribute("name") === "pf-color"));
});

test("panel: edit saves under the same name; rename reports from/to; nothing leaks as markup", async () => {
  const t = panelSetup({ custom: [saved({ name: "<b>x</b>" })] });
  await t.open();
  assert.equal(t.n("persona-custom").children[0].children[0].textContent.includes("<b>x</b>"), true);
  await t.button("persona-custom", "<b>x</b>を編集").dispatch("click");
  assert.equal(t.n("pf-title").textContent, "「<b>x</b>」を編集");
  assert.equal(t.n("pf-name").value, "<b>x</b>");
  t.fill({ "pf-name": "改名後" });
  await t.submit();
  assert.deepEqual(t.server.calls[0][0], "update");
  assert.equal(t.server.calls[0][1], "<b>x</b>");
  assert.equal(t.server.calls[0][2].name, "改名後");
  assert.deepEqual(t.changes[0].info, { renamed: { from: "<b>x</b>", to: "改名後" } });
  assert.equal(t.server.custom[0].name, "改名後");
});

test("panel: a server refusal shows its own Japanese message and keeps the form", async () => {
  const t = panelSetup({ custom: [saved({ name: "先客" })] });
  await t.open();
  await t.n("persona-new").dispatch("click");
  t.fill({ "pf-name": "新人", "pf-role": "r", "pf-model": "gemini:flash" });
  t.pickColor("blue");
  t.server.fail = { status: 413, code: "PERSONA_STORE_TOO_LARGE" };
  await t.submit();
  assert.equal(t.n("pf-error").textContent, errorMessage("PERSONA_STORE_TOO_LARGE"));
  assert.equal(t.n("persona-form").hidden, false);
  t.server.fail = null;
  t.server.calls.length = 0;
  t.fill({ "pf-name": "先客" });
  await t.submit();
  assert.match(t.n("pf-name-error").textContent, /重複/, "caught before sending");
  assert.equal(t.server.calls.length, 0);
  // A race the client cannot see: the server says duplicate.
  t.server.custom.push(saved({ name: "後から" }));
  t.fill({ "pf-name": "後から" });
  await t.submit();
  assert.equal(t.n("pf-error").textContent, errorMessage("PERSONA_DUPLICATE_NAME"));
});

test("panel: delete asks first, can be cancelled, then deletes and refreshes", async () => {
  const t = panelSetup({ custom: [saved(), saved({ name: "二人目" })] });
  await t.open();
  await t.button("persona-custom", "自作を削除").dispatch("click");
  assert.equal(t.n("persona-confirm").hidden, false);
  assert.match(t.n("persona-confirm-text").textContent, /「自作」を削除します/);
  assert.equal(t.n("persona-confirm-no").focused, true);
  await t.n("persona-confirm-no").dispatch("click");
  assert.equal(t.n("persona-confirm").hidden, true);
  assert.equal(t.server.calls.length, 0);
  await t.button("persona-custom", "自作を削除").dispatch("click");
  await t.n("persona-confirm-yes").dispatch("click");
  assert.deepEqual(t.server.calls, [["delete", "自作"]]);
  assert.equal(t.n("persona-custom").children.length, 1);
  assert.equal(t.n("persona-status").textContent, "削除しました。");
  assert.deepEqual(t.changes[0].info, { deleted: "自作" });
  assert.equal(t.n("persona-confirm").hidden, true);
});

test("panel: deleting something already gone says so and refreshes the list", async () => {
  const t = panelSetup({ custom: [saved()] });
  await t.open();
  t.server.custom.length = 0;
  await t.button("persona-custom", "自作を削除").dispatch("click");
  await t.n("persona-confirm-yes").dispatch("click");
  assert.equal(t.n("persona-status").textContent, errorMessage("PERSONA_NOT_FOUND"));
  assert.equal(t.n("persona-custom").children.length, 0);
  assert.equal(t.changes.length, 1, "the picker is told the list changed");
});

test("panel: an unavailable custom persona is flagged and still editable; saving needs a usable model", async () => {
  const t = panelSetup({ custom: [saved({ name: "古い", model: "groq:down", available: false })] });
  await t.open();
  const item = t.n("persona-custom").children[0];
  assert.match(item.textContent, /今使えません/);
  assert.equal(item.className, "persona-item flagged");
  await t.button("persona-custom", "古いを編集").dispatch("click");
  assert.equal(t.n("pf-model").value, "");
  assert.match(t.n("pf-model-hint").textContent, /選び直して/);
  await t.submit();
  assert.match(t.n("pf-model-error").textContent, /使えるモデル/);
  t.fill({ "pf-model": "openai:mini" });
  await t.submit();
  assert.equal(t.server.calls[0][2].model, "openai:mini");
});

test("panel: store unavailable disables writes and explains; corrupt explains the .bak restore without repair", async () => {
  const off = panelSetup({ store: "unavailable" });
  await off.open();
  assert.equal(off.n("persona-store-note").hidden, false);
  assert.match(off.n("persona-store-note").textContent, /保存先が設定されていない/);
  assert.equal(off.n("persona-new").disabled, true);
  assert.ok(off.buttons("persona-presets").every((b) => b.disabled));
  assert.equal(off.n("persona-count").textContent, "");
  await off.n("persona-new").dispatch("click");
  assert.equal(off.n("persona-form").hidden, true);
  assert.equal(off.n("persona-presets").children.length, 3, "presets are still listed");

  const bad = panelSetup({ store: "corrupt", custom: [saved()] });
  await bad.open();
  assert.match(bad.n("persona-store-note").textContent, /\.bak/);
  assert.match(bad.n("persona-store-note").textContent, /手動/);
  assert.doesNotMatch([...bad.doc.nodes.values()].map((x) => x.textContent).join(""), /修復する|リセット/);
  assert.equal(bad.n("persona-new").disabled, true);
  assert.equal(bad.n("persona-custom").children.length, 0);
});

test("panel: at the limit new and copy are disabled with an explanation", async () => {
  const t = panelSetup({ custom: [saved({ name: "a" }), saved({ name: "b" })], max: 2 });
  await t.open();
  assert.equal(t.n("persona-new").disabled, true);
  assert.equal(t.n("persona-new-note").textContent, errorMessage("PERSONA_LIMIT"));
  assert.ok(t.buttons("persona-presets").every((b) => b.disabled));
  assert.equal(t.button("persona-custom", "aを編集").disabled, false);
});

test("panel: the store turning corrupt mid-edit refreshes the view and explains", async () => {
  const t = panelSetup();
  await t.open();
  await t.n("persona-new").dispatch("click");
  t.fill({ "pf-name": "新人", "pf-role": "r", "pf-model": "gemini:flash" });
  t.pickColor("blue");
  t.server.store = "corrupt";
  await t.submit();
  assert.equal(t.n("pf-error").textContent, errorMessage("PERSONA_STORE_CORRUPT"));
  assert.equal(t.n("persona-store-note").hidden, false);
  assert.equal(t.n("persona-new").disabled, true);
});

// ----- the picker follows the editor -----

function screenSetup({ custom = [], store = "ok", config = {} } = {}) {
  const doc = fakeDocument();
  const server = fakeServer({ store, custom });
  const calls = { start: [] };
  const raw = () => rawConfig({
    custom_personas: server.custom.map((p) => ({ ...p })), personas_store: server.store, max_custom_personas: server.max, ...config,
  });
  const api = {
    loadConfig: async () => normalizeConfig(raw()),
    startDiscussion: async (body) => {
      calls.start.push(body);
      return { conversation_id: "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d", task_id: "t1" };
    },
    loadMessages: async () => ({ messages: [], total: 0 }),
    loadTaskStatus: async () => ({ status: "running" }),
    cancelDiscussion: async () => ({ ok: true }),
  };
  const screen = createScreen({
    doc, api, personaApi: server.api, search: "",
    schedule: () => 1, cancel: () => {}, isHidden: () => true, replaceUrl: () => {}, navigate: () => {},
  });
  const n = (id) => doc.nodes.get(id);
  const rows = () => n("participant-list").children;
  const names = () => rows().map((r) => r.find((x) => x.className === "participant-name")[0].textContent);
  const pick = (name) => screen.state.rows.find((r) => r.preset.name === name);
  return { doc, server, screen, n, rows, names, pick, calls };
}

test("screen: the picker lists custom personas after the presets and the panel opens with the start form", async () => {
  const t = screenSetup({ custom: [saved(), saved({ name: "古い", model: "groq:down", available: false })] });
  await t.screen.init();
  assert.deepEqual(t.names(), ["設計者", "批判役", "現実担当", "自作", "古い"]);
  assert.equal(t.n("persona-section").hidden, false);
  assert.equal(t.pick("自作").checkbox.checked, false);
  const old = t.pick("古い");
  assert.equal(old.checkbox.disabled, true, "an unavailable persona cannot be picked");
  assert.match(old.node.textContent, /選べません/);
});

test("screen: creating a persona refreshes the picker; selected participants keep their choices", async () => {
  const t = screenSetup();
  await t.screen.init();
  t.pick("設計者").checkbox.checked = false;
  await t.pick("設計者").checkbox.dispatch("change");
  t.pick("批判役").select.value = "openai:mini";
  await t.pick("批判役").select.dispatch("change");
  const before = t.screen.state.participants.map((p) => [p.name, p.model]);
  assert.deepEqual(before, [["批判役", "openai:mini"], ["現実担当", "openai:mini"]]);
  await t.n("persona-new").dispatch("click");
  t.n("pf-name").value = "新人";
  t.n("pf-role").value = "若手";
  t.n("pf-model").value = "gemini:flash";
  const colorInput = t.n("pf-colors").find((x) => x.type === "radio")[0];
  colorInput.checked = true;
  await t.n("persona-form").dispatch("submit");
  assert.deepEqual(t.names(), ["設計者", "批判役", "現実担当", "新人"]);
  assert.deepEqual(t.screen.state.participants.map((p) => [p.name, p.model]), before);
  // The new persona can be chosen and is sent by name.
  t.pick("新人").checkbox.checked = true;
  await t.pick("新人").checkbox.dispatch("change");
  t.n("topic").value = "議題";
  await t.n("topic").dispatch("input");
  await t.screen.submit();
  assert.ok(t.calls.start[0].personas.includes("新人"));
  assert.ok(t.calls.start[0].personas.includes("現実担当"));
});

test("screen: a changed model on a custom persona sends the full object; unchanged sends the name", async () => {
  const t = screenSetup({ custom: [saved({ name: "自作", color: "orange" })] });
  await t.screen.init();
  t.pick("自作").checkbox.checked = true;
  await t.pick("自作").checkbox.dispatch("change");
  t.pick("自作").select.value = "openai:mini";
  await t.pick("自作").select.dispatch("change");
  t.n("topic").value = "議題";
  await t.n("topic").dispatch("input");
  await t.screen.submit();
  const sent = t.calls.start[0].personas.find((p) => p?.name === "自作");
  assert.equal(sent.name, "自作");
  assert.equal(sent.model, "openai:mini");
  assert.equal(sent.color, "orange");
});

test("screen: renaming keeps the selection, deleting removes the persona from it", async () => {
  const t = screenSetup({ custom: [saved({ name: "旧名" }), saved({ name: "消す" })] });
  await t.screen.init();
  for (const name of ["旧名", "消す"]) {
    t.pick(name).checkbox.checked = true;
    await t.pick(name).checkbox.dispatch("change");
  }
  // Four is the maximum, so free a slot first.
  t.pick("設計者").checkbox.checked = false;
  await t.pick("設計者").checkbox.dispatch("change");
  const selected = () => t.screen.state.participants.map((p) => p.name);
  assert.deepEqual(selected(), ["批判役", "現実担当", "旧名", "消す"].filter((x) => selected().includes(x)));
  assert.ok(selected().includes("旧名") && selected().includes("消す"));
  // Rename
  const editButton = t.n("persona-custom").find((x) => x.tag === "button" && x.getAttribute("aria-label") === "旧名を編集")[0];
  await editButton.dispatch("click");
  t.n("pf-name").value = "新名";
  await t.n("persona-form").dispatch("submit");
  assert.ok(selected().includes("新名"), "renamed persona stays selected");
  assert.ok(!selected().includes("旧名"));
  // Delete
  const del = t.n("persona-custom").find((x) => x.tag === "button" && x.getAttribute("aria-label") === "消すを削除")[0];
  await del.dispatch("click");
  await t.n("persona-confirm-yes").dispatch("click");
  assert.ok(!selected().includes("消す"), "deleted persona leaves the selection");
  assert.ok(!t.names().includes("消す"));
  assert.ok(selected().includes("新名"));
});

test("screen: with the store unavailable the picker has presets only and the panel explains", async () => {
  const t = screenSetup({ store: "unavailable" });
  await t.screen.init();
  assert.deepEqual(t.names(), ["設計者", "批判役", "現実担当"]);
  assert.equal(t.n("persona-new").disabled, true);
  assert.equal(t.n("persona-store-note").hidden, false);
});

test("screen: the panel is hidden while a discussion runs", async () => {
  const t = screenSetup();
  await t.screen.init();
  t.n("topic").value = "議題";
  await t.n("topic").dispatch("input");
  await t.screen.submit();
  assert.equal(t.n("persona-section").hidden, true);
  assert.equal(t.n("start-section").hidden, true);
});
