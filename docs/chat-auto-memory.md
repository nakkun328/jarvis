# Automatic memory from chat (owner-approved exception)

> **Deliberate exception, recorded by the owner on 2026-10-10** ("調べたり喋ってたら勝手に記憶が出来上がっていく": memory should build up by itself from what the owner says). It reverses, for chat only, the earlier stance in [memory.md](memory.md), [candidate-detection.md](candidate-detection.md) and the Phase 2 gate decision D4 that conversation text is never turned into memory without human review. Everything here is **opt-in and off by default**; with the switches off, chat behaves exactly as before and makes no extra model call.

## What it does

After a chat turn has been answered and saved, JARVIS may ask the **default chat provider** to read the owner's message of that turn and return up to three short facts about the owner (preferences, projects, decisions, routines, tools, non-sensitive relationships). Each fact is checked by deterministic code and, if it survives, stored as a memory candidate:

- origin `chat`, tag `chat-auto` (plus `explicit` when the owner said 「覚えて」 and the like), category `user` (`project` for project facts);
- source `chat:<conversation id>:<message digest>`;
- content: the fact, then `引用: <the owner's verbatim words>` and `日付: <date>`;
- status `pending` (a person approves it on `/memory`), or `approved` at once when `JARVIS_CHAT_MEMORY_AUTO_APPROVE` is on.

## Switches

| Variable | Default | Meaning |
| --- | --- | --- |
| `JARVIS_CHAT_MEMORY_AUTO` | `0` | extract and stage candidates after chat turns. Needs a chat provider (otherwise `python -m backend.doctor` says so and nothing runs). |
| `JARVIS_CHAT_MEMORY_AUTO_APPROVE` | `0` | approve staged candidates immediately through `MemoryWriter.approve` with the audit actor `auto:chat`. **This skips human review.** Requires `JARVIS_CHAT_MEMORY_AUTO` and `JARVIS_MEMORY_VAULT_PATH` (approval publishes a vault note); otherwise the app refuses to start. |
| `JARVIS_CHAT_MEMORY_DAILY_LIMIT` | `50` | extraction model calls per UTC day, 1 to 1000. The counter lives in the process and restarts from zero on a restart. |
| `JARVIS_CHAT_MEMORY_MIN_CHARS` | `12` | shorter messages are not examined (1 to 1000). |
| `JARVIS_CHAT_MEMORY_PER_CONVERSATION_LIMIT` | `5` | most candidates staged from one conversation (1 to 50, per process). |
| `JARVIS_CHAT_MEMORY_PER_DAY_LIMIT` | `30` | most candidates staged per UTC day (1 to 500, per process). |

Invalid values refuse to start. `python -m backend.doctor` has an area `chat_memory` that shows the flags and says plainly that chat-derived memory is approved **without human review** when auto-approval is on. It never prints any content.

## Safety rules (all in code, all tested)

1. **Input is the owner's message only**, of the current turn. Assistant text, tool or research output, retrieved memory and earlier turns are never sent. The message is passed as JSON-quoted data and the prompt states it is data, not instructions.
2. **Off the request path.** Extraction is scheduled after the reply is saved, as a background task. It never delays or fails the chat turn; a failure is logged with a fixed code (`provider`, `timeout`, `invalid_output`, `storage`, `approval`, `overloaded`, `internal`) and the error type, never with the message or the model output. At most 8 turns wait for extraction; further ones are dropped.
3. **Strict output.** The model must return `{"items": [{"fact", "kind", "quote"}]}` with 0 to 3 items and exactly those keys; anything else is discarded whole.
4. **Deterministic verification.** The `quote` must occur verbatim in the message (after NFKC folding) and be at least 4 characters, or the item is dropped. Facts over 120 characters, and facts or quotes that read like instructions to a model (the patterns of `backend/research/claim_safety.py`) are dropped.
5. **Sensitive-information floor.** A message that trips the filter below skips the whole turn *before any model call*; each extracted fact and quote is checked again.
6. **Private markers skip the whole turn:** 内緒, 秘密, ひみつ, オフレコ, ここだけの話, 覚えないで, 覚えなくていい, メモ/保存/記録しないで, プライベート, 非公開, "off the record", "confidential", "keep this private", "don't remember/save", "secret" and similar.
7. **Pasted-looking messages are skipped** (a code fence, two or more links, more than 12 lines, more than 1500 characters). Chat has no flag for pasted external content, so this is judged by shape.
8. **Dedupe and caps.** A fact whose character-bigram (Dice) similarity to any approved or pending note is 0.6 or more is dropped. Candidate IDs are derived from the source and fact, so a retry adds nothing. At most `JARVIS_CHAT_MEMORY_PER_CONVERSATION_LIMIT` (5) candidates per conversation and `JARVIS_CHAT_MEMORY_PER_DAY_LIMIT` (30) per day are staged per process.
9. **Nothing else can approve.** The staging and approval code is referenced only by `backend/memory/chat_auto.py`, its audit actor constant and the app wiring; no chat, router, tool, provider, task or research code imports it (a source-scan test enforces this). The only approval is `MemoryWriter.approve`.

### The sensitive-information filter

Applied to the message and to every fact and quote, on NFKC-folded text. A hit skips the item (or, for the message, the turn); nothing is stored. Categories:

