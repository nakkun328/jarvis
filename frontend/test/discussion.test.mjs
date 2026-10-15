import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import {
  DiscussionApiError, cancelDiscussion, cancelUrl, loadConfig, loadMessages, loadTaskStatus,
  messagesUrl, startDiscussion,
} from "../discussion-api.js";
import { createDiscussionPoller } from "../discussion-poll.js";
import { messageNode } from "../discussion-render.js";
import {
  FEATURE_OFF_MESSAGE, GENERIC_ERROR, NETWORK_ERROR, buildStartBody, cancelErrorMessage, colorMap,
  errorMessage, initialModel, messageViewModel, noticeCode, noticeMeaning, normalizeConfig,
  parseMessage, progressText, startBlockers, topicLength,
} from "../discussion-view.js";
import { FakeNode, rawConfig } from "./fake-dom.mjs";

const CONV = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d";
const TASK = "1a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d";

const reply = (status, body) => ({ status, ok: status >= 200 && status < 300, json: async () => body });

// ----- parsing: the same rules as backend/discussion/format.py -----

test("parseMessage splits the 【name】 prefix like the backend", () => {
  assert.deepEqual(parseMessage("【設計者】こんにちは"), { speaker: "設計者", text: "こんにちは" });
  assert.deepEqual(parseMessage("【まとめ】要点"), { speaker: "まとめ", text: "要点" });
  assert.deepEqual(parseMessage("議題です"), { speaker: null, text: "議題です" });
  assert.deepEqual(parseMessage("前置き【設計者】x"), { speaker: null, text: "前置き【設計者】x" });
  assert.deepEqual(parseMessage("【】x"), { speaker: null, text: "【】x" });
  assert.deepEqual(parseMessage("【あ\nい】x"), { speaker: null, text: "【あ\nい】x" });
  assert.deepEqual(parseMessage("【【あ】】x"), { speaker: null, text: "【【あ】】x" });
  const twenty = "あ".repeat(20);
  assert.equal(parseMessage(`【${twenty}】t`).speaker, twenty);
  assert.equal(parseMessage(`【${twenty}あ】t`).speaker, null);
  assert.deepEqual(parseMessage("【A】【B】t"), { speaker: "A", text: "【B】t" });
  assert.deepEqual(parseMessage(42), { speaker: null, text: "" });
});

test("noticeCode reads only a trailing full-width-parenthesised code", () => {
  assert.equal(noticeCode("批判役を飛ばしました（PERSONA_SKIPPED）"), "PERSONA_SKIPPED");
  assert.equal(noticeCode("停止（CANCELLED）"), "CANCELLED");
  assert.equal(noticeCode("（CANCELLED）あと"), null);
  assert.equal(noticeCode("(CANCELLED)"), null);
  assert.equal(noticeCode("（cancelled）"), null);
  assert.equal(noticeCode("（A）"), null);
  assert.equal(noticeCode("（CANCELLED）\n"), null);
  assert.equal(noticeCode(null), null);
});

test("every notice code has a readable Japanese meaning and unknown ones a fallback", () => {
  const codes = [
    "PERSONA_SKIPPED", "PROVIDER_FAILURES", "CALL_CAP", "SUMMARY_FAILED", "CONVERGED", "CANCELLED",
    "INTERRUPTED", "EXECUTION_FAILED", "TIMEOUT", "INTERNAL_ERROR", "VERIFICATION_FAILED",
  ];
  const meanings = codes.map(noticeMeaning);
  assert.equal(new Set(meanings).size, codes.length);
  for (const m of meanings) assert.match(m, /[ぁ-ん]/);
  assert.match(noticeMeaning("SOMETHING_NEW"), /お知らせ/);
  assert.match(noticeMeaning("constructor"), /お知らせ/);
  assert.match(noticeMeaning(null), /お知らせ/);
});

