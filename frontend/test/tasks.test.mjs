import assert from "node:assert/strict";
import test from "node:test";
import { TasksApiError, listUrl, loadTask, loadTaskList } from "../tasks-api.js";
import {
  backoffDelay,
  followTask,
  initialStreamState,
  reduceStreamEvent,
} from "../tasks-stream.js";
import {
  TASK_STATUSES,
  connectionMessage,
  countByStatus,
  detailViewModel,
  failureLabel,
  formatTimestamp,
  listViewModel,
  mergeIntoList,
  normalizeTask,
  statusChangeMessage,
  statusInfo,
  stepCountText,
  stepSegments,
  verificationLabel,
  waitingLabel,
} from "../tasks-view.js";

const encode = (text) => new TextEncoder().encode(text);
const ID = "11111111-1111-4111-8111-111111111111";

function task(overrides = {}) {
  return {
    id: ID,
    goal: "goal",
    status: "pending",
    current_step: null,
    steps_total: 2,
    steps_completed: 0,
    target_device: null,
    attempt: 1,
    retry_of: null,
    verified: "not_verified",
    failure_code: null,
    waiting_reason: null,
    created_at: "2026-10-07T00:00:00.000000Z",
    updated_at: "2026-10-07T00:00:00.000000Z",
    started_at: null,
    finished_at: null,
    ...overrides,
  };
}

function detail(overrides = {}) {
  return {
    ...task(),
    result_summary: null,
    steps: [
      { index: 0, description: "a", status: "pending", started_at: null, finished_at: null, note: null },
      { index: 1, description: "b", status: "pending", started_at: null, finished_at: null, note: null },
    ],
    ...overrides,
  };
}

const frame = (event, data) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;

function sseResponse(...frames) {
  return new Response(
    new ReadableStream({
      start(controller) {
        for (const part of frames) controller.enqueue(encode(part));
        controller.close();
      },
    }),
    { headers: { "Content-Type": "text/event-stream" } },
  );
}

// ----- mapping -----

test("every status has a text label and a distinct glyph; running only when the API says so", () => {
  const infos = TASK_STATUSES.map(statusInfo);
  assert.equal(new Set(infos.map((info) => info.label)).size, TASK_STATUSES.length);
  assert.equal(new Set(infos.map((info) => info.glyph)).size, TASK_STATUSES.length);
  for (const info of infos) assert.ok(info.label && info.glyph);
  const pendingWithProgress = normalizeTask(task({ status: "pending", current_step: 1 }));
  assert.equal(statusInfo(pendingWithProgress.status).key, "pending");
  assert.notEqual(statusInfo("waiting").label, statusInfo("running").label);
});

test("unknown status and codes map to その他, including prototype keys", () => {
  for (const unknown of ["mystery", "constructor", "__proto__", "toString", "", null, 3]) {
    assert.equal(statusInfo(unknown).label, "その他");
    assert.equal(failureLabel(unknown), "その他");
    assert.equal(waitingLabel(unknown), "その他");
    assert.equal(verificationLabel(unknown), "その他");
  }
  assert.equal(failureLabel("timeout"), "制限時間を超えました");
  assert.equal(waitingLabel("needs_input"), "入力が必要です");
  for (const code of ["execution_failed", "verification_failed", "timeout", "interrupted", "internal_error"]) {
    assert.notEqual(failureLabel(code), "その他");
  }
  for (const code of ["needs_confirmation", "needs_input", "dependency"]) {
    assert.notEqual(waitingLabel(code), "その他");
  }
});

test("step count is a plain count, never a percentage", () => {
  assert.equal(stepCountText(task({ steps_completed: 2, steps_total: 5 })), "2/5 ステップ完了");
  assert.equal(stepCountText({ steps_completed: null, steps_total: 5 }), "ステップ数 —");
  assert.ok(!/%|％/.test(stepCountText(task({ steps_completed: 1, steps_total: 4 }))));
});

test("normalizeTask keeps only known fields and rejects non-tasks", () => {
  assert.equal(normalizeTask(null), null);
  assert.equal(normalizeTask([]), null);
  assert.equal(normalizeTask({ id: 1, status: "pending" }), null);
  assert.equal(normalizeTask({ id: "x" }), null);
  const clean = normalizeTask({ ...task(), steps_completed: -1, goal: 5, evil: "<script>" });
  assert.equal(clean.steps_completed, null);
  assert.equal(clean.goal, "");
  assert.ok(!("evil" in clean));
  assert.ok(!("steps" in clean));
  const full = normalizeTask(detail({ steps: [null, { description: 3, status: "completed" }] }));
  assert.equal(full.steps.length, 2);
  assert.equal(full.steps[1].description, "");
});

