# Phase 1 chat

The initial chat flow uses the existing vendor-neutral `LLMProvider` contract. A personality prompt is sent as a system message, followed by at most ten recent user/assistant turns and the current user message. A turn enters context only after a complete, nonblank provider response. Provider failures leave the previous context intact.

Phase 2 stores complete successful turns in SQLite and reloads the latest 20 messages for the provider, so conversation IDs survive a process restart. It keeps at most 100 active context locks in a process and evicts an idle lock when needed; evicting a lock does not delete the transcript. Requests in one process are serialized by conversation. Cross-worker ordering is not yet coordinated, so run one worker when conversation continuity matters.

`POST /api/chat` accepts `{ "message": "...", "conversation_id": "optional UUID" }` and returns `conversation_id`, `reply`, `provider`, and `model`. `POST /api/chat/stream` accepts the same request and emits SSE `delta`, `done`, or `error` events. A successful `done` event contains the conversation ID and provider metadata. A missing or expired conversation ID returns HTTP 404 for the regular endpoint and an SSE `error` for streaming. Input is limited to 4,000 characters; blank messages are rejected.

Set `JARVIS_LLM_PROVIDER=openai` plus the adapter's server-side key and model variables to enable live chat. With the default `none`, chat returns 503 while health checks and the web client remain available. The UI is served from `/` when `frontend/index.html` is present. This release has no login or remote access control; bind the server to `127.0.0.1`.