test("messageViewModel classifies topic, turn, summary, notice and unknown speakers", () => {
  const colors = colorMap([{ name: "設計者", color: "blue" }]);
  assert.deepEqual(messageViewModel({ role: "user", content: "議題" }, colors),
    { kind: "topic", speaker: null, text: "議題", color: "neutral" });
  // The user's own text is never split, even if it starts with a bracket.
  assert.equal(messageViewModel({ role: "user", content: "【設計者】x" }, colors).text, "【設計者】x");
  assert.deepEqual(messageViewModel({ role: "assistant", content: "【設計者】案" }, colors),
    { kind: "turn", speaker: "設計者", text: "案", color: "blue" });
  assert.equal(messageViewModel({ role: "assistant", content: "【知らない人】案" }, colors).color, "neutral");
  assert.equal(messageViewModel({ role: "assistant", content: "【まとめ】要点" }, colors).kind, "summary");
  const notice = messageViewModel({ role: "assistant", content: "【お知らせ】停止しました（CANCELLED）" }, colors);
  assert.equal(notice.kind, "notice");
  assert.equal(notice.code, "CANCELLED");
  assert.equal(notice.text, "停止しました");
  assert.equal(notice.meaning, noticeMeaning("CANCELLED"));
  assert.equal(messageViewModel({ role: "assistant", content: "plain" }, colors).kind, "other");
});

test("colours come from the personas the page sent, with a neutral fallback", () => {
  const map = colorMap([{ name: "A", color: "red" }], [{ name: "A", color: "blue" }, { name: "B", color: "teal" }]);
  assert.equal(map.get("A"), "red");
  assert.equal(map.get("B"), "teal");
  const vm = messageViewModel({ role: "assistant", content: "【C】x" }, map);
  assert.equal(vm.color, "neutral");
  assert.equal(messageViewModel({ role: "assistant", content: "【D】x" }, colorMap([{ name: "D", color: "javascript:x" }])).color, "neutral");
});

// ----- error mapping -----

test("every discussion error code maps to a distinct Japanese message", async () => {
  const source = await readFile(new URL("../../backend/discussion/errors.py", import.meta.url), "utf8");
  const codes = [...source.matchAll(/= "([A-Z_]+)"/g)].map((m) => m[1]);
  assert.ok(codes.length >= 20);
  const seen = new Set();
  for (const code of codes) {
    const message = errorMessage(code);
    assert.notEqual(message, GENERIC_ERROR, code);
    assert.match(message, /[ぁ-ん]/, code);
    seen.add(message);
  }
  assert.equal(seen.size, codes.length);
  assert.match(errorMessage("DISCUSSION_BUSY"), /実行中/);
  assert.match(errorMessage("DISCUSSION_DAILY_LIMIT"), /上限/);
});

test("transport and status fallbacks are readable and never echo server text", () => {
  assert.equal(errorMessage("network"), NETWORK_ERROR);
  assert.equal(errorMessage(null, 404), FEATURE_OFF_MESSAGE);
  assert.match(errorMessage(null, 429), /上限/);
  assert.match(errorMessage(null, 409), /実行中/);
  assert.match(errorMessage(null, 401), /ログイン/);
  assert.equal(errorMessage("<img src=x onerror=alert(1)>", 500), GENERIC_ERROR);
  assert.equal(errorMessage("constructor", 500), GENERIC_ERROR);
  assert.match(cancelErrorMessage(409), /終了/);
  assert.equal(cancelErrorMessage("network"), NETWORK_ERROR);
  assert.match(cancelErrorMessage(500), /停止できません/);
});

// ----- config and the start form -----

test("normalizeConfig keeps what the screen reads and rejects a non-config", () => {
  assert.equal(normalizeConfig({ enabled: false }), null);
  assert.equal(normalizeConfig(null), null);
  const config = normalizeConfig(rawConfig({ active: { task_id: TASK, conversation_id: CONV } }));
  assert.equal(config.presets.length, 3);
  assert.equal(config.active.conversation_id, CONV);
  assert.equal(normalizeConfig(rawConfig({ active: { task_id: TASK, conversation_id: "bad" } })).active, null);
  assert.equal(normalizeConfig(rawConfig()).presets[0].color, "blue");
  const odd = normalizeConfig(rawConfig({ presets: [{ name: "x", model: "m", color: "pink" }, 5] }));
  assert.equal(odd.presets.length, 1);
  assert.equal(odd.presets[0].color, "neutral");
});

test("a preset whose model is unavailable falls back to the default available one", () => {
  const config = normalizeConfig(rawConfig());
  assert.equal(initialModel(config.presets[0], config), "gemini:flash");
  assert.equal(initialModel(config.presets[1], config), "gemini:flash");
  assert.equal(initialModel(config.presets[2], config), "openai:mini");
});

