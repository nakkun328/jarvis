import assert from "node:assert/strict";
import test from "node:test";
import {
  LIST_LIMIT,
  MemoryApiError,
  candidatesUrl,
  classifyStatus,
  loadMemory,
  notesUrl,
} from "../memory-api.js";
import {
  TABS,
  apiErrorMessage,
  categoryLabel,
  formatScore,
  formatTimestamp,
  itemModel,
  listViewModel,
  normalizeMemory,
  originInfo,
  shortRevision,
  statusInfo,
} from "../memory-view.js";

const ID = "11111111-1111-4111-8111-111111111111";
const REVISION = "ab".repeat(32);
const HOSTILE = '<img src=x onerror="alert(1)"> [x](javascript:alert(1))\n';

function raw(overrides = {}) {
  return {
    id: ID, status: "approved", category: "user", content: "好みの記憶", source: "user:chat",
    origin: "user_explicit", importance: 0.8, confidence: 0.95, tags: ["a", "b"], project: "p",
    revision: REVISION, supersedes_id: null,
    created_at: "2026-10-07T12:00:00.000000Z", updated_at: "2026-10-07T12:01:00.000000Z",
    ...overrides,
  };
}

test("normalizeMemory keeps only usable values and rejects non-records", () => {
  assert.equal(normalizeMemory(null), null);
  assert.equal(normalizeMemory([]), null);
  assert.equal(normalizeMemory({ id: "" }), null);
  assert.equal(normalizeMemory({ id: 5 }), null);
  const odd = normalizeMemory(raw({ importance: 7, confidence: "x", tags: ["ok", 3, null], content: 1, project: 2 }));
  assert.equal(odd.importance, null);
  assert.equal(odd.confidence, null);
  assert.deepEqual(odd.tags, ["ok"]);
  assert.equal(odd.content, "");
  assert.equal(odd.project, null);
  assert.deepEqual(Object.keys(normalizeMemory(raw())).sort(), [
    "category", "confidence", "content", "created_at", "id", "importance", "origin", "project",
    "revision", "source", "status", "supersedes_id", "tags", "updated_at",
  ]);
});

test("status, origin and category have labels, glyphs and a safe fallback", () => {
  assert.equal(statusInfo("approved").label, "承認済み");
  assert.equal(statusInfo("pending").glyph, "○");
  assert.equal(statusInfo("conflict").label, "競合");
  assert.equal(statusInfo("constructor").key, "other");
  assert.equal(statusInfo("__proto__").label, "その他");
  assert.equal(originInfo("ai_inference").label, "AIの推測");
  assert.notEqual(originInfo("ai_inference").glyph, originInfo("user_explicit").glyph);
  assert.equal(originInfo("toString").key, "other");
  assert.equal(categoryLabel("project"), "プロジェクト");
  assert.equal(categoryLabel("hasOwnProperty"), "その他");
});

test("scores, revisions and timestamps are formatted without inventing values", () => {
  assert.equal(formatScore(0.8), "0.80");
  assert.equal(formatScore(null), "—");
  assert.equal(shortRevision(REVISION), "abababababab");
  assert.equal(shortRevision("short"), null);
  assert.equal(shortRevision(null), null);
  assert.equal(shortRevision("AB".repeat(32)), null);
  assert.match(formatTimestamp("2026-10-07T12:00:00.000000Z", { timeZone: "Asia/Tokyo" }), /2026\/10\/07 21:00:00/);
  assert.equal(formatTimestamp("nonsense"), "—");
  assert.equal(formatTimestamp(null), "—");
});

test("an item shows provenance, confidence, origin, importance and revision", () => {
  const model = itemModel(normalizeMemory(raw()), { timeZone: "Asia/Tokyo" });
  const fields = Object.fromEntries(model.fields);
  assert.equal(fields["確信度"], "0.95");
  assert.equal(fields["重要度"], "0.80");
  assert.equal(fields["出典"], "user:chat");
  assert.equal(fields["リビジョン"], "abababababab");
  assert.equal(fields["プロジェクト"], "p");
  assert.equal(fields["タグ"], "a / b");
  assert.equal(model.origin.label, "ユーザー本人の発言");
  assert.equal(model.isInference, false);
});

test("an AI inference is flagged and a candidate has no revision yet", () => {
  const model = itemModel(normalizeMemory(raw({
    status: "pending", origin: "ai_inference", revision: null, supersedes_id: ID, project: null, tags: [],
  })));
  const fields = Object.fromEntries(model.fields);
  assert.equal(model.isInference, true);
  assert.equal(fields["リビジョン"], "未承認");
  assert.equal(fields["訂正の対象"], ID);
  assert.equal("プロジェクト" in fields, false);
  assert.equal("タグ" in fields, false);
});

