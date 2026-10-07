# Phase 1 chat

The initial chat flow uses the existing vendor-neutral `LLMProvider` contract. A personality prompt is sent as a system message, followed by at most ten recent user/assistant turns and the current user message. A turn enters context only after a complete, nonblank provider response. Provider failures leave the previous context intact.

Phase 2 stores complete successful turns in SQLite and reloads the latest 20 messages for the provider, so conversation IDs survive a process restart. It keeps at most 100 active context locks in a process and evicts an idle lock when needed; evicting a lock does not delete the transcript. Requests in one process are serialized by conversation. Cross-worker ordering is not yet coordinated, so run one worker when conversation continuity matters.

Setting `JARVIS_MEMORY_VAULT_PATH` opts chat into local text retrieval of up to three approved, relevant notes. JARVIS reads the current Obsidian body and immutable provenance through SQLite; pending, conflicting and rejected records never enter the provider request. The reference is bounded to 2,400 JSON characters, with at most 500 content characters and 200 source characters per note. It includes the memory ID, category, source, origin, importance, confidence, stale and edited-since-approval flags, plus truncation flags. The current review state is checked again after retrieval; a state change, corrupt database record, or missing or invalid approved note stops the request with HTTP 503 or a stream `error` before the provider is called. A review or note edit after the final check can still race with the provider call, so pause chat while changing sensitive vault content.

The configured LLM provider receives the current user message, bounded conversation history, and these memory references. If the OpenAI provider is selected, those fields are sent to the OpenAI Responses API with `store=False`. Enable memory only for a vault and provider whose data sharing is acceptable. References are not added to conversation history or sent to the frontend as a separate API field. The note body is human editable and lower trust than the user's live request. Even when labeled as reference data, note text could contain prompt injection or secrets and the model may repeat it in a reply; review vault content before enabling this option.

If SQLite becomes unavailable while serving chat, the regular endpoint returns HTTP 503 and the stream emits an `error` event. Failed storage writes do not enter prompt context. Runtime connections require an existing database, so a missing file is never recreated by a chat request.

`POST /api/chat` accepts `{ "message": "...", "conversation_id": "optional UUID" }` and returns `conversation_id`, `reply`, `provider`, and `model`. `POST /api/chat/stream` accepts the same request and emits SSE `delta`, `done`, or `error` events. A successful `done` event contains the conversation ID and provider metadata. A missing or expired conversation ID returns HTTP 404 for the regular endpoint and an SSE `error` for streaming. Input is limited to 4,000 characters; blank messages are rejected.

Set `JARVIS_LLM_PROVIDER=openai` plus the adapter's server-side key and model variables to enable live chat. With the default `none`, chat returns 503 while health checks and the web client remain available. The UI is served from `/` when `frontend/index.html` is present. This release has no login or remote access control; bind the server to `127.0.0.1`.

## Web UI behaviour

The plain HTML/JS client (`frontend/`) shows text as it streams, but only a stream that ends with a `done` event is a saved reply. The server stores a turn only after `done`, so a failed or stopped turn is never in later context and retrying it is safe. Text received before a failure stays visible, but it is labelled "incomplete, not saved" and styled differently from a reply.

| Situation | What the user sees | Conversation id | Input / actions |
| --- | --- | --- | --- |
| Normal reply | Deltas appear incrementally; provider and model shown after `done` | Stored from `done`, reused on the next turn | Input focused again |
| Provider failure before the first delta (`error` event or HTTP 502) | Japanese error under the user message, no reply text | Unchanged | Input usable, **再試行** resends the same text |
| Provider failure after some deltas | Partial text in an "incomplete, not saved" box plus the error | Unchanged | **再試行** resends the same text and replaces the partial |
| Stop button while streaming | Partial text (if any) plus "stopped, not saved" | Unchanged | **再試行** available; the server drops the stream without saving |
| Network failure / stream ends without `done` | "Could not connect" or "ended early" error; partial marked unsaved | Unchanged | **再試行** |
| Second send while a request is in flight | Status "応答中です…"; nothing is sent, the draft stays in the box | Unchanged | Send and New conversation are disabled; Stop is shown |
| Missing or expired `conversation_id` (SSE `error` or HTTP 404) | Explains the conversation is gone and nothing was carried over | Kept as is; never silently replaced | No retry button. Only **新しい会話** starts a new one |
| Empty or whitespace-only message | Status "メッセージを入力してください。" | Unchanged | Nothing is sent |
| 4,000-character limit | Counter from 3,600 characters; notice at the limit (browsers truncate pastes silently); over-limit text is refused | Unchanged | Draft is kept |
| HTTP 503 with provider `none` | Explains that no LLM provider is configured on the server | Unchanged | No retry button (retrying cannot help) |
| HTTP 503 capacity/storage/memory, other 5xx | Japanese explanation of the cause | Unchanged | **再試行** |

Server error strings are English and are mapped to Japanese in `frontend/chat-api.js`; unknown text is never shown verbatim. `tests/test_chat_ui_contract.py` fails if the backend and that table drift apart. One request runs at a time per browser tab. Two tabs can still target one conversation; the server serializes them but the UI does not reconcile the transcripts.

## Testing the UI without a provider

`scripts/dev_fake_provider_server.py` is a dev/test-only harness. It starts the real app with a scripted provider on a temporary SQLite file, binds only to loopback, and is never imported by production code:

```
python scripts/dev_fake_provider_server.py --db "$(mktemp -d)/jarvis.sqlite3" --port 8765
python scripts/dev_fake_provider_server.py --db /absolute/temp/j.sqlite3 --port 8765 --no-provider
```

Type `/slow`, `/fail-now`, `/fail-after N`, `/flaky` (fails once, then succeeds), `/empty` or `/history` as the message to select a behaviour; anything else gets a short streamed reply. Restarting it on a new database while a page is open reproduces an expired conversation id. Frontend logic is covered by `node --test frontend/test/*.test.mjs` (use the glob form on Node 24); `chat-api.test.mjs` also loads `chat-session.test.mjs` so the single-file gate in `scripts/verify.py` and CI runs both. These checks do not make the whole web milestone complete: login, remote access control, and history browsing are out of scope here.
