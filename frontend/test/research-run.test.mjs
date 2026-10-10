import assert from "node:assert/strict";
import test from "node:test";
import {
  RunApiError,
  cancelResearch,
  loadAvailability,
  pollSession,
  startResearch,
} from "../research-run-api.js";
import {
  CITATION_NOTE,
  LEVELS,
  POLL_BASE_MS,
  POLL_MAX_MS,
  QUESTION_MAX,
  SEARCH_NOTICE,
  STAGES,
  activeSessionId,
  availability,
  levelHint,
  parseResultText,
  pollDelay,
  progressModel,
  questionState,
  resultModel,
  runErrorMessage,
} from "../research-run-view.js";
import { detailViewModel, normalizeSession } from "../research-view.js";

const ID = "11111111-1111-4111-8111-111111111111";
const SOURCE_ID = "33333333-3333-4333-8333-333333333333";
const HOSTILE = '<script>window.__pwned=1</script><img src=x onerror="alert(1)">';

const response = (body, status = 200) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => body,
});

function detailBody(overrides = {}) {
  return {
    id: ID,
    question: "What is the answer?",
    level: "standard",
    status: "completed",
    failure_reason: null,
    has_result: true,
    created_at: "2026-10-07T12:00:00.000000Z",
    updated_at: "2026-10-07T12:05:00.000000Z",
    result_text: "Standard research result.\n\nVerified claims:\n- A claim [1]",
    queries: [{ position: 0, text: "q", created_at: "2026-10-07T12:00:01.000000Z" }],
    sources: [
      {
        id: SOURCE_ID,
        url: "https://example.test/a",
        final_url: "https://example.test/a",
        title: "Title",
        publisher: null,
        published_at: null,
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
        quote: "exact quote here",
        quote_start: 5,
        quote_end: 16,
      },
    ],
    conflicts: [],
    ...overrides,
  };
}

// ----- the notice and fixed texts -----

test("the notice states what is sent, where, and what is not", () => {
  assert.equal(
    SEARCH_NOTICE,
    "調べるときは、質問文が検索サービス(Tavily)へ送信されます。サービス側で保持・利用される可能性があります。記憶の内容は送信しません。",
  );
  assert.match(CITATION_NOTE, /そのまま含まれている/);
  assert.match(CITATION_NOTE, /簡易な目安/);
});

test("quick, standard and deep can be requested, each with a one line explanation", () => {
  assert.deepEqual(LEVELS.map((level) => level.value), ["quick", "standard", "deep"]);
  for (const level of LEVELS) assert.ok(levelHint(level.value).length > 10);
  assert.match(levelHint("deep"), /調べ項目/);
  assert.equal(levelHint("extensive"), "");
  assert.equal(levelHint("__proto__"), "");
});

// ----- the question field -----

test("questionState counts code points and validates", () => {
  assert.deepEqual(
    { length: questionState("日本語").length, valid: questionState("日本語").valid },
    { length: 3, valid: true },
  );
  assert.equal(questionState("😀😀").length, 2);
  assert.equal(questionState("😀😀").counterText, `2 / ${QUESTION_MAX}`);
  assert.equal(questionState("").valid, false);
  assert.equal(questionState("   \n\t").blank, true);
  assert.equal(questionState(undefined).valid, false);
  assert.equal(questionState(null).length, 0);
});

test("the length limit is on code points and at the exact boundary", () => {
  assert.equal(questionState("a".repeat(QUESTION_MAX)).valid, true);
  const over = questionState("a".repeat(QUESTION_MAX + 1));
  assert.equal(over.valid, false);
  assert.equal(over.problem, "question_too_long");
  assert.equal(over.remaining, -1);
  assert.equal(questionState("😀".repeat(QUESTION_MAX)).valid, true);
});

