import assert from "node:assert/strict";
import test from "node:test";
import { sendChat, ChatError } from "../chat-api.js";
import { readEventStream } from "../sse.js";

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
