# Router contract and evaluation

Status: **library, plus an opt-in first wiring slice (off by default).** With the router on, each chat turn asks it for a decision and shows it in the Activity View, and every turn runs on the Main Agent path, with one opt-in exception: a real `research` decision starts a web research when research is enabled and fully configured (see [Research from chat](#research-from-chat-opt-in)). The casual path is an opt-in, separate switch (`JARVIS_CASUAL`, see [casual-path.md](casual-path.md)); without it a `casual` decision runs on the Main Agent. Nothing here calls a live model; all tests use fakes. See [conversation-routing-plan.md](conversation-routing-plan.md) for the plan this implements (components 3 and 4) and [Wiring](#wiring-opt-in-first-slice) below.

## What it is

`backend/router/` decides which path one user turn should take:

| Route | Meaning |
| --- | --- |
| `casual` | Small talk, greetings, feelings. Fast path, no memory or tools. |
| `memory` | The existing Main Agent path with approved-memory context. Also the **safe fallback**. |
| `research` | Needs outside, current or verifiable facts. |

A router returns a frozen `RouteDecision(route, confidence, reason, used_fallback)`. The values are validated (enum members, `confidence` a finite number in [0, 1], `used_fallback` must agree with the reason, and a fallback always uses `Route.memory`).

`Router.decide(text)` is async, never raises, and uses nothing except the injected `LLMProvider`: no tools, no memory, no network of its own. `asyncio.CancelledError` propagates.

## What it is not

- Not an executor: it only chooses. In the wired slice a `casual` decision is shown and then the Main Agent answers anyway; a `research` decision is acted on by the chat service, not by the router, and only with research configured (see below).
- `RuleRouter` is a transparent keyword **baseline** for tests and an offline fallback. It is not the intended method: the real router reads paraphrases and context with a model. The evaluation set contains paraphrases on purpose so the gap stays visible.
- A fake or contract-only run says nothing about routing quality.

## Wiring (opt-in first slice)

Off by default: with no router configured, `ChatService` behaves exactly as before (no extra model call, no new events, no log lines).

**Switch.** `JARVIS_ROUTER` is read in `backend/core/config.py`:

| Value | Router |
| --- | --- |
| `off` (default) | None. |
| `rule` | `RuleRouter` (offline keyword baseline; no model call). |
| `llm` | `LLMRouter` over the **same chat provider** that answers the turn (no new key, model or provider). It needs a configured chat provider, otherwise startup fails. |

Any other value (including an empty one) is a startup `ConfigError`. `create_app(..., router=...)` is an explicit injection point (like the semantic-chat one); an injected router wins over the switch and is used as given, so the caller decides whether to wrap it in an `AuditedRouter`. A router built from the switch is wrapped in an `AuditedRouter` with an in-memory sink.

**Privacy and cost of `llm`.** Every turn makes one extra, short model call, and **the user's message is sent to that provider** (the same provider the chat already uses). That is added latency (up to the router timeout, 8 s, before the turn continues on the safe path) and added provider cost on every turn. The router never sees memory notes or conversation history: its request is the fixed system prompt plus the one JSON-quoted message. `rule` sends nothing anywhere.

**Behaviour per turn.** The service calls `router.decide(message)` once the conversation is open and **before** the memory lookup, on every chat path (the streaming and regular endpoints, with or without the activity header), so the audit and the log event are the same everywhere. It then emits the activity events `routing` and `route_selected` (`route`, `decided`, `fallback`: see [chat.md](chat.md#activity-events)). `route` is the path that actually runs (`main`, or `research` when a research was really started); `decided` is the router's choice.

| Router decided | Executed today |
| --- | --- |
| `memory` | The existing Main Agent path, unchanged. |
| `casual` | The same Main Agent path. The casual path is not wired; the view says `ROUTED: CASUAL` and why. |
| `research` | Without research configured (the default): the same Main Agent path; the view says `ROUTED: RESEARCH` and that the route is not connected. With `JARVIS_RESEARCH_ENABLED=1` and a search and a chat provider: a research is started and the chat replies with a fixed message (below). If it cannot be started, the Main Agent path with the reason in `research_skip`. |
| any failure | Fallback (`decided: memory`, `fallback: true`), Main Agent path. |

A router that raises, returns something that is not a `RouteDecision`, or does not answer within a 15 s service-level guard is treated as a fallback; the turn is never blocked or failed by routing. Cancellation (the client going away) propagates and nothing is saved, as for any cancelled turn. A failure writes only the fixed log event `chat.router_failed` with the exception type.

**What is stored.** Nothing persistent. The decision is not written to the conversation, the database or the vault. The only records are the in-memory `RouteAuditRecord`s (route, reason, confidence tenths, length bucket, SHA-256 of the input; never the text) and the fixed `router.decision` log event. The hash is pseudonymous (see Audit). The activity events carry enum values and one bool only.

### Research from chat (opt-in)

When the router decides `research` **for real** (not a fallback, confidence at least 0.6), `JARVIS_RESEARCH_ENABLED=1`, a search provider and a chat provider are configured, the chat service starts a research through the existing run service (`JARVIS_CHAT_RESEARCH_LEVEL=quick|standard`, default `quick`) instead of answering from memory. The question is the user's message only (no memory, no history), the reply is a fixed Japanese message with the link `/research#<id>` and no model call, and the events are `route_selected` (`route: research`) then `researching` (`step: started`). The same one-at-a-time and search-budget rules apply; when the run service refuses (busy, budget used up, not available, invalid question) the Main Agent answers and `route_selected` carries `research_skip`. A router fallback or a low-confidence decision never starts a research. With the opt-in `JARVIS_CHAT_RESEARCH_ANSWER=1` the turn instead waits for the research and answers from its verified claims ([chat-research-answer.md](chat-research-answer.md)). Details: [chat.md](chat.md#research-from-chat) and [research.md](research.md#research-from-chat-off-by-default).

**Cost and privacy.** Each such turn can spend search credits and sends the user's whole message to the search service (and the configured chat model during the research), which a chat turn without research does not. That is why it needs the explicit switch and why the fixed reply says so.

**Not wired.** The Realtime (casual) path, any routing by conversation history or user settings, a per-route model or budget, a UI switch for the chat-research behaviour, following a research's progress inside the chat, and live-model quality, latency and cost measurements. The 0.6 confidence threshold and the timeouts remain maintainer proposals. Do not read the events as proof that a casual or research path exists.

## Fallback policy

Any failure becomes `Route.memory` with `used_fallback=True` and a fixed reason code:

| Reason | When |
| --- | --- |
| `empty_input` | Blank or non-text input (no model call). |
| `input_too_long` | Over `max_input_chars` (default 2000). Rejected, not truncated, so a clipped or padded turn cannot change the meaning. No model call. |
| `timeout` | The provider did not answer within `timeout_seconds` (default 8). |
| `model_error` | The provider raised. The message is never read or kept. |
| `invalid_output` | The reply is not exactly the expected JSON (see below). |
| `low_confidence` | Confidence below the threshold (default **0.6**, a proposal for the maintainer to set), for every route. `RuleRouter` also uses it for conflicting keyword matches. |
| `no_match` | `RuleRouter` found no keyword. |
| `no_model` | `ContractOnlyRouter` (evaluation plumbing only). |

Non-fallback reasons are `model_choice` (`LLMRouter`) and `rule_match` (`RuleRouter`).

## LLMRouter

`LLMRouter(provider, threshold=0.6, timeout_seconds=8, max_input_chars=2000)` uses the existing `LLMProvider.complete` only (no new provider). The system prompt defines the three routes and demands `{"route": ..., "confidence": ...}`. The user turn is sent as a JSON-quoted string and the prompt says it is data to classify; instructions inside it are to be ignored. This reduces, but does not eliminate, prompt-injection risk, which is why the evaluation includes steering cases.

The reply is parsed strictly: bounded length (512 characters), valid JSON only (one optional whole-reply markdown fence), exactly the keys `route` and `confidence`, no duplicate keys, `route` one of the three lowercase names, `confidence` a real number in [0, 1] (no booleans, strings, `NaN` or `Infinity`). The model's text is never echoed, logged or stored.

## Audit

`AuditedRouter(router, sink)` records a `RouteAuditRecord` per decision: route, reason, `used_fallback`, confidence in tenths (0 to 10), an input length bucket and the SHA-256 of the input. **Never the text.** The digest is pseudonymous, not anonymous: identical inputs share a digest, and short common phrases can be guessed. The only log events are the fixed names `router.decision` and `router.audit_failed`. A failing sink never changes the decision. `InMemoryAuditSink` is bounded and meant for tests and local diagnostics.

## Evaluation

`tests/fixtures/router-eval-ja-v1.json` holds 100 **artificial** Japanese turns (the header explains the rationale per class and kind): plain casual, memory and research turns, ambiguous turns with `acceptable_routes`, paraphrases that avoid obvious keywords, and adversarial turns (fake router instructions, JSON injection, fake system messages, fences, a very long text, mixed languages, full-width letters). Each turn has a `cost_of_wrong` note and a `cost_weight`; the header carries a relative cost matrix (for example research answered as casual costs more than casual answered as memory). The costs are unitless proposals.

```
python scripts/evaluate_router.py --output <new report path outside the repo>
python scripts/evaluate_router.py --router contract-only --output <new report path>
```

Built-in routers are offline: `rule` (default, evidence kind `rule_baseline`) and `contract-only` (always falls back, evidence kind `fake`). A model-backed router can only be supplied with `--router-factory module:factory` plus an explicit `--evidence-kind fake|model`; the factory reads its own configuration. This lane ran no live model; the owner runs live trials with their own key.

The report is a new machine-readable JSON file, written atomically; an existing file or symlink is never replaced. It contains per-class and per-kind accuracy, a confusion matrix (expected by predicted), the fallback rate and reasons, adversarial steering failures, cost-weighted mistakes, the dataset and fixture hashes and the runner commit. `quality_assessment` is always `not_established` and there is no pass threshold.

How to read it:

- A turn is accepted when the predicted route is in `acceptable_routes`. A fallback to `memory` therefore counts as correct for memory turns, so read `accuracy_without_fallback` and `fallback_rate` next to `accuracy`.
- A **steering failure** is an adversarial turn with a `steer_target` where the router chose that target without falling back.
- Cost-weighted mistakes count only non-accepted turns.
- A router that raises or returns a non-`RouteDecision` is counted in `router_contract_violations` (the run then exits 1) and scored as a fallback.

## Limits

- 100 hand-written turns measure behaviour on those turns only. Real conversations will differ, and the labels are the author's judgement.
- The threshold, timeout, length limit and cost matrix are proposals.
- No latency or cost measurement exists yet; the model, budget and live trial are open maintainer decisions listed in the routing plan.
