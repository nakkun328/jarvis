import assert from "node:assert/strict";
import test from "node:test";
import {
  IDLE_MS, MAX_QUEUED_CHARS, NOTICES, createVoice, recordLatency, summarizeLatency, supportOf,
} from "../voice.js";

function fakeEnv({ speak = true, listen = true } = {}) {
  const log = { spoken: [], cancels: 0, recognitions: [] };
  const env = {};
  if (speak) {
    env.SpeechSynthesisUtterance = class {
      constructor(text) { this.text = text; }
    };
    env.speechSynthesis = {
      speak(utterance) { log.spoken.push(utterance); },
      cancel() { log.cancels += 1; log.spoken.length = 0; },
    };
  }
  if (listen) {
    env.SpeechRecognition = class {
      constructor() { log.recognitions.push(this); }
      start() { this.started = true; }
      stop() { this.onend?.(); }
    };
  }
  return { env, log };
}

function harness(options = {}) {
  const { env, log } = fakeEnv(options);
  const clock = { t: 0, timers: [] };
  const notices = [];
  const first = [];
  const voice = createVoice({
    env,
    now: () => clock.t,
    setTimer: (fn, ms) => { clock.timers.push({ fn, ms }); return clock.timers.length; },
    clearTimer: (id) => { if (clock.timers[id - 1]) clock.timers[id - 1].fn = null; },
    onNotice: (text) => notices.push(text),
    onFirstSound: (ms) => first.push(ms),
  });
  const fire = () => { for (const timer of clock.timers.splice(0)) timer.fn?.(); };
  return { voice, log, clock, notices, first, fire };
}

const start = (utterance) => utterance.onstart?.();
const end = (utterance) => utterance.onend?.();

test("support is detected from the browser objects", () => {
  assert.deepEqual(supportOf({}), { listen: false, speak: false });
  assert.deepEqual(supportOf(fakeEnv().env), { listen: true, speak: true });
});

test("speech starts at the first sentence end, not at the end of the reply", () => {
  const h = harness();
  h.voice.setSpeaking(true);
  h.voice.beginTurn();
  h.clock.t = 100;
  h.voice.feed("こんにちは。今日は");
  assert.deepEqual(h.log.spoken.map((u) => u.text), ["こんにちは。"]);
  h.clock.t = 350;
  start(h.log.spoken[0]);
  assert.deepEqual(h.first, [350]);
  end(h.log.spoken[0]);
  h.voice.finish("こんにちは。今日はいい天気。");
  assert.equal(h.log.spoken.at(-1).text, "今日はいい天気。");
});

test("a long first sentence is spoken after the idle timeout", () => {
  const h = harness();
  h.voice.setSpeaking(true);
  h.voice.beginTurn();
  h.voice.feed("ええと");
  assert.equal(h.log.spoken.length, 0);
  assert.equal(h.clock.timers[0].ms, IDLE_MS);
  h.fire();
  assert.deepEqual(h.log.spoken.map((u) => u.text), ["ええと"]);
});

test("chunks are spoken one at a time, in order", () => {
  const h = harness();
  h.voice.setSpeaking(true);
  h.voice.beginTurn();
  h.voice.feed("一つ目。二つ目。三つ目。");
  assert.deepEqual(h.log.spoken.map((u) => u.text), ["一つ目。"]);
  end(h.log.spoken[0]);
  assert.deepEqual(h.log.spoken.map((u) => u.text), ["一つ目。", "二つ目。"]);
});

test("cancel clears the timer, the queue and the engine, and late events are ignored", () => {
  const h = harness();
  h.voice.setSpeaking(true);
  h.voice.beginTurn();
  h.voice.feed("一つ目。二つ目。");
  const [pending] = h.log.spoken;
  h.voice.cancel();
  assert.equal(h.log.cancels > 0, true);
  assert.equal(h.log.spoken.length, 0);
  end(pending);
  h.fire();
  assert.equal(h.log.spoken.length, 0);
});

