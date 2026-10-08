import assert from "node:assert/strict";
import test from "node:test";
import { sendChat, ChatError } from "../chat-api.js";
import { readEventStream } from "../sse.js";
// The gate runs only this file, so the Research screen tests are pulled in from here.
import "./research.test.mjs";

const encode = (text) => new TextEncoder().encode(text);

function chunks(...parts) {
  return new ReadableStream({
    start(controller) {
      for (const part of parts) controller.enqueue(part);
      controller.close();
    },
  });
}

function eventResponse(...parts) {
  return new Response(chunks(...parts), {
    headers: { "Content-Type": "text/event-stream" },
  });
}

test("SSE parser handles split UTF-8, CRLF, comments, and multiple data lines", async () => {
  const unicode = encode('event: delta\ndata: {"text":"🙂"}\n\n');
  const emojiStart = unicode.indexOf(0xf0);
  const events = [];
  await readEventStream(chunks(
    encode(': ping\r\nevent: delta\r\ndata: one\r'),
    encode('\n\n\nevent: done\ndata: first\ndata: second\n\n'),
    unicode.slice(0, emojiStart + 1), unicode.slice(emojiStart + 1, emojiStart + 3),
    unicode.slice(emojiStart + 3),
  ), (event) => events.push(event));
  assert.deepEqual(events, [
    { event: "delta", data: "one" },
    { event: "done", data: "first\nsecond" },
    { event: "delta", data: '{"text":"🙂"}' },
  ]);
});

test("streaming chat sends conversation id and receives deltas and completion", async () => {
  const calls = [];
  const delta = [];
  const result = await sendChat({
    message: "続き",
    conversationId: "previous",
    onDelta: (text) => delta.push(text),
    fetchImpl: async (url, options) => {
      calls.push({ url, options });
      return eventResponse(
        encode('event: delta\ndata: {"text":"こん"}\n\n'),
        encode('event: delta\ndata: {"text":"にちは"}\n\n'),
        encode('event: done\ndata: {"conversation_id":"next","provider":"test","model":"mock"}\n\n'),
      );
    },
  });
  assert.deepEqual(calls.map(({ url }) => url), ["/api/chat/stream"]);
  assert.deepEqual(JSON.parse(calls[0].options.body), { message: "続き", conversation_id: "previous" });
  assert.deepEqual(delta, ["こん", "にちは"]);
  assert.deepEqual(result, { conversation_id: "next", provider: "test", model: "mock" });
});

test("falls back to nonstreaming API when stream route is unavailable", async () => {
  const calls = [];
  const delta = [];
  const result = await sendChat({
    message: "hello",
    conversationId: null,
    onDelta: (text) => delta.push(text),
    fetchImpl: async (url, options) => {
      calls.push({ url, options });
      return url.endsWith("/stream")
        ? new Response(null, { status: 404 })
        : Response.json({ conversation_id: "id", reply: "hi", provider: "test", model: "mock" });
    },
  });
  assert.deepEqual(calls.map(({ url }) => url), ["/api/chat/stream", "/api/chat"]);
  assert.deepEqual(JSON.parse(calls[1].options.body), { message: "hello" });
  assert.deepEqual(delta, ["hi"]);
  assert.equal(result.conversation_id, "id");
});

test("SSE error and incomplete streams fail without a duplicate fallback request", async () => {
  for (const event of [
    'event: error\ndata: {"message":"利用できません"}\n\n',
    'event: delta\ndata: {"text":"partial"}\n\n',
  ]) {
    let calls = 0;
    await assert.rejects(
      sendChat({
        message: "hello",
        onDelta: () => {},
        fetchImpl: async () => {
          calls += 1;
          return eventResponse(encode(event));
        },
      }),
      ChatError,
    );
    assert.equal(calls, 1);
  }
});

test("server error text is mapped to actionable Japanese, never shown verbatim", async () => {
  const cases = [
    ["conversation not found", "conversation-missing", false],
    ["chat provider failed", "provider", true],
    ["conversation capacity reached", "capacity", true],
    ["conversation storage unavailable", "unavailable", true],
    ["memory context unavailable", "unavailable", true],
    ["something unexpected from a future server", "http", true],
  ];
  for (const [serverMessage, kind, retryable] of cases) {
    await assert.rejects(
      sendChat({
        message: "hello",
        onDelta: () => {},
        fetchImpl: async () =>
          eventResponse(encode(`event: error\ndata: ${JSON.stringify({ message: serverMessage })}\n\n`)),
      }),
      (error) => {
        assert.ok(error instanceof ChatError);
        assert.equal(error.kind, kind, serverMessage);
        assert.equal(error.retryable, retryable, serverMessage);
        assert.ok(!error.message.includes(serverMessage), `leaked: ${error.message}`);
        return true;
      },
    );
  }
});

test("HTTP 503 without a provider is explained, not retryable, and not retried as nonstreaming", async () => {
  const calls = [];
  await assert.rejects(
    sendChat({
      message: "hello",
      onDelta: () => {},
      fetchImpl: async (url) => {
        calls.push(url);
        return Response.json({ detail: "chat provider is not configured" }, { status: 503 });
      },
    }),
    (error) => error.kind === "not-configured" && error.retryable === false && /LLM/.test(error.message),
  );
  assert.deepEqual(calls, ["/api/chat/stream"]);
});

test("nonstreaming fallback reports a missing conversation and 5xx/422 statuses", async () => {
  const fallback = (status, detail) => async (url) =>
    url.endsWith("/stream")
      ? new Response(null, { status: 404 })
      : Response.json({ detail }, { status });
  const run = (status, detail) =>
    sendChat({ message: "x", conversationId: "old", onDelta: () => {}, fetchImpl: fallback(status, detail) });
  await assert.rejects(run(404, "conversation not found"), (e) => e.kind === "conversation-missing" && !e.retryable);
  await assert.rejects(run(502, "chat provider failed"), (e) => e.kind === "provider" && e.retryable);
  await assert.rejects(run(500, "boom"), (e) => e.kind === "server" && /500/.test(e.message));
  await assert.rejects(run(422, [{ msg: "too long" }]), (e) => e.kind === "invalid" && !e.retryable);
});

test("a stream that ends without done is reported as incomplete", async () => {
  await assert.rejects(
    sendChat({ message: "hi", onDelta: () => {}, fetchImpl: async () => eventResponse(encode("")) }),
    (error) => error.kind === "incomplete" && /途中/.test(error.message),
  );
});

test("abort signal is passed to fetch and aborts an in-flight stream", async () => {
  const controller = new AbortController();
  const deltas = [];
  let seenSignal;
  const pending = sendChat({
    message: "slow",
    signal: controller.signal,
    onDelta: (text) => deltas.push(text),
    fetchImpl: async (_url, options) => {
      seenSignal = options.signal;
      return new Response(
        new ReadableStream({
          start(stream) {
            stream.enqueue(encode('event: delta\ndata: {"text":"a"}\n\n'));
            options.signal.addEventListener("abort", () => stream.error(options.signal.reason));
          },
        }),
        { headers: { "Content-Type": "text/event-stream" } },
      );
    },
  });
  await new Promise((resolve) => setTimeout(resolve, 10));
  controller.abort();
  await assert.rejects(pending);
  assert.equal(seenSignal, controller.signal);
  assert.deepEqual(deltas, ["a"]);
});

// UI state-machine tests live in their own file; importing it keeps them inside the single
// test file that scripts/verify.py and CI run.
import "./chat-session.test.mjs";
