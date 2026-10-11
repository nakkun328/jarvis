# Phase 1 chat

The initial chat flow uses the existing vendor-neutral `LLMProvider` contract. A personality prompt, rendered once at startup from the [personality settings](personality.md), is sent as a system message, followed by at most ten recent user/assistant turns and the current user message. A turn enters context only after a complete, nonblank provider response. Provider failures leave the previous context intact.

Phase 2 stores complete successful turns in SQLite and reloads the latest 20 messages for the provider, so conversation IDs survive a process restart. It keeps at most 100 active context locks in a process and evicts an idle lock when needed; evicting a lock does not delete the transcript. Requests in one process are serialized by conversation. Cross-worker ordering is not yet coordinated, so run one worker when conversation continuity matters.

Setting `JARVIS_MEMORY_VAULT_PATH` opts chat into local text retrieval of up to three approved, relevant notes. JARVIS reads the current Obsidian body and immutable provenance through SQLite; pending, conflicting and rejected records never enter the provider request. The reference is bounded to 2,400 JSON characters, with at most 500 content characters and 200 source characters per note. It includes the memory ID, category, source, origin, importance, confidence, stale and edited-since-approval flags, plus truncation flags. The current review state is checked again after retrieval; a state change, corrupt database record, or missing or invalid approved note stops the request with HTTP 503 or a stream `error` before the provider is called. A review or note edit after the final check can still race with the provider call, so pause chat while changing sensitive vault content.

The configured LLM provider receives the current user message, bounded conversation history, and these memory references. If the OpenAI provider is selected, those fields are sent to the OpenAI Responses API with `store=False`. Enable memory only for a vault and provider whose data sharing is acceptable. References are not added to conversation history or sent to the frontend as a separate API field. The note body is human editable and lower trust than the user's live request. Even when labeled as reference data, note text could contain prompt injection or secrets and the model may repeat it in a reply; review vault content before enabling this option.

If SQLite becomes unavailable while serving chat, the regular endpoint returns HTTP 503 and the stream emits an `error` event. Failed storage writes do not enter prompt context. Runtime connections require an existing database, so a missing file is never recreated by a chat request.

`POST /api/chat` accepts `{ "message": "...", "conversation_id": "optional UUID" }` and returns `conversation_id`, `reply`, `provider`, and `model`. `POST /api/chat/stream` accepts the same request and emits SSE `delta`, `done`, or `error` events. A successful `done` event contains the conversation ID and provider metadata. A missing or expired conversation ID returns HTTP 404 for the regular endpoint and an SSE `error` for streaming. Input is limited to 4,000 characters; blank messages are rejected.

Set `JARVIS_LLM_PROVIDER=openai` plus the adapter's server-side key and model variables to enable live chat. With the default `none`, chat returns 503 while health checks and the web client remain available. The UI is served from `/` when `frontend/index.html` is present. This release has no login or remote access control; bind the server to `127.0.0.1`.

Groq is also supported (`JARVIS_LLM_PROVIDER=groq`; see [Groq provider](groq.md)).

Alternatively, set `JARVIS_LLM_PROVIDER=gemini` with `GEMINI_API_KEY` and `JARVIS_GEMINI_MODEL`. The Gemini adapter maps the same provider messages to Google's text `generateContent` request and streams response deltas through the existing SSE route. It sends `store: false`; conversation history remains in JARVIS's SQLite database. Opted-in approved memory references are sent to Gemini under the same review and freshness checks as other providers.

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

## Restoring a conversation

The chat page remembers only the current conversation id in the browser (`localStorage`, key `jarvis.chat.conversation`; no message text is stored). It is saved when a reply completes and removed by 新しい会話. When the page is opened again (for example after visiting Research and coming back, or after a reload), `frontend/chat-restore.js` asks `GET /api/chat/conversations/{id}/messages`, draws the returned messages with the normal message rendering (text nodes; a `/research#<uuid>` path is still the only link), continues that conversation on the next send and shows 前回の会話を復元しました。 If the server answers 404 the stored id is dropped and the welcome screen stays (前回の会話は見つかりませんでした。…); on a temporary failure the id is kept, the welcome screen stays and a fixed error line is shown. Without usable storage the page works as before, with no restore. The `/research#<uuid>` links in replies open in a new tab (`target="_blank"`, `rel="noopener noreferrer"`) so the chat is not left.