test("startBlockers explains every reason the button is disabled", () => {
  const config = normalizeConfig(rawConfig());
  const good = { topic: "議題", participants: [{ name: "a" }, { name: "b" }], rounds: 2 };
  assert.deepEqual(startBlockers(config, good), []);
  assert.deepEqual(startBlockers(null, good), [FEATURE_OFF_MESSAGE]);
  assert.match(startBlockers(config, { ...good, topic: "  " }).join(), /議題を入力/);
  assert.match(startBlockers(config, { ...good, topic: "あ".repeat(2001) }).join(), /2000文字以内/);
  assert.deepEqual(startBlockers(config, { ...good, topic: "あ".repeat(2000) }), []);
  assert.match(startBlockers(config, { ...good, participants: [{ name: "a" }] }).join(), /2〜4人/);
  assert.match(startBlockers(config, { ...good, participants: Array(5).fill({ name: "a" }) }).join(), /2〜4人/);
  assert.match(startBlockers(config, { ...good, rounds: 4 }).join(), /ラウンド/);
  assert.match(startBlockers({ ...config, remaining_today: 0 }, good).join(), /上限/);
  assert.match(startBlockers({ ...config, active: { task_id: TASK, conversation_id: CONV } }, good).join(), /実行中/);
  assert.match(startBlockers({ ...config, models: [] }, good).join(), /使えるモデル/);
  assert.equal(topicLength("😀😀"), 2);
});

test("buildStartBody sends a preset by name unless its model was changed", () => {
  const config = normalizeConfig(rawConfig());
  const body = buildStartBody(config, {
    topic: " 議題 ",
    participants: [
      { name: "設計者", model: "gemini:flash", color: "blue" },
      { name: "現実担当", model: "gemini:flash", color: "green" },
    ],
    rounds: 3,
  });
  assert.equal(body.topic, "議題");
  assert.equal(body.rounds, 3);
  assert.equal(body.personas[0], "設計者");
  assert.deepEqual(body.personas[1], {
    name: "現実担当", role: "現実的に", stance: "s", checklist: [], model: "gemini:flash", color: "green",
  });
});

// ----- requests (fake fetch) -----

test("start sends the confirm header and a JSON body, and returns the ids", async () => {
  const calls = [];
  const fetchImpl = async (url, init) => { calls.push({ url, init }); return reply(201, { conversation_id: CONV, task_id: TASK }); };
  const result = await startDiscussion({ topic: "t", personas: ["a", "b"], rounds: 2 }, { fetchImpl });
  assert.deepEqual(result, { conversation_id: CONV, task_id: TASK });
  assert.equal(calls[0].url, "/api/discussions");
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.headers["X-Jarvis-Confirm"], "1");
  assert.equal(calls[0].init.headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(calls[0].init.body), { topic: "t", personas: ["a", "b"], rounds: 2 });
});

test("a refused start surfaces the fixed code", async () => {
  for (const [status, code] of [[409, "DISCUSSION_BUSY"], [429, "DISCUSSION_DAILY_LIMIT"], [400, "PERSONA_MODEL_NOT_ALLOWED"], [503, "DISCUSSION_MODEL_UNAVAILABLE"]]) {
    await assert.rejects(
      startDiscussion({}, { fetchImpl: async () => reply(status, { detail: code }) }),
      (e) => e instanceof DiscussionApiError && e.status === status && e.code === code,
    );
  }
  await assert.rejects(startDiscussion({}, { fetchImpl: async () => { throw new TypeError("x"); } }), (e) => e.kind === "network");
  await assert.rejects(startDiscussion({}, { fetchImpl: async () => reply(201, { conversation_id: "no" }) }), (e) => e.kind === "format");
});

test("cancel posts with the confirm header and reports the outcome", async () => {
  const calls = [];
  const ok = await cancelDiscussion(TASK, { fetchImpl: async (url, init) => { calls.push({ url, init }); return reply(200, { id: TASK, status: "cancelled" }); } });
  assert.deepEqual(ok, { ok: true });
  assert.equal(calls[0].url, cancelUrl(TASK));
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.headers["X-Jarvis-Confirm"], "1");
  assert.equal(calls[0].init.body, undefined);
  assert.deepEqual(await cancelDiscussion(TASK, { fetchImpl: async () => reply(409, { detail: "x" }) }), { ok: false, status: 409 });
  assert.deepEqual(await cancelDiscussion(TASK, { fetchImpl: async () => { throw new Error("x"); } }), { ok: false, status: "network" });
  assert.deepEqual(await cancelDiscussion(TASK, { fetchImpl: async () => ({ status: 200, ok: true, json: async () => { throw new Error("empty"); } }) }), { ok: true });
});

