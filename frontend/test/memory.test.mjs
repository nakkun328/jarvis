import assert from "node:assert/strict";
import test from "node:test";
import {
  LIST_LIMIT,
  MemoryApiError,
  candidateDetailUrl,
  candidatesUrl,
  classifyStatus,
  loadMemory,
  loadMemoryDetail,
  noteDetailUrl,
  notesUrl,
} from "../memory-api.js";
import {
  TABS,
  apiErrorMessage,
  categoryLabel,
  formatScore,
  formatTimestamp,
  ID_PATTERN,
  MAX_QUERY_CHARS,
  cleanQuery,
  createDebouncer,
  detailModel,
  emptyMessage,
  isMemoryId,
  itemModel,
  listViewModel,
  normalizeDetail,
  queryMessage,
  resultSummary,
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
  const cases = [[503, "unavailable"], [422, "bad_request"], [500, "server"], [404, "not_found"], [401, "unauthorized"]];
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

// ----- search -----

test("search text is a URL-encoded query value only, and blank is left out", () => {
  assert.equal(notesUrl(100, ""), "/api/memory/notes?limit=100");
  assert.equal(notesUrl(100), "/api/memory/notes?limit=100");
  assert.equal(notesUrl(20, "緑茶 tea"), "/api/memory/notes?limit=20&q=%E7%B7%91%E8%8C%B6+tea");
  const hostile = "a&limit=1#x/../?z=%00";
  const url = candidatesUrl(5, hostile);
  assert.equal(new URL(url, "http://x").searchParams.get("q"), hostile);
  assert.equal(new URL(url, "http://x").searchParams.get("limit"), "5");
  assert.equal(new URL(url, "http://x").pathname, "/api/memory/candidates");
});

test("loadMemory passes the query to both lists", async () => {
  const urls = [];
  await loadMemory({
    q: "tea",
    fetchImpl: async (url) => {
      urls.push(url);
      return json(url.includes("/notes") ? { notes: [] } : { candidates: [] });
    },
  });
  assert.deepEqual(urls.sort(), ["/api/memory/candidates?limit=100&q=tea", "/api/memory/notes?limit=100&q=tea"]);
});

test("cleanQuery mirrors the API rules and explains refusals", () => {
  assert.deepEqual(cleanQuery("  tea  "), { ok: true, value: "tea", terms: ["tea"] });
  assert.deepEqual(cleanQuery("緑　茶 tea"), { ok: true, value: "緑　茶 tea", terms: ["緑", "茶", "tea"] });
  assert.deepEqual(cleanQuery(""), { ok: true, value: "", terms: [] });
  assert.deepEqual(cleanQuery("   "), { ok: true, value: "", terms: [] });
  assert.deepEqual(cleanQuery(undefined), { ok: true, value: "", terms: [] });
  assert.equal(cleanQuery("x".repeat(MAX_QUERY_CHARS)).ok, true);
  assert.equal(cleanQuery("語".repeat(MAX_QUERY_CHARS)).ok, true);
  assert.equal(cleanQuery("x".repeat(MAX_QUERY_CHARS + 1)).reason, "too_long");
  for (const bad of ["a\u0000b", "a\nb", "a\tb", "a\u001bb", "a\u007fb", "a\u0085b", "a\u2028b", "a\u2029b"]) {
    assert.deepEqual(cleanQuery(bad), { ok: false, reason: "control" }, JSON.stringify(bad));
  }
  assert.equal(cleanQuery("1 2 3 4 5 6 7 8").ok, true);
  assert.equal(cleanQuery("1 2 3 4 5 6 7 8 9").reason, "too_many_terms");
  for (const reason of ["too_long", "control", "too_many_terms", "nope"]) {
    assert.ok(queryMessage(reason).length > 0);
  }
  // A refused query is never repeated back in the message.
  assert.equal(queryMessage("constructor"), queryMessage("control"));
});

test("the debouncer runs the last call once and can be cancelled or flushed", () => {
  const timers = [];
  const fake = {
    set: (fn, ms) => { timers.push({ fn, ms, live: true }); return timers.length - 1; },
    clear: (handle) => { timers[handle].live = false; },
  };
  const ran = [];
  const debounced = createDebouncer((value) => ran.push(value), 300, fake);
  debounced.schedule("a");
  debounced.schedule("b");
  assert.equal(debounced.pending, true);
  assert.deepEqual(timers.map((t) => [t.ms, t.live]), [[300, false], [300, true]]);
  timers[1].fn();
  assert.deepEqual(ran, ["b"]);
  assert.equal(debounced.pending, false);
  debounced.schedule("c");
  debounced.cancel();
  assert.equal(debounced.pending, false);
  assert.equal(timers[2].live, false);
  debounced.schedule("d");
  debounced.flush("e");
  assert.deepEqual(ran, ["b", "e"]);
});

test("search wording: summary only while searching, and an empty result says so", () => {
  assert.equal(resultSummary("", { notes: 1, candidates: 2 }), "");
  assert.match(resultSummary("tea", { notes: 1, candidates: 2 }), /承認済みノート 1 件・確認待ちの候補 2 件/);
  assert.match(emptyMessage("notes", "tea"), /一致する記録はありません/);
  assert.match(emptyMessage("notes", ""), /まだありません/);
  assert.match(emptyMessage("candidates", ""), /候補はありません/);
});

// ----- detail -----

const OTHER_ID = "22222222-2222-4222-8222-222222222222";

function rawDetail(overrides = {}) {
  return {
    ...raw(),
    replaced_by_id: null,
    reviews: [{ action: "approve", previous_status: "pending", new_status: "approved", occurred_at: "2026-10-07T12:01:00.000000Z", revision: REVISION }],
    lifecycle: [],
    ...overrides,
  };
}

test("ids are canonical lowercase UUIDs only", () => {
  assert.equal(isMemoryId(ID), true);
  const lettered = "abcdef01-2345-4678-8abc-def012345678";
  assert.equal(isMemoryId(lettered), true);
  for (const bad of [lettered.toUpperCase(), ID.replaceAll("-", ""), `{${ID}}`, `${ID}\n`, ` ${ID}`, "", null, 5, "../x"]) {
    assert.equal(isMemoryId(bad), false, String(bad));
  }
  assert.ok(ID_PATTERN.test(OTHER_ID));
});

test("normalizeDetail adds links and histories and drops non-record shapes", () => {
  assert.equal(normalizeDetail(raw()), null); // no reviews/lifecycle arrays
  assert.equal(normalizeDetail(rawDetail({ reviews: [1] })), null);
  assert.equal(normalizeDetail(rawDetail({ lifecycle: "x" })), null);
  const detail = normalizeDetail(rawDetail({ replaced_by_id: OTHER_ID, supersedes_id: "not-an-id" }));
  assert.equal(detail.replaced_by_id, OTHER_ID);
  assert.equal(detail.supersedes_id, null);
  assert.equal(normalizeDetail(rawDetail({ replaced_by_id: "<b>" })).replaced_by_id, null);
  assert.equal(detail.reviews.length, 1);
});

test("detailModel shows links, the full revision and a time-ordered history", () => {
  const model = detailModel(normalizeDetail(rawDetail({
    status: "superseded",
    replaced_by_id: OTHER_ID,
    supersedes_id: "33333333-3333-4333-8333-333333333333",
    lifecycle: [{ action: "supersede", related_id: OTHER_ID, occurred_at: "2026-10-08T01:00:00.000000Z", revision: REVISION }],
  })), { timeZone: "UTC" });
  const fields = Object.fromEntries(model.fields);
  assert.equal(fields["ID"], ID);
  assert.equal(fields["リビジョン（SHA-256）"], REVISION);
  assert.equal("訂正の対象" in fields, false);
  assert.equal(model.status.label, "置換済み");
  assert.deepEqual(model.links.map((link) => link.id), ["33333333-3333-4333-8333-333333333333", OTHER_ID]);
  assert.deepEqual(model.history.map((entry) => entry.text), [
    "承認（確認待ち → 承認済み）",
    "訂正で置換",
  ]);
  assert.equal(model.history[1].relatedId, OTHER_ID);
  assert.equal(model.history[0].revision, REVISION.slice(0, 12));
});

test("unknown actions and statuses in a detail fall back to fixed text", () => {
  const model = detailModel(normalizeDetail(rawDetail({
    status: "constructor",
    reviews: [{ action: "__proto__", previous_status: "x", new_status: "y", occurred_at: null, revision: "zz" }],
  })));
  assert.equal(model.status.key, "other");
  assert.match(model.history[0].text, /^その他（その他 → その他）$/);
  assert.equal(model.history[0].revision, null);
  assert.equal(model.history[0].time, "—");
});

test("hostile text in a detail stays plain strings", () => {
  const model = detailModel(normalizeDetail(rawDetail({ content: HOSTILE, source: HOSTILE, tags: [HOSTILE], project: HOSTILE })));
  assert.equal(model.content, HOSTILE);
  assert.equal(Object.fromEntries(model.fields)["タグ"], HOSTILE);
});

test("loadMemoryDetail asks the note endpoint first, then the candidate endpoint", async () => {
  const urls = [];
  const detail = await loadMemoryDetail(ID, {
    fetchImpl: async (url, options) => {
      urls.push(url);
      assert.equal(options.method, undefined);
      return url.includes("/notes/") ? json({ detail: "not_found" }, 404) : json(rawDetail({ status: "pending" }));
    },
  });
  assert.deepEqual(urls, [noteDetailUrl(ID), candidateDetailUrl(ID)]);
  assert.equal(detail.status, "pending");
  urls.length = 0;
  await loadMemoryDetail(ID, { fetchImpl: async (url) => { urls.push(url); return json(rawDetail()); } });
  assert.deepEqual(urls, [noteDetailUrl(ID)]);
});

test("loadMemoryDetail reports not_found, other failures, and mismatched or bad bodies", async () => {
  await assert.rejects(loadMemoryDetail(ID, { fetchImpl: async () => json({}, 404) }), (e) => e.kind === "not_found");
  await assert.rejects(loadMemoryDetail(ID, { fetchImpl: async () => json({}, 503) }), (e) => e.kind === "unavailable");
  await assert.rejects(loadMemoryDetail(ID, { fetchImpl: async () => json(rawDetail({ id: OTHER_ID })) }), (e) => e.kind === "format");
  await assert.rejects(loadMemoryDetail(ID, { fetchImpl: async () => json(raw()) }), (e) => e.kind === "format");
  await assert.rejects(loadMemoryDetail(ID, { fetchImpl: async () => { throw new TypeError("down"); } }), (e) => e.kind === "network");
});

test("an id that is not a canonical UUID is never requested", async () => {
  let called = 0;
  const fetchImpl = async () => { called += 1; return json({}); };
  for (const bad of ["../tasks", "A".repeat(36), `${ID}/x`, "", undefined]) {
    await assert.rejects(loadMemoryDetail(bad, { fetchImpl }), (e) => e.kind === "not_found");
  }
  assert.equal(called, 0);
  assert.equal(noteDetailUrl("a/b?c"), "/api/memory/notes/a%2Fb%3Fc");
});

test("the debouncer works with the real timers (they must not be called as methods)", async () => {
  const ran = [];
  const debounced = createDebouncer((value) => ran.push(value), 5);
  debounced.schedule("a");
  debounced.schedule("b");
  await new Promise((resolve) => setTimeout(resolve, 40));
  assert.deepEqual(ran, ["b"]);
});
