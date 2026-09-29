# Phase 2 landing status — 2026-09-29

This is a dated recovery record, not a merge authorization. Check GitHub and
`origin/main` again before acting. At this snapshot, `main` and `origin/main`
are both `769d3c7` (Phase 1 PR #6); no Phase 2 PR has entered `main`.
PR #1 is an older conflicting Phase 0 draft and is outside this landing plan.

## Open PR dependencies

| PR | Unique work | Current base | Readiness |
| --- | --- | --- | --- |
| #7 | Obsidian vault adapter | `main` | Ready, CI green |
| #8 | Vector contract and engine decision | `main` | Ready, CI green |
| #9 | Phase 1 security wording | `main` | Ready, CI green |
| #10 | Conversation persistence and memory model | `main` | Ready, CI green |
| #11 | Conversation storage error handling | #10 | Ready, CI green |
| #12 | Memory candidate repository | #11 | Ready, CI green |
| #13 | Reviewed memory writer | `feature/phase2-integration` | Ready, CI green; synthetic base needs cleanup |
| #14 | Approved memory retrieval | #13 | Ready, CI green |
| #15 | Persistent Chroma adapter | #8 | Ready, CI green |
| #16 | Chroma candidate integration | #14, with #15 merged into its branch | Ready, CI green; duplicate ancestry needs cleanup |
| #17 | Typed Self Memory candidates | #14 | Ready, CI green |
| #18 | Versioned embedding contract | #8 | Draft, CI green |
| #19 | Local review CLI and actor history | #22 | Ready, CI green |
| #20 | Conservative consolidation candidates | #17 | Draft, CI green |
| #21 | Approved-note index rebuild and read-only ID audit | `feature/phase2-index-base` | Draft, CI green; synthetic base needs cleanup |
| #22 | Atomic review audit | #20 | Draft, CI green |
| #23 | Opt-in reviewed memory in chat | #14 | Draft, CI green |
| #24 | Index refresh after explicit publication | #21 | Draft, CI green |
| #25 | Optional OpenAI embedding adapter | #18 | Draft, CI green |
| #26 | Semantic query to canonical memory | #24 | Draft, CI green |

`feature/phase2-integration` combines early branches for development.
`feature/phase2-index-base` combines work through #20 to make #21 reviewable.
Neither synthetic branch is a direct `main` merge candidate. Compare each
stacked PR with its intended parent and preserve only its unique commits when
rebasing for `main`.

## Verified combinations

- Stage 1: temporary merge in order #7 → #8 → #9 → #10, no conflict;
  Python 45, frontend 4, Ruff and compile passed. No Secret, `.env`, or API
  key pattern was found in the four PR diffs. Their bases are `main`, and
  no rebase was needed at this snapshot.
- Later unique commits were replayed from the Stage 1 result into a temporary
  integration branch. Two test-file import/doc prose conflicts were resolved
  while keeping both intended changes. A further temporary branch combines
  #17 through #26. Its latest run passed Python 119 (with real Chroma),
  frontend 4, Ruff, compile, and diff checks. A fake OpenAI client verifies
  the embedding adapter; no live key was used.
- #26's vector CI explicitly includes `tests/test_semantic_memory.py`.
  Backend, frontend, and vector checks all passed at `cfbdc3c`.

These runs prove compatibility of the tested trees. They do not prove live
OpenAI quality, real browser behavior, or permission to merge.

## Landing sequence once each main merge is authorized

1. Recheck `origin/main`, all PR heads, review state, CI, Secret scans, and
   the Stage 1 merge simulation. With explicit permission for those targets,
   land #7 → #8 → #9 → #10. Do not merge a temporary verification branch.
2. Rebase #11 and #12 onto the new `main` in order. Replace #13's synthetic
   parent with a clean, unique writer commit on top of the resulting `main`;
   then land #13 → #14. Keep full Python/frontend regression checks at each
   landing boundary.
3. Land #15 after #8, then rebuild #16 from only its unique integration change
   after #14 and #15. Land #17 after #14. #18 can land after #8. Resolve the
   docs overlap between #15 and #18 while retaining both descriptions.
4. Land #20 after #17, #22 after #20, and #19 after #22. #23 depends on #14;
   #25 depends on #18. Rebase and retest their unique diffs before landing.
5. Rebuild #21 from only its index-rebuild changes after #14, #15, and #18
   are present, replacing `feature/phase2-index-base`. #24 then needs #20 and
   #21; #26 needs #21 and is currently stacked on #24. Rebase each onto the
   latest intended parent, verify the PR file list and commit list, run the
   full Python/frontend/vector suite, and seek target-specific main approval.

The ordering above is a dependency plan, not permission to run any main merge.
If a parent lands with a different merge strategy, recompute commit ancestry
before force-pushing a child branch. Never merge #13, #16, or #21 together
with duplicated parent or synthetic integration commits.

## Phase 2 still open

- Approved corrections and older-note supersession need a reviewed lifecycle
  decision. Current approved/rejected states are terminal; nothing silently
  overwrites or deletes an old note.
- Index maintenance can rebuild a new space, audit missing/extra IDs, and
  refresh after an explicit publication, but does not watch human Obsidian
  edits or atomically switch an active reader. Canonical SQLite and vault
  data remain authoritative.
- The optional OpenAI embedding provider and semantic query path are tested
  with fakes and Chroma. A real provider selection, private-data transfer
  decision, representative quality/latency evaluation, and key-backed run
  remain outstanding. Chat currently uses local text retrieval when opted in.
- Phase 1's real OpenAI and browser UI smoke checks remain outstanding.
  Phase 3 Research Engine implementation should wait for the Phase 2
  Definition of Done and landing audit.