test("timestamps format deterministically and bad values show a dash", () => {
  assert.match(formatTimestamp("2026-10-07T12:34:56.000000Z", { timeZone: "UTC" }), /12:34:56/);
  assert.equal(formatTimestamp(null), "—");
  assert.equal(formatTimestamp("yesterday"), "—");
  assert.equal(formatTimestamp("2026-13-45T99:00:00Z"), "—");
});

// ----- list -----

test("counts come only from returned tasks and unknown statuses go to other", () => {
  const counts = countByStatus([
    task({ status: "running" }),
    task({ status: "running" }),
    task({ status: "failed" }),
    task({ status: "brand-new" }),
  ]);
  assert.equal(counts.all, 4);
  assert.equal(counts.running, 2);
  assert.equal(counts.failed, 1);
  assert.equal(counts.completed, 0);
  assert.equal(counts.other, 1);
});

test("list view model filters, keeps the API's newest-first order, and reports empty states", () => {
  const tasks = [
    task({ id: "c", status: "completed", goal: "new" }),
    task({ id: "b", status: "running", goal: "mid" }),
    task({ id: "a", status: "completed", goal: "old" }),
  ];
  const all = listViewModel(tasks, tasks, { selectedId: "b" });
  assert.deepEqual(all.items.map((item) => item.id), ["c", "b", "a"]);
  assert.deepEqual(all.items.map((item) => item.selected), [false, true, false]);
  const done = listViewModel(tasks, tasks, { filter: "completed" });
  assert.deepEqual(done.items.map((item) => item.id), ["c", "a"]);
  assert.equal(done.counts.completed, 2);
  assert.equal(listViewModel(tasks, tasks, { filter: "failed" }).empty, "filtered");
  assert.equal(listViewModel([], [], {}).empty, "none");
  assert.equal(listViewModel(tasks, tasks, {}).empty, null);
});

test("goal text stays an untouched string in view models", () => {
  const hostile = '<img src=x onerror="alert(1)"><script>1</script>';
  const model = listViewModel([task({ goal: hostile })], [task({ goal: hostile })]);
  assert.equal(model.items[0].goal, hostile);
  const view = detailViewModel(normalizeTask(detail({ goal: hostile, result_summary: hostile })));
  assert.equal(view.goal, hostile);
  assert.equal(view.resultSummary, hostile);
});

test("mergeIntoList updates one row from detail data without adding fields", () => {
  const merged = mergeIntoList([task({ id: "a" }), task({ id: "b" })], {
    ...detail({ id: "b", status: "running", steps_completed: 1 }),
  });
  assert.equal(merged[1].status, "running");
  assert.equal(merged[1].steps_completed, 1);
  assert.ok(!("steps" in merged[1]) && !("result_summary" in merged[1]));
  assert.equal(merged[0].status, "pending");
});

// ----- detail -----

test("step segments come from each step's own status", () => {
  const segments = stepSegments([
    { status: "completed" },
    { status: "running" },
    { status: "pending" },
    { status: "weird" },
  ]);
  assert.deepEqual(segments.map((segment) => segment.key), ["completed", "running", "pending", "other"]);
  assert.ok(segments.every((segment) => /手順 \d+: /.test(segment.label)));
  assert.deepEqual(stepSegments([]), []);
});

test("detail view model maps failure, waiting, and verification codes", () => {
  const failed = detailViewModel(normalizeTask(detail({
    status: "failed", failure_code: "timeout", verified: "verification_failed", finished_at: "2026-10-07T00:01:00.000000Z",
  })), { timeZone: "UTC" });
  assert.equal(failed.failure, "制限時間を超えました");
  assert.equal(failed.verification, "検証に失敗");
  assert.equal(failed.terminal, true);
  assert.equal(failed.waiting, null);
  const waiting = detailViewModel(normalizeTask(detail({ status: "waiting", waiting_reason: "future_reason" })));
  assert.equal(waiting.waiting, "その他");
  assert.equal(waiting.terminal, false);
  assert.equal(waiting.status.label, "応答待ち");
});

