import assert from "node:assert/strict";
import test from "node:test";
import { CHUNK_MAX, FIRST_CHUNK_MAX, SpeechChunker, speakable } from "../speech-chunker.js";

function run(parts) {
  const chunker = new SpeechChunker();
  let full = "";
  const out = [];
  for (const part of parts) {
    full += part;
    out.push(...chunker.push(full));
  }
  out.push(...chunker.flush(full));
  return out;
}

test("Japanese sentences are spoken at 。！？ and newlines as they complete", () => {
  assert.deepEqual(run(["こんにちは。", "今日は", "いい天気ですね！", "散歩\nしよう"]), [
    "こんにちは。",
    "今日はいい天気ですね！",
    "散歩",
    "しよう",
  ]);
});

test("a chunk is not emitted before its sentence is complete", () => {
  const chunker = new SpeechChunker();
  assert.deepEqual(chunker.push("まだ途中の"), []);
  assert.deepEqual(chunker.push("まだ途中の文"), []);
  assert.deepEqual(chunker.push("まだ途中の文。"), ["まだ途中の文。"]);
});

test("English sentences end at . ! ? followed by a space, not inside numbers", () => {
  assert.deepEqual(run(["Pi is 3.14 ", "roughly. It is ", "irrational! Ok"]), [
    "Pi is 3.14 roughly.",
    "It is irrational!",
    "Ok",
  ]);
  const chunker = new SpeechChunker();
  assert.deepEqual(chunker.push("Version 3."), []);
});

test("the first chunk uses a lower cap and breaks at a comma", () => {
  const text = `${"あ".repeat(30)}、${"い".repeat(30)}`;
  const chunker = new SpeechChunker();
  const [first] = chunker.push(text);
  assert.ok(first.length <= FIRST_CHUNK_MAX);
  assert.ok(first.endsWith("、"));
});

test("later chunks are cut at the length cap", () => {
  const long = "あ".repeat(CHUNK_MAX * 2 + 5);
  const chunks = run([long]);
  assert.ok(chunks.every((c) => c.length <= CHUNK_MAX));
  assert.equal(chunks.join(""), long);
});

test("idle speaks a half-finished first chunk once, and only before anything was spoken", () => {
  const chunker = new SpeechChunker();
  assert.deepEqual(chunker.push("ええと"), []);
  assert.deepEqual(chunker.idle("ええと"), ["ええと"]);
  assert.deepEqual(chunker.idle("ええと、それは"), []);
  assert.deepEqual(chunker.flush("ええと、それは。"), ["、それは。"]);
});

test("markdown, code and urls are not read aloud", () => {
  assert.equal(speakable("# 見出し\n- 項目 **太字**", true), "見出し\n項目 太字");
  assert.equal(speakable("見て https://example.com/a?b=1 ね", true), "見て リンク ね");
  assert.equal(speakable("[docs](https://x.y/z) を読む", true), "docs を読む");
  assert.equal(speakable("実行: `rm -rf` です", true), "実行: コード です");
  assert.match(speakable("前\n```js\nconst a = 1;\n```\n後", true), /前\s+コード\s+後/);
});

test("an unfinished code fence, link or url tail is held back while streaming", () => {
  assert.equal(speakable("説明\n```py\nprint(", false), "説明\n");
  assert.equal(speakable("詳細は [ここ](https://exa", false), "詳細は ");
  assert.equal(speakable("見て https://exam", false), "見て ");
  const chunker = new SpeechChunker();
  assert.deepEqual(chunker.push("説明です。\n```py\nprint(1"), ["説明です。"]);
  assert.deepEqual(chunker.flush("説明です。\n```py\nprint(1)\n```\n以上"), ["コード", "以上"]);
});

test("flush returns the remainder and then nothing", () => {
  const chunker = new SpeechChunker();
  assert.deepEqual(chunker.push("はい。次の"), ["はい。"]);
  assert.deepEqual(chunker.flush("はい。次の文"), ["次の文"]);
  assert.deepEqual(chunker.flush("はい。次の文"), []);
});
