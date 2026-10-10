# Tool confirmation (human approval)

Some tools must never run on the model's say-so alone: anything the permission policy marks Red, or Yellow and not allow-listed (see [tools.md](tools.md)). This slice adds the missing human step so later write or shell tools can be registered safely. **It registers no tools.** There is no filesystem write tool, no shell registration and no LLM wiring here; the tests use fake tools only.

## Flow

```text
model/agent -> ConfirmedExecutor.execute(call)
                 -> ToolRegistry.invoke(call)         "confirmation_required"  (nothing ran)
                 -> store.request(call)               pending row + redacted summary
human       -> Approvals page / API: approve or deny   (the only caller of decide)
agent       -> ConfirmedExecutor.execute(call) again, or waiting with wait_seconds
                 -> store.consume(tool, args)         atomic, one-shot
                 -> ToolRegistry.invoke(call, grant)  the existing permission path accepts the grant
```

- `backend/tools/approvals.py`: `ApprovalStore` (SQLite, schema v9 table `tool_approvals`), `ApprovalRequester` (the narrow view the executor holds), `summarize_arguments`.
- `backend/tools/confirmed.py`: `ConfirmedExecutor`. Policy stays in `PermissionPolicy`; the executor only reacts to its `confirmation_required` answer. A deny-list entry or failed scope check is still a plain denial and creates no request.
- `backend/api/approvals.py`: `GET /api/approvals`, `POST /api/approvals/{id}/approve`, `POST /api/approvals/{id}/deny`. The handlers record a decision and return; they never run, schedule or wake a tool.
- `frontend/approvals.html` (route `/approvals`, nav item "承認"): lists pending requests with expiry and Approve/Deny buttons. Text nodes only.

## Rules

- A request records the tool name, the SHA-256 of the canonical (sorted, compact) JSON arguments, a redacted summary, `requested_at`, `expires_at` and a state: `pending`, `approved`, `denied` or `expired`. `consumed_at` is write-once.
- A decision is bound to the tool name and exact argument digest. It is not bound to the `call_id`, so the agent can retry after approval with a fresh id; the existing in-process grant ledger still refuses a replayed `call_id`.
- Default lifetime is 5 minutes (allowed 10 s to 1 h). Past `expires_at` a request can no longer be decided and an approval can no longer be consumed. Expiry is applied lazily on every operation.
- A call runs only if an approved, unexpired, matching, unconsumed row exists. `consume` is one `UPDATE ... WHERE id = (SELECT ...)` in an immediate transaction; of N concurrent callers at most one gets it.
- Asking again for the same pending call returns the same request, and at most 50 requests may be pending, so a looping model cannot flood the human.
- Anything unclear means "not approved": storage errors, a full queue, an unknown id, a wait that times out, a request that expires while waiting.
- Results use the existing fixed codes: `confirmation_required` (still waiting or timed out) and `permission_denied` (denied, expired, or approval unavailable). `ApprovalOutcome` carries the request id and state for the caller.

## Redacted summary

The summary is a fixed shape: at most 8 top-level argument names (sorted, 40 characters each) with previews of at most 80 characters, the number of omitted fields, the canonical argument size in bytes, and the first 12 hex characters of the digest. Control and bidi characters are replaced. Nested values show only their type and size. Names that look like secrets (password, token, key, auth, cookie, session, ...) and values that look like credentials (well-known key prefixes, bearer strings, long unbroken token-like strings) show `[redacted]`. Redaction is best effort: the human sees only a preview, so tool authors must keep security-relevant arguments in short, readable top-level fields. The digest, not the summary, is what an approval binds to.

## Who can approve

- The executor holds an `ApprovalRequester` (request, read state, consume). It has no approve or deny method. `ToolContext` carries no approvals handle, so a tool cannot reach the store either.
- `ApprovalStore.decide` is called only from `backend/api/approvals.py`; a test scans the source to keep it so. There is no tool, prompt path or LLM output that maps to that route.
- This is a code-structure guarantee inside one process, not a sandbox: code that can import the store and open the database file can write rows. The SQL trigger additionally refuses to re-decide, re-bind or un-consume a row, but a process with file access remains trusted. Keep untrusted tool code out of the backend process.

## Auth and the public allowlist

With login enabled (see [auth.md](auth.md)) the auth middleware is deny-by-default and the approval routes and page are **not** in the public allowlist: no session gets 401 (API) or a redirect to `/login` (page), and unsafe methods also need a same-origin `Origin`. Without login the routes use the same-origin guard as the other unsafe routes and rely on the app being local-only.

## Threat notes

| Threat | Mitigation |
| --- | --- |
| Replay: reuse one approval for a second call | `consumed_at` is set atomically; a second `consume` finds nothing. The trigger forbids clearing it. |
| Arg-swap: get a harmless call approved, run a different one | The digest covers tool name and every argument; any difference needs a new request. The executor freezes the arguments once, so what was checked is what runs. |
| Expired approval | `expires_at` is checked in the consume query and in `decide`; approving at the last second still leaves the call unusable. |
| Double-consume under concurrency | Single-statement atomic update; tested with 16 threads and 8 concurrent executions. |
| CSRF from another site | Same-origin check on `Origin`/`Referer`/`Sec-Fetch-Site`, plus a required `X-Jarvis-Confirm: 1` header that a cross-site page cannot send without a CORS preflight (never granted). With login on the session cookie and `Origin` are also required. `GET` never changes state. |
| Agent approves itself | No code path from model output or tools to `decide`; see above. |
| Queue flooding / approval fatigue | De-duplication, a pending cap, short expiry. |
| Secrets in the approval screen or database | Only the redacted summary and a digest are stored; the raw arguments never are. |
| Markup injection through names or previews | JSON only; the page uses `textContent`. |

## Not covered

- Who the confirming human is. Any authenticated session may approve; no per-person identity or audit actor is recorded.
- Finished rows are kept (no pruning) and there is no history screen.
- The executor is not wired into chat or the task queue yet; nothing registers a Red tool. Waiting inside a request handler is not provided: callers either poll by calling `execute` again or pass `wait_seconds` from a background task.
