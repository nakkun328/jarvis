import assert from "node:assert/strict";
import test from "node:test";
import {
  LIST_LIMIT,
  ResearchApiError,
  classifyStatus,
  detailUrl,
  listUrl,
  loadSession,
  loadSessionList,
} from "../research-api.js";
import {
  SESSION_STATUSES,
  apiErrorMessage,
  backoffDelay,
  countByStatus,
  detailViewModel,
  failureLabel,
  formatRating,
  formatTimestamp,
  levelLabel,
  listViewModel,
  normalizeSession,
  reuseInfo,
  safeHref,
  sourceTypeLabel,
  statusChangeMessage,
  statusInfo,
} from "../research-view.js";

const ID = "11111111-1111-4111-8111-111111111111";
const ID2 = "22222222-2222-4222-8222-222222222222";
const SOURCE_ID = "33333333-3333-4333-8333-333333333333";
const HOSTILE = '<script>window.__pwned=1</script><img src=x onerror="alert(1)">';

function summary(overrides = {}) {
  return {
    id: ID,
    question: "What is the answer?",
    level: "standard",
    status: "completed",
    failure_reason: null,
    has_result: true,
    created_at: "2026-10-07T12:00:00.000000Z",
    updated_at: "2026-10-07T12:05:00.000000Z",
    ...overrides,
  };
}

function detail(overrides = {}) {
  return {
    ...summary(),
    result_text: "The answer.",
    queries: [
      { position: 0, text: "first", created_at: "2026-10-07T12:00:01.000000Z" },
      { position: 1, text: "second", created_at: "2026-10-07T12:00:02.000000Z" },
    ],
    sources: [
      {
        id: SOURCE_ID,
        url: "https://example.test/a",
        final_url: "https://example.test/a",
        title: "Title",
        publisher: "Pub",
        published_at: "2026-10-01T00:00:00.000000Z",
        retrieved_at: "2026-10-07T12:00:00.000000Z",
        source_type: "docs",
        evaluation: { authority: 0.9, freshness: null, primary: 1, relevance: 0.25, agreement: 0 },
      },
    ],
    claims: [
      {
        id: "44444444-4444-4444-8444-444444444444",
        claim_text: "A claim",
        source_id: SOURCE_ID,
        quote: "exact quote",
        quote_start: 5,
        quote_end: 16,
      },
    ],
    ...overrides,
  };
}

const respond = (body, status = 200) => async () => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => body,
});

// ----- labels and normalisation -----

test("status, level, source type and failure labels fall back for unknown values", () => {
  assert.equal(statusInfo("running").label, "実行中");
  assert.equal(statusInfo("bogus").key, "other");
  assert.equal(statusInfo("constructor").key, "other");
  assert.equal(statusInfo("__proto__").key, "other");
  assert.equal(statusInfo(null).key, "other");
  assert.equal(levelLabel("deep"), "詳細");
  assert.equal(levelLabel("toString"), "その他");
  assert.equal(sourceTypeLabel("docs"), "ドキュメント");
  assert.equal(sourceTypeLabel("hasOwnProperty"), "その他");
  assert.equal(failureLabel("timeout"), "制限時間を超えました");
  assert.equal(failureLabel(`${HOSTILE}`), "その他");
  assert.equal(failureLabel(undefined), "その他");
});

test("normalizeSession keeps only known fields and rejects non-sessions", () => {
  assert.equal(normalizeSession(null), null);
  assert.equal(normalizeSession([]), null);
  assert.equal(normalizeSession({ id: ID }), null);
  assert.equal(normalizeSession({ status: "pending" }), null);
  const session = normalizeSession({ ...summary(), extra: "<b>x</b>", toString: "y" });
  assert.deepEqual(Object.keys(session).sort(), [
    "created_at", "failure_reason", "has_result", "id", "level", "question", "reuse", "status", "updated_at",
  ]);
  assert.ok(!("sources" in session));
});

