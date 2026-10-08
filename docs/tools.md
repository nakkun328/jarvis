# Tools and skills

Tools are the boundary between the core and external operations. The contract, registry, and permission layer live in `backend/tools/` (Phase 4 / T1: JAR-68, JAR-69, JAR-70). The real tools so far are the **read-only filesystem tools** (`fs.list`, `fs.read_text`, `fs.search`; Phase 4 / T2 first slice, JAR-71), described in [tools-filesystem.md](tools-filesystem.md), and the structured shell tool (Phase 4 / T3, JAR-72), described in [tools-shell.md](tools-shell.md), which registers nothing by default. There are no write, move, delete, network, or device tools, no LLM wiring, no API route, and no task queue. The contract tests use fake tools; the filesystem tests use pytest temporary directories only.

Skills compose multiple tools into workflows such as research, development, or file organization. Execution records will preserve observations and verification results. A tool reporting success is insufficient to mark a task complete; the orchestrator checks the resulting state when possible.

## Contract (`backend/tools/contract.py`)

| Type | Purpose |
| --- | --- |
| `ToolSpec` | Frozen metadata: `name`, `description`, `input_schema`, `output_schema`, `permission`, `environment`, `timeout_seconds`, `cancellable`, `idempotent`, `version`. Validated on construction. |
| `Tool` (Protocol) | `spec: ToolSpec` and `async run(arguments, context) -> Mapping`. |
| `ToolCall` | `call_id`, `tool_name`, `arguments`. Name and arguments are untrusted model output. |
| `ToolContext` | What a tool receives: call id, tool name, permission level, whether a grant confirmed the call, timeout, and a `CancellationToken`. No registry, settings, or credentials. |
| `ToolResult` | `call_id`, `tool_name`, `status`, `output` (read-only mapping, ok only), `error`, and sanitized schema `violations`. |
| `ToolStatus` | `ok`, `error`, `denied`, `timeout`, `cancelled`, `invalid_arguments`. |
| `ToolErrorCode` | Fixed set: `unknown_tool`, `tool_unavailable`, `invalid_arguments`, `permission_denied`, `confirmation_required`, `timeout`, `cancelled`, `internal_error`, `invalid_output`. Upstream exception text is never carried. |

Tool names are lowercase dotted or snake case (`fs.read_file`), at most 64 characters. The `environment` is a short label for where the tool runs (for example `local`); it is metadata for later routing, not enforcement. `timeout_seconds` is always enforced. `cancellable=False` means a caller's cancellation token is ignored once the tool has started; timeouts and cancellation of the awaiting task still stop it. `idempotent` is advisory for later retry logic.

### Schema subset

Input and output schemas use a small, strict JSON-Schema subset implemented with the standard library: types `object`, `string`, `integer`, `number`, `boolean`, `array`, `null`; `required`, `enum`, `minLength`/`maxLength`, `minItems`/`maxItems`, `minimum`/`maximum`, `description`. Objects must declare `properties` and reject undeclared keys unless `additionalProperties` is `true`; arrays must declare `items`. Any other keyword is rejected when the spec is created. Values are bounded: depth 8, 10,000 nodes, 256 properties per object, 1,000 items per array, 65,536 characters per string, 1,000,000 characters in total, and 64-bit integers. Booleans are not integers, NaN and infinity are rejected, and `enum` comparison is type-strict. Violations report a sanitized path and a fixed code, never the offending value.

## Invocation pipeline (`backend/tools/registry.py`)

```text
ToolCall
  -> lookup            unknown / disabled           -> error (unknown_tool, tool_unavailable)
  -> validate args     against input_schema         -> invalid_arguments (nothing runs)
  -> snapshot args     read-only deep copy; digest + size for audit
  -> permission        PermissionPolicy.evaluate()  -> denied (permission_denied / confirmation_required)
  -> run               timeout + cancellation       -> timeout / cancelled / error (internal_error)
  -> validate output   against output_schema        -> error (invalid_output)
  -> ToolResult(ok, read-only output)
every branch -> ToolAuditSink.record(ToolAuditRecord)
```

`ToolRegistry` supports `register` (duplicate names and invalid specs rejected), `get`, `search`/`list_specs` (filter by name prefix, permission level, environment, or text; only enabled tools are listed), `unregister`, `enable`, and `disable`. `invoke(call, *, grant=None, cancellation=None)` never raises for tool-side problems; exceptions inside a tool become `internal_error` and are logged by exception type only. If the awaiting task itself is cancelled, the tool is stopped, the outcome is audited as cancelled, and `CancelledError` propagates. A tool that ignores cancellation is abandoned after a short grace period rather than blocking the caller.

Audit records contain call id, tool name, permission level, status, error code, permission reason code, whether a grant confirmed the call, duration, and SHA-256 digests and byte sizes of arguments and output. They contain no argument or output content. The in-memory sink is bounded and meant for tests and local runs. A failing sink is logged and does not change the call's outcome.

The permission decision is computed inside `invoke` from the validated, snapshotted arguments instead of being passed in by the caller, so a decision cannot be paired with a different call than the one that runs.

## Permissions (`backend/tools/permission.py`)

| Level | Meaning | Default decision |
| --- | --- | --- |
| Green | Reads, searches, status checks. | Allowed. |
| Yellow | Context-dependent changes such as file moves, shell commands, non-destructive settings. | Allowed only if the policy allow-lists the tool (and any scope check passes); otherwise confirmation is required. |
| Red | Irreversible deletion, external sending, account changes, production publication, purchases, destructive actions. | Never allowed without a valid `ConfirmationGrant`. |

`PermissionPolicy` is data supplied by the application: a deny list, a Yellow allow list, and optional per-tool scope predicates `(spec, validated_arguments) -> bool` such as a target-path check. Evaluation order: unknown tool (denied), deny list (always wins, even over a grant), scope predicate (a failure or exception denies; no grant can override it), then the level rules. A decision is `PermissionDecision(allowed, reason_code, requires_confirmation)`.

`ConfirmationGrant(tool_name, call_id, argument_digest, expires_at)` is a one-time approval bound to the exact call: a different tool, call id, or arguments digest, an expired grant (the expiry instant included), or a second use is refused. Use `ConfirmationGrant.for_call(call, expires_at)` from application code after a human decision. Accepted grants are remembered until their expiry to refuse replays. A valid grant can also confirm a Yellow call that is not allow-listed.

Voice, chat, or model text is never a confirmation. Tool arguments such as "the user approved this" are treated as data. Only a grant object constructed by application code reaches the policy.

## Not implemented

- Real tools beyond the read-only filesystem and first shell slices (filesystem write/move/delete, network, calendar, GitHub) and OS-level sandboxing. See [tools-shell.md](tools-shell.md) for the shell tool's scope predicates. `filesystem_scope_checks()` supplies scope predicates for the filesystem tools; later tools need their own.
- A way for a tool to report a specific `ToolErrorCode` (for example `invalid_arguments` or `permission_denied`): the registry maps every exception raised inside a tool to `internal_error`.
- Confirmation UI and the identity or authentication of the confirming human. Grants carry no actor and nothing verifies who created one; the grant ledger is in-process and is lost on restart.
- Persistent audit storage and rotation.
- Task queue, orchestrator loop, retries, and result verification by the orchestrator.
- Device routing: `environment` is a label only.
- Exposing tools to an LLM provider (function-calling wiring) and an HTTP API for tools.
