# Group discussion (P1 engine, P2 API and task, P4 custom personas)

Several personas discuss a topic in fixed turns and a moderator closes with a short summary. P1 is the library `backend/discussion/` (engine and personas). P2 adds the background task, the API and the message format below; P4 adds the custom persona store and its API below (the editor screen comes after); there is **no schema change** (`SCHEMA_VERSION` stays 10). Plan and later phases: [group-discussion-plan.md](group-discussion-plan.md).

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `JARVIS_DISCUSSION` | off | Master switch (`true` / `false`). Off: no discussion route and no worker. On also needs `JARVIS_MODEL_CHOICES` and a chat provider (the moderator); without them the routes are absent too. |
| `JARVIS_DISCUSSION_MAX_ROUNDS` | 3 | Upper bound for the rounds of one discussion, 1 to 3. |
| `JARVIS_DISCUSSION_PER_DAY_LIMIT` | 10 | Discussions per day, 1 to 100. Counted from today's discussion tasks (server local day). |

| `JARVIS_DISCUSSION_PERSONAS_PATH` | next to the DB | JSON file of the owner's custom personas. Unset: `discussion-personas.json` in the directory of the SQLite file when `JARVIS_DB_PATH` is set, otherwise none (editing is unavailable, presets still work). The directory must already exist; it is never created. |

An invalid value makes the server refuse to start (`ConfigError`, without echoing the value). `python -m backend.doctor` has a `discussion` row: `OFF` by default; `INCOMPLETE` when on without a ready chat provider or without `JARVIS_MODEL_CHOICES`; otherwise `OK`. It prints only the on/off word, the two limits and `persona_editing=available|unavailable` (the path is never printed).

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
- Every message goes through `emit(DiscussionMessage(speaker, text, kind, code))`. `kind` is `turn`, `summary` or `notice`; only notices carry a code. The service below posts these into a conversation.
- Logs contain fixed event names and codes only. Prompt text, turns, topic, persona fields and provider error messages are never logged.


## Message format in a conversation (`format.py`)

The message table has no speaker column, so the speaker is the fixed prefix of the text (plan section 4.4, option B):

| Message | Stored as |
| --- | --- |
| the user's topic | role `user`, the topic text, no prefix |
| a persona turn | role `assistant`, `【<persona name>】<text>` |
| the moderator's summary | `【まとめ】<text>` |
| a notice | `【お知らせ】<text>（<CODE>）` with a fixed code such as `PERSONA_SKIPPED`, `PROVIDER_FAILURES`, `CALL_CAP`, `SUMMARY_FAILED`, `CANCELLED`, `INTERRUPTED`, `EXECUTION_FAILED`, `TIMEOUT`, `INTERNAL_ERROR`, `VERIFICATION_FAILED` |

`parse_message(content)` returns `(speaker, text)`; a message without the prefix has speaker `None`. `notice_code(text)` returns the trailing code of a notice. Persona names cannot contain `【` or `】` and cannot be `まとめ`, `お知らせ`, `まとめ役` or `システム`. A later migration only has to change `format.py`.

## Running a discussion (`service.py`)

`POST /api/discussions` creates a conversation holding the topic as the user message and queues a task whose goal is `group discussion <conversation uuid> <job uuid>` (no user text). Only the discussion worker claims it: `claim_next()` without a prefix skips chat-tool and discussion tasks, and the chat-tool worker asks for its own prefix. The worker runs the engine and posts each turn as an assistant message the moment it exists, the summary last. The task is shown on the Tasks screen as "グループ議論" with fixed steps (発言 1..n, まとめ) and a cancel button.

- Cancel (`POST /api/tasks/{id}/cancel`) stops the discussion before the next turn (an answer in flight is abandoned). If at least one turn was posted, the moderator posts a short summary of the turns so far, then the notice `CANCELLED`. A discussion that never started only gets the notice.
- A provider that fails is skipped with a `PERSONA_SKIPPED` notice; three failures in a row stop the discussion with `PROVIDER_FAILURES`. A restart while running ends the task as interrupted and posts `INTERRUPTED`. The job lives in memory, so a queued discussion lost to a restart fails with `EXECUTION_FAILED`.
- Limits: rounds above `JARVIS_DISCUSSION_MAX_ROUNDS` are clamped (not refused), 2 to 4 participants, models only from `JARVIS_MODEL_CHOICES` and only those whose provider is ready, one running or queued discussion at a time (the app has a single owner), and `JARVIS_DISCUSSION_PER_DAY_LIMIT` discussions per server-local day, counted from the task table (cancelled and failed ones count).
- Logs hold fixed event names and counts only; the topic, persona text, turns and provider messages are never logged or echoed in errors.

## API

All routes are behind the login layer (when enabled). `POST` also needs the same origin and the header `X-Jarvis-Confirm: 1` (403 `forbidden` / `confirm_header_required`). Errors are `{"detail": "<CODE>"}`.

`GET /api/discussions/config` returns `enabled` (always true when the route exists), `max_rounds`, `default_rounds`, `per_day_limit`, `used_today`, `remaining_today`, `participants_min`, `participants_max`, `topic_max_chars`, `models` (the allowlist: `id`, `provider`, `model`, `available`, `is_default`), `presets` (persona objects with their assigned models), `palette` and `active` (`{task_id, conversation_id}` of the running discussion, or `null`).

