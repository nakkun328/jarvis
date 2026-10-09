import assert from "node:assert/strict";
import test from "node:test";
import { sendChat } from "../chat-api.js";
import {
  fetchModels,
  mountModelSelect,
  optionLabel,
  parseModels,
  resolveChoice,
} from "../model-select.js";

class Node {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.listeners = {};
    this.hidden = true;
    this.disabled = false;
    this.value = "";
    this.className = "";
    this.textContent = "";
  }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
}
const doc = { createElement: (tag) => new Node(tag) };

const MODELS = [
  { id: "openai:a", provider: "openai", model: "a", available: true, is_default: true },
  { id: "gemini:b", provider: "gemini", model: "b", available: true, is_default: false },
  { id: "gemini:c", provider: "gemini", model: "c", available: false, is_default: false },
];
const jsonFetch = (body, status = 200) => async () => new Response(JSON.stringify(body), { status });

function memoryStorage(initial = {}) {
  const data = { ...initial };
  return {
    data,
    getItem: (k) => data[k] ?? null,
    setItem: (k, v) => { data[k] = v; },
    removeItem: (k) => { delete data[k]; },
  };
}
const throwingStorage = {
  getItem() { throw new Error("blocked"); },
  setItem() { throw new Error("blocked"); },
  removeItem() { throw new Error("blocked"); },
};

test("parseModels keeps only well-formed entries", () => {
  assert.deepEqual(parseModels("nope"), []);
  assert.deepEqual(parseModels([null, {}, { id: 1 }, ...MODELS]).map((m) => m.id), ["openai:a", "gemini:b", "gemini:c"]);
  assert.equal(parseModels(MODELS)[2].available, false);
});

test("fetchModels never throws and returns [] on any failure", async () => {
  assert.equal((await fetchModels(jsonFetch(MODELS))).length, 3);
  assert.deepEqual(await fetchModels(jsonFetch([], 200)), []);
  assert.deepEqual(await fetchModels(jsonFetch({}, 401)), []);
  assert.deepEqual(await fetchModels(async () => { throw new TypeError("offline"); }), []);
  assert.deepEqual(await fetchModels(async () => new Response("not json")), []);
});

test("resolveChoice falls back to the default when the stored choice is gone or unusable", () => {
  const models = parseModels(MODELS);
  assert.equal(resolveChoice(models, "gemini:b"), "gemini:b");
  assert.equal(resolveChoice(models, "gemini:c"), null);
  assert.equal(resolveChoice(models, "gone:x"), null);
  assert.equal(resolveChoice(models, null), null);
});

test("no models: nothing is built and the value stays null", async () => {
  const mount = new Node("div");
  const control = await mountModelSelect(doc, mount, { fetchImpl: jsonFetch([]), storage: memoryStorage() });
  assert.equal(mount.hidden, true);
  assert.equal(mount.children.length, 0);
  assert.equal(control.value, null);
});

test("builds a labelled select with text nodes and remembers the pick", async () => {
  const mount = new Node("div");
  const storage = memoryStorage();
  const control = await mountModelSelect(doc, mount, { fetchImpl: jsonFetch(MODELS), storage });
  const [label, select] = mount.children;
  assert.equal(mount.hidden, false);
  assert.equal(label.textContent, "モデル");
  assert.equal(label.htmlFor, select.id);
  assert.deepEqual(select.children.map((o) => o.value), ["", "openai:a", "gemini:b", "gemini:c"]);
  assert.equal(select.children[3].disabled, true);
  assert.match(select.children[3].textContent, /利用不可/);
  assert.equal(control.value, null);
  select.value = "gemini:b";
  select.listeners.change();
  assert.equal(control.value, "gemini:b");
  assert.equal(storage.data["jarvis.modelChoice"], "gemini:b");
  select.value = "";
  select.listeners.change();
  assert.equal(control.value, null);
  assert.equal("jarvis.modelChoice" in storage.data, false);
  control.setDisabled(true);
  assert.equal(select.disabled, true);
});

test("a stored choice is restored; a vanished one is dropped silently", async () => {
  const kept = await mountModelSelect(doc, new Node("div"), {
    fetchImpl: jsonFetch(MODELS), storage: memoryStorage({ "jarvis.modelChoice": "gemini:b" }),
  });
  assert.equal(kept.value, "gemini:b");
  const storage = memoryStorage({ "jarvis.modelChoice": "gone:x" });
  const dropped = await mountModelSelect(doc, new Node("div"), { fetchImpl: jsonFetch(MODELS), storage });
  assert.equal(dropped.value, null);
  assert.equal("jarvis.modelChoice" in storage.data, false);
});

test("blocked storage does not break the selector", async () => {
  const control = await mountModelSelect(doc, new Node("div"), {
    fetchImpl: jsonFetch(MODELS), storage: throwingStorage,
  });
  assert.equal(control.value, null);
});

test("optionLabel marks default and unavailable entries", () => {
  const [a, , c] = parseModels(MODELS);
  assert.match(optionLabel(a), /既定/);
  assert.match(optionLabel(c), /利用不可/);
});

test("sendChat sends model_choice only when one is selected", async () => {
  const bodies = [];
  const fetchImpl = async (_url, init) => {
    bodies.push(JSON.parse(init.body));
    return new Response(
      'event: delta\ndata: {"text":"x"}\n\nevent: done\ndata: {"conversation_id":"c","provider":"p","model":"m"}\n\n',
      { headers: { "Content-Type": "text/event-stream" } },
    );
  };
  await sendChat({ message: "hi", onDelta() {}, fetchImpl });
  await sendChat({ message: "hi", modelChoice: "gemini:b", onDelta() {}, fetchImpl });
  assert.equal("model_choice" in bodies[0], false);
  assert.equal(bodies[1].model_choice, "gemini:b");
});