test("control characters are refused but newlines and tabs are fine", () => {
  assert.equal(questionState("a\nb\tc").valid, true);
  for (const bad of ["a\u0000b", "a\u0007", "a\u001b[31m", "a\u007f"]) {
    const state = questionState(bad);
    assert.equal(state.valid, false, JSON.stringify(bad));
    assert.equal(state.problem, "question_invalid_characters");
  }
});

// ----- availability and errors -----

test("availability reads the status body and falls back to unavailable", () => {
  assert.deepEqual(availability({ enabled: true }), { enabled: true, reasonText: "" });
  for (const reason of ["disabled", "no_search_provider", "no_chat_provider"]) {
    const result = availability({ enabled: false, reason });
    assert.equal(result.enabled, false);
    assert.ok(result.reasonText.length > 5);
  }
  const reasons = new Set(
    ["disabled", "no_search_provider", "no_chat_provider"].map(
      (reason) => availability({ enabled: false, reason }).reasonText,
    ),
  );
  assert.equal(reasons.size, 3);
  for (const odd of [null, undefined, {}, { enabled: "true" }, { enabled: false, reason: HOSTILE }, 5]) {
    assert.equal(availability(odd).enabled, false);
    assert.ok(availability(odd).reasonText.length > 5);
  }
});

test("every server error code has its own message and nothing leaks through", () => {
  const codes = [
    "busy", "search_budget_exhausted", "research_not_configured", "forbidden", "unauthorized",
    "question_required", "question_too_long", "question_invalid_characters", "invalid_level",
    "invalid_body", "session_not_found", "not_cancellable", "storage_unavailable", "network",
    "offline", "format",
  ];
  for (const code of codes) {
    assert.notEqual(runErrorMessage(code), runErrorMessage("server"), code);
  }
  for (const odd of [HOSTILE, "constructor", "__proto__", null, undefined, 5]) {
    assert.equal(runErrorMessage(odd), runErrorMessage("server"));
  }
  assert.match(runErrorMessage("search_budget_exhausted"), /上限/);
});

// ----- progress -----

test("progress marks the stages before the current one as done", () => {
  const model = progressModel({
    status: "running",
    progress: { stage: "reading", round: 0, queries: 2, pages: 3, sources: 2, claims: 0 },
    queries: [{}, {}],
    sources: [],
    claims: [],
  });
  assert.equal(model.active, true);
  assert.equal(model.cancellable, true);
  assert.deepEqual(
    model.stages.map((stage) => stage.state),
    ["done", "done", "current", "todo", "todo"],
  );
  assert.deepEqual(model.stages.map((stage) => stage.key), STAGES.map((stage) => stage.key));
  assert.match(model.label, /ページ/);
  const values = Object.fromEntries(model.counters.map((counter) => [counter.key, counter.value]));
  assert.deepEqual(values, { queries: 2, pages: 3, sources: 2, claims: 0 });
});

test("deep progress shows which sub-question is being researched", () => {
  const model = progressModel({
    status: "running",
    progress: { stage: "reading", round: 0, queries: 3, pages: 4, sources: 3, claims: 2, sub_question: 2, sub_questions: 4 },
  });
  assert.deepEqual(model.subQuestion, { index: 2, total: 4 });
  assert.match(model.label, /調べ項目 2\/4/);
  assert.equal(progressModel({ status: "running", progress: { stage: "reading" } }).subQuestion, null);
  const session = normalizeSession(
    detailBody({
      status: "running",
      progress: { stage: "reading", round: 0, queries: 1, pages: 0, sources: 0, claims: 0, sub_question: 1, sub_questions: 3 },
    }),
  );
  assert.equal(session.progress.sub_questions, 3);
});

test("progress without live data shows the queued and preparing states", () => {
  const queued = progressModel({ status: "pending" });
  assert.match(queued.label, /順番待ち/);
  assert.equal(queued.cancellable, true);
  assert.ok(queued.stages.every((stage) => stage.state === "todo"));
  const preparing = progressModel({ status: "running" });
  assert.match(preparing.label, /準備/);
  assert.equal(progressModel({ status: "completed" }).active, false);
  assert.equal(progressModel({ status: "completed" }).cancellable, false);
  assert.equal(progressModel(null).active, false);
});

