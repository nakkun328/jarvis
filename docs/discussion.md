# Group discussion engine (P1)

Several personas discuss a topic in fixed turns and a moderator closes with a short summary. This phase is a backend library only: `backend/discussion/`. It is not connected to the API, the task queue, the database or the screen, and it adds no schema change. Plan and later phases: [group-discussion-plan.md](group-discussion-plan.md).

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `JARVIS_DISCUSSION` | off | Master switch (`true` / `false`). Nothing reads it yet except the doctor. |
| `JARVIS_DISCUSSION_MAX_ROUNDS` | 3 | Upper bound for the rounds of one discussion, 1 to 3. |
| `JARVIS_DISCUSSION_PER_DAY_LIMIT` | 10 | Discussions per day, 1 to 100. Enforced by the later wiring phase. |

An invalid value makes the server refuse to start (`ConfigError`, without echoing the value). `python -m backend.doctor` has a `discussion` row: `OFF` by default; `INCOMPLETE` when on without a ready chat provider or without `JARVIS_MODEL_CHOICES`; otherwise `OK`. It prints only the on/off word and the two limits.

## Personas (`personas.py`)

A `Persona` has `name` (1 to 20 characters, no brackets or control characters), `role` (one line, up to 100), `stance` (up to 500), `checklist` (up to 5 items, each up to 100), `model` and `color`.

- `model` is an exact `provider:model` entry of the server allowlist ([model-select.md](model-select.md)). The format is checked on construction; `require_allowed` and the loader check membership. A persona never holds a credential.
- `color` is one of the fixed palette names `blue`, `green`, `orange`, `purple`, `red`, `teal`.
- Errors are `DiscussionError`; its message is a fixed code such as `PERSONA_NAME_INVALID`, never the offending text.
- `preset_personas(models)` builds the three built-in personas (設計者, 批判役, 現実担当), taking their models from the given allowlist entries in turn.
- `load_personas(path, allowed_models)` reads a JSON file (up to 64 KiB): a list of persona objects, or an object with a `personas` list. 2 to 4 personas, distinct names, unknown fields refused. `color` may be omitted. No setting points at such a file yet.

## Engine (`engine.py`)

`DiscussionEngine(personas, topic, resolve_provider=..., moderator=..., emit=..., rounds=2)`.

- 2 to 4 personas, a topic of up to 2000 characters, `rounds` 1 to 3 (and at most `max_rounds`, which the later phase takes from the setting).
- `resolve_provider` maps a model id to an `LLMProvider`; tests pass fakes. `moderator` is the provider for the summary.
- Turns are a fixed round-robin: in every round each persona speaks once, in the given order.
- Each turn's prompt is the topic, the earlier transcript and that persona's own role, stance and checklist, plus the instruction not to repeat earlier points. Other personas' settings are never included. The transcript is capped (4000 characters by default); the newest turns are kept and older ones collapse into one line of counts.
- A turn is cut at 600 characters, the summary at 900. The summary lists agreements, disagreements, open points and the next step.
- Stop conditions (`StopCode`): rounds done (`COMPLETED`, then the summary), the `is_cancelled` callback (`CANCELLED`, checked before every call, no summary), the call cap of participants x rounds + 1 (`CALL_CAP`), and three provider failures in a row (`PROVIDER_FAILURES`, no summary). A single failing persona (error, empty reply, timeout, unusable model) is skipped, recorded in `DiscussionResult.skipped` and announced with a notice. A summary failure gives a `SUMMARY_FAILED` notice.
- Every message goes through `emit(DiscussionMessage(speaker, text, kind, code))`. `kind` is `turn`, `summary` or `notice`; only notices carry a code. A later phase will post these into a conversation.
- Logs contain fixed event names and codes only. Prompt text, turns, topic, persona fields and provider error messages are never logged.

Not in this phase: API, background task, daily counting, conversation storage, screen, persona editing.
