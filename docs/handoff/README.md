# Continuation protocol and state snapshot

Purpose: let any new session (human or agent) resume JARVIS development from the repository and the issue tracker alone. This file contains no secrets and no machine-specific paths; local scratch state belongs outside the repository.

## Source of truth
1. The maintainer's current instructions and the repository rules.
2. GitHub: code, `main`, PRs, CI.
3. Linear (project *Jarvis-v0.1*, team JAR): purpose, completion conditions, dependencies, decisions.
4. Design docs in `docs/` and the original plan (00–03 Linear documents).
5. Dated resume notes and old PR text are history, not current state. Re-fetch before acting.

## Cycle
1. Restore state: `git fetch origin --prune`; list open PRs with head/base/CI; read this file's snapshot only as a hint.
2. Pick 1–3 concrete outcomes from the roadmap below; search existing code/PRs first (do not rebuild what exists).
3. Split independent work across at least two agents with separate branches/worktrees and file ownership; the coordinator owns shared dependencies, venv/model cache and integration.
4. Focused tests → committed snapshot → full gate → Draft PR with the correct parent base → Linear readback → update the snapshot.

## Roadmap (summary)
| Phase | Scope | Linear |
|---|---|---|
| 0–1 | foundation, chat, personality, UI | M1, M2 Core, M4 Chat |
| 2 | memory: review, Obsidian, vector, semantic chat, candidate detection, importance, consolidation | M2 Memory |
| 3 | Research: Search→Reader→Source→Citation (Quick first), then Standard/Deep, reuse, UI | M3 |
| 4 | Task/Tools: contract, registry, permissions, filesystem, shell, queue, verification | M4 |
| 5–7 | productivity, multi-device, voice/home | later |
| M5 (plan) | conversation routing (casual / memory / research), activity view, realtime voice: see [conversation-routing-plan.md](../conversation-routing-plan.md) | M5 |

Order inside Research and Tools: smallest end-to-end path first; no scheduler/parallel-agent machinery before the single path works.

## Open decisions (maintainer's call; none adopted)
The per-capability gate matrix and recommendations are in [phase2-gate.md](phase2-gate.md).

D1 production embedding encoder · D2 semantic quality acceptance · D3 answer policy when memory lacks support · D4 general candidate extraction scope · D5 importance scale (design 0–5 vs stored 0–1) · D6 semantic consolidation. Until decided: lexical chat stays default, local E5 is an artificial-data trial only, no thresholds, no auto-approval, no physical deletion of vault notes.

## State snapshot — 2026-10-07 (verify before relying)
- `main` = `3030299` (includes #29). Landed Memory stack: #2–#26, #29, #30, #32–#35.
- Draft stack: #36 semantic chat (base main) · #37 evaluation runner/gold (base main) · #38 local E5 trial + search CLI (base #37 branch) · #39 browser entry for artificial search (base #38 branch). Parent order #37 → #38 → #39; none is permitted to merge unless the maintainer names it.
- Independent Drafts on `main`: #41 deterministic memory candidate detection (JAR-27 slice) · #42 structured redacted logging (JAR-13) · #43 validated personality settings (JAR-19) · #44 chat UI abort/retry/error handling (JAR-61/62).
- Research R1 (library only, no route, no real search provider): #45 data model (schema v6), #46 search contract/normalizer/mock, #47 safe page reader → #48 Quick Research with verified citations (base = the three merged) → #50 level selection/planner/source classification & ratings.
- Tools/Tasks: #49 tool contract/registry/permission → #51 read-only filesystem tools and #54 structured shell tool (library only, nothing registered by default); #52 task manager/queue (schema v7, base #45) → #53 read-only task API with SSE (`/api/tasks`). Schema numbering: #45 = v6, #52 = v7; any further migration must be rebased on the landed order.
- Merge order is the maintainer's call, per PR number. Everything above is Draft and unmerged.
- Hold: #31 Gemini provider (Draft, no Ready/merge). Optional: #25 OpenAI embedding (conflicting). Stale docs PRs: #27, #28, #1.
- Evidence so far: real E5 (`intfloat/multilingual-e5-small`@`614241f…`) only on artificial Japanese sets (`docs/evidence/` on the #38 branch); production quality is not established.

## Reporting
Short: what works now (and how to run it), what is only Draft, what is unverified, next work. Distinguish main vs Draft and fake vs real evidence.