test("status change message only fires for a real change of the same task", () => {
  const before = normalizeTask(detail({ status: "running" }));
  const after = normalizeTask(detail({ status: "completed" }));
  assert.match(statusChangeMessage(before, after), /「実行中」から「完了」/);
  assert.equal(statusChangeMessage(before, before), null);
  assert.equal(statusChangeMessage(null, after), null);
  assert.equal(statusChangeMessage(before, { ...after, id: "other" }), null);
});

test("connection messages are fixed text", () => {
  assert.equal(connectionMessage({ phase: "idle" }), "");
  assert.match(connectionMessage({ phase: "reconnecting", attempt: 3 }), /3回目/);
  assert.equal(connectionMessage({ phase: "constructor" }), "");
});

// ----- reducer -----

test("reducer: snapshot, progress, done in order", () => {
  let state = initialStreamState();
  state = reduceStreamEvent(state, { event: "snapshot", data: detail({ status: "running" }) });
  assert.equal(state.phase, "live");
  assert.equal(state.task.status, "running");
  state = reduceStreamEvent(state, {
    event: "progress",
    data: detail({ status: "running", steps_completed: 1, updated_at: "2026-10-07T00:00:01.000000Z" }),
  });
  assert.equal(state.task.steps_completed, 1);
  state = reduceStreamEvent(state, {
    event: "done",
    data: { id: ID, status: "completed", failure_code: null, verified: "verified", finished_at: "2026-10-07T00:00:02.000000Z" },
  });
  assert.equal(state.phase, "done");
  assert.equal(state.task.status, "completed");
  assert.equal(state.task.verified, "verified");
  assert.equal(state.task.finished_at, "2026-10-07T00:00:02.000000Z");
});

test("reducer ignores stale, foreign, malformed, and unknown events", () => {
  const live = reduceStreamEvent(initialStreamState(), {
    event: "snapshot",
    data: detail({ status: "running", updated_at: "2026-10-07T00:00:05.000000Z" }),
  });
  const stale = reduceStreamEvent(live, {
    event: "progress", data: detail({ status: "pending", updated_at: "2026-10-07T00:00:01.000000Z" }),
  });
  assert.equal(stale, live);
  assert.equal(reduceStreamEvent(live, { event: "progress", data: detail({ id: "other" }) }), live);
  assert.equal(reduceStreamEvent(live, { event: "progress", data: "nope" }), live);
  assert.equal(reduceStreamEvent(live, { event: "progress", data: task() }), live);
  assert.equal(reduceStreamEvent(live, { event: "done", data: { id: "other", status: "failed" } }), live);
  assert.equal(reduceStreamEvent(live, { event: "whatever", data: {} }), live);
  assert.equal(reduceStreamEvent(initialStreamState(), { event: "progress", data: detail() }).task, null);
});

test("reducer: error codes are recognized and unknown codes are kept apart", () => {
  const live = reduceStreamEvent(initialStreamState(), { event: "snapshot", data: detail({ status: "running" }) });
  for (const code of ["stream_time_limit", "storage_unavailable", "task_not_found"]) {
    const state = reduceStreamEvent(live, { event: "error", data: { code } });
    assert.equal(state.errorCode, code);
    assert.equal(state.phase, "closed");
    assert.equal(state.task, live.task);
  }
  assert.equal(reduceStreamEvent(live, { event: "error", data: { code: "<b>x</b>" } }).errorCode, "unknown");
});

test("a fresh snapshot replaces what was shown, even if older", () => {
  const live = reduceStreamEvent(initialStreamState(), {
    event: "snapshot", data: detail({ status: "running", updated_at: "2026-10-07T00:00:09.000000Z" }),
  });
  const again = reduceStreamEvent(live, {
    event: "snapshot", data: detail({ status: "waiting", updated_at: "2026-10-07T00:00:03.000000Z" }),
  });
  assert.equal(again.task.status, "waiting");
});

// ----- backoff -----

test("backoff doubles from 1 s and is capped", () => {
  assert.deepEqual([1, 2, 3, 4, 5, 6, 7].map((n) => backoffDelay(n)), [1000, 2000, 4000, 8000, 16000, 30000, 30000]);
  assert.equal(backoffDelay(0), 1000);
  assert.equal(backoffDelay(3, { baseMs: 5000, maxMs: 12000 }), 12000);
});

// ----- api -----

