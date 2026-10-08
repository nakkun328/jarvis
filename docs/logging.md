# Logging

JARVIS writes one JSON object per line to stderr. The level comes from `JARVIS_LOG_LEVEL`. `configure_logging(level)` runs at application startup, replaces any existing root handlers, and can be called repeatedly. Uvicorn's own loggers are routed through the same handler and its plain-text access log is disabled, because the request record below replaces it and uvicorn's version includes query strings.

## Record format

Fixed fields on every line:

| Field | Meaning |
| --- | --- |
| `timestamp` | UTC, ISO 8601 with milliseconds and a `Z` suffix. |
| `level` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. |
| `logger` | Logger name. |
| `event` | The log message. Prefer short event names such as `chat.provider_failed` over sentences. |

After those come the active log context (for example `request_id`), then fields passed with `extra={...}`. Extra fields are bounded: at most 24 fields, strings cut at 2000 characters, containers at 20 items and 4 levels. Bytes become `<bytes len=N>` and unknown objects become `<TypeName>`; `str()` of an arbitrary object is never used. Extra names that collide with a fixed field are emitted as `extra_<name>`. Output is ASCII-only JSON, so a message cannot inject extra lines.

Exceptions add `error_type`, optionally `error_chain` (types of wrapped causes), `traceback`, and `cause_traceback` (frames of the root cause). See the exception policy below.

## Request correlation and access records

`RequestLoggingMiddleware` (`backend/api/request_logging.py`) is pure ASGI, so it works for streaming (SSE) responses too.

- A single `X-Request-ID` request header of 8 to 64 characters from `A-Z a-z 0-9 . _ -`, starting with a letter or digit, is kept. Anything else (too short or long, other characters, duplicated header) is replaced by a generated UUID4.
- The ID is returned in the `X-Request-ID` response header and attached as `request_id` to every record logged during the request, including records emitted while a stream is being sent, in worker threads, and in tasks started by the request.
- Each request produces one `http.request` record with `method`, `path`, `status`, and `duration_ms`. `path` is the route template (`/items/{item_id}`) when a route matched, otherwise the raw path (cut at 200 characters). The query string is never logged. Level is `INFO`, `WARNING` for 5xx, and `ERROR` for an unhandled exception (adds `error_type` and frames). A client disconnect that cancels the request logs `INFO` with `error_type` set to `CancelledError`. For streams, `duration_ms` covers the whole stream and nothing is logged per delta.
- Request and response bodies, headers, user messages, Memory text, provider payloads, and vault contents are never read by the middleware and must not be passed to loggers.
- Limitation: for an unhandled exception Starlette builds the 500 response outside this middleware, so that response has no `X-Request-ID` header. The log record still has the ID.

## Failure records in the chat service

When storage, memory retrieval, or the provider fails, `ChatService` logs a `WARNING` with the event name (`chat.storage_failed`, `chat.memory_context_failed`, `chat.provider_failed`), `error_type`, `duration_ms` (time since the operation started), and `streaming`. Nothing else is attached: not the exception message, the prompt, or the reply. Expected client errors (unknown conversation, capacity) are not logged as failures.

## Redaction

Every record passes through redaction while it is formatted, so it covers the message (after `%` arguments are applied), extra fields, nested dicts and lists, and context values:

- Known credential shapes in text: OpenAI-style `sk-...`, Google `AIza...`, GitHub tokens, AWS access key IDs, Slack tokens, JWTs, `Bearer ...` tokens, `Authorization: ...` values, `name=value` or `"name": "value"` pairs whose name contains api key, secret, password, token, or credential, and PEM private key blocks (including truncated ones).
- Dict keys and field names such as `authorization`, `api_key`, `password`, `secret`, `token`, `cookie`, `credential`, `private_key` have their values replaced by `[REDACTED]`. Matching is by word, so `completion_tokens` is kept and `auth_token` is redacted.
- Redaction is pattern based and best effort. It does not make logging arbitrary user or provider text safe; do not log that text at all.
- Fail safe: if redaction of a field raises, that field is dropped (the message is replaced by a fixed placeholder). Raw text is never emitted as a fallback. The formatter itself never raises.

Redaction runs in the formatter instead of a `logging.Filter` so records shared with other handlers are not modified and so a record cannot reach a JARVIS handler unredacted.

## Exception policy

Exception messages are never logged. They often echo upstream payloads, request text, or file contents, and redaction cannot reliably recognize those. A record with `exc_info` (for example `logger.exception(...)`) gets only:

- `error_type`: the exception class name,
- `error_chain`: class names of `__cause__` and `__context__` links, at most 5,
- `traceback` and `cause_traceback`: up to the last 30 frames as `path:line in function`, with paths relative to the repository (or `package/module.py` for installed libraries), no source lines and no local variables.

`exc_text` and `stack_info` are ignored for the same reason. To record why something failed, log an event name and bounded non-sensitive fields, not `str(exc)`.

## Log context for later features

`log_context(**fields)` in `backend/core/logging.py` attaches scalar fields to every record logged inside the `with` block, and `current_log_context()` reads them. Contexts nest and follow `contextvars`. The request middleware uses it for `request_id`. Research or Task runs can reuse it, for example `with log_context(task_id=...)`, without changes to the logging module. Nothing besides `request_id` is defined for Research or Task correlation yet.

## Operating notes

- Use `INFO` or above in normal use. Keep `DEBUG` for local investigation; library loggers (for example `httpx`) may log URLs.
- Logs go to stderr only. Rotation, retention, and shipping are left to the process supervisor.
- Tests that call `configure_logging` (directly or through the app lifespan) are isolated by `tests/conftest.py`, which restores root and uvicorn logger state after every test.
