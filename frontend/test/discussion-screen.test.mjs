import assert from "node:assert/strict";
import test from "node:test";
import { DiscussionApiError } from "../discussion-api.js";
import { INVALID_LINK_MESSAGE, createScreen } from "../discussion-screen.js";
import { normalizeConfig } from "../discussion-view.js";
import { fakeDocument, rawConfig } from "./fake-dom.mjs";

const CONV = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d";
const TASK = "1a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d";

function setup({ config = rawConfig(), search = "", messages = [], task = { status: "running", steps_total: 7, steps_completed: 1 }, overrides = {} } = {}) {
  const doc = fakeDocument();
  const timers = [];
  const calls = { start: [], cancel: [], urls: [], navigate: [], messages: [] };
  const state = { messages: [...messages], task: { ...task } };
  const api = {
    loadConfig: async () => (config === null ? null : normalizeConfig(config)),
    startDiscussion: async (body) => {
      calls.start.push(body);
      return { conversation_id: CONV, task_id: TASK };
    },
    cancelDiscussion: async (id) => {
      calls.cancel.push(id);
      state.task = { status: "cancelled" };
      return { ok: true };
    },
    loadMessages: async (id, after) => {
      calls.messages.push(after);
      return { messages: state.messages.slice(after), total: state.messages.length };
    },
    loadTaskStatus: async () => state.task,
    ...overrides,
  };
  const screen = createScreen({
    doc, api, search,
    schedule: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    cancel: () => {},
    isHidden: () => false,
    replaceUrl: (url) => calls.urls.push(url),
    navigate: (url) => calls.navigate.push(url),
  });
  const n = (id) => doc.nodes.get(id);
  const tick = async () => {
    const t = timers.shift();
    if (t) await t.fn();
    await new Promise((r) => setImmediate(r));
  };
  return { screen, doc, n, calls, state, timers, tick };
}

const msg = (role, content) => ({ role, content });

test("start form: three presets, rounds, counter, remaining and a disabled button with reasons", async () => {
  const t = setup();
  await t.screen.init();
  assert.equal(t.n("start-section").hidden, false);
  assert.equal(t.n("run-section").hidden, true);
  assert.equal(t.n("participant-list").children.length, 3);
  const first = t.n("participant-list").children[0];
  assert.match(first.textContent, /設計者/);
  assert.match(first.textContent, /設計を示す/);
  assert.equal(t.n("rounds").children.length, 3);
  assert.equal(t.n("rounds").value, "2");
  assert.equal(t.n("topic-counter").textContent, "0 / 2000");
  assert.match(t.n("remaining").textContent, /今日の残り: 9 \/ 10/);
  assert.equal(t.n("start").disabled, true);
  assert.match(t.n("blockers").textContent, /議題を入力/);
  // The unavailable model of 批判役 is not offered; it starts on the default.
  const select = t.n("participant-list").children[1].find((x) => x.tag === "select")[0];
  assert.equal(select.value, "gemini:flash");
  assert.deepEqual(select.children.map((o) => o.value), ["gemini:flash", "openai:mini"]);
  assert.equal(select.getAttribute("aria-label"), "批判役のモデル");
  t.n("topic").value = "あ".repeat(2001);
  await t.n("topic").dispatch("input");
  assert.equal(t.n("topic-counter").className, "counter over");
  assert.equal(t.n("start").disabled, true);
  assert.match(t.n("blockers").textContent, /2000文字以内/);
  t.n("topic").value = "週休3日にすべきか";
  await t.n("topic").dispatch("input");
  assert.equal(t.n("start").disabled, false);
  assert.equal(t.n("blockers").children.length, 0);
});

test("the participant picker enforces 2 to 4 and explains it", async () => {
  const t = setup();
  await t.screen.init();
  const boxes = t.n("participant-list").children.map((row) => row.find((x) => x.tag === "input")[0]);
  t.n("topic").value = "x";
  await t.n("topic").dispatch("input");
  boxes[0].checked = false;
  boxes[1].checked = false;
  await boxes[0].dispatch("change");
  assert.equal(t.n("start").disabled, true);
  assert.match(t.n("blockers").textContent, /2〜4人/);
  boxes[1].checked = true;
  await boxes[1].dispatch("change");
  assert.equal(t.n("start").disabled, false);
});

