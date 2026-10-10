// Live progress for one task over GET /api/tasks/{id}/events (read-only SSE).
//
// Events: snapshot -> progress* -> done, or error {code}. The stream is read with fetch and the
// shared sse.js reader. Reconnects are bounded: see followTask.
import { eventsUrl, classifyStatus } from "./tasks-api.js";
import { sessionEnded } from "./session.js";
import { readEventStream } from "./sse.js";
import { normalizeTask } from "./tasks-view.js";

export const STREAM_ERROR_CODES = ["stream_time_limit", "storage_unavailable", "task_not_found"];

export function initialStreamState() {
  return { task: null, phase: "connecting", errorCode: null };
}

// Pure reducer. `data` is the decoded JSON of one event. Anything malformed or for another
// task is ignored, so a bad event can never replace what is already shown.
export function reduceStreamEvent(state, { event, data }) {
  if (event === "snapshot") {
    const task = normalizeTask(data);
    if (task === null || !Array.isArray(task.steps)) return state;
    if (state.task && state.task.id !== task.id) return state;
    // A snapshot comes from a fresh connection and always replaces what was shown.
    return { task, phase: "live", errorCode: null };
  }
  if (event === "progress") {
    const task = normalizeTask(data);
    if (task === null || !Array.isArray(task.steps) || !state.task || state.task.id !== task.id) {
      return state;
    }
    // Timestamps are fixed-width UTC strings, so string order is time order.
    if (state.task.updated_at && task.updated_at && task.updated_at < state.task.updated_at) {
      return state;
    }
    return { ...state, task, phase: "live" };
  }
  if (event === "done") {
    if (!state.task || !data || typeof data !== "object" || data.id !== state.task.id) return state;
    const task = { ...state.task };
    if (typeof data.status === "string") task.status = data.status;
    task.failure_code = typeof data.failure_code === "string" ? data.failure_code : null;
    if (typeof data.verified === "string") task.verified = data.verified;
    if (typeof data.finished_at === "string") task.finished_at = data.finished_at;
    return { ...state, task, phase: "done" };
  }
  if (event === "error") {
    const code = typeof data?.code === "string" ? data.code : null;
    return {
      ...state,
      phase: "closed",
      errorCode: STREAM_ERROR_CODES.includes(code) ? code : "unknown",
    };
  }
  return state;
}

// Deterministic exponential backoff: 1 s, 2 s, 4 s ... capped. `attempt` counts from 1.
export function backoffDelay(attempt, { baseMs = 1000, maxMs = 30000, factor = 2 } = {}) {
  const step = Math.max(1, Math.floor(attempt));
  return Math.min(maxMs, baseMs * factor ** (step - 1));
}

const defaultSleep = (ms, signal) =>
  new Promise((resolve) => {
    if (signal?.aborted) return resolve();
    const timer = setTimeout(done, ms);
    function done() {
      signal?.removeEventListener("abort", done);
      clearTimeout(timer);
      resolve();
    }
    signal?.addEventListener("abort", done, { once: true });
  });

class StopReading extends Error {}

function parse(data) {
  try {
    return JSON.parse(data);
  } catch {
    return undefined;
  }
}

function retryAfterMs(response, maxMs) {
  const seconds = Number(response.headers?.get?.("Retry-After"));
  return Number.isFinite(seconds) && seconds > 0 ? Math.min(seconds * 1000, maxMs) : 0;
}