test("progress counters come from stored rows when the live numbers are behind", () => {
  const model = progressModel({
    status: "running",
    progress: { stage: "verifying", round: 1, queries: 1, pages: 2, sources: 0, claims: 0 },
    queries: [{}, {}, {}],
    sources: [{}, {}],
    claims: [{}],
  });
  const values = Object.fromEntries(model.counters.map((counter) => [counter.key, counter.value]));
  assert.deepEqual(values, { queries: 3, pages: 2, sources: 2, claims: 1 });
  assert.equal(model.round, 1);
});

test("an unknown stage or bad counters from the server are ignored", () => {
  const model = progressModel({
    status: "running",
    progress: { stage: HOSTILE, queries: -1, pages: "x", sources: 1.5, claims: null },
  });
  assert.equal(model.stageKey, null);
  assert.ok(model.counters.every((counter) => counter.value === 0));
});

test("polling starts at two seconds and backs off to a ceiling", () => {
  assert.equal(pollDelay(0), POLL_BASE_MS);
  assert.equal(POLL_BASE_MS, 2000);
  assert.equal(pollDelay(1), 4000);
  assert.equal(pollDelay(2), 8000);
  assert.equal(pollDelay(3), POLL_MAX_MS);
  assert.equal(pollDelay(50), POLL_MAX_MS);
  assert.equal(pollDelay(-3), POLL_BASE_MS);
});

test("activeSessionId picks the newest session still queued or running", () => {
  const rows = [
    { id: "a", status: "completed" },
    { id: "b", status: "running" },
    { id: "c", status: "pending" },
  ];
  assert.equal(activeSessionId(rows), "b");
  assert.equal(activeSessionId([{ id: "a", status: "failed" }]), null);
  assert.equal(activeSessionId(null), null);
  assert.equal(activeSessionId([null, { id: "z", status: "waiting" }]), "z");
});

// ----- the result -----

test("parseResultText separates conflicts from caveats and translates the fixed sentences", () => {
  const text = [
    "Standard research result. Every statement below is backed by a quote.",
    "",
    "Verified claims:",
    "- The cache keeps entries for 60 seconds. [1]",
    "- Entries last 120 seconds. [2]",
    "",
    "Sources:",
    "[1] Title - https://example.test/a (retrieved 2026-10-07)",
    "",
    "Open conflicts (the sources disagree; nothing here decides who is right):",
    '- different figures: "The cache keeps entries for 60 seconds." [1] against "Entries last 120 seconds." [2]',
    "- and 2 more",
    "",
    "Caveats:",
    "- Some sources disagree and the disagreement is not resolved.",
    "- 2 page(s) could not be read. Some pages could not be read.",
    "- A sentence this client does not know.",
    "",
    "Note: a citation shows that the quote occurs in the page text.",
  ].join("\n");
  const parsed = parseResultText(text);
  assert.equal(parsed.conflicts.length, 2);
  assert.match(parsed.conflicts[0], /^数値が違います: /);
  assert.equal(parsed.conflicts[1], "and 2 more");
  assert.deepEqual(parsed.caveats, [
    "出典の間で食い違いがあり、解決されていません。",
    "2件のページを読み取れませんでした。",
    "A sentence this client does not know.",
  ]);
  assert.ok(!parsed.caveats.some((line) => line.includes("Verified")));
});

test("parseResultText reads the quick notices and ignores non-strings", () => {
  const quick = parseResultText(
    "Nothing found.\n\nNo claim could be verified against a source.\n\n" +
      "Note: 3 proposed claim(s) were removed because their sources or quotes could not be verified.",
  );
  assert.equal(quick.caveats.length, 2);
  assert.match(quick.caveats[0], /検証できる主張は得られませんでした/);
  assert.match(quick.caveats[1], /3件/);
  assert.deepEqual(parseResultText(null), { conflicts: [], caveats: [] });
  assert.deepEqual(parseResultText(5), { conflicts: [], caveats: [] });
});

