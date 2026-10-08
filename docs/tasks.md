# Tasks

This is the first slice of the task layer (roadmap T4): durable task state, a small single-worker queue, progress, and result verification. The queue itself is a library: there is no scheduler or real tool behind it yet. A small read-only HTTP API reports task state and progress (see [HTTP API](#http-api-read-only)); there is no UI.

Code: `backend/tasks/models.py` (frozen dataclasses and enums), `backend/tasks/repository.py` (`TaskRepository`), `backend/tasks/queue.py` (`TaskQueue` and the executor and verifier contracts). Storage is SQLite schema version 7 (`tasks`, `task_steps`), applied by the existing migration path; earlier data is kept and a database from a newer schema is refused.

## Task record

A task keeps its goal (at most 2000 characters), its steps (1 to 50, at most 500 characters each, fixed when the task is created), the current step, a target device label, status, timestamps (`created_at`, `updated_at`, `started_at`, `finished_at`), a bounded result summary (at most 4000 characters), a fixed failure code, a fixed waiting reason, the attempt number, and a verification state. The target device is only a label; nothing routes work by it yet.

The goal is untrusted data. It is stored and given to an executor verbatim, and nothing in this package interprets it, so text inside a goal cannot change a task's state.

Fixed codes, never free text:

- Failure: `execution_failed`, `verification_failed`, `timeout`, `interrupted`, `internal_error`.
- Waiting: `needs_confirmation`, `needs_input`, `dependency`.
- Verification: `not_verified`, `verified`, `verification_failed`.
- Step status: `pending`, `running`, `completed`, `failed`, `skipped`, `cancelled`.

## State machine

```
```
pending ---> running ---> completed   (only via complete(), needs "verified")
   |          |   ^  \
   |          |   |   `--> failed     (fixed failure code)
   |          v   |
   |         waiting ----> failed      (waiting -> running only by explicit resume)
   |
   `--> cancelled   (reachable from pending, running, and waiting)

completed, failed, cancelled are final
```

Allowed moves: `pending` to `running` or `cancelled`; `running` to `waiting`, `completed`, `failed`, or `cancelled`; `waiting` to `running`, `cancelled`, or `failed`. `failed`, `completed`, and `cancelled` are final and never change (a database trigger enforces this as well). Each move is a compare-and-swap on the status the caller last saw, so two writers cannot both succeed; the loser gets `TaskStateChanged`.

Two moves are not available through `transition`:

- Completion goes only through `complete(task_id, result_summary, verification)`, which refuses anything but `verified` (the database also refuses a completed row that is not verified). Done means the postcondition holds, not that an executor said so.
- A verification failure goes only through `fail_verification`, which records `failed(verification_failed)`. A failed check never produces `completed`.

## Queue and claim policy

`TaskQueue.submit` creates a pending task. `run_next(executor, verifier)` claims the oldest pending task (by creation time) and runs it. The claim is a `BEGIN IMMEDIATE` transaction that selects the oldest pending row and updates it to `running` guarded by `status = 'pending'`, so the same task cannot be claimed twice, even from separate connections or processes sharing one database file. A waiting task is never picked up by `run_next`.

The executor receives an immutable `Task` snapshot, a `ProgressReporter` (`start_step`, `finish_step` with a bounded note), and a `CancellationToken`. It returns an `ExecutionOutcome` that is exactly one of: a success claim with a summary, a request to wait with a reason, or a failure. A success claim is then checked by a separate `Verifier` against the real state; only a passing check completes the task. `run_next` requires a verifier, so a task cannot be completed without one.

Progress: `current_step` only moves forward. Starting a step needs the previous step to be finished, a finished step records its status and time, and notes are bounded. Updates are accepted only while the task is running. Steps still running when a task fails or is cancelled are marked failed or cancelled.

Outcomes of a run:

| Situation | Result |
| --- | --- |
| verified success | `completed` |
| verifier says the postcondition does not hold | `failed(verification_failed)` |
| executor reports failure | `failed(execution_failed)` |
| executor or verifier exceeds the timeout (default 300 s, per call override) | `failed(timeout)` |
| executor or verifier raises | `failed(internal_error)`; only the exception type is logged and no message is stored or returned |
| cancelled while running | `cancelled` (cancellation wins over a late success claim) |
| executor asks to wait | `waiting` with the reason |
| the worker coroutine itself is cancelled | `failed(interrupted)` |

Cancellation: `cancel` moves a pending or waiting task straight to `cancelled`. For a running task it sets the token of the queue that is running it; the run then cancels the executor and records `cancelled`. If the executor ignores cancellation, the run stops waiting after a short grace period. A queue that is not running the task refuses to cancel it (`TaskNotCancellable`). Executors run on the caller's event loop, so blocking calls inside them cannot be interrupted by a timeout or cancel.

Waiting: a waiting task stays waiting until someone calls `resume(task_id, executor, verifier)`, which swaps `waiting` to `running` and runs the executor again with the progressed snapshot. There is no timer and no automatic resume.

## Restart recovery

`recover_in_flight()` marks every task still `running` after a restart as `failed(interrupted)` and returns them. It does not re-run them: a task that was running may already have had external side effects, which are not known to be idempotent, so re-running could repeat them. Pending tasks stay pending and waiting tasks stay waiting. Call it once at startup before any worker starts; with a single worker, every running row at that moment belongs to a dead process.

## Retry

There is no automatic retry. `retry(task_id)` (also on the queue) is explicit and creates a new pending task with the same goal, steps, and device label, `attempt` one higher, and `retry_of` pointing at the original. It works only for a `failed` or `cancelled` task, only once per task, and only while the attempt number stays within `max_attempts` (default 3, at most 10). The original stays unchanged. Retrying an `interrupted` task is a deliberate human decision because its earlier attempt may have partly run.

## Not done in this slice

- No daemon, scheduler, or timer that calls the queue, and no multi-worker coordination, leases, or heartbeats. A second process may only claim safely through the atomic claim; it must not call `recover_in_flight` while another worker is live.
- No device routing: the target device is a label.
- No real tools or planner. Executors and verifiers are contracts; this package contains only fakes in tests. Permission levels from [tools.md](tools.md) are not enforced here.
- No UI, push notification, or write API. Progress is stored, readable over the read-only HTTP API, and streamed over SSE; what the screen does with it is later work.
- Cross-process cancel of a running task, and deletion or archival of old tasks.

## HTTP API (read-only)

Code: `backend/api/tasks.py` (`create_tasks_router`), registered in `create_app`. All routes are `GET` and report only what is persisted in SQLite. Nothing is estimated: there is no percentage, no ETA, and a task is shown as `running` only while its stored status is `running`. Step counts (`steps_total`, `steps_completed`) are counted from stored step statuses.

| Route | Purpose |
| --- | --- |
| `GET /api/tasks?status=&limit=` | `{"tasks": [...]}` summaries, oldest first (creation time, then insertion order). `status` is one of the task statuses; `limit` is 1 to 100, default 50. |
| `GET /api/tasks/{task_id}` | One task with its goal, `result_summary`, and `steps` (index, description, status, `started_at`, `finished_at`, note). |
| `GET /api/tasks/{task_id}/events` | Server-sent events for one task, described below. |

Errors use the same `{"detail": ...}` shape as chat: 422 for a malformed UUID, an unknown `status`, or an out-of-range `limit`; 404 `task not found` (for the stream this is returned before any streaming starts); 429 `too many task streams` (with `Retry-After`); 503 `task storage unavailable`. Storage error text is logged by type only and never returned. Other methods on these paths return 405.

Fields: `id`, `goal`, `status`, `current_step`, `steps_total`, `steps_completed`, `target_device`, `attempt`, `retry_of`, `verified`, `failure_code`, `waiting_reason`, and the UTC timestamps `created_at`, `updated_at`, `started_at`, `finished_at` (`...Z`, null when unset). `failure_code` and `waiting_reason` are the fixed values listed above or null, never free text. The goal, step descriptions, notes, and result summary are untrusted data: they appear only as JSON string values, and a client must render them as text, not markup or instructions.

### Event stream

Framing matches the chat stream (`event:` and `data:` lines, one JSON object per event). JSON escapes newlines, so task text cannot end or forge a frame. A line starting with `:` is a comment (heartbeat) and should be ignored.

| Event | Data | When |
| --- | --- | --- |
| `snapshot` | the same object as `GET /api/tasks/{id}` | immediately |
| `progress` | the same object again | each time the persisted task or step state differs from the last one sent |
| `done` | `id`, `status`, `failure_code`, `verified`, `finished_at` | once the task is `completed`, `failed`, or `cancelled`; the stream then closes |
| `error` | `{"code": ...}` with `stream_time_limit`, `storage_unavailable`, or `task_not_found` | then the stream closes |

A task that is already terminal gets `snapshot` and `done` only. A `waiting` task is not terminal, so its stream stays open (until the time limit). The server polls the repository every 0.5 s, so `progress` events are state notifications, not a log: several quick changes between two polls arrive as one event carrying the latest state. After `error` with `stream_time_limit` (300 s hard cap) a client may reconnect and receives a fresh `snapshot`. An idle stream sends `: keep-alive` every 15 s.

### Limits and resource handling

- At most 20 streams at once; further requests get 429. A slot is taken before the task is read and returned when the response ends for any reason (normal end, error, client disconnect, cancellation, or a failed send), including when the body never started.
- Disconnect cancels the generator, which stops polling. A server that reports a gone client only as a failed send (ASGI spec 2.4 and later) is noticed at the next send, so an idle stream can hold its slot for up to the heartbeat interval; uvicorn reports disconnects directly.
- Each repository call opens and closes its own short-lived SQLite connection through `Database.connect`. The list and detail endpoints are sync functions run in FastAPI's thread pool; the stream runs every poll in a worker thread, so the event loop is never blocked by SQLite. Worker threads are shared with other sync endpoints, and a poll that is already running finishes (SQLite timeout 5 s) before a cancelled stream completes.
- Local-only posture, as for the rest of the app: bind to `127.0.0.1`, run one worker, no authentication, and no CORS changes. Goals and results can be sensitive, so do not expose this API beyond the local machine until access control exists.
- The list has no pagination: it returns the oldest tasks first, up to the limit. Use the `status` filter to reach active tasks in a long history.

### Not done in the API

- No write, cancel, retry, or create endpoints. Those change state and need a separate permission decision before they exist.
- No authentication or per-client access control.
- No UI and no notification delivery (desktop, mobile, or otherwise); the stream is the data source a screen can consume.
- No cross-process change feed. The stream observes SQLite by polling, so it also sees changes made by another process, at polling latency.
