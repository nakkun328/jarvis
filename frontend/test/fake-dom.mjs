// A tiny stand-in for the DOM for the discussion screen tests. It keeps textContent separate from
// children and has no HTML parsing at all, so any markup in a string stays literal text.
export class FakeNode {
  constructor(tag, id = "") {
    this.tag = tag;
    this.id = id;
    this.className = "";
    this._text = "";
    this.attributes = {};
    this.children = [];
    this.handlers = {};
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.value = "";
    this.type = "";
  }
  get textContent() {
    return this._text + this.children.map((c) => c.textContent).join("");
  }
  set textContent(value) {
    this._text = String(value);
    this.children = [];
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this._text = ""; this.children = [...nodes]; }
  addEventListener(type, fn) { (this.handlers[type] ??= []).push(fn); }
  focus() { this.focused = true; }
  async dispatch(type, extra = {}) {
    const event = { type, target: this, preventDefault() { this.prevented = true; }, ...extra };
    for (const fn of this.handlers[type] ?? []) await fn(event);
    return event;
  }
  *walk() { yield this; for (const c of this.children) yield* c.walk(); }
  find(predicate) { return [...this.walk()].filter(predicate); }
}

export const SCREEN_IDS = [
  "discussion-heading", "status", "retry", "start-section", "start-form", "topic", "topic-counter",
  "participants-hint", "participant-list", "rounds", "remaining", "blockers", "form-error", "start",
  "run-section", "run-title", "progress", "stop", "again", "readonly-note", "run-error", "transcript",
];

export function fakeDocument() {
  const nodes = new Map(SCREEN_IDS.map((id) => [id, new FakeNode("div", id)]));
  for (const id of ["start-section", "run-section", "retry", "stop", "again", "readonly-note"]) {
    nodes.get(id).hidden = true;
  }
  return {
    nodes,
    getElementById: (id) => nodes.get(id) ?? null,
    createElement: (tag) => new FakeNode(tag),
  };
}

export const rawConfig = (over = {}) => ({
  enabled: true, max_rounds: 3, default_rounds: 2, per_day_limit: 10, used_today: 1, remaining_today: 9,
  participants_min: 2, participants_max: 4, topic_max_chars: 2000,
  models: [
    { id: "gemini:flash", provider: "gemini", model: "flash", available: true, is_default: true },
    { id: "openai:mini", provider: "openai", model: "mini", available: true, is_default: false },
    { id: "groq:down", provider: "groq", model: "down", available: false, is_default: false },
  ],
  presets: [
    { name: "設計者", role: "設計を示す", stance: "s", checklist: ["a"], model: "gemini:flash", color: "blue" },
    { name: "批判役", role: "弱点を突く", stance: "s", checklist: [], model: "groq:down", color: "orange" },
    { name: "現実担当", role: "現実的に", stance: "s", checklist: [], model: "openai:mini", color: "green" },
  ],
  palette: ["blue"], active: null, ...over,
});