test("normalizeSession tolerates wrong types and drops malformed children", () => {
  const session = normalizeSession({
    ...detail(),
    question: 5,
    queries: "nope",
    sources: [null, 3, { id: "" }, ...detail().sources],
    claims: [{}, ...detail().claims],
  });
  assert.equal(session.question, "");
  assert.deepEqual(session.queries, []);
  assert.equal(session.sources.length, 1);
  assert.equal(session.claims.length, 1);
  const odd = normalizeSession({
    ...detail(),
    sources: [{ ...detail().sources[0], evaluation: { authority: 5, freshness: "x", primary: NaN } }],
  });
  assert.deepEqual(odd.sources[0].evaluation, {
    authority: null, freshness: null, primary: null, relevance: null, agreement: null,
  });
});

// ----- links -----

test("safeHref accepts only absolute http and https URLs", () => {
  assert.equal(safeHref("https://example.test/a?b=1#c"), "https://example.test/a?b=1#c");
  assert.equal(safeHref("http://example.test/"), "http://example.test/");
  for (const bad of [
    "javascript:alert(1)",
    "JaVaScRiPt:alert(1)",
    " javascript:alert(1)",
    "\tjavascript:alert(1)",
    "java\nscript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:x",
    "file:///etc/passwd",
    "ftp://example.test/",
    "//example.test/a",
    "/relative",
    "example.test/a",
    "https://user:pass@example.test/",
    "https://example.test/a b",
    "",
    null,
    undefined,
    5,
    {},
    `https://example.test/${"a".repeat(3000)}`,
  ]) {
    assert.equal(safeHref(bad), null, String(bad));
  }
});

// ----- formatting -----

test("ratings are shown as plain numbers and null as not evaluated", () => {
  assert.equal(formatRating(0.9), "0.90");
  assert.equal(formatRating(0), "0.00");
  assert.equal(formatRating(1), "1.00");
  assert.equal(formatRating(null), "未評価");
  assert.equal(formatRating(undefined), "未評価");
  assert.equal(formatRating("0.5"), "未評価");
});

test("formatTimestamp renders valid ISO strings and a dash otherwise", () => {
  const out = formatTimestamp("2026-10-07T12:00:00.000000Z", { timeZone: "UTC" });
  assert.match(out, /2026/);
  assert.equal(formatTimestamp("not a date"), "—");
  assert.equal(formatTimestamp(null), "—");
  assert.equal(formatTimestamp("9999-99-99T99:99:99Z"), "—");
});

// ----- list -----

test("countByStatus counts every status and groups unknown ones", () => {
  const counts = countByStatus(
    ["pending", "running", "running", "completed", "weird"].map((status) => summary({ status })),
  );
  assert.equal(counts.all, 5);
  assert.equal(counts.running, 2);
  assert.equal(counts.other, 1);
  assert.equal(counts.failed, 0);
  assert.deepEqual(
    Object.keys(counts).sort(),
    ["all", "other", ...SESSION_STATUSES].sort(),
  );
});

test("listViewModel keeps the API's newest-first order, marks selection and reports emptiness", () => {
  const all = [summary({ id: ID2, question: "new", status: "failed" }), summary({ id: ID, question: "old" })];
  const model = listViewModel(all, all, { selectedId: ID2, timeZone: "UTC" });
  assert.deepEqual(model.items.map((item) => item.question), ["new", "old"]);
  assert.equal(model.items[0].selected, true);
  assert.equal(model.items[0].status.label, "失敗");
  assert.equal(model.items[0].levelText, "標準");
  assert.equal(model.empty, null);
  assert.equal(listViewModel([], [], {}).empty, "none");
  assert.equal(listViewModel(all, all, { filter: "pending" }).empty, "filtered");
  assert.deepEqual(
    listViewModel(all, all, { filter: "failed" }).items.map((item) => item.id),
    [ID2],
  );
});

