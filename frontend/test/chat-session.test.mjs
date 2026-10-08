import assert from "node:assert/strict";
import test from "node:test";
import { ChatError } from "../chat-api.js";
import { ChatSession, MAX_MESSAGE_LENGTH } from "../chat-session.js";

function fakeView() {
  const log = { statuses: [], turns: [], busy: [], cleared: 0 };
  return {
    log,
    setStatus: (text, error = false) => log.statuses.push({ text, error }),
    clearInput: () => { log.cleared += 1; },
    setBusy: (value) => log.busy.push(value),
    beginTurn(text) {
      const turn = {
        text, shown: [], completed: null, failed: null, resets: 0, retryCleared: 0,
        showText(full) { this.shown.push(full); },
        complete(result) { this.completed = result; },
        fail(info) { this.failed = info; },
        reset() { this.resets += 1; this.failed = null; this.shown = []; },
        clearRetry() { this.retryCleared += 1; },
      };
      log.turns.push(turn);
      return turn;
    },
  };
}

function deferred() {
  let resolve; let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
const ok = (id) => ({ conversation_id: id, provider: "fake", model: "m" });

test("successful turn streams text, stores the conversation id, and reuses it", async () => {
  const view = fakeView();
  const bodies = [];
  const session = new ChatSession({
    view,
    async send({ message, conversationId, onDelta }) {
      bodies.push({ message, conversationId });
      onDelta("こん"); onDelta("にちは");
      return ok("conv-1");
    },
  });
  assert.equal(session.submit("  one  "), "sent");
  await settle();
  assert.equal(session.submit("two"), "sent");
  await settle();
  assert.deepEqual(bodies, [
    { message: "one", conversationId: null },
    { message: "two", conversationId: "conv-1" },
  ]);
  assert.deepEqual(view.log.turns[0].shown, ["こん", "こんにちは"]);
  assert.equal(view.log.turns[0].completed.conversation_id, "conv-1");
  assert.equal(view.log.turns[0].failed, null);
  assert.equal(session.busy, false);
});

test("double submit while a request is in flight is rejected and sends nothing", async () => {
  const view = fakeView();
  const gate = deferred();
  let calls = 0;
  const session = new ChatSession({ view, send: async ({ onDelta }) => { calls += 1; onDelta("r"); return gate.promise; } });
  assert.equal(session.submit("first"), "sent");
  assert.equal(session.submit("second"), "busy");
  assert.equal(session.submit("third"), "busy");
  assert.equal(calls, 1);
  assert.equal(view.log.turns.length, 1);
  assert.equal(view.log.cleared, 1);
  assert.match(view.log.statuses.at(-1).text, /応答中/);
  gate.resolve(ok("c"));
  await settle();
  assert.equal(session.busy, false);
  assert.equal(session.submit("after"), "sent");
});

test("empty, whitespace-only, and over-limit messages are refused with feedback", () => {
  const view = fakeView();
  let calls = 0;
  const session = new ChatSession({ view, send: async () => { calls += 1; return ok("c"); } });
  assert.equal(session.submit(""), "empty");
  assert.equal(session.submit(" \n\t "), "empty");
  assert.equal(session.submit("あ".repeat(MAX_MESSAGE_LENGTH + 1)), "too-long");
  assert.equal(calls, 0);
  assert.equal(view.log.cleared, 0);
  assert.ok(view.log.statuses.every((status) => status.error));
  // Exactly the limit is accepted; astral characters count once, like the backend.
  assert.equal(session.submit("😀".repeat(MAX_MESSAGE_LENGTH)), "sent");
});

test("failure after partial text is shown as unsaved, keeps conversation id, and retry resends", async () => {
  const view = fakeView();
  const calls = [];
  let attempt = 0;
  const session = new ChatSession({
    view,
    async send({ message, conversationId, onDelta }) {
      calls.push({ message, conversationId });
      attempt += 1;
      onDelta("途中");
      if (attempt === 2) throw new ChatError("provider down", { kind: "provider", retryable: true });
      return ok("conv-1");
    },
  });
  session.submit("first");
  await settle();
  session.submit("second");
  await settle();
  const failedTurn = view.log.turns[1];
  assert.equal(failedTurn.completed, null);
  assert.deepEqual(
    { ...failedTurn.failed },
    { message: "provider down", partial: "途中", kind: "provider", retryable: true },
  );
  assert.equal(session.conversationId, "conv-1");
  assert.equal(view.log.busy.at(-1), false);

  assert.equal(session.retry(), true);
  await settle();
  assert.equal(failedTurn.resets, 1);
  assert.equal(view.log.turns.length, 2, "retry reuses the same turn");
  assert.deepEqual(calls.at(-1), { message: "second", conversationId: "conv-1" });
  assert.equal(failedTurn.completed.conversation_id, "conv-1");
  assert.equal(session.retry(), false, "nothing left to retry");
});

test("failure before the first delta has no partial text and the input stays usable", async () => {
  const view = fakeView();
  const session = new ChatSession({
    view,
    send: async () => { throw new ChatError("down", { kind: "provider", retryable: true }); },
  });
  session.submit("hello");
  await settle();
  assert.equal(view.log.turns[0].failed.partial, "");
  assert.equal(session.busy, false);
  assert.equal(session.conversationId, null);
  assert.equal(session.submit("again"), "sent");
});

test("a new submit removes the stale retry button of an earlier failure", async () => {
  const view = fakeView();
  let fail = true;
  const session = new ChatSession({
    view,
    async send({ onDelta }) {
      if (fail) throw new ChatError("down", { kind: "provider", retryable: true });
      onDelta("reply");
      return ok("c");
    },
  });
  session.submit("one");
  await settle();
  fail = false;
  session.submit("two");
  await settle();
  assert.equal(view.log.turns[0].retryCleared, 1);
  assert.equal(session.retry(), false);
});

test("a missing conversation id is an error: not retryable and never replaced by a new conversation", async () => {
  const view = fakeView();
  const sent = [];
  let expired = false;
  const session = new ChatSession({
    view,
    async send({ conversationId, onDelta }) {
      sent.push(conversationId);
      if (expired) throw new ChatError("gone", { kind: "conversation-missing", retryable: false });
      onDelta("reply");
      return ok("conv-1");
    },
  });
  session.submit("one");
  await settle();
  expired = true;
  session.submit("two");
  await settle();
  session.submit("three");
  await settle();
  assert.deepEqual(sent, [null, "conv-1", "conv-1"]);
  assert.equal(session.conversationId, "conv-1");
  assert.equal(view.log.turns[1].failed.kind, "conversation-missing");
  assert.equal(view.log.turns[1].failed.retryable, false);
  assert.equal(session.retry(), false);
  // Only an explicit reset (the "new conversation" button) starts over.
  assert.equal(session.reset(), true);
  assert.equal(session.conversationId, null);
});

test("stop aborts the request, shows an unsaved partial, and allows retry", async () => {
  const view = fakeView();
  let signal;
  const session = new ChatSession({
    view,
    send({ signal: received, onDelta }) {
      signal = received;
      onDelta("半分");
      return new Promise((_resolve, reject) => {
        received.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
      });
    },
  });
  session.submit("slow");
  await settle();
  assert.equal(session.busy, true);
  assert.equal(session.stop(), true);
  await settle();
  assert.equal(signal.aborted, true);
  const { failed } = view.log.turns[0];
  assert.equal(failed.kind, "aborted");
  assert.equal(failed.partial, "半分");
  assert.equal(failed.retryable, true);
  assert.equal(view.log.turns[0].completed, null);
  assert.equal(session.conversationId, null);
  assert.equal(session.busy, false);
  assert.equal(session.stop(), false, "nothing to stop when idle");
  assert.equal(session.retry(), true);
});

test("a late success after Stop does not overwrite the stopped state", async () => {
  const view = fakeView();
  const gate = deferred();
  const session = new ChatSession({ view, send: () => gate.promise });
  session.submit("x");
  session.stop();
  gate.reject(new Error("anything"));
  await settle();
  assert.equal(view.log.turns[0].failed.kind, "aborted");
});

test("network failure and empty replies are retryable failures, not successes", async () => {
  const view = fakeView();
  let mode = "network";
  const session = new ChatSession({
    view,
    async send() {
      if (mode === "network") throw new TypeError("Failed to fetch");
      return ok("c");
    },
  });
  session.submit("a");
  await settle();
  assert.equal(view.log.turns[0].failed.kind, "network");
  assert.equal(view.log.turns[0].failed.retryable, true);
  mode = "empty";
  session.retry();
  await settle();
  assert.equal(view.log.turns[0].failed.kind, "empty");
  assert.equal(session.conversationId, null);
});

test("new conversation is refused while a request is in flight", async () => {
  const view = fakeView();
  const gate = deferred();
  const session = new ChatSession({ view, send: () => gate.promise });
  session.submit("x");
  assert.equal(session.reset(), false);
  gate.resolve(ok("c"));
  await settle();
  assert.equal(session.reset(), true);
});