test("a claim line that imitates a section header cannot add a caveat", () => {
  const text = [
    "Verified claims:",
    "- Caveats: [1]",
    "- Open conflicts (x): [2]",
    "",
    "Sources:",
    "[1] Caveats: - fake - https://example.test (retrieved 2026-10-07)",
  ].join("\n");
  assert.deepEqual(parseResultText(text), { conflicts: [], caveats: [] });
});

test("resultModel joins claims to their sources and states the citation note", () => {
  const session = normalizeSession(detailBody());
  const model = resultModel(session, detailViewModel(session));
  assert.equal(model.kind, "completed");
  assert.equal(model.claims.length, 1);
  assert.equal(model.claims[0].quote, "exact quote here");
  assert.equal(model.claims[0].source.href, "https://example.test/a");
  assert.equal(model.noVerifiedClaims, false);
  assert.equal(model.citationNote, CITATION_NOTE);
  assert.equal(model.sourceCount, 1);
});

test("a completed run with no claim says so instead of showing an empty list", () => {
  const session = normalizeSession(
    detailBody({ claims: [], result_text: "x\n\nNo claim could be verified against a source." }),
  );
  const model = resultModel(session, detailViewModel(session));
  assert.equal(model.noVerifiedClaims, true);
  assert.equal(model.claims.length, 0);
  assert.deepEqual(model.caveats, []); // the callout says it; the caveat is not repeated
});

test("open conflicts from the structured field show even without text lines", () => {
  const session = normalizeSession(
    detailBody({ conflicts: [{ kind: "number_mismatch", status: "open" }, { kind: "x", status: "resolved" }] }),
  );
  const model = resultModel(session, detailViewModel(session));
  assert.equal(model.conflicts.length, 1);
  assert.match(model.conflicts[0], /1件/);
});

test("failed and cancelled runs carry the fixed Japanese reason", () => {
  const failed = normalizeSession(
    detailBody({ status: "failed", failure_reason: "reader_failed", result_text: null, claims: [] }),
  );
  const model = resultModel(failed, detailViewModel(failed));
  assert.equal(model.kind, "failed");
  assert.equal(model.failure, "ページの読み取りに失敗しました");
  assert.equal(model.noVerifiedClaims, false);
  const cancelled = normalizeSession(detailBody({ status: "cancelled", result_text: null, claims: [] }));
  assert.equal(resultModel(cancelled, detailViewModel(cancelled)).kind, "cancelled");
});

test("hostile text passes through as plain strings and a bad link gets no href", () => {
  const raw = detailBody({
    result_text: `Caveats:\n- ${HOSTILE}`,
    claims: [{ ...detailBody().claims[0], claim_text: HOSTILE, quote: HOSTILE }],
  });
  raw.sources[0].url = "javascript:alert(1)";
  raw.sources[0].final_url = "javascript:alert(1)";
  const session = normalizeSession(raw);
  const model = resultModel(session, detailViewModel(session));
  assert.equal(model.claims[0].claimText, HOSTILE);
  assert.equal(model.claims[0].quote, HOSTILE);
  assert.equal(model.claims[0].source.href, null);
  assert.equal(model.caveats[0], HOSTILE);
});

test("normalizeSession keeps live progress and conflicts only from a detail", () => {
  const session = normalizeSession(
    detailBody({
      status: "running",
      progress: { stage: "searching", round: 0, queries: 1, pages: 0, sources: 0, claims: 0, extra: HOSTILE },
      conflicts: [{ kind: "date_mismatch", status: "open", claim_a_id: "x" }, null, 5],
    }),
  );
  assert.deepEqual(session.progress, {
    stage: "searching", round: 0, queries: 1, pages: 0, sources: 0, claims: 0,
  });
  assert.deepEqual(session.conflicts, [{ kind: "date_mismatch", status: "open" }]);
  assert.ok(!("progress" in normalizeSession(detailBody())));
});

