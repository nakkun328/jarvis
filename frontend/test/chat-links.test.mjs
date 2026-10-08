import assert from "node:assert/strict";
import test from "node:test";
import { renderReply, replySegments } from "../chat-links.js";

const ID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d";

test("only the research path of a session becomes a link", () => {
  const text = `調査を開始しました。リサーチ画面（/research#${ID}）で見られます。`;
  const segments = replySegments(text);
  assert.deepEqual(segments, [
    { type: "text", text: "調査を開始しました。リサーチ画面（" },
    { type: "link", text: `/research#${ID}`, href: `/research#${ID}` },
    { type: "text", text: "）で見られます。" },
  ]);
  assert.equal(segments.map((s) => s.text).join(""), text);
});

test("nothing else is linked", () => {
  for (const text of [
    "plain text",
    "https://example.com/research#" + ID,
    "javascript:alert(1)",
    "/research#not-an-id",
    "x/research#" + ID,
    "/research#" + ID + "0",
    "/research#" + ID.toUpperCase(),
    "/research?x=1#" + ID,
    "//evil.test/research#" + "x",
    "<a href=\"/research\">x</a>",
    "",
  ]) {
    assert.ok(replySegments(text).every((s) => s.type === "text"), text);
    assert.equal(replySegments(text).map((s) => s.text).join(""), text);
  }
  assert.deepEqual(replySegments(undefined), []);
  assert.deepEqual(replySegments(42), []);
});

test("several paths and surrounding markup-looking text stay text", () => {
  const text = `<b>a</b> /research#${ID} & /research#${ID}`;
  const segments = replySegments(text);
  assert.deepEqual(segments.map((s) => s.type), ["text", "link", "text", "link"]);
  assert.equal(segments[0].text, "<b>a</b> ");
});

test("markdown-looking text is not interpreted; only the bare path is linked", () => {
  const segments = replySegments(`[x](/research#${ID})`);
  assert.deepEqual(segments.map((s) => s.text), ["[x](", `/research#${ID}`, ")"]);
  assert.deepEqual(segments.map((s) => s.type), ["text", "link", "text"]);
});

class El {
  constructor(tag) { this.tag = tag; this.attrs = {}; this.children = []; this.textContent = ""; this.className = ""; }
  setAttribute(n, v) { this.attrs[n] = String(v); }
  replaceChildren(...nodes) { this.children = nodes; }
}
const doc = {
  createElement: (t) => new El(t),
  createTextNode: (text) => ({ tag: "#text", text }),
};

test("renderReply builds text nodes and same-origin anchors with the DOM API", () => {
  const container = new El("div");
  renderReply(doc, container, `</script> /research#${ID}`);
  assert.equal(container.children.length, 2);
  assert.deepEqual(container.children[0], { tag: "#text", text: "</script> " });
  const link = container.children[1];
  assert.equal(link.tag, "a");
  assert.equal(link.attrs.href, `/research#${ID}`);
  assert.equal(link.textContent, `/research#${ID}`);
  assert.equal("innerHTML" in link, false);
  renderReply(doc, container, "later text");
  assert.deepEqual(container.children, [{ tag: "#text", text: "later text" }]);
  renderReply(doc, container, "");
  assert.deepEqual(container.children, []);
});