// One connection attempt. Resolves to {kind, state, progressed}, kind being one of:
// done | time_limit | not_found | storage | busy | network | ended | unknown | aborted.
async function connectOnce(taskId, state, { fetchImpl, signal, onState, maxDelayMs }) {
  let current = state;
  let progressed = false;
  let outcome = null;
  let response;
  try {
    response = await fetchImpl(eventsUrl(taskId), {
      headers: { Accept: "text/event-stream" },
      signal,
    });
  } catch (error) {
    return { kind: error?.name === "AbortError" || signal?.aborted ? "aborted" : "network", state };
  }
  if (response.status === 401) {
    // The session ended: go to the login page once and do not reconnect.
    sessionEnded();
    return { kind: "unauthorized", state };
  }
  if (!response.ok) {
    const kind = classifyStatus(response.status);
    const map = { not_found: "not_found", busy: "busy", unavailable: "storage", server: "network" };
    return {
      kind: map[kind],
      state,
      delayMs: kind === "busy" ? retryAfterMs(response, maxDelayMs) : 0,
    };
  }
  if (!response.body) return { kind: "network", state };

  try {
    await readEventStream(response.body, ({ event, data }) => {
      const decoded = parse(data);
      if (decoded === undefined) throw new StopReading();
      const next = reduceStreamEvent(current, { event, data: decoded });
      if (event === "progress" && next !== current) progressed = true;
      if (next !== current) {
        current = next;
        onState(current, event);
      }
      if (event === "done") outcome = "done";
      if (event === "error") {
        outcome = {
          stream_time_limit: "time_limit",
          storage_unavailable: "storage",
          task_not_found: "not_found",
        }[current.errorCode] ?? "unknown";
      }
      // Stop reading at once: the server closes after done/error and the reader is cancelled.
      if (outcome) throw new StopReading();
    });
  } catch (error) {
    if (!(error instanceof StopReading)) {
      return { kind: signal?.aborted ? "aborted" : "network", state: current, progressed };
    }
  }
  return { kind: outcome ?? (signal?.aborted ? "aborted" : "ended"), state: current, progressed };
}

// Follows one task until it ends. Resolves to why it stopped:
//   done | not_found | unauthorized | gave_up | aborted
// Bounded behaviour:
//   - stream_time_limit (server hard cap, 300 s) reconnects after `timeLimitDelayMs` and gets a
//     fresh snapshot; at most `maxTimeLimitReconnects` in a row without any progress event.
//   - network errors, storage_unavailable, 429 and other failures retry with exponential
//     backoff, giving up after `maxFailures` consecutive failures. A connection that delivered a
//     progress event resets the failure count.
export async function followTask(taskId, {
  fetchImpl = fetch,
  signal,
  onState = () => {},
  onConnection = () => {},
  sleep = defaultSleep,
  maxFailures = 6,
  maxTimeLimitReconnects = 20,
  timeLimitDelayMs = 500,
  backoff = {},
} = {}) {
  let state = initialStreamState();
  let failures = 0;
  let timeLimits = 0;
  const maxDelayMs = backoff.maxMs ?? 30000;
  const report = (next, event) => {
    const previous = state.phase;
    state = next;
    onState(next, event);
    if (next.phase !== previous && (next.phase === "live" || next.phase === "done")) {
      onConnection({ phase: next.phase });
    }
  };
  onConnection({ phase: "connecting" });
  while (!signal?.aborted) {
    const result = await connectOnce(taskId, state, {
      fetchImpl, signal, onState: report, maxDelayMs,
    });
    state = result.state;
    if (result.kind === "aborted") return "aborted";
    if (result.kind === "done") return "done";
    if (result.kind === "not_found") return "not_found";
    if (result.kind === "unauthorized") return "unauthorized";
    if (result.progressed) {
      failures = 0;
      timeLimits = 0;
    }
    let delay;
    if (result.kind === "time_limit") {
      failures = 0;
      timeLimits += 1;
      if (timeLimits > maxTimeLimitReconnects) {
        onConnection({ phase: "gave_up", attempt: timeLimits });
        return "gave_up";
      }
      delay = timeLimitDelayMs;
    } else {
      failures += 1;
      if (failures > maxFailures) {
        onConnection({ phase: "gave_up", attempt: failures });
        return "gave_up";
      }
      delay = Math.max(backoffDelay(failures, backoff), result.delayMs ?? 0);
    }
    onConnection({ phase: "reconnecting", attempt: failures + timeLimits, delayMs: delay });
    await sleep(delay, signal);
  }
  return "aborted";
}