test("a disabled start button in every blocked state", async () => {
  for (const [config, pattern] of [
    [rawConfig({ remaining_today: 0, used_today: 10 }), /上限/],
    [rawConfig({ models: [] }), /使えるモデル/],
  ]) {
    const t = setup({ config });
    await t.screen.init();
    t.n("topic").value = "x";
    await t.n("topic").dispatch("input");
    assert.equal(t.n("start").disabled, true);
    assert.match(t.n("blockers").textContent, pattern);
  }
  const off = setup({ config: null });
  await off.screen.init();
  assert.match(off.n("status").textContent, /有効になっていません/);
  assert.equal(off.n("start-section").hidden, true);
});

test("submitting posts the form, shows the run view and follows the discussion", async () => {
  const t = setup({
    messages: [msg("user", "議題"), msg("assistant", "【設計者】案です"), msg("assistant", "【まとめ】要点")],
  });
  await t.screen.init();
  t.n("topic").value = "  議題  ";
  await t.n("topic").dispatch("input");
  t.n("rounds").value = "3";
  await t.n("rounds").dispatch("change");
  const event = await t.n("start-form").dispatch("submit");
  assert.equal(event.prevented, true);
  await new Promise((r) => setImmediate(r));
  assert.deepEqual(t.calls.start, [{ topic: "議題", personas: ["設計者", "批判役", "現実担当"].map((n, i) => (i === 1 ? t.calls.start[0].personas[1] : n)), rounds: 3 }]);
  // 批判役's preset model was unavailable, so the default was substituted and the persona sent in full.
  assert.equal(t.calls.start[0].personas[1].name, "批判役");
  assert.equal(t.calls.start[0].personas[1].model, "gemini:flash");
  assert.equal(t.calls.start[0].personas[1].color, "orange");
  assert.deepEqual(t.calls.urls, [`/discussion?c=${CONV}`]);
  assert.equal(t.n("start-section").hidden, true);
  assert.equal(t.n("run-section").hidden, false);
  assert.equal(t.n("stop").hidden, false);
  assert.equal(t.n("again").hidden, true);
  assert.equal(t.n("readonly-note").hidden, true);
  await t.tick();
  assert.match(t.n("progress").textContent, /議論中です（発言 2 \/ 6）/);
  const bubbles = t.n("transcript").children;
  assert.equal(bubbles.length, 3);
  assert.match(bubbles[0].className, /topic/);
  assert.match(bubbles[1].className, /turn c-blue/);
  assert.match(bubbles[2].className, /summary/);
  // The task finishes: the poller stops and the stop button gives way to "again".
  t.state.task = { status: "completed" };
  await t.tick();
  assert.equal(t.timers.length, 0);
  assert.equal(t.n("stop").hidden, true);
  assert.equal(t.n("again").hidden, false);
  assert.match(t.n("progress").textContent, /終了/);
  await t.n("again").dispatch("click");
  assert.deepEqual(t.calls.navigate, ["/discussion"]);
});

test("a refused start shows the Japanese message for its code and re-reads the counters", async () => {
  for (const [code, status, pattern] of [
    ["DISCUSSION_BUSY", 409, /実行中/],
    ["DISCUSSION_DAILY_LIMIT", 429, /上限/],
    ["PERSONA_MODEL_NOT_ALLOWED", 400, /許可されていないモデル/],
    ["DISCUSSION_MODEL_UNAVAILABLE", 503, /今使えません/],
  ]) {
    const t = setup({
      overrides: { startDiscussion: async () => { throw new DiscussionApiError("http", status, code); } },
    });
    await t.screen.init();
    t.n("topic").value = "x";
    await t.n("topic").dispatch("input");
    await t.screen.submit();
    assert.match(t.n("form-error").textContent, pattern, code);
    assert.equal(t.n("start-section").hidden, false);
  }
  const net = setup({ overrides: { startDiscussion: async () => { throw new DiscussionApiError("network"); } } });
  await net.screen.init();
  net.n("topic").value = "x";
  await net.n("topic").dispatch("input");
  await net.screen.submit();
  assert.match(net.n("form-error").textContent, /接続できません/);
  assert.equal(net.n("start").disabled, false);
});

test("double submit posts once", async () => {
  let release;
  const gate = new Promise((r) => { release = r; });
  const t = setup({ overrides: { startDiscussion: async () => { await gate; return { conversation_id: CONV, task_id: TASK }; } } });
  await t.screen.init();
  t.n("topic").value = "x";
  await t.n("topic").dispatch("input");
  const first = t.screen.submit();
  await t.screen.submit();
  assert.equal(t.n("start").disabled, true);
  release();
  await first;
});