test("list rows carry hostile text through unchanged as plain strings", () => {
  const model = listViewModel([summary({ question: HOSTILE })], [summary({ question: HOSTILE })]);
  assert.equal(model.items[0].question, HOSTILE);
  assert.equal(typeof model.items[0].question, "string");
});

// ----- detail -----

test("detailViewModel joins claims to their sources and numbers them", () => {
  const model = detailViewModel(normalizeSession(detail()), { timeZone: "UTC" });
  assert.equal(model.status.label, "完了");
  assert.equal(model.levelText, "標準");
  assert.equal(model.resultText, "The answer.");
  assert.deepEqual(model.queries, [{ number: 1, text: "first" }, { number: 2, text: "second" }]);
  const [source] = model.sources;
  assert.equal(source.number, 1);
  assert.equal(source.typeText, "ドキュメント");
  assert.equal(source.href, "https://example.test/a");
  assert.equal(source.finalUrl, null);
  assert.equal(source.claimCount, 1);
  assert.deepEqual(
    source.ratings.map((rating) => [rating.label, rating.text]),
    [["権威性", "0.90"], ["新しさ", "未評価"], ["一次性", "1.00"], ["関連性", "0.25"], ["一致度", "0.00"]],
  );
  const [claim] = model.claims;
  assert.equal(claim.quote, "exact quote");
  assert.equal(claim.rangeText, "本文の 5〜16 文字目");
  assert.equal(claim.source.number, 1);
  assert.equal(claim.source.url, "https://example.test/a");
  assert.equal(claim.source.href, "https://example.test/a");
  assert.equal(model.terminal, true);
});

test("a claim whose source is missing is kept and marked as having no source", () => {
  const raw = detail();
  raw.claims[0].source_id = "99999999-9999-4999-8999-999999999999";
  const model = detailViewModel(normalizeSession(raw));
  assert.equal(model.claims.length, 1);
  assert.equal(model.claims[0].source, null);
  assert.equal(model.sources[0].claimCount, 0);
});

test("a differing final URL is exposed and non-http URLs get no href", () => {
  const raw = detail();
  raw.sources[0].url = "javascript:alert(1)";
  raw.sources[0].final_url = "https://example.test/final";
  const model = detailViewModel(normalizeSession(raw));
  assert.equal(model.sources[0].href, null);
  assert.equal(model.sources[0].url, "javascript:alert(1)");
  assert.equal(model.sources[0].finalHref, "https://example.test/final");
  assert.equal(model.claims[0].source.href, null);
});

test("hostile question, quote, title and result stay inert strings in the view model", () => {
  const raw = detail({
    question: HOSTILE,
    result_text: HOSTILE,
  });
  raw.sources[0].title = HOSTILE;
  raw.claims[0].quote = HOSTILE;
  raw.claims[0].claim_text = HOSTILE;
  raw.queries[0].text = HOSTILE;
  const model = detailViewModel(normalizeSession(raw));
  for (const value of [
    model.question, model.resultText, model.sources[0].title, model.claims[0].quote,
    model.claims[0].claimText, model.queries[0].text,
  ]) {
    assert.equal(value, HOSTILE);
  }
});

test("a failed session shows the fixed reason label, never stored text", () => {
  const failed = normalizeSession(detail({
    status: "failed", failure_reason: "no_results", result_text: null, has_result: false,
  }));
  const model = detailViewModel(failed);
  assert.equal(model.failure, "検索結果がありませんでした");
  assert.equal(model.resultText, null);
  assert.equal(model.terminal, true);
  const odd = detailViewModel(normalizeSession(detail({ status: "failed", failure_reason: HOSTILE })));
  assert.equal(odd.failure, "その他");
});

test("a running session is not terminal and an empty one has no children", () => {
  const running = detailViewModel(normalizeSession(detail({
    status: "running", result_text: null, has_result: false, queries: [], sources: [], claims: [],
  })));
  assert.equal(running.terminal, false);
  assert.deepEqual([running.queries, running.sources, running.claims], [[], [], []]);
});

