# Realtime path and streaming playback (design)

Status: **design only. Nothing here is implemented.** It details component 7 (Realtime path, JAR-98) and component 8 (Streaming playback, JAR-99) of [conversation-routing-plan.md](conversation-routing-plan.md), on top of the router in [router.md](router.md), the activity events in `backend/chat/activity.py` ([chat.md](chat.md#activity-events)) and the login layer in `backend/auth` ([auth.md](auth.md)). Provider facts were checked against the vendors' public docs in October 2026 (links in [Provider facts](#provider-facts)); anything not confirmed is marked **unverified**. Prices and model names change often: re-check before adopting. Open choices are collected in [Decisions for the owner](#decisions-for-the-owner).

## Goals and non-goals

Goals

- Small talk answered at conversation tempo (first visible/audible output well under a second after the user stops, as a target, not a promise).
- Reuse what exists: the router decides `casual`; the activity vocabulary, login layer, SSE transport, transcript storage and retention are unchanged in kind.
- The casual path knows the personality settings and the current conversation only. It never sees approved memory, never calls tools, never starts research.
- Safe by default: server-held keys, short-lived browser tokens, spend caps with fixed refusals, no audio stored.
- Every failure degrades to the Main Agent path (or a fixed message), never to a fabricated answer.

Non-goals

- Memory, research, tools or file access on the casual path.
- Wake words, multi-device hand-off, phone/SIP, home control, speaker identification, multi-user (see the routing plan's "Not in scope").
- Automatic memory approval from transcripts.
- Choosing the production provider by benchmark: no live measurement exists yet; this document proposes how to take one.

## Provider facts

Checked 2026-10 (fetched summaries of the pages; read the pages before relying on a number).

| | Gemini Live API | OpenAI Realtime API | Groq |
| --- | --- | --- | --- |
| Transport | Stateful WebSocket ([Live API](https://ai.google.dev/gemini-api/docs/live)) | WebRTC for browsers, WebSocket for servers ([guide](https://developers.openai.com/api/docs/guides/realtime)) | HTTPS only (OpenAI-compatible REST); no realtime audio session found |
| Browser auth | Ephemeral tokens, Live API only, `v1beta`; default 1 min to start a session, 30 min lifetime, 1 use; can be locked to model/config ([ephemeral tokens](https://ai.google.dev/gemini-api/docs/ephemeral-tokens)) | Server creates an ephemeral client secret per session; browser connects with it (same guide). Expiry values: **unverified** | No browser token scheme found: **unverified**; keep the key server-side |
| Audio | In: raw 16-bit PCM, 16 kHz LE. Out: raw 16-bit PCM, 24 kHz LE | Formats not stated on the page fetched: **unverified** (WebRTC negotiates Opus in the browser) | STT input: flac, mp3, mp4, mpeg, mpga, m4a, ogg, wav, webm ([STT](https://console.groq.com/docs/speech-to-text)) |
| Barge-in | Supported ("users can interrupt the model at any time") | Presented as a strength (barge-in, turn taking) | Not applicable (no duplex session); implemented client-side in options B/C |
| Models named | "Gemini 3.8 Live" family on the pricing page ([pricing](https://ai.google.dev/gemini-api/docs/pricing)) | `gpt-realtime-2.1` in the guide's example | STT: `whisper-large-v3`, `whisper-large-v3-turbo`; TTS: only English and Arabic Orpheus models, no Japanese ([TTS](https://console.groq.com/docs/text-to-speech)) |
| Japanese | Live API advertises 70 languages; Japanese quality **unverified** | **unverified** | Whisper models are multilingual (Japanese included per STT page); TTS has no Japanese |
| Pricing unit | Per 1M tokens, audio also shown per minute. Example from the page: audio in about $0.005/min, out about $0.018/min (Gemini 3.8 Live) | Per audio token: **unverified** (the pricing page was not fetchable) | STT per hour of audio (turbo $0.04/h, large-v3 $0.111/h, 10 s minimum billed); text per token |
| Free tier | Live models and Flash-Lite listed as free of charge, with limits; **free-tier content is used to improve Google's products, paid tier is not** | None known: **unverified** | Free tier exists; limits in RPM/RPD/TPM/TPD and audio seconds per hour/day ([rate limits](https://console.groq.com/docs/rate-limits)); STT file limit 25 MB free |
| Retention | Paid: not used for product improvement; storage period **unverified** | **unverified** (check the data-controls page) | Inference requests not stored by default, but up to 30 days for reliability/abuse monitoring unless Zero Data Retention is enabled; US storage ([your data](https://console.groq.com/docs/your-data)) |
| Latency | No vendor number found: measure | "low first-audio latency" claimed, no number: measure | STT 189x (large-v3) to 216x (turbo) real time per Groq; text-LLM latency: measure |
| Session limits | **unverified** (not on the overview page) | **unverified** | n/a |

Text-only fast models for option B: Gemini Flash-Lite (3.1 and 3.5 listed, paid price roughly $0.25 to $0.30 in and $1.50 to $2.50 out per 1M tokens, free tier available) and any Groq chat model. JARVIS already has `gemini` and `groq` providers ([groq.md](groq.md)).

Consequence: browser-direct speech-to-speech is possible with Gemini or OpenAI via ephemeral tokens. Groq cannot do Japanese speech output, so it only fits a text LLM role (and STT).

## Architecture options

```
A  speech-to-speech   browser ──audio──► provider (Live / Realtime) ──audio──► browser
                      server only mints the token and records transcripts events
B  text first        browser ──text──► JARVIS ──► fast text LLM ──SSE tokens──► browser
                      browser STT (Web Speech) in, browser TTS (speechSynthesis) ou
C  pipeline          browser ──audio──► JARVIS ──► STT ──► text LLM ──► TTS ──audio chunks──► browser
```

| | A: speech-to-speech | B: fast text + browser STT/TTS | C: STT + LLM + TTS |
| --- | --- | --- | --- |
| Latency | Best (one model, native barge-in) | Good for text (first token fast); voice depends on browser engines | Sum of three hops; streamable but slowest to tune |
| Cost | Highest per minute (audio tokens); free tier possible on Gemini with data-use caveat | Lowest (text tokens only; browser speech is free) | Mid (STT per hour is cheap; TTS usually dominates) |
| Japanese quality | Provider-dependent, unverified | LLM good; browser TTS voice quality varies by OS | STT good (Whisper); TTS needs a Japanese provider (not Groq) |
| Complexity | Mid on server (token minting, transcript capture), high in browser (audio worklet, WS/WebRTC) | Lowest: reuses SSE, chat service, activity events | Highest: three vendors, chunk sync, cancel plumbing |
| Privacy | Raw voice leaves to the provider; free tier trains | Text only leaves; browser STT may send audio to the browser vendor (engine-dependent, **unverified**) | Voice to STT vendor, text to LLM, text to TTS vendor |
| Lock-in | High (proprietary session protocol, voices) | Low (any chat provider) | Low to mid |
| Control of what the model knows | Weak: the session is configured by us, but the browser talks to it directly, so server cannot gate each turn | Strong: every turn passes the server | Strong |

A weakness of A deserves emphasis: the browser holds a live channel to the model, so the router cannot intercept a turn before it is answered. A would need the router on the transcript stream (after the fact) or a policy of "A handles only the casual session and hands off". That is why B comes first.

## Recommended phased path

Phase R0 (prerequisites, mostly done): router (`JARVIS_ROUTER`), activity events, login layer on main. Add the casual path behind its own switch, default off.

Phase R1 (JAR-98, text): **option B text-only.** A `casual` decision runs `CasualService`: personality-only system prompt, bounded current-conversation history, a fast model (Gemini Flash-Lite or a Groq chat model, owner's choice), streamed over the existing SSE route. Typed input, streamed text output. No new browser permissions, no audio. Measures real latency/cost and fixes the route integration, fallbacks, caps and tests.

Phase R2 (JAR-99, voice on B): browser speech recognition for input (push-to-talk) and `speechSynthesis` for output, driven by the same stream, with sentence chunking and cancel (see [Streaming playback](#streaming-playback-design-jar-99)). Free, no new vendor. Quality of Japanese voices is OS-dependent; accept or move on.

Phase R3 (optional): **option A for a dedicated "voice mode"** if R2 voice quality or tempo is not enough. Server mints a short-lived token; the session is limited to casual talk; transcripts come back as events and are stored. Decide per [decision 1](#decisions-for-the-owner) after R1 numbers exist.

Option C only if A is rejected on privacy or cost and R2's browser TTS is unacceptable.

## Route integration

Where it hooks in: `ChatService` already calls `router.decide(message)` before the memory lookup and emits `routing` and `route_selected`. Today a `casual` decision runs the Main Agent. With the casual path enabled:

| Router decided | Condition | Executed |
| --- | --- | --- |
| `casual` (not a fallback, confidence at least the threshold) | casual path on, within caps, provider healthy | Casual path; `route_selected{route: casual, decided: casual, fallback: false}` |
| `casual` | casual path off, over cap, circuit open or provider error before first token | Main Agent; `route_selected{route: main, decided: casual}` plus the fixed `casual_skip` reason (below) |
| `memory`, `research`, or any router fallback | any | Unchanged (Main Agent / research) |

Rules

1. **Fail to the Main Agent.** The casual path is chosen only when sure. A router fallback never selects it. If the casual provider fails before the first token, the turn is re-run on the Main Agent once (fixed `casual_skip: provider`); after the first token a failure ends the turn with `error{code: provider}` and the partial text is not saved as a complete reply (same rule as any failed turn).
2. **What the casual path may know.** Personality settings (`backend/personality`) and the current conversation, bounded to the last N turns (proposal: 6). No memory notes, no vault excerpts, no semantic search, no tools, no search/research. This matches decision 3 of the routing plan (personality only). It also means the casual path must not claim to remember anything: the system prompt says so, and a request that needs memory is the router's job to send elsewhere. Misroutes (a memory question sent to casual) are mitigated by a cheap guard: the casual system prompt allows the reply "I would need to check my notes; ask me again" which sets nothing and costs one turn. Measuring this is part of the router evaluation (casual precision).
3. **Same conversation.** Casual turns are stored in the same conversation as ordinary messages through the existing persistence, so the next Main Agent turn sees them in its bounded history. The reverse also holds.
4. **Wrong-route policy.** Uncertain means Main Agent (routing plan decision 4, kept).
5. **Sticky voice mode.** In a voice session the router still runs per turn (one cheap call on the transcript); a non-casual decision pauses speech output, hands the turn to the Main Agent, and the reply is then spoken only if the user left TTS on. No routing shortcut is allowed to skip the router.

New fixed code `casual_skip` (optional field on `route_selected`, like `research_skip`): `disabled`, `over_budget`, `circuit_open`, `provider`. Enum values only.

## Streaming playback design (JAR-99)

Text is the master stream; audio is derived from it. This keeps cancel, retention and the activity view identical for every option.

Chunking (options B and C)

- The SSE stream already yields text deltas. A `SpeechChunker` in the browser buffers deltas and emits a speakable chunk at a sentence end (Japanese `。！？` and newline, Latin `.!?` followed by space), or when the buffer passes a length cap (proposal: 80 characters, break at `、` or a space), or after a short idle timeout (proposal: 400 ms) if the first chunk would otherwise wait. The first chunk may be shorter (about 12 characters) to start sooner; later chunks prefer natural boundaries.
- Markdown, URLs and code are not read aloud: the chunker strips or replaces them (for example "a link") before synthesis.
- Option C: each chunk is sent to the TTS endpoint on the server (`POST /api/voice/speak`, login required, capped), returned as a small audio blob and queued. Order is preserved by sequence number.

Queue and backpressure

- Playback queue holds at most K synthesized or pending chunks (proposal: 4). If the model outruns playback, the browser stops reading the SSE body (it does not drop text) until the queue drains; text still renders in the chat in full, speech simply lags. If the client falls more than M seconds behind (proposal: 20 s of queued speech), it stops speaking, marks the rest as "text only" and says so in the status line. Silent dropping of the middle is not allowed.
- Server side, the SSE generator is cancelled on disconnect, which is already how a cancelled turn works. Per-turn output is bounded (max tokens for casual: proposal 300), which also bounds cost.

Cancel and barge-in

- Cancel sources: user pressing stop, new push-to-talk press, new typed message, tab hidden for more than a set time, or logout.
- On cancel the browser, in this order: (1) aborts the SSE fetch (server cancels the model call), (2) calls `speechSynthesis.cancel()` or stops and clears the audio queue, (3) discards unplayed chunks, (4) marks the assistant message as interrupted, stored text being what was actually spoken or shown up to that point plus a marker.
- Barge-in with push-to-talk is just "button down = cancel playback, start listening". Barge-in with open microphone (not recommended first) needs echo cancellation and a voice-activity threshold; it is deferred, see [decision 3](#decisions-for-the-owner).
- Option A: provider-side interruption truncates the model's audio; the client must report how much was played so the transcript matches what the user heard (provider-specific, **unverified** detail). The server records the transcript the provider returns and marks it interrupted.

Audio details (option A/C)

- Output PCM 24 kHz (Gemini Live) is scheduled through an `AudioWorklet` or chained `AudioBufferSourceNode`s with a small jitter buffer (proposal: 80 to 120 ms). Input from the microphone is downsampled to 16 kHz PCM before sending.
- Speech chunks are not cached or persisted on the server or in the browser beyond playback.

## Session and token security

Rules that hold for every option:

1. **No provider key in the browser, ever.** Keys remain server-side environment variables, like the existing provider keys (docs and tests use placeholders such as `<your-key>`).
2. **Login required.** Every voice/casual endpoint sits behind `backend/auth` middleware. When bound to a non-loopback address the login layer is already mandatory; voice adds no exception. No anonymous token minting.
3. **Server-issued, short-lived, single-use tokens (option A).** `POST /api/voice/session` (login + CSRF/same-origin as the existing state-changing routes) asks the provider for an ephemeral token and returns only that token plus the allowed settings. Lifetime: session start within 1 minute, session length capped by us (proposal 10 minutes, then the client must request a new one), one use. On Gemini the token can be locked to model, modality and config so the browser cannot raise the model tier or enable tools; OpenAI's client secret is session-scoped by the same pattern (details **unverified**). The token is never logged or stored; the log event carries the session id and result code only.
4. **Session configuration is ours.** System prompt (personality only), tools disabled, modalities, voice and max duration are set server-side when minting; the browser cannot supply them.
5. **Rate limits.** A per-owner limiter in the style of `backend/auth/limiter.py` (sliding window, in-memory, fixed refusal): at most N session mints per hour (proposal 20) and one concurrent voice session. Exceeding returns HTTP 429 with a fixed code, no upstream text.
6. **Spend caps with fixed-code refusals.** A daily ledger (SQLite, the same store family as the research search budget `JARVIS_SEARCH_MONTHLY_LIMIT`) counts units per day: casual text tokens (estimated from lengths), STT seconds, TTS characters, A-mode session seconds. When the cap is reached the endpoint refuses with a fixed code and the turn falls back where possible:

   | Code | Meaning | Behaviour |
   | --- | --- | --- |
   | `voice_disabled` | feature switch off | 404-style refusal; UI hides the control |
   | `voice_over_budget` | daily cap reached | refuse mint / casual call; chat continues on Main Agent (text) |
   | `voice_rate_limited` | window exceeded | 429 |
   | `voice_busy` | another voice session active | 409 |
   | `voice_unavailable` | provider not configured or circuit open | 503; no upstream detail |

   The cap is a local estimate, not the vendor's bill; the doc and UI say so. A provider-side spend limit should also be set in the vendor console (owner action).
7. **Origin and transport.** Tokens are only usable by the browser via TLS WebSocket/WebRTC; the page needs HTTPS (microphone access also requires a secure context, localhost excepted).
8. **Logging.** Fixed event names only (for example `voice.session_minted`, `voice.session_refused`, `casual.turn`). No audio, transcript, token or upstream error text in logs, matching [logging.md](logging.md).

## Transcript and data policy

- Transcripts are stored as ordinary conversation messages with the existing conversation retention and deletion rules. Nothing new is retained for longer.
- **No audio is stored**, on the server or in the browser (no recording blobs, no IndexedDB). Audio exists only in transit and in playback buffers. Providers may retain per their own policy (Groq up to 30 days for abuse monitoring unless ZDR is on; Gemini free tier trains on content: see table). Owner-visible warning in the settings screen when a free-tier provider is selected.
- **Transcripts are never approved memory.** They may feed candidate detection exactly as chat messages do today; review stays manual ([memory-review.md](memory-review.md)). A voice turn cannot approve, edit or delete a note.
- Speech-recognition errors are normal: the UI shows the recognised text before sending when push-to-talk is used (editable, one tap to send), and flags it with `source: voice` in the stored message metadata (enum, no content) so the Main Agent can discount typo-like errors if desired.
- Hashes in router audit remain pseudonymous as documented; voice adds no new identifier.

## Activity events additions

`ActivityStage.SPEAKING` already exists in the vocabulary and is reserved. Proposed additions, still a fixed enum with allowlisted fields and no text:

| Stage | Fields | Emitted when |
| --- | --- | --- |
| `listening` | none | Push-to-talk is down or the mic session is open and the turn has not been sent |
| `transcribing` | none | Option C: audio uploaded to STT, no transcript yet |
| `speaking` (existing) | optional `voice` in {`browser`, `provider`} | Playback of a first chunk actually began (never before audio exists) |
| `interrupted` | `by` in {`user`, `new_turn`, `error`} | Playback or generation cancelled before completion |
| `route_selected` (existing) | new optional `casual_skip` | See route integration |

Rules: `listening`, `transcribing` and `speaking` are emitted by the **browser-side state** and by the server only where the server really knows (the server knows `transcribing`; it cannot know playback). For playback states the client reports them to the activity view locally; they are not stored and not sent as free text. A turn still ends in exactly one of `done`, `error` or `interrupted`. The view never animates a stage that did not happen (routing-plan principle). The status line (`aria-live`) text per stage is fixed in the client; error codes keep using `ActivityErrorCode` with possible additions `rate_limited` and `over_budget`.

## Failure modes

| Failure | Behaviour |
| --- | --- |
| Router fails or says fallback | Main Agent (existing) |
| Casual provider error/timeout before first token | Re-run on Main Agent, `casual_skip: provider`; circuit breaker opens after 3 consecutive failures for 60 s (proposals) |
| Casual provider fails mid-stream | `error{provider}`, partial not saved as a full reply, user may retry |
| Cap reached | Fixed refusal code; text chat continues on Main Agent |
| STT returns nothing or low confidence | Say "I did not catch that" (fixed text), no model call |
| Microphone denied / unsupported browser | Voice control disabled with a fixed explanation; text still works |
| Browser TTS missing a Japanese voice | Fall back to text-only; one-time notice |
| Token expired mid-session (A) | Client requests a new one once; else ends the session cleanly with `error{provider}` |
| Network drop | SSE aborted; cancel path runs; no half-spoken queue left |
| Prompt injection in speech | Same as text: router treats the transcript as data; casual path has no tools and no memory, so the blast radius is a wrong sentence |
| Misroute (memory question to casual) | Guard reply as above; measured in evaluation |
| Casual model hallucinating personal facts | System prompt forbids claims about the user; evaluation set adds such probes |

## Tests (fakes first)

No test calls a live model or provider (house rule).

- `FakeCasualProvider` (scripted deltas, injectable delay/failure/hang), `FakeTokenMinter`, `FakeClock`, `FakeSTT`, `FakeTTS`.
- Unit: route table above (including `casual_skip` for each reason); casual prompt contains personality and bounded history and **no** memory text (assert on the exact request passed to the fake); no tool/memory service is called on the casual path (spies); the Main Agent fallback after pre-token failure; no duplicate saved assistant message.
- Caps: ledger arithmetic, daily rollover, refusal codes, concurrent session refusal, limiter window; tokens never appear in logs or responses beyond the mint reply (log-capture assertion, secret-scan-friendly placeholders only).
- Auth: every voice endpoint returns 401 without a session, including on a non-loopback app.
- Activity: events built by constructors only; unknown fields rejected; each turn ends in exactly one terminal stage; no text in payloads (extend the existing payload-allowlist tests).
- Frontend (the existing transport check style): `SpeechChunker` table tests (Japanese and English sentences, length cap, idle timeout, markdown stripping), queue backpressure, cancel ordering (fetch abort, then audio clear), interrupted marker.
- Privacy: no code path writes audio to disk or storage (static grep test plus a fake that fails on any blob persistence call).
- Evaluation (offline, report only): extend the router gold set with voice-style transcripts (fillers, misrecognitions) and report casual precision, since a false `casual` is the costly error.
- Live trial (owner, own key, stop-at-limit rule): measure time to first token, first audible chunk and per-turn cost on 20 scripted casual turns; record in a new evidence file. Until then the numbers in this document are unmeasured.

## Cost-control design

- Casual path is capped by max output tokens (300), history window (6 turns), and the daily ledger.
- The router call is already one small call per turn; with the casual path on, a casual turn costs router + casual calls, both small. Keep router prompt unchanged and short.
- Prefer free or cheap tiers only after reading the data-use caveat (Gemini free tier content is used to improve products; Groq retains up to 30 days unless ZDR).
- Push-to-talk by default so audio seconds (A) and STT seconds (C) are bounded by user action; an idle timeout (30 s) ends a session.
- Per-day caps are in provider units converted to a single "estimated cost unit" so one limit covers text, STT and TTS; the owner sets the number (decision 4). Default is deliberately small.
- Prompt caching of the fixed personality prompt where the provider supports it (**unverified** per provider).

## Acceptance criteria

R1 (text casual, JAR-98)
1. With the switch off (default) behaviour and tests are unchanged; no extra calls, events or log lines.
2. A confident `casual` decision is answered by the casual path, streamed over SSE, saved to the conversation; `route_selected.route == casual`.
3. The request sent to the casual provider contains personality and bounded history only (asserted with a fake).
4. Pre-token failure, over-cap and circuit-open each produce a Main Agent answer and a fixed `casual_skip` code; no upstream text anywhere.
5. Every turn ends in exactly one terminal activity stage; no message text in events, logs or audit.
6. The endpoints require login when exposed; refusal codes match the table.
7. Owner live trial recorded: median and p95 time to first token for the chosen model, and daily cost for a sample day.

R2 (voice on B, JAR-99)
8. Push-to-talk shows the recognised text for confirmation; no audio stored.
9. First speech starts at the first sentence boundary, not at end of reply (fake-clock test).
10. Stop and new input cancel generation and speech in the specified order within one event-loop tick in tests; interrupted turns are marked.
11. Backpressure test: a fast stream and a slow player never drop text silently and never exceed the queue bound.
12. Missing mic or voice degrades to text with a fixed notice.

R3 (option A, only if adopted)
13. Token minted server-side, single use, at most the session cap, configuration fixed by the server; no key in any response or log (assertion).
14. Transcript stored with an interrupted marker matching what was played; no audio stored.
15. Cap and rate refusals use the fixed codes.

## Decisions for the owner

Each has a recommended default; nothing is implemented until you decide.

1. **Provider.** Recommended: Gemini Flash-Lite (or the already-configured text provider) for R1 text; revisit Gemini Live for R3 after measurements. Reason: the Gemini adapter exists, browser tokens are documented, and Japanese is claimed for many languages. Groq is good for STT (Whisper) but has no Japanese TTS. OpenAI Realtime is the fallback if Gemini Live Japanese quality or limits disappoint (several of its facts are unverified here).
2. **Voice or text first.** Recommended: text first (R1), then push-to-talk voice on browser STT/TTS (R2). Reason: reuses SSE, activity events and tests, no new data leaving the device, and yields real latency numbers.
3. **Push-to-talk or always listening.** Recommended: push-to-talk. Reason: cost, privacy, no echo or barge-in false triggers. Always-listening and wake words stay out of scope.
4. **Spend caps.** Recommended: casual+voice combined daily cap of a small amount (for example an estimated cost equivalent of about one US dollar per day), research budget unchanged and separate, 20 session mints per hour, 10 minute session maximum, 30 s idle timeout. You set the real number; also set a vendor-side limit.
5. **Transcript retention.** Recommended: keep as ordinary conversation messages under the existing retention, no audio, never approved memory, `source: voice` flag. Warn when a training-on-free-tier provider is selected.
6. **Router model and budget.** Recommended: keep `LLMRouter` on the same provider as chat, threshold 0.6, timeout 8 s; for casual traffic a budget of one router call (about 100 input tokens plus prompt) and one casual call of at most 300 output tokens per turn. Re-tune after a live trial of the 100-turn gold set; require casual precision to be reported before enabling casual by default.
7. **What the casual path may know.** Recommended: personality plus the last 6 turns of the current conversation; no summary, no memory.
8. **Rollout switch.** Recommended: a separate opt-in `JARVIS_CASUAL` (name a proposal) off by default, independent of `JARVIS_ROUTER`, so a router can run for display only as today.
9. **Interrupted-message storage.** Recommended: store only what was spoken or shown, plus an interrupted marker.