`POST /api/discussions` with `{"topic": "<1..2000 chars>", "personas": [<preset name or persona object>, ...], "rounds": 1..3}` answers `201 {"conversation_id", "task_id"}`. `personas` has 2 to 4 entries; a persona object has `name`, `role`, `model` and optionally `stance`, `checklist`, `color`. `rounds` is optional (default 2).

| Status | Code |
| --- | --- |
| 400 | `DISCUSSION_REQUEST_INVALID`, `DISCUSSION_TOPIC_INVALID`, `DISCUSSION_ROUNDS_INVALID`, `DISCUSSION_PARTICIPANTS_INVALID`, `DISCUSSION_PRESET_UNKNOWN`, `PERSONA_*` (for example `PERSONA_MODEL_NOT_ALLOWED`) |
| 409 | `DISCUSSION_BUSY` |
| 429 | `DISCUSSION_DAILY_LIMIT` |
| 503 | `DISCUSSION_MODEL_UNAVAILABLE`, `DISCUSSION_STORAGE_UNAVAILABLE` |

Cancel with the existing `POST /api/tasks/{task_id}/cancel` (200 `{id, status}`; 409 when it cannot be cancelled; 404 for a task of another kind). The task's `conversation_id` in the task API is the discussion's conversation. Read the conversation with the existing `GET /api/chat/conversations/{id}/messages?after=<count>` and use `total` to poll. With the feature off none of these discussion routes exist.


## Custom personas (P4: `store.py`)

Custom personas live in one JSON file, `{"version": 1, "personas": [...]}`, not in the database (plan option B; `SCHEMA_VERSION` stays 10). Each entry has the fields of a persona (`name`, `role`, `stance`, `checklist`, `model`, `color`).

- Writes are atomic (temp file in the same directory, fsync, `os.replace`), mode `0600`, and the previous good file is kept as `<file>.bak` on every write. The directory must exist; a symlink at the file or the `.bak` is refused. A failure mid-write leaves the old file.
- At most 20 custom personas and 64 KiB. Names are unique case-folded and cannot be a preset name or one of `まとめ`, `お知らせ`, `まとめ役`, `システム`.
- The model must be in `JARVIS_MODEL_CHOICES` when saved. It is checked again when a discussion starts: a saved persona whose model later leaves the allowlist or whose provider is not ready is listed with `available: false` (not deleted) and cannot be used until it is edited.
- A file that is not valid (bad JSON, wrong version, invalid or duplicate entries, over the size cap) is *corrupt*: reads give `store: "corrupt"`, writes fail with `PERSONA_STORE_CORRUPT`, and the file is never overwritten. Fix or remove it by hand (the `.bak` holds the last good write).
- Access is serialized by a lock inside the process. A running discussion keeps a snapshot of its personas, so edits and deletes never change it.

### Persona API

Behind the login layer. `POST`, `PUT` and `DELETE` need the same origin and `X-Jarvis-Confirm: 1`; bodies are parsed by hand (up to 64 KiB, only the persona fields). Errors are `{"detail": "<CODE>"}`; nothing the client sent is echoed.

| Route | Answer |
| --- | --- |
| `GET /api/discussions/personas` | `{"presets": [persona], "custom": [persona + "available"], "max_custom": 20, "store": "ok" or "unavailable" or "corrupt"}` (always 200; `custom` is empty unless `store` is `ok`) |
| `POST /api/discussions/personas` | 201, the saved persona with `available` |
| `PUT /api/discussions/personas/{name}` | 200, the replaced persona (the body's `name` may differ: a rename). `{name}` is URL-decoded, matched case-folded, at most 20 characters |
| `DELETE /api/discussions/personas/{name}` | 200 `{"deleted": true}` |

`GET /api/discussions/config` also carries `custom_personas` (the same objects), `personas_store` and `max_custom_personas`. `POST /api/discussions` accepts the name of a saved custom persona in `personas`, like a preset name; it is read from the store at start time.

| Status | Code |
| --- | --- |
| 400 | `PERSONA_*` field errors (`PERSONA_NAME_INVALID`, `PERSONA_ROLE_INVALID`, `PERSONA_MODEL_INVALID`, `PERSONA_MODEL_NOT_ALLOWED`, ...), `PERSONA_NAME_RESERVED`, `DISCUSSION_REQUEST_INVALID` |
| 404 | `PERSONA_NOT_FOUND` |
| 409 | `PERSONA_DUPLICATE_NAME`, `PERSONA_LIMIT` |
| 413 | `PERSONA_STORE_TOO_LARGE` |
| 503 | `PERSONA_STORE_UNAVAILABLE` (no path, missing directory, symlink, I/O failure), `PERSONA_STORE_CORRUPT` |

When starting a discussion with a saved name: `DISCUSSION_PRESET_UNKNOWN` (no such name, or no store), `PERSONA_STORE_CORRUPT`, `PERSONA_MODEL_NOT_ALLOWED` (model left the allowlist) or `DISCUSSION_MODEL_UNAVAILABLE` (provider not ready).