test("config: a 404 means the feature is off, other failures throw", async () => {
  const urls = [];
  const config = await loadConfig({ fetchImpl: async (url) => { urls.push(url); return reply(200, rawConfig()); } });
  assert.equal(urls[0], "/api/discussions/config");
  assert.equal(config.max_rounds, 3);
  assert.equal(await loadConfig({ fetchImpl: async () => reply(404, { detail: "Not Found" }) }), null);
  await assert.rejects(loadConfig({ fetchImpl: async () => reply(503, { detail: "DISCUSSION_STORAGE_UNAVAILABLE" }) }), (e) => e.status === 503);
  await assert.rejects(loadConfig({ fetchImpl: async () => reply(200, { enabled: false }) }), (e) => e.kind === "format");
});

test("messages are read with ?after= and follow has_more", async () => {
  const urls = [];
  const pages = [
    { messages: [{ role: "user", content: "a" }, { role: "assistant", content: "b" }], has_more: true, total: 3 },
    { messages: [{ role: "assistant", content: "c" }], has_more: false, total: 3 },
  ];
  const fetchImpl = async (url) => { urls.push(url); return reply(200, pages.shift()); };
  const result = await loadMessages(CONV, 0, { fetchImpl });
  assert.deepEqual(urls, [messagesUrl(CONV, 0), messagesUrl(CONV, 2)]);
  assert.equal(result.messages.length, 3);
  assert.equal(result.total, 3);
  await assert.rejects(loadMessages(CONV, 0, { fetchImpl: async () => reply(200, { messages: [] }) }), (e) => e.kind === "format");
  const status = await loadTaskStatus(TASK, { fetchImpl: async () => reply(200, { status: "running", steps_total: 4, steps_completed: 1, goal: "secret" }) });
  assert.deepEqual(status, { status: "running", steps_total: 4, steps_completed: 1 });
});

// ----- progress text -----

test("progress text comes from the task status", () => {
  assert.match(progressText({ status: "pending" }), /順番待ち/);
  assert.match(progressText({ status: "running", steps_total: 7, steps_completed: 0 }), /発言 1 \/ 6/);
  assert.match(progressText({ status: "running", steps_total: 7, steps_completed: 3 }), /発言 4 \/ 6/);
  assert.match(progressText({ status: "running", steps_total: 7, steps_completed: 6 }), /まとめ/);
  assert.match(progressText({ status: "completed" }), /終了/);
  assert.match(progressText({ status: "cancelled" }), /停止/);
  assert.match(progressText({ status: "failed" }), /失敗/);
  assert.match(progressText({ status: "weird" }), /確認/);
  assert.match(progressText(null), /確認/);
});

// ----- polling -----

function pollHarness({ tasks, pages, hidden = false, maxPolls = 50 }) {
  const timers = [];
  const out = { messages: [], statuses: [], finished: [], errors: [], gaveUp: 0, loads: [] };
  const poller = createDiscussionPoller({
    conversationId: CONV,
    taskId: TASK,
    api: {
      loadTaskStatus: async () => {
        const next = tasks.shift();
        if (next instanceof Error) throw next;
        return next;
      },
      loadMessages: async (id, after) => {
        out.loads.push(after);
        return pages.shift() ?? { messages: [], total: after };
      },
    },
    callbacks: {
      onMessages: (m) => out.messages.push(...m),
      onStatus: (t) => out.statuses.push(t.status),
      onFinished: (t) => out.finished.push(t.status),
      onError: (e) => out.errors.push(e),
      onGiveUp: () => { out.gaveUp += 1; },
    },
    schedule: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    cancel: () => {},
    isHidden: () => hidden,
    maxPolls,
  });
  const fire = async () => {
    const t = timers.shift();
    if (t) await t.fn();
    await new Promise((r) => setImmediate(r));
  };
  return { poller, out, timers, fire };
}

