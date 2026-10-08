# Conversation routing, activity view and realtime voice (plan)

Status: **plan only** (the router contract and evaluation set exist as a library, and an opt-in, off-by-default first wiring slice shows the router's decision in the Activity View while the Main Agent still answers every turn: see [router.md](router.md)). Nothing else here is implemented, and nothing is adopted until the maintainer decides the open questions at the end. The Linear milestone is *M5 — Conversation Routing & Voice*.

## Goal

Natural conversation tempo **and** JARVIS-specific abilities (memory, research, tools). Talking only to a realtime speech model is fast and natural but cannot use approved memory or outside research. Always going through the full agent is capable but slow for small talk. The plan is a **router** in the application that sends each user turn down one of three paths, plus a **live activity view** that shows which path is running.

```
user turn ─► Router ─┬─► casual   ─► Realtime path (fast, no memory/tools)
                     ├─► memory   ─► Main Agent (memory context, personality)
                     └─► research ─► Researcher (Research engine, async task)
                                   │
              every stage emits an ActivityEvent ─► activity view
```

The routing decision is made by a model that reads paraphrases and context, not by fixed keywords. The application, not the model, owns what each path may do.

## Principles (carried over from the existing design)

- **The router only chooses a route.** It never calls a tool, reads memory, or writes anything. The user's text is data, never instructions to the router's own policy.
- **Fixed vocabulary.** The decision is a validated structured value: `route` in {`casual`, `memory`, `research`}, a confidence, and a fixed reason code. Anything else, or a model failure, falls back to the **Main Agent** path (the safe default that already exists).
- **The memory contract is unchanged.** Only reviewed, current-approved notes reach a model, and only on the Main Agent path. The realtime path sees personality settings and the current conversation only. Transcripts are stored as transcripts, never as approved memory (candidate detection and review stay as they are).
- **Research goes through the Task queue** and the Research engine, so its steps, errors and verified citations are visible and a failure never becomes a fabricated answer.
- **Nothing is faked in the view.** A path that is not wired yet is shown as not connected, not animated.
- **Security first for remote use.** Voice and the activity stream require the login layer (JAR-60) when reachable from outside; no API keys in the browser (short-lived server-issued tokens only); no audio is stored by default.

## Components

| # | Component | What it is | Depends on |
|---|-----------|------------|------------|
| 1 | Activity events | A fixed set of `ActivityEvent`s (`received`, `routing`, `route_selected`, `memory_lookup{count}`, `researching{stage}`, `generating`, `speaking`, `done`, `error`) emitted by the chat service and delivered as additive SSE events. Allowlisted fields only; no message text. | existing chat SSE |
| 2 | Activity view | A panel on the chat page: a central status orb and a small route diagram (Router → Realtime / Main Agent / Researcher), a text status line (`aria-live`), reduced-motion support. Unwired routes are dimmed. | 1 |
| 3 | Router contract | `RouteDecision` type, validation, safe fallback, a rule-based baseline and an LLM-backed implementation (Gemini flash for now) behind one interface; decision audit with no message text. | LLM provider |
| 4 | Router evaluation | An artificial Japanese gold set (small talk, memory-needing, research-needing, ambiguous, adversarial "ignore the router" text) and a runner in the style of the semantic evaluation runner; report accuracy and the cost of mistakes, no pass threshold until the maintainer sets one. | 3 |
| 5 | Main Agent path | Wire the router to the existing `ChatService` with memory context. | 3 |
| 6 | Researcher path | Wire `research` to a Task: Research engine (Quick first), progress shown through the Task API, answer with verified citations. | Research engine, a real search provider (JAR-36), Task queue |
| 7 | Realtime path | Speech-to-speech or low-latency text path for small talk. Provider choice is open (see decisions). | provider decision, login |
| 8 | Streaming playback | Play audio as chunks arrive instead of waiting for the full reply; cancel on interruption. | 7 |

## Order of work

1. **Activity events + view (1, 2)** on the existing chat: stages `received → memory_lookup → generating → done/error`. Useful immediately, no router needed, and it fixes the event vocabulary the later steps reuse.
2. **Router contract + baseline + evaluation set (3, 4)** with fakes; no live model needed to start.
3. **Main Agent path (5)**, then the LLM-backed router on Gemini (live trial under the existing "stop at the limit" rule).
4. **Researcher path (6)** once a search provider exists.
5. **Realtime path and streaming playback (7, 8)** after the provider decision and after login is on main.

## Acceptance sketches

- Every user turn produces exactly one `route_selected` event with a fixed reason code, or a documented fallback to the Main Agent.
- A forced router failure still gives a normal Main Agent answer and an `error`/`fallback` event with a fixed code (no upstream text).
- The view never shows a stage that did not happen; a stopped or failed turn ends in a visible final state.
- Router evaluation reports per-class results and the adversarial cases; a message that tries to steer the router is treated as ordinary text.
- No message text, memory content or audio appears in activity events or audit records.

## Open decisions (maintainer)

1. **Realtime provider.** OpenAI Realtime is planned for production but the current standard is Gemini; is there a Gemini equivalent to use now, or is the realtime path deferred until OpenAI is available?
2. **Router model and cost.** Which model, and the per-turn latency/cost budget. A small fast model is assumed.
3. **What the casual path may know.** Personality only (assumed), or also a short non-memory conversation summary.
4. **Wrong-route policy.** When the router is unsure, always prefer the Main Agent (assumed) even if slower.
5. **Voice.** Voice and speaking style, push-to-talk versus always listening, and whether transcripts are kept (assumed: kept as transcripts with the existing retention rules, audio not kept).
6. **Spend cap** for realtime and research per day.

## Not in scope here

Wake words, multi-device hand-off, home control, scheduling, parallel agents, and automatic memory approval. They stay later phases.
