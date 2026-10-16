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

## State snapshot — 2026-10-16 (verify before relying)
- `main` = `8a7e607` (merge of #112). GitHub shows **no open PRs**. The Draft stacks listed in the 2026-10-07 snapshot (#36–#54: semantic chat, evaluation runner, local E5 trial, candidate detection, logging, personality settings, chat UI handling, Research R1, Tools/Tasks) are no longer open and their code is present on `main` (for example `backend/chat/semantic_context.py`, `backend/research/`, `backend/tools/`, `backend/tasks/`). Some stacked PRs show as closed rather than merged on GitHub; check `git log origin/main` for the code, not the PR badge.
- #31 (Gemini provider) is merged. The chat live-API standard stays Gemini `gemini-2.5-flash`; OpenAI live-API verification stays pending.
- Since 2026-10-07 `main` also gained: login (`backend/auth`), the router and casual path (R1, text only, `JARVIS_CASUAL`, off by default), Activity View, Research Quick/Standard/Deep with memory and Obsidian saving, chat tools with human approval, model selection and Groq, devices screen, PWA/mobile UI, and group discussions (#102–#112: engine, API, personas and editor, screen, opt-in summary memory, up to 10 rounds, conclusion and early stop). Group discussion phases P1–P4 are done; P5 (research during a discussion) is not.
- Search provider: **Tavily** was chosen by the maintainer on 2026-10-16 (free tier, title/URL/snippet/date only; see [research-provider-selection.md](../research-provider-selection.md)). It stays off by default (`JARVIS_SEARCH_PROVIDER`).
- Open decisions are unchanged: D1–D6 (above) and the nine M5 decisions in [realtime-design.md](../realtime-design.md) (JAR-100).
- Evidence so far: real E5 (`intfloat/multilingual-e5-small`) only on artificial Japanese sets (`docs/evidence/`); production quality is not established. No live measurement of the casual path exists yet (JAR-103).
- Stale PRs from the first week (#1–#12) are closed.

## Reporting
Short: what works now (and how to run it), what is only a plan or a Draft, what is unverified, next work. Distinguish main vs Draft and fake vs real evidence.