test("polls about every 2 s while running, then stops after the final read", async () => {
  const h = pollHarness({
    tasks: [{ status: "pending" }, { status: "running" }, { status: "completed" }],
    pages: [
      { messages: [{ role: "user", content: "t" }], total: 1 },
      { messages: [{ role: "assistant", content: "【A】x" }], total: 2 },
      { messages: [{ role: "assistant", content: "【まとめ】y" }], total: 3 },
    ],
  });
  h.poller.start();
  assert.equal(h.timers.length, 1);
  await h.fire();
  assert.equal(h.timers[0].ms, 2000);
  await h.fire();
  assert.equal(h.timers.length, 1);
  await h.fire();
  assert.equal(h.timers.length, 0);
  assert.deepEqual(h.out.loads, [0, 1, 2]);
  assert.deepEqual(h.out.statuses, ["pending", "running", "completed"]);
  assert.deepEqual(h.out.finished, ["completed"]);
  assert.equal(h.out.messages.length, 3);
  assert.equal(h.poller.known, 3);
  assert.equal(h.poller.stopped, true);
});

test("a failed poll is reported and retried, stop() ends it, hidden tabs do not fetch", async () => {
  const h = pollHarness({ tasks: [new DiscussionApiError("network"), { status: "running" }], pages: [] });
  h.poller.start();
  await h.fire();
  assert.equal(h.out.errors.length, 1);
  assert.equal(h.out.errors[0].kind, "network");
  assert.ok(h.timers.length === 1);
  await h.fire();
  assert.deepEqual(h.out.statuses, ["running"]);
  h.poller.stop();
  assert.equal(h.poller.stopped, true);
  await h.fire();
  assert.equal(h.out.loads.length, 2);

  const hidden = pollHarness({ tasks: [{ status: "running" }], pages: [], hidden: true });
  hidden.poller.start();
  await hidden.fire();
  assert.deepEqual(hidden.out.loads, []);
  assert.equal(hidden.timers.length, 1);
});

test("polling gives up after the bounded number of polls", async () => {
  const h = pollHarness({ tasks: Array(5).fill({ status: "running" }), pages: [], maxPolls: 2 });
  h.poller.start();
  await h.fire();
  await h.fire();
  assert.equal(h.out.gaveUp, 1);
  assert.equal(h.timers.length, 0);
});

// ----- rendering: text nodes only -----

const doc = { createElement: (tag) => new FakeNode(tag) };

test("bubbles show the speaker and text as plain text, summary framed, notices muted", () => {
  const colors = colorMap([{ name: "設計者", color: "blue" }]);
  const turn = messageNode(doc, messageViewModel({ role: "assistant", content: "【設計者】<b>強い</b>" }, colors));
  assert.match(turn.className, /bubble turn c-blue/);
  assert.equal(turn.find((n) => n.className === "speaker")[0].textContent, "設計者");
  assert.equal(turn.find((n) => n.className === "bubble-text")[0].textContent, "<b>強い</b>");
  assert.equal(turn.find((n) => n.className.startsWith("chip"))[0].getAttribute("aria-hidden"), "true");
  const summary = messageNode(doc, messageViewModel({ role: "assistant", content: "【まとめ】要点" }, colors));
  assert.match(summary.className, /summary/);
  assert.equal(summary.getAttribute("aria-label"), "まとめ");
  const notice = messageNode(doc, messageViewModel({ role: "assistant", content: "【お知らせ】飛ばしました（PERSONA_SKIPPED）" }, colors));
  assert.equal(notice.className, "notice");
  assert.match(notice.textContent, /お知らせ: .*飛ばして/);
  assert.match(notice.textContent, /飛ばしました/);
  const topic = messageNode(doc, messageViewModel({ role: "user", content: "議題" }, colors));
  assert.match(topic.className, /topic/);
  assert.equal(topic.find((n) => n.className === "speaker")[0].textContent, "議題");
});

test("no discussion module or page uses innerHTML or other markup parsing", async () => {
  const forbidden = /innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(|new Function|DOMParser|createContextualFragment/;
  for (const name of [
    "discussion.js", "discussion-api.js", "discussion-poll.js", "discussion-render.js",
    "discussion-screen.js", "discussion-view.js",
  ]) {
    const source = await readFile(new URL(`../${name}`, import.meta.url), "utf8");
    assert.doesNotMatch(source, forbidden, name);
  }
});