- credentials: API-key and token shapes, private keys, bearer headers, long opaque strings, 12 to 19 digit runs, and words such as password, パスワード, 暗証番号, トークン, 秘密鍵, 認証情報 (shared with `candidate_detection`);
- card, bank and government identifiers (カード番号, 口座, マイナンバー, 免許証, パスポート, 保険証, passport, credit card, bank account, SSN and similar);
- precise contact data: e-mail addresses, phone numbers, postal codes, street addresses and room numbers;
- health and medical (病気, 通院, 診断, 薬, 治療, うつ, 障害, アレルギー, 妊娠, diagnosis, medication, therapy and similar);
- sexual life and orientation;
- political and religious affiliation, ethnicity;
- minors' personal details (息子, 娘, 子供, 小学生, 未成年, "my son/daughter/kid" and similar);
- money and criminal record (年収, 給料, 貯金, 借金, ローン, 逮捕, 前科).

It is a best-effort floor made of fixed patterns, **not a guarantee**. It over-blocks on purpose (for example any mention of 子供 skips the turn) and will still miss things (paraphrases, other languages, context-dependent secrets). Do not rely on it to protect information you would not want stored; use a private marker, or leave the switch off.

## Explicit commands

- **「覚えて」「覚えておいて」「記憶して」「忘れないで」** (and "remember that"): the extracted candidate gets confidence 0.9 and importance 0.7 and the extra tag `explicit`. The same filters apply: a sensitive message is never remembered even when asked. (The older `GeneralCandidateExtractor` does not handle these phrases and is not used here.)
- **「忘れて」「忘れてください」「記憶から消して」** (and "forget that"): no model call is made. The request text is matched against the facts of `chat-auto` notes only. If exactly one note clearly matches (at least 3 shared bigrams, at least 40% of the request's bigrams, strictly better than the runner-up), an approved note is **retired** through the existing retirement flow (the vault note and history stay; it is no longer retrieved) and a pending candidate is **rejected**; the actor is `auto:chat`. Weak or ambiguous matches change nothing, and notes of other origins are never touched. This is best effort: `/memory` lists everything, and the withdraw button there is the reliable way.

## Visibility and reversal

- `/memory` shows chat notes with the origin 「会話から自動抽出」; an automatically approved one also shows 「自動承認(会話)」 with the owner's verbatim quote (引用) and the date. The review history says 「自動で承認」.
- **Withdraw** (human only): the detail page of an auto-approved note has 「この自動承認を撤回」, which calls `POST /api/memory/notes/{id}/withdraw` (same-origin, `X-Jarvis-Confirm: 1`, login when on; no model or agent path). The note becomes `retired` and is never retrieved again; nothing is deleted. It refuses notes a person approved and notes of other origins.
- In the model-facing context a chat note's text starts with a fixed label: `(会話由来・自動承認)` for an automatic approval, `(会話由来)` for a human-approved one, so a model reads it as lower-trust data about the owner, not as an instruction. The existing rule still applies: retrieved memory is reference data, never a request.

## Seeing when a memory was made

Extraction runs after the reply, so the per-turn activity stream has already ended. Each staged or auto-approved fact (and each 「忘れて」 withdrawal) is therefore published to the memory activity feed ([memory.md](memory.md#memory-activity-feed)); the chat page's Activity View polls it after every reply, lights its MEMORY node and says 『記憶しました(自動承認・会話): …』 (auto-approve on) or 『記憶の候補を作りました(確認待ち・会話): …』 (staged only), with a link to `/memory`. Publishing is best-effort: a failure there never affects extraction or the chat turn.

## Privacy

Be clear about where the text goes:

- **Extra model call.** With the switch on, every eligible message is sent **a second time** to the default chat provider (the owner's message only, wrapped in the extraction prompt). That is additional cost and additional provider traffic, bounded by the daily limit. Messages the filters skip are not sent for extraction.
- **Approved memories are sent to the selected LLM provider as context.** Once a note is approved (automatically or by a person), later chat turns may include its text (and the verbatim quote it stores) in the request to whichever provider answers that turn. Auto-approval therefore widens what reaches the provider without a review in between.
- **Provider data-sharing settings matter.** Check the retention, training and data-sharing settings of the provider you use (for OpenAI and Gemini in particular, their API versus consumer terms and any "use my data to improve" options). JARVIS cannot see or change them.
- Candidates and approval history are kept in the local SQLite database; approved notes are Markdown files in the vault you configured, where you can read, edit or delete them.

## Cost and model

Each extraction is one short completion (a prompt of a few hundred tokens, a reply of a few dozen). Using a small, inexpensive model as the default provider is enough; the daily limit is the cost ceiling (default 50 calls). Extraction currently uses the default provider, not a per-request model choice.

## Tests

`tests/test_chat_auto_memory.py` uses a scripted fake provider, a temporary SQLite file and a temporary vault: defaults off, user-only input, strict parsing, quote verification (including NFKC), instruction-like and sensitive drops, private markers, dedupe, caps and day rollover, failure isolation and log content, single approval through the writer with the `auto:chat` audit, labels, withdrawal, remember and forget commands, settings validation, the doctor area, app wiring, and a source scan of who may reference the staging code. Frontend checks are in `frontend/test/withdraw-memory.test.mjs` and `frontend/test/memory.test.mjs`.