// ----- the client -----

test("startResearch posts the question and level as JSON and returns the id", async () => {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push([url, init]);
    return response({ id: ID }, 202);
  };
  const id = await startResearch({ question: "なぜ?", level: "quick" }, { fetchImpl });
  assert.equal(id, ID);
  const [url, init] = calls[0];
  assert.equal(url, "/api/research/sessions");
  assert.equal(init.method, "POST");
  assert.equal(init.headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(init.body), { question: "なぜ?", level: "quick" });
});

test("start errors carry only a fixed code", async () => {
  const cases = [
    [429, { detail: "busy" }, "busy"],
    [429, { detail: "search_budget_exhausted" }, "search_budget_exhausted"],
    [503, { detail: "research_not_configured" }, "research_not_configured"],
    [403, { detail: "forbidden" }, "forbidden"],
    [422, { detail: "question_invalid_characters" }, "question_invalid_characters"],
    [500, { detail: HOSTILE }, "server"],
    [500, { detail: ["not", "a", "string"] }, "server"],
    [422, null, "server"],
  ];
  for (const [status, body, code] of cases) {
    const fetchImpl = async () => response(body, status);
    await assert.rejects(
      startResearch({ question: "q", level: "quick" }, { fetchImpl }),
      (error) => error instanceof RunApiError && error.code === code && error.status === status,
    );
  }
});

test("network failures, bad bodies and aborts are told apart", async () => {
  await assert.rejects(
    startResearch({ question: "q", level: "quick" }, { fetchImpl: async () => { throw new TypeError("x"); } }),
    (error) => error.code === "network",
  );
  await assert.rejects(
    startResearch({ question: "q", level: "quick" }, { fetchImpl: async () => response({ id: 5 }, 202) }),
    (error) => error.code === "format",
  );
  await assert.rejects(
    startResearch({ question: "q", level: "quick" }, {
      fetchImpl: async () => ({ ok: true, status: 202, json: async () => { throw new Error("bad"); } }),
    }),
    (error) => error.code === "format",
  );
  const abort = Object.assign(new Error("aborted"), { name: "AbortError" });
  await assert.rejects(
    pollSession(ID, { fetchImpl: async () => { throw abort; } }),
    (error) => error.name === "AbortError",
  );
});

test("cancelResearch posts without a body and reports the outcome", async () => {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push([url, init]);
    return response({ id: ID, status: "cancelling" });
  };
  assert.equal(await cancelResearch(ID, { fetchImpl }), "cancelling");
  assert.equal(calls[0][0], `/api/research/sessions/${ID}/cancel`);
  assert.equal(calls[0][1].method, "POST");
  assert.equal(calls[0][1].body, undefined);
  assert.equal(
    await cancelResearch(ID, { fetchImpl: async () => response({ id: ID, status: "cancelled" }) }),
    "cancelled",
  );
  await assert.rejects(
    cancelResearch(ID, { fetchImpl: async () => response({ detail: "not_cancellable" }, 409) }),
    (error) => error.code === "not_cancellable",
  );
});

test("loadAvailability and pollSession are plain GETs", async () => {
  const calls = [];
  const fetchImpl = async (url, init = {}) => {
    calls.push([url, init.method]);
    return response(url.endsWith("/status") ? { enabled: true } : detailBody());
  };
  assert.deepEqual(await loadAvailability({ fetchImpl }), { enabled: true });
  const session = await pollSession(ID, { fetchImpl });
  assert.equal(session.id, ID);
  assert.deepEqual(calls.map((call) => call[1]), [undefined, undefined]);
  await assert.rejects(
    pollSession(ID, { fetchImpl: async () => response({ detail: "session_not_found" }, 404) }),
    (error) => error.code === "session_not_found",
  );
  await assert.rejects(
    pollSession(ID, { fetchImpl: async () => response({ nope: 1 }) }),
    (error) => error.code === "format",
  );
});