test("statusChangeMessage speaks only for a real change of the same session", () => {
  const a = { id: ID, status: "running" };
  const b = { id: ID, status: "completed" };
  assert.match(statusChangeMessage(a, b), /実行中.*完了/);
  assert.equal(statusChangeMessage(a, a), null);
  assert.equal(statusChangeMessage(a, { id: ID2, status: "completed" }), null);
  assert.equal(statusChangeMessage(null, b), null);
});

// ----- errors and backoff -----

test("apiErrorMessage gives fixed text and falls back for unknown kinds", () => {
  assert.match(apiErrorMessage("network"), /接続できません/);
  assert.equal(apiErrorMessage("constructor"), apiErrorMessage("server"));
  assert.equal(apiErrorMessage(HOSTILE), apiErrorMessage("server"));
});

test("backoffDelay doubles up to the ceiling", () => {
  assert.equal(backoffDelay(1), 5000);
  assert.equal(backoffDelay(2), 10000);
  assert.equal(backoffDelay(3), 20000);
  assert.equal(backoffDelay(4), 30000);
  assert.equal(backoffDelay(50), 30000);
  assert.equal(backoffDelay(0), 5000);
});

// ----- api client -----

test("URLs are built for GET only and encode the id", () => {
  assert.equal(listUrl(), `/api/research/sessions?limit=${LIST_LIMIT}`);
  assert.equal(listUrl({ status: "running", limit: 5 }), "/api/research/sessions?status=running&limit=5");
  assert.equal(listUrl({ status: "bogus" }), `/api/research/sessions?limit=${LIST_LIMIT}`);
  assert.equal(detailUrl(ID), `/api/research/sessions/${ID}`);
  assert.equal(detailUrl("../x?y"), "/api/research/sessions/..%2Fx%3Fy");
});

test("classifyStatus maps statuses to fixed kinds", () => {
  assert.equal(classifyStatus(404), "not_found");
  assert.equal(classifyStatus(422), "bad_request");
  assert.equal(classifyStatus(503), "unavailable");
  assert.equal(classifyStatus(500), "server");
});

test("loadSessionList normalises sessions and never sends a body or method", async () => {
  const calls = [];
  const fetchImpl = async (url, options) => {
    calls.push([url, options]);
    return { ok: true, status: 200, json: async () => ({ sessions: [summary()] }) };
  };
  const result = await loadSessionList({ fetchImpl });
  assert.equal(result.all.length, 1);
  assert.equal(result.truncated, false);
  assert.equal(calls.length, 1);
  assert.equal(calls[0][1].method, undefined);
  assert.equal(calls[0][1].body, undefined);
});

test("loadSessionList asks the server for the chosen status only when truncated", async () => {
  const urls = [];
  const rows = Array.from({ length: 3 }, (_, i) => summary({ id: `${i}`.repeat(8) + ID.slice(8) }));
  const fetchImpl = async (url) => {
    urls.push(url);
    const body = url.includes("status=failed")
      ? { sessions: [summary({ status: "failed" })] }
      : { sessions: rows };
    return { ok: true, status: 200, json: async () => body };
  };
  const result = await loadSessionList({ filter: "failed", limit: 3, fetchImpl });
  assert.equal(result.truncated, true);
  assert.equal(urls.length, 2);
  assert.equal(result.shown[0].status, "failed");
  urls.length = 0;
  await loadSessionList({ filter: "failed", limit: 4, fetchImpl });
  assert.equal(urls.length, 1);
});

