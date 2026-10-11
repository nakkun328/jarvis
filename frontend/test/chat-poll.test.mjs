import assert from "node:assert/strict";
import test from "node:test";
import { afterUrl, createMessagePoller, isToolRequested } from "../chat-poll.js";
import { parseActivity, reduce, begin, viewModel } from "../activity-view.js";

const ID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d";

function harness(responses, { hidden = false } = {}) {
  const calls = [];
  const delivered = [];
  const timers = [];
  const poller = createMessagePoller({
    onMessages: (m) => delivered.push(...m),
    fetchImpl: async (url) => {
      calls.push(url);
      const next = responses.shift();
      return { ok: true, status: 200, json: async () => next };
    },
    schedule: (fn) => timers.push(fn) && timers.length,
    cancel: () => {},
    isHidden: () => hidden,
  });
  const fire = async () => {
    const fn = timers.pop();
    if (fn) await fn();
    await new Promise((r) => setImmediate(r));
  };
  return { poller, calls, delivered, timers, fire };
}

test("polls after the known count and stops once a message arrived", async () => {
  const h = harness([{ messages: [] }, { messages: [{ role: "assistant", content: "done" }] }]);
  h.poller.sync(ID, 4);
  h.poller.turnDone(ID); // this page's own turn stored two more
  h.poller.noteTask();
  h.poller.watch();
  await h.fire();
  await h.fire();
  assert.deepEqual(h.calls, [afterUrl(ID, 6), afterUrl(ID, 6)]);
  assert.deepEqual(h.delivered, [{ role: "assistant", content: "done" }]);
  assert.equal(h.poller.known, 7);
  assert.equal(h.timers.length, 0);
});

test("does nothing without a task, while hidden, or after stop", async () => {
  const idle = harness([]);
  idle.poller.sync(ID, 2);
  idle.poller.watch();
  assert.equal(idle.timers.length, 0);
  const hidden = harness([{ messages: [{ role: "assistant", content: "x" }] }], { hidden: true });
  hidden.poller.sync(ID, 2);
  hidden.poller.watchAlways();
  await hidden.fire();
  assert.equal(hidden.calls.length, 0);
  hidden.poller.stop();
  await hidden.fire();
  assert.equal(hidden.calls.length, 0);
});

test("tool_requested is recognised and shown as a queued tool task", () => {
  assert.equal(isToolRequested('{"stage":"tool_requested","tool":"fs.list"}'), true);
  assert.equal(isToolRequested({ stage: "generating" }), false);
  assert.equal(isToolRequested("not json"), false);
  assert.equal(parseActivity({ stage: "tool_requested", tool: "<script>" }), null);
  assert.equal(parseActivity({ stage: "tool_requested" }), null);
  const state = reduce(begin(), { stage: "tool_requested", tool: "shell.run" });
  const model = viewModel(state);
  assert.equal(model.caption, "TOOL TASK QUEUED");
  assert.equal(model.approvalHint, true);
  const tools = model.nodes.find((n) => n.id === "tools");
  assert.equal(tools.active, true);
  assert.equal(viewModel(begin()).nodes.find((n) => n.id === "tools").connected, false);
});
