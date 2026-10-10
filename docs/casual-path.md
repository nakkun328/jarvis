# Casual path (text only)

Status: **implemented behind a switch, off by default.** Phase R1 (JAR-98) of [realtime-design.md](realtime-design.md). Text in, streamed text out. There is no audio and no voice code; later phases are separate.

## What it does

When the router ([router.md](router.md)) decides `casual` with a confidence of at least the threshold (0.6), and the casual path is switched on, `CasualService` (`backend/chat/casual.py`) answers instead of the Main Agent. The answer is streamed over the existing chat SSE route and saved like any other turn.

The casual path knows only:

- the personality settings (the same rendered system prompt as the Main Agent, plus a short fixed rule block: reply briefly, claim no memory, say "I would need to check my notes; ask me again" when the user needs more);
- the last 6 turns (12 messages) of the current conversation.

It never receives approved memory, a summary, tools, files or web research, and it does not call them. A casual turn emits no `memory_lookup` event. It uses the default chat provider, or the provider of the request's `model_choice` when there is one ([model-select.md](model-select.md)).

## Switch and settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `JARVIS_CASUAL` | `0` | `1`/`true` turns the casual path on. Independent of `JARVIS_ROUTER`, but it needs a router (`rule` or `llm`) and a chat provider. |
| `JARVIS_CASUAL_DAILY_CALL_LIMIT` | `200` | Casual model calls per day, 1 to 10000. |

With the switch off, behavior is exactly as before: no extra calls, events or fields. With the switch on but no router or provider, nothing changes either, and `python -m backend.doctor` reports the `casual` row as `INCOMPLETE`.

## Limits

- **Output:** the providers have no output-token option here, so the reply is cut at 600 characters (about 300 tokens) and the model call is closed. The turn ends normally with the cut text.
- **Daily cap:** an in-process counter per UTC day. It is not stored and starts from zero when the server restarts, so it is a spend guard, not an accounting figure. A turn that reaches the cap runs on the Main Agent with `casual_skip: over_budget`; the user sees an ordinary answer, never an error. Set a limit at the vendor too.
- No circuit breaker yet (the design's `circuit_open`).

## Failures

| Situation | Result |
| --- | --- |
| Router failure or router fallback | Main Agent, as before (the casual path is never chosen) |
| Confidence below the threshold | Main Agent, `casual_skip: low_confidence` |
| Daily cap reached | Main Agent, `casual_skip: over_budget` |
| Provider error, no text, or any exception before the first token | Main Agent, `casual_skip: provider` |
| Provider fails after the first token | `error{provider}`; the partial text is not saved |

`route_selected` is sent after the first token arrives, so it never says `casual` for a turn that fell back. Events carry fixed codes only; no upstream text.

## Activity view

`route_selected{route: casual, decided: casual}` lights the REALTIME node (it is connected for that turn), generating is shown on REALTIME, and the turn ends with REALTIME marked done. A fallback shows `CASUAL NOT USED` with a fixed reason line. Without the switch, a casual decision still reads as "not connected yet" on the Main Agent. All text is placed with text nodes.

## Transcripts

Casual turns are stored as ordinary conversation messages under the existing retention, so the next Main Agent turn sees them in its history. They are never approved memory. Candidate detection ([candidate-detection.md](candidate-detection.md)) is not wired into chat today, so no casual or normal turn triggers it.

## Tests

`tests/test_casual.py` (fakes only): route used only with switch, router and provider; history cut to 6 turns; spies prove memory and research are never called; cap and failure fallbacks; output cut; activity events; doctor; config validation; switch-off equivalence. Frontend: `frontend/test/activity.test.mjs`.