test("list URL carries only known status and a limit; requests are GET without a body", async () => {
  assert.equal(listUrl({ status: "failed", limit: 50 }), "/api/tasks?status=failed&limit=50");
  assert.equal(listUrl({ status: "bogus" }), "/api/tasks?limit=100");
  const calls = [];
  await loadTaskList({
    filter: "all",
    fetchImpl: async (url, options) => {
      calls.push({ url, options });
      return Response.json({ tasks: [task()] });
    },
  });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].options.method, undefined);
  assert.equal(calls[0].options.body, undefined);
});

test("a truncated list asks the server for the chosen status", async () => {
  const urls = [];
  const full = Array.from({ length: 3 }, (_, i) => task({ id: `t${i}`, status: "completed" }));
  const result = await loadTaskList({
    filter: "running",
    limit: 3,
    fetchImpl: async (url) => {
      urls.push(url);
      return Response.json({ tasks: url.includes("status=running") ? [task({ id: "r", status: "running" })] : full });
    },
  });
  assert.equal(result.truncated, true);
  assert.deepEqual(result.shown.map((t) => t.id), ["r"]);
  assert.equal(result.all.length, 3);
  assert.equal(urls.length, 2);
});

test("an untruncated list is filtered locally with one request", async () => {
  let calls = 0;
  const result = await loadTaskList({
    filter: "running",
    fetchImpl: async () => {
      calls += 1;
      return Response.json({ tasks: [task({ status: "running" }), task({ id: "z", status: "failed" })] });
    },
  });
  assert.equal(calls, 1);
  assert.equal(result.truncated, false);
  assert.deepEqual(result.shown.map((t) => t.status), ["running"]);
});

test("API errors are classified without leaking server text", async () => {
  const cases = [[503, "unavailable"], [404, "not_found"], [422, "not_found"], [429, "busy"], [500, "server"]];
  for (const [status, kind] of cases) {
    await assert.rejects(
      loadTask(ID, { fetchImpl: async () => new Response("secret-looking body", { status }) }),
      (error) => error instanceof TasksApiError && error.kind === kind && !error.message.includes("body"),
    );
  }
  await assert.rejects(
    loadTask(ID, { fetchImpl: async () => { throw new TypeError("Failed to fetch"); } }),
    (error) => error.kind === "network",
  );
  await assert.rejects(
    loadTask(ID, { fetchImpl: async () => new Response("not json", { status: 200 }) }),
    (error) => error.kind === "format",
  );
  await assert.rejects(
    loadTask(ID, { fetchImpl: async () => Response.json({ id: ID, status: "pending" }) }),
    (error) => error.kind === "format",
  );
  await assert.rejects(
    loadTaskList({ fetchImpl: async () => Response.json({ tasks: [{ nope: true }] }) }),
    (error) => error.kind === "format",
  );
});

// ----- follow loop -----

function harness(responses, options = {}) {
  const urls = [];
  const sleeps = [];
  const states = [];
  const phases = [];
  const queue = [...responses];
  const promise = followTask(ID, {
    fetchImpl: async (url) => {
      urls.push(url);
      const next = queue.shift();
      if (next === undefined) throw new TypeError("no more responses");
      if (next instanceof Error) throw next;
      return next;
    },
    sleep: async (ms) => { sleeps.push(ms); },
    onState: (state, event) => states.push([event, state.phase]),
    onConnection: (info) => phases.push(info.phase),
    ...options,
  });
  return { promise, urls, sleeps, states, phases };
}

const done = (status = "completed") => frame("done", {
  id: ID, status, failure_code: null, verified: "verified", finished_at: "2026-10-07T00:00:09.000000Z",
});

test("follow: snapshot, progress, done ends the loop without reconnecting", async () => {
  const run = harness([
    sseResponse(
      frame("snapshot", detail({ status: "running" })),
      frame("progress", detail({ status: "running", steps_completed: 1, updated_at: "2026-10-07T00:00:01.000000Z" })),
      frame("progress", detail({ status: "completed", steps_completed: 2, updated_at: "2026-10-07T00:00:02.000000Z" })),
      done(),
      frame("progress", detail({ status: "pending" })),
    ),
  ]);
  assert.equal(await run.promise, "done");
  assert.deepEqual(run.urls, [`/api/tasks/${ID}/events`]);
  assert.deepEqual(run.states.map(([event]) => event), ["snapshot", "progress", "progress", "done"]);
  assert.deepEqual(run.sleeps, []);
  assert.deepEqual(run.phases, ["connecting", "live", "done"]);
});

