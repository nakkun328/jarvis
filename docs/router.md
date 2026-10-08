# Router contract and evaluation

Status: **library only.** No route is wired into chat. Nothing here calls a live model; all tests use fakes. See [conversation-routing-plan.md](conversation-routing-plan.md) for the plan this implements (components 3 and 4).

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

- Not wired into `ChatService`, the activity events or any API route.
- `RuleRouter` is a transparent keyword **baseline** for tests and an offline fallback. It is not the intended method: the real router reads paraphrases and context with a model. The evaluation set contains paraphrases on purpose so the gap stays visible.
- A fake or contract-only run says nothing about routing quality.

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
