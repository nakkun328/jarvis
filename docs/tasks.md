# Tasks

This is the first slice of the task layer (roadmap T4): durable task state, a small single-worker queue, progress, and result verification. It is a library only. There is no API route, UI, scheduler, or real tool behind it yet.

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
- No API route, UI, or progress notification to the screen. Progress is stored and readable; pushing it is later work.
- Cross-process cancel of a running task, and deletion or archival of old tasks.