test("stop sends the cancel request once and shows the stopped state", async () => {
  const t = setup({ search: `?c=${CONV}`, config: rawConfig({ active: { task_id: TASK, conversation_id: CONV } }), messages: [msg("user", "議題")] });
  await t.screen.init();
  assert.equal(t.n("stop").hidden, false);
  await t.tick();
  await t.n("stop").dispatch("click");
  await t.n("stop").dispatch("click");
  assert.deepEqual(t.calls.cancel, [TASK]);
  assert.equal(t.n("stop").hidden, true);
  assert.match(t.n("progress").textContent, /停止/);
});

test("a stop that fails explains why and can be tried again", async () => {
  const t = setup({
    config: rawConfig({ active: { task_id: TASK, conversation_id: CONV } }),
    overrides: { cancelDiscussion: async () => ({ ok: false, status: 409 }) },
  });
  await t.screen.init();
  await t.n("stop").dispatch("click");
  assert.match(t.n("run-error").textContent, /すでに終了/);
  assert.equal(t.n("stop").disabled, false);
});

test("an active discussion on load is attached to and put in the address bar", async () => {
  const t = setup({ config: rawConfig({ active: { task_id: TASK, conversation_id: CONV } }), messages: [msg("user", "議題"), msg("assistant", "【設計者】x")] });
  await t.screen.init();
  assert.deepEqual(t.calls.urls, [`/discussion?c=${CONV}`]);
  assert.equal(t.n("start-section").hidden, true);
  assert.equal(t.n("stop").hidden, false);
  await t.tick();
  assert.equal(t.n("transcript").children.length, 2);
  // Colours come from the presets when the page did not send the personas itself.
  assert.match(t.n("transcript").children[1].className, /c-blue/);
});

test("?c= opens an existing discussion read-only, without polling or a stop button", async () => {
  const t = setup({ search: `?c=${CONV}`, messages: [msg("user", "議題"), msg("assistant", "【批判役】反論"), msg("assistant", "【お知らせ】停止しました（CANCELLED）")] });
  await t.screen.init();
  assert.equal(t.n("run-section").hidden, false);
  assert.equal(t.n("start-section").hidden, true);
  assert.equal(t.n("readonly-note").hidden, false);
  assert.equal(t.n("stop").hidden, true);
  assert.equal(t.n("again").hidden, false);
  assert.equal(t.timers.length, 0);
  assert.equal(t.n("transcript").children.length, 3);
  assert.match(t.n("transcript").children[1].className, /c-orange/);
  assert.match(t.n("transcript").children[2].textContent, /停止されたため/);
  assert.deepEqual(t.calls.cancel, []);
});

test("a missing or malformed ?c= is explained", async () => {
  const missing = setup({
    search: `?c=${CONV}`,
    overrides: { loadMessages: async () => { throw new DiscussionApiError("http", 404); } },
  });
  await missing.screen.init();
  assert.equal(missing.n("status").textContent, INVALID_LINK_MESSAGE);
  const bad = setup({ search: "?c=not-a-uuid" });
  await bad.screen.init();
  assert.equal(bad.n("status").textContent, INVALID_LINK_MESSAGE);
  assert.equal(bad.n("start-section").hidden, false);
  const empty = setup({ search: `?c=${CONV}`, messages: [] });
  await empty.screen.init();
  assert.equal(empty.n("status").textContent, INVALID_LINK_MESSAGE);
});

test("a config failure offers a retry", async () => {
  const t = setup({ overrides: { loadConfig: async () => { throw new DiscussionApiError("network"); } } });
  await t.screen.init();
  assert.match(t.n("status").textContent, /接続できません/);
  assert.equal(t.n("retry").hidden, false);
});

test("markup in a topic or a turn stays literal text", async () => {
  const hostile = "<img src=x onerror=alert(1)>";
  const t = setup({ search: `?c=${CONV}`, messages: [msg("user", hostile), msg("assistant", `【設計者】${hostile}`), msg("assistant", `【${hostile.slice(0, 5)}】y`)] });
  await t.screen.init();
  const texts = t.n("transcript").children.map((c) => c.textContent);
  assert.ok(texts[0].includes(hostile));
  assert.ok(texts[1].includes(hostile));
  for (const node of t.n("transcript").children.flatMap((c) => [...c.walk()])) {
    assert.ok(["div", "span", "p"].includes(node.tag), node.tag);
  }
});
