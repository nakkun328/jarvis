# Phase 1 chat

The initial chat flow uses the existing vendor-neutral `LLMProvider` contract. A personality prompt is sent as a system message, followed by at most ten recent user/assistant turns and the current user message. A turn enters context only after a complete, nonblank provider response. Provider failures leave the previous context intact.

Phase 2 stores complete successful turns in SQLite and reloads the latest 20 messages for the provider, so conversation IDs survive a process restart. It keeps at most 100 active context locks in a process and evicts an idle lock when needed; evicting a lock does not delete the transcript. Requests in one process are serialized by conversation. Cross-worker ordering is not yet coordinated, so run one worker when conversation continuity matters.

Setting `JARVIS_MEMORY_VAULT_PATH` opts chat into local text retrieval of up to three approved, relevant notes. JARVIS reads the current Obsidian body and immutable provenance through SQLite; pending, conflicting and rejected records never enter the provider request. The reference is bounded to 2,400 JSON characters, with at most 500 content characters and 200 source characters per note. It includes the memory ID, category, source, origin, importance, confidence, stale and edited-since-approval flags, plus truncation flags. The current review state is checked again after retrieval; a state change, corrupt database record, or missing or invalid approved note stops the request with HTTP 503 or a stream `error` before the provider is called. A review or note edit after the final check can still race with the provider call, so pause chat while changing sensitive vault content.

The configured LLM provider receives the current user message, bounded conversation history, and these memory references. If the OpenAI provider is selected, those fields are sent to the OpenAI Responses API with `store=False`. Enable memory only for a vault and provider whose data sharing is acceptable. References are not added to conversation history or sent to the frontend as a separate API field. The note body is human editable and lower trust than the user's live request. Even when labeled as reference data, note text could contain prompt injection or secrets and the model may repeat it in a reply; review vault content before enabling this option.

If SQLite becomes unavailable while serving chat, the regular endpoint returns HTTP 503 and the stream emits an `error` event. Failed storage writes do not enter prompt context. Runtime connections require an existing database, so a missing file is never recreated by a chat request.

`POST /api/chat` accepts `{ "message": "...", "conversation_id": "optional UUID" }` and returns `conversation_id`, `reply`, `provider`, and `model`. `POST /api/chat/stream` accepts the same request and emits SSE `delta`, `done`, or `error` events. A successful `done` event contains the conversation ID and provider metadata. A missing or expired conversation ID returns HTTP 404 for the regular endpoint and an SSE `error` for streaming. Input is limited to 4,000 characters; blank messages are rejected.

Set `JARVIS_LLM_PROVIDER=openai` plus the adapter's server-side key and model variables to enable live chat. With the default `none`, chat returns 503 while health checks and the web client remain available. The UI is served from `/` when `frontend/index.html` is present. This release has no login or remote access control; bind the server to `127.0.0.1`.
# Explicit semantic memory opt-in (Draft)

The default application still uses lexical memory when
`JARVIS_MEMORY_VAULT_PATH` is configured, and no memory when it is unset.
The versioned index stack through #29 is now on main (3030299, 2026-10-04).
This explicit semantic chat path remains Draft #36; it selects no deployment encoder.
An application factory caller can opt in by supplying both dependencies:

```python
app = create_app(
    settings,
    chat_provider,
    embedding_provider=reviewed_embedding_provider,
    memory_index=verified_index,
)
```

`settings.memory_vault_path` must identify the canonical vault corresponding to
`settings.db_path`. Build/audit the supplied index from those approved notes
with `MemoryIndexBuilder` before selecting it. Query and document embeddings
must use the same exact model/version/dimension space. The caller owns the
embedding/index resources and their provisioning; this patch chooses no model,
does not rebuild or switch an index automatically, and does not require #25.
The ordinary environment-only entry point continues to use lexical retrieval.

Both completion and SSE chat encode only the current query, search for candidate
IDs, and resolve those IDs through current SQLite/vault review and provenance.
Semantic selection uses the existing three-match limit and shared bounded JSON
serializer (content 500, source 200, total 2400 characters). Confidence, origin,
importance, edited-note and freshness markers remain lower-trust reference data.
Stale vector rankings may select a current edited note; the cached vector never
supplies its old facts. Use the separate index revision audit/explicit refresh
to repair stale rankings. Pending, conflicting, superseded and retired memories
stay excluded even when their IDs remain in the index.

After asynchronous retrieval, canonical review/revision is checked again.
Detected changes or embedding/index/vault failures return the existing safe
HTTP 503 or SSE error before an LLM request or successful transcript write.
There is no silent lexical fallback; an empty valid result simply adds no memory.
Cancellation propagates. This remains one local worker, not an atomic snapshot
across concurrent external vault edits or multiple workers.

Fake embeddings/chat plus real Chroma test routing and integrity, not semantic
quality. Production embedding provider/model selection and quality evaluation,
browser UI and live API checks remain separate. OpenAI live validation is pending;
necessary live checks use Gemini/gemini-2.5-flash in the separate artificial
connection trial. This Draft is not merge permission for itself or the dependent index stack.

Alternatively, set `JARVIS_LLM_PROVIDER=gemini` with `GEMINI_API_KEY` and `JARVIS_GEMINI_MODEL`. The Gemini adapter maps the same provider messages to Google's text `generateContent` request and streams response deltas through the existing SSE route. It sends `store: false`; conversation history remains in JARVIS's SQLite database. Opted-in approved memory references are sent to Gemini under the same review and freshness checks as other providers.