test("hostile text passes through untouched as plain strings", () => {
  const model = itemModel(normalizeMemory(raw({ content: HOSTILE, source: HOSTILE, project: HOSTILE, tags: [HOSTILE] })));
  assert.equal(model.content, HOSTILE);
  const fields = Object.fromEntries(model.fields);
  assert.equal(fields["出典"], HOSTILE);
  assert.equal(fields["プロジェクト"], HOSTILE);
  assert.equal(fields["タグ"], HOSTILE);
});

test("listViewModel picks the tab, counts both lists and reports emptiness", () => {
  const notes = [normalizeMemory(raw())];
  const candidates = [normalizeMemory(raw({ id: "2", status: "pending" })), normalizeMemory(raw({ id: "3", status: "conflict" }))];
  assert.deepEqual(TABS, ["notes", "candidates"]);
  const first = listViewModel(notes, candidates);
  assert.equal(first.tab, "notes");
  assert.deepEqual(first.counts, { notes: 1, candidates: 2 });
  assert.equal(first.items.length, 1);
  const second = listViewModel(notes, candidates, { tab: "candidates", truncated: { candidates: true } });
  assert.deepEqual(second.items.map((item) => item.status.key), ["pending", "conflict"]);
  assert.equal(second.truncated, true);
  assert.equal(listViewModel([], [], { tab: "bogus" }).tab, "notes");
  assert.equal(listViewModel([], []).empty, true);
});

test("error messages are fixed text with a fallback", () => {
  assert.match(apiErrorMessage("unavailable"), /保存先/);
  assert.equal(apiErrorMessage("nope"), apiErrorMessage("server"));
  assert.equal(apiErrorMessage("constructor"), apiErrorMessage("server"));
});

// ----- API client -----

const json = (body, status = 200) => new Response(JSON.stringify(body), { status });

test("urls only ask for bounded lists", () => {
  assert.equal(LIST_LIMIT, 100);
  assert.equal(notesUrl(), "/api/memory/notes?limit=100");
  assert.equal(candidatesUrl(10), "/api/memory/candidates?limit=10");
});

test("loadMemory reads both lists with GET and flags a list that hit the limit", async () => {
  const calls = [];
  const result = await loadMemory({
    limit: 2,
    fetchImpl: async (url, options) => {
      calls.push([url, options]);
      return url.includes("/notes")
        ? json({ notes: [raw(), raw({ id: "2" })] })
        : json({ candidates: [raw({ id: "3", status: "pending" })] });
    },
  });
  assert.deepEqual(calls.map(([url]) => url).sort(), ["/api/memory/candidates?limit=2", "/api/memory/notes?limit=2"]);
  for (const [, options] of calls) {
    assert.equal(options.method, undefined);
    assert.equal(options.headers.Accept, "application/json");
  }
  assert.equal(result.notes.length, 2);
  assert.deepEqual(result.truncated, { notes: true, candidates: false });
});

test("loadMemory maps failures to fixed kinds", async () => {
  const cases = [[503, "unavailable"], [422, "bad_request"], [500, "server"], [404, "server"]];
  for (const [status, kind] of cases) {
    await assert.rejects(
      loadMemory({ fetchImpl: async () => json({ detail: "x" }, status) }),
      (error) => error instanceof MemoryApiError && error.kind === kind && error.status === status,
    );
  }
  assert.equal(classifyStatus(503), "unavailable");
  await assert.rejects(loadMemory({ fetchImpl: async () => { throw new TypeError("down"); } }), (e) => e.kind === "network");
  await assert.rejects(loadMemory({ fetchImpl: async () => new Response("not json") }), (e) => e.kind === "format");
  await assert.rejects(loadMemory({ fetchImpl: async () => json({ notes: "x", candidates: [] }) }), (e) => e.kind === "format");
  await assert.rejects(
    loadMemory({ fetchImpl: async (url) => json(url.includes("/notes") ? { notes: [1] } : { candidates: [] }) }),
    (e) => e.kind === "format",
  );
});

test("an aborted request is not reported as a network failure", async () => {
  await assert.rejects(
    loadMemory({ fetchImpl: async () => { throw new DOMException("aborted", "AbortError"); } }),
    (error) => error.name === "AbortError",
  );
});