test("loadSessionList and loadSession reject malformed bodies", async () => {
  await assert.rejects(loadSessionList({ fetchImpl: respond({ sessions: "x" }) }), (e) => e.kind === "format");
  await assert.rejects(loadSessionList({ fetchImpl: respond({ sessions: [{}] }) }), (e) => e.kind === "format");
  await assert.rejects(loadSession(ID, { fetchImpl: respond(summary()) }), (e) => e.kind === "format");
  await assert.rejects(loadSession(ID, { fetchImpl: respond(null) }), (e) => e.kind === "format");
  const broken = async () => ({ ok: true, status: 200, json: async () => { throw new Error("bad"); } });
  await assert.rejects(loadSession(ID, { fetchImpl: broken }), (e) => e.kind === "format");
});

test("HTTP and network failures become fixed error kinds", async () => {
  await assert.rejects(loadSession(ID, { fetchImpl: respond({ detail: HOSTILE }, 404) }),
    (e) => e instanceof ResearchApiError && e.kind === "not_found" && !e.message.includes("script"));
  await assert.rejects(loadSession(ID, { fetchImpl: respond({}, 503) }), (e) => e.kind === "unavailable");
  await assert.rejects(loadSession(ID, { fetchImpl: respond({}, 500) }), (e) => e.kind === "server");
  const down = async () => { throw new TypeError("secret detail"); };
  await assert.rejects(loadSession(ID, { fetchImpl: down }),
    (e) => e.kind === "network" && !e.message.includes("secret"));
  const aborted = async () => { throw Object.assign(new Error("x"), { name: "AbortError" }); };
  await assert.rejects(loadSession(ID, { fetchImpl: aborted }), (e) => e.name === "AbortError");
});

test("loadSession returns a full session", async () => {
  const session = await loadSession(ID, { fetchImpl: respond(detail()) });
  assert.equal(session.sources.length, 1);
  assert.equal(session.claims[0].quote, "exact quote");
});

test("reuse decision shows a fixed Japanese label and the prior date as plain text", () => {
  const base = { prior_at: "2026-10-07T12:00:00.000000Z", previous_session_id: ID2 };
  const fresh = normalizeSession(summary({ reuse: { reason: "reused_fresh", ...base } }));
  const info = reuseInfo(fresh.reuse, { timeZone: "UTC" });
  assert.equal(info.label, "過去の調査を再利用");
  assert.equal(info.reused, true);
  assert.ok(info.note.includes("2026/10/07"));
  for (const reason of ["prior_stale", "time_sensitive_topic"]) {
    const stale = reuseInfo({ reason, ...base }, { timeZone: "UTC" });
    assert.equal(stale.label, "古いので再検索");
    assert.equal(stale.reused, false);
    assert.equal(stale.previousSessionId, ID2);
  }
  assert.equal(reuseInfo({ reason: "no_prior_research", ...base }), null);
  assert.equal(reuseInfo({ reason: "constructor", ...base }), null);
  assert.equal(reuseInfo(null), null);
  assert.equal(normalizeSession(summary({ reuse: "x" })).reuse, null);
  const hostile = normalizeSession(summary({ reuse: { reason: HOSTILE, prior_at: HOSTILE } }));
  assert.equal(reuseInfo(hostile.reuse), null);
});

test("detailViewModel carries the reuse info", () => {
  const session = normalizeSession({
    ...summary({ reuse: { reason: "prior_stale", prior_at: null, previous_session_id: null } }),
    result_text: null,
    queries: [],
    sources: [],
    claims: [],
    conflicts: [],
  });
  assert.equal(detailViewModel(session).reuse.label, "古いので再検索");
  assert.ok(detailViewModel(session).reuse.note.endsWith("不明"));
});

test("stripListNumber drops a leading list number but keeps decimals", async () => {
  const { stripListNumber } = await import("../research-view.js");
  assert.equal(stripListNumber("1.SQLite WAL"), "SQLite WAL");
  assert.equal(stripListNumber("2) foo"), "foo");
  assert.equal(stripListNumber("3.5 inch display"), "3.5 inch display");
  assert.equal(stripListNumber(null), "");
});