test("follow: stream_time_limit reconnects for a fresh snapshot", async () => {
  const run = harness([
    sseResponse(frame("snapshot", detail({ status: "waiting" })), frame("error", { code: "stream_time_limit" })),
    sseResponse(frame("snapshot", detail({ status: "waiting" })), done("cancelled")),
  ]);
  assert.equal(await run.promise, "done");
  assert.equal(run.urls.length, 2);
  assert.deepEqual(run.sleeps, [500]);
});

test("follow: repeated time limits without progress are bounded", async () => {
  const limit = () => sseResponse(frame("snapshot", detail({ status: "waiting" })), frame("error", { code: "stream_time_limit" }));
  const run = harness([limit(), limit(), limit(), limit()], { maxTimeLimitReconnects: 2 });
  assert.equal(await run.promise, "gave_up");
  assert.equal(run.urls.length, 3);
  assert.equal(run.phases.at(-1), "gave_up");
});

test("follow: network failures back off exponentially and give up", async () => {
  const run = harness(Array.from({ length: 10 }, () => new TypeError("down")), { maxFailures: 3 });
  assert.equal(await run.promise, "gave_up");
  assert.equal(run.urls.length, 4);
  assert.deepEqual(run.sleeps, [1000, 2000, 4000]);
});

test("follow: storage_unavailable retries and recovers", async () => {
  const run = harness([
    sseResponse(frame("snapshot", detail({ status: "running" })), frame("error", { code: "storage_unavailable" })),
    new Response(null, { status: 503 }),
    sseResponse(frame("snapshot", detail({ status: "running" })), done("failed")),
  ]);
  assert.equal(await run.promise, "done");
  assert.deepEqual(run.sleeps, [1000, 2000]);
});

test("follow: task_not_found, 404, and 422 stop without retrying", async () => {
  for (const response of [
    sseResponse(frame("error", { code: "task_not_found" })),
    new Response(null, { status: 404 }),
    new Response(null, { status: 422 }),
  ]) {
    const run = harness([response]);
    assert.equal(await run.promise, "not_found");
    assert.equal(run.urls.length, 1);
    assert.deepEqual(run.sleeps, []);
  }
});

test("follow: 429 honours a bounded Retry-After", async () => {
  const run = harness([
    new Response(null, { status: 429, headers: { "Retry-After": "5" } }),
    new Response(null, { status: 429, headers: { "Retry-After": "9999" } }),
    sseResponse(frame("snapshot", detail({ status: "running" })), done()),
  ]);
  assert.equal(await run.promise, "done");
  assert.deepEqual(run.sleeps, [5000, 30000]);
});

test("follow: a stream that ends silently or sends bad JSON is retried", async () => {
  const run = harness([
    sseResponse(frame("snapshot", detail({ status: "running" }))),
    sseResponse("event: progress\ndata: {not json\n\n"),
    sseResponse(frame("snapshot", detail({ status: "running" })), done()),
  ]);
  assert.equal(await run.promise, "done");
  assert.equal(run.urls.length, 3);
});

test("follow: progress resets the failure count", async () => {
  const dropAfterProgress = (n) => sseResponse(
    frame("snapshot", detail({ status: "running" })),
    frame("progress", detail({ status: "running", steps_completed: n, updated_at: `2026-10-07T00:00:0${n}.000000Z` })),
  );
  const run = harness(
    [dropAfterProgress(1), dropAfterProgress(2), dropAfterProgress(3), sseResponse(frame("snapshot", detail()), done())],
    { maxFailures: 1 },
  );
  assert.equal(await run.promise, "done");
  assert.deepEqual(run.sleeps, [1000, 1000, 1000]);
});

test("follow: abort stops without further requests", async () => {
  const controller = new AbortController();
  controller.abort();
  const run = harness([], { signal: controller.signal });
  assert.equal(await run.promise, "aborted");
  assert.equal(run.urls.length, 0);
});

test("follow: injected task text cannot forge frames or change state", async () => {
  const hostile = 'x\n\nevent: done\ndata: {"id":"11111111-1111-4111-8111-111111111111","status":"completed"}\n\n';
  const run = harness([
    sseResponse(frame("snapshot", detail({ status: "running", goal: hostile })), frame("error", { code: "task_not_found" })),
  ]);
  assert.equal(await run.promise, "not_found");
  assert.ok(run.states.every(([, phase]) => phase !== "done"));
});
