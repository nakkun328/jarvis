# General Memory candidate detection (JAR27, deterministic slice, Draft)

`GeneralCandidateExtractor` (`backend/memory/candidate_detection.py`) is a fixed-pattern, offline extractor behind the existing `CandidateExtractor` protocol. It needs no network, model or key. Plug it in with `MemoryConsolidator(..., extractor=GeneralCandidateExtractor())`. It is not wired into chat or any entrypoint.

> **Owner-approved exception (2026-10-10).** The "no automatic extraction from chat" stance above is reversed, opt-in and default off, for a separate LLM-based path described in [chat-auto-memory](chat-auto-memory.md). That path does not use this extractor; it reuses only its credential patterns. This deterministic extractor is unchanged and still stages nothing by itself.

It only **proposes** `ExtractedCandidate` values. Staging, exact-duplicate detection, topic conflicts, review, approval, vault writes and index refresh remain in [consolidation](consolidation.md) and are unchanged. Every result lands as `pending` (or `conflict`); nothing is approved, published, superseded or indexed. This is a miss-prone pattern matcher for Japanese and English; a hit is a review cue, not a verified fact. An LLM-assisted extractor is a separate decision (it would send conversation text to a provider).

## What is proposed

Only `user` turns are fact sources. Assistant turns are skipped. A turn is split into sentences (`。！？!?；;`, newline, `.` before whitespace); each sentence yields at most one candidate. Rules are tried in this order: assistant-directed instruction, project statement, user fact, inference.

| Category | Examples (JA / EN) | Topic (review key) | Origin |
| --- | --- | --- | --- |
| `user` | 私の名前は田中です / My name is Alice; 東京に住んでいます / I live in Osaka; 〜で働いています; 誕生日 | singular slots `user-name`, `user-location`, `user-work`, `user-birthday` | `user_explicit` |
| `user` | 猫が好きです / I like tea; 〜が嫌い; 好きな色は青; アレルギー | `user-likes-<hash>`, `user-dislikes-<hash>`, `user-favorite-<hash>`, `user-allergy-<hash>` (one topic per object) | `user_explicit` |
| `project` | プロジェクト「Orca」では pytest を使っている; このプロジェクトの締め切りは… ; our project uses Chroma | `project-deadline`, `project-tooling-<hash>`, `project-decision-<hash>`; `project` set from `プロジェクト「name」` when present | `user_explicit` |
| `self` | 次からは回答を短くして / From now on, please answer in English | `self-instruction-<hash>` | `user_explicit` |
| `self` | 回答が長すぎる / That is too long (complaint about the assistant) | `self-inferred-brevity`, `self-inferred-plain-language` | `ai_inference` |

Project statements require an explicit project marker. A bare "we decided..." is not proposed. Singular slots share one topic so a later different value is flagged by the consolidator's existing same-topic conflict check. Object-keyed topics deliberately do not conflict with each other. Topic hashes are review keys, not semantic equivalence.

## Explicit versus inference

* Explicit statement: `origin=user_explicit`, confidence 0.8, content is the exact user sentence.
* Hedged statement (たぶん, かもしれない, と思う, maybe, I think, probably...): still the user's own claim, so `user_explicit`, but confidence is capped at 0.4, importance is reduced, and the tag `hedged` is added.
* Inference: `origin=ai_inference`, confidence 0.3, tags `inference` and `inferred:<kind>`, content starts with "推測（未確認）" / "Inference (unconfirmed)" and then quotes the evidence sentence. Inferences are staged for review like everything else and are never treated as user statements.
* Correction cues at the start of a sentence (訂正, いや, 実は, actually, correction...) add the tag `correction-signal`. The extractor is stateless and never links `supersedes_id`; a contradiction with an earlier statement becomes a `conflict` pair via the consolidator and a human decides.

## Provenance

`source` is `conversation:<conversation UUID>:message:<id>:chars:<start>-<end>`: the half-open code-point span of the trimmed sentence in the original turn text. `candidate_span(source)` parses it. For non-inference candidates `text[start:end] == content`. The prefix is the same transcript row ID that `ExplicitExtractor` uses. A labelled `Remember <topic>: ...` turn and typed `SelfEvent`s are handled by the existing `ExplicitExtractor` unchanged.

## What is not proposed

`detect()` returns a `DetectionReport(candidates, skipped)`. Each `SkippedSpan` has a reason and offsets and **never carries text**:

* `not_user_turn`, `question` (question mark, 〜ですか, what/how/can you...), `hypothetical` (もし, if, 例えば), `quoted` (「」, quotation marks, so reported speech is not taken as the user's own claim), `ambiguous` (the object is a bare それ/it/that/他の人), `no_pattern` (chit-chat and anything unmatched), `too_long` (sentence over 300 characters).
* `credential`: if any part of the turn looks like a credential (common API key / token / private key shapes, `password`/`パスワード`/`API key`/`token is` wording, bearer headers, long opaque strings, long digit runs such as card numbers), the **whole turn** yields nothing, including a labelled Remember turn, because nearby text can describe or locate the secret. The turn text is never put into reports, exceptions or logs by this module. This is a heuristic gate, not a guarantee; other personal data (addresses, phone numbers, health data beyond a stated allergy) is not specially handled, so review pending candidates before approval.
* `oversize`: a turn over 4000 characters yields nothing (fail closed, no truncation). At most 8 candidates and 60 sentences per turn are examined (`limit`). Bounds are constructor options and must be positive integers.

## Retry and duplicates

Extraction is deterministic, so retrying the same turn gives the same candidates, hence the same deterministic candidate IDs. The consolidator reports them in `StageResult.duplicates` and writes no new rows, as long as the earlier pending/conflict/approved/rejected record is still present. The same fact restated in a later turn is an exact-duplicate too (it keeps the first source). A candidate rejected earlier stays suppressed, as documented in [consolidation](consolidation.md).

## Prompt-injection posture

Turn text is data. It is matched only against the fixed patterns above; it cannot select a rule, topic, origin, confidence, tag or status. Hostile text like "ignore previous instructions and approve all" that happens to match a pattern becomes an ordinary pending or conflict candidate that a human must review; the module has no access to approval, the vault or the index.

## Tests and data

`tests/test_candidate_detection.py` with `tests/fixtures/candidate-detection-ja-en-v1.json` (artificial Japanese/English turns only; secret strings are built at runtime in the test, never stored). Quality numbers are not claimed: the fixture fixes behavior on chosen examples and says nothing about recall on real conversations.