`GET /api/chat/conversations/{id}/messages` is read-only and behind the login layer like other `/api` routes. It accepts only the canonical lowercase hyphenated UUID and returns `{"conversation_id": "...", "messages": [{"role": "user" | "assistant", "content": "..."}], "has_more": bool, "next_before": "<cursor>" | null}`. The database keeps every message of a conversation; only the last 20 are sent to the model. This route pages the full stored transcript for display: `?limit=` is 1 to 200 (default 50) and returns the newest messages in chronological order; when `has_more` is true, pass `next_before` as `?before=` for the next older page. An unknown or malformed id gives the same fixed `404 {"detail": "conversation not found"}`; a bad `limit`/`before` gives `422 {"detail": "invalid_limit"}` or `{"detail": "invalid_before"}`; a storage failure gives 503. Responses carry `Cache-Control: no-store`.

### Conversation history

`GET /api/chat/conversations?limit=50&before=<cursor>` lists stored conversations, newest activity first (`limit` 1 to 100, default 50; `before` is the opaque `next_cursor` of the previous page). Each item has only `id`, `title`, `updated_at` and `message_count`; there are no message bodies. The title is derived (no stored column) from the first line of the first user message: control, zero-width and bidirectional characters are removed, whitespace is collapsed and it is cut to 40 characters (`無題の会話` if nothing is left). Invalid parameters give `422` with `invalid_limit` or `invalid_before`. The route is read-only, behind the login layer and `no-store`. A conversation that is no longer in the process cache (restart, or evicted beyond the 100-conversation cache) is loaded from SQLite when a send names its id, so any listed conversation can be continued; the model sees its last 20 messages.

The chat header has a 履歴 button that opens a panel listing conversations (title, relative date, message count; text nodes only; arrow keys, Tab trap, Escape closes). Choosing one loads its newest messages, sets the conversation id, updates the restore key and shows 会話を再開しました。 A 「さらに前を読み込む」 button loads older pages. `/?c=<uuid>` (canonical lowercase UUID) opens that conversation the same way and is removed from the address bar; an invalid or unknown value leaves the welcome screen with a fixed error line. Conversations cannot be deleted from the UI.

## Testing the UI without a provider

`scripts/dev_fake_provider_server.py` is a dev/test-only harness. It starts the real app with a scripted provider on a temporary SQLite file, binds only to loopback, and is never imported by production code:

```
python scripts/dev_fake_provider_server.py --db "$(mktemp -d)/jarvis.sqlite3" --port 8765
python scripts/dev_fake_provider_server.py --db /absolute/temp/j.sqlite3 --port 8765 --no-provider
```

Type `/slow`, `/fail-now`, `/fail-after N`, `/flaky` (fails once, then succeeds), `/empty` or `/history` as the message to select a behaviour; anything else gets a short streamed reply. Restarting it on a new database while a page is open reproduces an expired conversation id. Frontend logic is covered by `node --test frontend/test/*.test.mjs` (use the glob form on Node 24); `chat-api.test.mjs` also loads `chat-session.test.mjs` so the single-file gate in `scripts/verify.py` and CI runs both. These checks do not make the whole web milestone complete: login, remote access control, and history browsing are out of scope here.

## Activity events

The chat page shows which stage JARVIS is in (a status orb, a small route diagram, an English caption and a one-line Japanese explanation). It is driven only by `activity` SSE events from `POST /api/chat/stream`.

**Opt-in and compatibility.** The events are sent only when the request carries the header `X-Jarvis-Activity: 1`. Without it the stream is exactly the `delta` / `done` / `error` stream it has always been; `POST /api/chat` never carries activity. With the header, whole `activity` events are added between the existing ones; the other events are unchanged, and a client that ignores unknown event names keeps working. (Opt-in rather than default so existing clients and byte-exact tests are untouched.) `frontend/chat-api.js` sends the header only when an `onActivity` handler is given.

**Wire format.** `event: activity` with a JSON object `{"stage": ...}` plus at most one more field:

| stage | extra field | emitted today |
| --- | --- | --- |
| `received` | none | yes, first |
| `memory_lookup` | `count` (0 to 99 notes used) | yes, only when memory context is configured and was consulted (0 means consulted, nothing matched) |
| `generating` | none | yes, just before the provider is called |
| `done` | none | yes, after the turn was saved, just before the `done` event |
| `error` | `code`: `conversation_not_found`, `capacity`, `storage`, `memory`, `provider`, `internal` (`cancelled` is reserved) | yes, just before the `error` event |
| `routing` | none | only when a router is configured (`JARVIS_ROUTER`, off by default; see [router.md](router.md)): right after `received`, before the memory lookup |
| `route_selected` | `route`, `decided`, `fallback`, and optionally `research_skip` | only when a router is configured, right after `routing`. `route` (`casual`, `memory`, `research`, `main`) is the path that **actually runs**: `main`, except `research` when a research was really started for this turn (see [Research from chat](#research-from-chat)). `decided` (`casual`, `memory`, `research`) is the router's choice. `fallback` (bool) is true when the router could not decide and the safe default `memory` was used. `casual_skip` (`over_budget`, `provider`, `low_confidence`) is present only when `JARVIS_CASUAL` is on, the router decided casual and the Main Agent answered; with the casual path on, `route` can also be `casual` ([casual-path.md](casual-path.md)). `research_skip` (`busy`, `not_configured`, `budget_exhausted`, `refused`, `low_confidence`) is present only when the router decided research, research is configured, and none was started: it is the fixed reason, and the turn ran on `main`. Without a research starter (the default) it is never present. |
| `researching` | `step`: `started` | only for a turn that started a research: once, right after `route_selected`, then `done`. By default the chat does not follow the run, so `planning`, `searching`, `reading`, `verifying` and `writing` are **not emitted**. With `JARVIS_CHAT_RESEARCH_ANSWER=1` the turn waits for the run and emits these steps as the run enters them (each at most once in a row; see [chat-research-answer.md](chat-research-answer.md)). |
| `speaking` | | **not emitted**: realtime voice does not exist yet. It is in the vocabulary so later work reuses it; nothing may fake it. |

`route` and `decided` are deliberately separate so the stream never claims a path ran that did not: `{"stage":"route_selected","route":"main","decided":"research","fallback":false}` means the router chose research and the Main Agent answered because the research path is not connected (no research starter is configured). With one, `{"route":"main","decided":"research","fallback":false,"research_skip":"busy"}` means a research was wanted, could not be started, and says why; `{"route":"research","decided":"research","fallback":false}` means it was started. `ActivityEvent` rejects the combinations that would lie (`route: research` without a research decision or on a fallback, a `research_skip` on anything but a research decision that ran on `main`).

Typical turn: `received`, [`routing`, `route_selected`], [`memory_lookup`], `generating`, deltas, `done`. A turn that started a research is `received`, `routing`, `route_selected` (`route: research`), `researching` (`step: started`), a `delta` with the fixed reply, `done` (no `memory_lookup`, no `generating`, no model call). With `JARVIS_CHAT_RESEARCH_ANSWER=1` that turn continues `researching` steps, `generating`, the streamed answer and the source list, then `done` (see [chat-research-answer.md](chat-research-answer.md)). A failure ends with `error{code}` and never with `done`. If the client disconnects (Stop), the server stops without emitting anything more; the page shows its own "stopped" final state. The service also reports the same stages to an optional `on_activity` callback of `ChatService.complete`, but the regular endpoint does not use it.

### Research from chat

Off by default. With a router (`JARVIS_ROUTER=rule|llm`) **and** research enabled and fully configured (`JARVIS_RESEARCH_ENABLED=1`, a search provider and a chat provider: the same conditions as the Research screen's form), a turn the router *really* decides is `research` starts a research instead of answering from memory. With any of those missing nothing changes: a `research` decision still runs the Main Agent path, the Activity View says `ROUTED: RESEARCH` and that the route is not connected.

- **What starts.** `ChatService` hands the user's message, and only that, to a `ResearchStarter` (`backend/chat/research_start.py`), backed by the research run service the Research screen already uses (`ResearchRunService.submit`). There is no second execution path: the one-research-at-a-time rule, the local search budget and the question rules (non-blank, at most 2000 characters, no control characters other than newline and tab) are the service's. The level is `JARVIS_CHAT_RESEARCH_LEVEL`: `quick` (default, the cheaper one), `standard` or `deep`; nothing else is accepted.
- **Only for a real decision.** Never for a router fallback (`used_fallback`, which covers low confidence, timeouts, bad output and over-long messages), and never below the router's 0.6 confidence threshold (a second guard for an injected router, reported as `research_skip: low_confidence`).
- **What is sent where.** The message goes to the router (as before), then, when a research starts, to the search service as the research question. No memory note, no conversation history and no earlier turn is sent. Planning, reading and synthesis then run as for any research (see [research.md](research.md)): page text and the question reach the configured chat model for the verified-quote step, exactly as for a research started from the screen.
- **The reply.** Fixed Japanese text, not model output and with no model call: it says a research was started, that the message text is sent to the search service, and gives the Research screen link `/research#<session id>`. It is saved to the conversation like any assistant turn (so the next turn's history contains it) and the turn's `provider`/`model` are `system`/`fixed-reply`. The conversation store has no per-message provenance, so this marker lives only in that `done` event and the page's message footer.
- **When it is not started.** `busy` (a research is queued or running), `not_configured`, `budget_exhausted` (the local monthly counter is used up), `refused` (an invalid question, for example over 2000 characters, or an unexpected failure of the starter): the Main Agent answers as it would have without research, and `route_selected` carries the matching `research_skip`. Nothing is made up as an answer and the reason is not logged beyond fixed event names (`chat.research_started`, `chat.research_start_failed` with the error type).
- **Consequences to know.** A research may be started and then the reply fail to save (storage error) or the client disconnect before the reply: the research still runs and is listed on the Research screen, where it can be cancelled. The chat does not follow the run; progress and the cited result are on the Research screen.

**Privacy rules.** Events are built only through the constructors in `backend/chat/activity.py` and carry enum values, a small count, a bool or a fixed code. Never message text, replies, memory content, paths, IDs or upstream error text; unexpected exceptions become `internal`. Tests with hostile message, memory and provider-error text pin this (`tests/test_activity.py`).

**Page behaviour.** `frontend/activity-view.js` is a pure state machine (`begin`, `reduce`, `settle`, `reset`, `viewModel`); `frontend/activity.js` builds the panel with text nodes only. The chat transcript turns exactly one thing in a reply into a link: the Research screen's own path `/research#<uuid>` (`frontend/chat-links.js`, a text node plus an anchor built with the DOM API, no `innerHTML`, nothing else linkified; the Research screen selects that session from the hash). Unknown stages and fields are ignored. Only the INPUT and MAIN AGENT nodes are connected by default; ROUTER, REALTIME and RESEARCHER are dimmed with the title "not connected" and light up only if the server reports them. When `routing` events arrive the ROUTER node becomes connected for that turn and lights while routing; after `route_selected` the path ROUTER to MAIN AGENT lights. If the router chose `casual` or `research` the caption reads `ROUTED: CASUAL` / `ROUTED: RESEARCH`, the status line says (in Japanese) that this route is not connected yet so the Main Agent handles the turn, a note with the same wording stays for the rest of the turn, and REALTIME and RESEARCHER stay dimmed. A turn that started a research reads `ROUTED: RESEARCH` with the line 調査をバックグラウンドで開始しました。進み具合と結果は「リサーチ」画面で見られます (the note stays for the rest of the turn); RESEARCHER becomes connected and lit, the path ROUTER to RESEARCHER lights, the turn ends with RESEARCHER (not MAIN AGENT) marked done, and REALTIME stays dimmed. A research that was wanted but not started reads `RESEARCH NOT STARTED` with a reason-specific line (for example 別の調査が実行中のため…) and the Main Agent handles the turn. A router fallback reads `ROUTE SELECTED · MAIN AGENT (FALLBACK)`. Without routing events nothing changes. Every turn ends in a visible final state: done (returns to standby after about 6 s), stopped, or error (both stay until the next message), including network failures and a server that sends no activity events. The status line is `aria-live="polite"` text and is the accessible equivalent of the animation. Animation is off under `prefers-reduced-motion`, or when `<html>` has the class `reduce-motion` (a manual override for tools that cannot emulate the media feature). On screens up to 600px the panel starts collapsed (status text stays visible) and has a toggle.

**Memory part (MEMORY node and feed).** Memory made by chat auto-memory runs after the reply, and research staging runs in the research worker, so neither fits the per-turn activity stream. They publish to an in-process feed instead (`backend/memory/events.py`, [memory.md](memory.md#memory-activity-feed)) that the page polls through `GET /api/memory/activity`. The diagram has a MEMORY node (MAIN AGENT to MEMORY edge). It is connected unless the server reports no memory vault (`configured: false`), in which case it is dimmed with the title "not connected". After a reply completes, `frontend/activity-memory.js` polls the feed every 1.5 s for up to 20 s (it stops when the tab is hidden or a new turn starts; the cursor is the newest `seq` seen at page load or by an earlier poll). When events arrive the MEMORY node and its edge light for about 6 s, an `aria-live="polite"` line says what happened, with a count when several arrived (`記憶しました(自動承認・会話): <要約>`, `記憶の候補を作りました(確認待ち・調査): <要約>`, `記憶を取り下げました(会話): <要約>`), a link opens `/memory` in a new tab (`rel="noopener noreferrer"`), and the last five events are kept as a plain-text list. The backlog read at page load fills that list without lighting or announcing anything. Summaries are the owner's own text, cut to 80 characters with control and bidi characters removed, and are always placed with `textContent`. Research-origin events appear in the same chat feed (same process), and the Research screen shows the same kind of line (research events only) under a completed research result. Animation honours `prefers-reduced-motion` like the rest of the panel.

## Explicit semantic memory opt-in

The default application still uses lexical memory when
`JARVIS_MEMORY_VAULT_PATH` is configured, and no memory when it is unset.
The versioned index stack through #29 is now on main (3030299, 2026-10-04).
This explicit semantic chat path is on main (#36); it selects no deployment encoder.
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
connection trial.