test("a new turn cancels the previous speech", () => {
  const h = harness();
  h.voice.setSpeaking(true);
  h.voice.beginTurn();
  h.voice.feed("前の返事。");
  const cancels = h.log.cancels;
  h.voice.beginTurn();
  assert.ok(h.log.cancels > cancels);
});

test("nothing is spoken while read-aloud is off", () => {
  const h = harness();
  h.voice.beginTurn();
  h.voice.feed("読まない。");
  h.voice.finish("読まない。");
  assert.equal(h.log.spoken.length, 0);
});

test("read-aloud cannot be switched on without a speech engine", () => {
  const h = harness({ speak: false });
  assert.equal(h.voice.setSpeaking(true), false);
  assert.deepEqual(h.notices, [NOTICES.noSpeak]);
});

test("too much queued speech degrades to text only, with a notice, and never throws text away", () => {
  const h = harness();
  h.voice.setSpeaking(true);
  h.voice.beginTurn();
  const sentence = `${"あ".repeat(70)}。`;
  let full = "";
  for (let i = 0; i < Math.ceil(MAX_QUEUED_CHARS / 70) + 3; i += 1) {
    full += sentence;
    h.voice.feed(full);
  }
  assert.deepEqual(h.notices, [NOTICES.behind]);
  const spokenChars = h.log.spoken.length;
  h.voice.finish(full);
  assert.equal(h.log.spoken.length, spokenChars);
});

test("a speech engine error switches to text only once", () => {
  const h = harness();
  h.voice.setSpeaking(true);
  h.voice.beginTurn();
  h.voice.feed("一つ目。二つ目。");
  h.log.spoken[0].onerror();
  h.voice.feed("一つ目。二つ目。三つ目。");
  assert.deepEqual(h.notices, [NOTICES.speakFailed]);
  assert.equal(h.log.spoken.length, 1);
});

test("push-to-talk cancels playback, reports text and never sends it", () => {
  const h = harness();
  h.voice.setSpeaking(true);
  h.voice.beginTurn();
  h.voice.feed("話し中。");
  const texts = [];
  assert.equal(h.voice.startListening((text, final) => texts.push([text, final])), true);
  assert.equal(h.log.spoken.length, 0);
  const [recognition] = h.log.recognitions;
  assert.equal(recognition.lang, "ja-JP");
  recognition.onresult({ results: [Object.assign([{ transcript: "天気は" }], { isFinal: false })] });
  recognition.onresult({ results: [Object.assign([{ transcript: "天気は？" }], { isFinal: true })] });
  assert.deepEqual(texts, [["天気は", false], ["天気は？", true]]);
  h.voice.stopListening();
  assert.equal(h.voice.listening, false);
  assert.deepEqual(h.notices, []);
});

test("listening without a recogniser, a denied microphone and silence give fixed notices", () => {
  const none = harness({ listen: false });
  assert.equal(none.voice.startListening(() => {}), false);
  assert.deepEqual(none.notices, [NOTICES.noListen]);

  const denied = harness();
  denied.voice.startListening(() => {});
  denied.log.recognitions[0].onerror({ error: "not-allowed" });
  denied.log.recognitions[0].onend();
  assert.deepEqual(denied.notices, [NOTICES.micDenied]);

  const silent = harness();
  silent.voice.startListening(() => {});
  silent.voice.stopListening();
  assert.deepEqual(silent.notices, [NOTICES.noSpeech]);
});

test("first-sound times are kept as numbers only, newest 20, with median and p95", () => {
  const store = new Map();
  const storage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v) };
  let list = [];
  for (let i = 1; i <= 25; i += 1) list = recordLatency(storage, i * 10);
  assert.equal(list.length, 20);
  assert.equal(list[0], 60);
  assert.deepEqual(summarizeLatency([100, 200, 300, 400, 1000]), { count: 5, median: 300, p95: 1000 });
  assert.equal(summarizeLatency([]), null);
  assert.deepEqual(recordLatency(storage, Number.NaN), []);
  assert.deepEqual(recordLatency(null, 5), [5]);
});
