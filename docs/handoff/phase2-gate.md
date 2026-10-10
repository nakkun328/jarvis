# Phase 2 (Memory) gate matrix — working draft for JAR-88

Status: **draft for maintainer review**. Nothing here marks an issue Done, adopts a deployment model, a threshold, or a deferral. Snapshot date 2026-10-07; re-verify heads before use.

Legend — Implementation: `main` / `Draft #n` / `none`. Verification: `fake` (fake embedder/provider) / `real-model` (artificial data only) / `live-API` / `browser` / `none`.

| Capability | Original intent | Implementation | Verification | Remaining before it can be called complete |
|---|---|---|---|---|
| Conversation persistence | durable successful turns | main (#10/#11) | tests | none known for local single-worker |
| Memory model / candidate review / reviewed write | explicit review, no auto-approve | main (#10–#14, #19, #20, #22) | tests | actor is a caller label, not authenticated identity |
| Obsidian canonical notes | human-editable source of truth | main (#7) | tests | real-vault operation needs explicit permission + backup |
| Self Memory | success/failure/correction events | main (#17) | tests | general extraction of Self events is JAR-27/D4 |
| Correction / supersession / retirement | history preserved | main (#30) | tests | — |
| Lexical memory in chat | bounded approved refs | main (#23) | tests | — |
| Vector index (Chroma) + versioned embedding contract | derived cache, rebuildable | main (#15, #16, #18, #21, #24) | fake + Chroma | production encoder not chosen (D1) |
| Semantic search | versioned query → canonical resolve | main (#26, #29 audit/cleanup) | fake + Chroma | real-encoder quality (D2) |
| Opt-in semantic chat | bounded, lexical stays default | Draft #36 | fake | review + user-named merge |
| Artificial evaluation runner / gold | frozen gold, new-path reports | Draft #37 | fake | review + user-named merge |
| Local E5 trial + search CLI | pinned revision, offline | Draft #38 | real-model (artificial ja-v1 12q, ja-extra-v1 25q) | quality not established; threshold null; negative/unsupported queries still return candidates |
| Browser entry for artificial search | try retrieval by hand | Draft #39 | fake + browser (contract-only) | one real-E5 browser pass; mobile layout |
| Gemini connection trial | E5 → Gemini bounded context | local branch only (not pushed) | fake HTTP | needs current-head merge, then live run under confirmed budget |
| General candidate detection (JAR-27) | candidates from conversation | Draft #41 (deterministic slice) | fake / artificial conversations | LLM-assisted extraction not adopted (D4); not wired to chat/CLI |
| Importance (JAR-28) | 0–5 design vs stored 0–1 | none | none | D5 mapping decision |
| Semantic consolidation (JAR-30) | merge similar memories | exact dedup / topic conflict only (main) | tests | D6; proposals-only first |

## Hand-off to Research (JAR-32 / JAR-35)
Research may start on a minimal Search→Reader→Source→Citation path without waiting for D1–D6, because it does not read Memory as evidence. Dependencies that remain formal in Linear (JAR-88 blocks JAR-32/35) should be relaxed or satisfied by the maintainer's explicit agreement, not silently ignored.

## Open decisions (recommendations; none adopted)
| ID | Question | Recommendation | Reversibility |
|---|---|---|---|
| D1 | Production embedding encoder | Keep local E5 as an explicit opt-in comparison candidate; compare one more local model on the same frozen sets; no hosted embedding of real notes | fully reversible (derived index) |
| D2 | Acceptance for semantic quality | Separate relevance from support; include similar-target, multi-note, unsupported and negative cases; fix gold before results | reversible |
| D3 | Answer policy when memory lacks support | Never present non-memory content as memory-derived; surface candidates as "related, unconfirmed"; decide abstain/ask/Research after observing confusion in the connection trial | reversible |
| D4 | General extraction | Start with the deterministic slice (#41); LLM extraction only after prompt/schema/budget/privacy review. **Owner-approved exception (2026-10-10):** opt-in, default-off LLM extraction from the owner's own chat messages, with optional automatic approval, as specified in [chat-auto-memory](../chat-auto-memory.md); the review step is skipped only when `JARVIS_CHAT_MEMORY_AUTO_APPROVE` is on | reversible |
| D5 | Importance scale | Keep storage 0–1, display mapping first; separate field only if display mapping proves lossy; never a blind ×5 migration | migration is not reversible — decide first |
| D6 | Semantic consolidation | Proposal-only grouping reviewed by a human; no scheduler, no auto-approval, no physical deletion | reversible |
