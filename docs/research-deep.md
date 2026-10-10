# Deep Research

Deep is the third level a person can request (`quick`, `standard`, `deep`). It splits a question
into sub-questions and researches each one with the same machinery Standard uses
([research-standard.md](research-standard.md)): search, page selection, safe reader, verified-quote
claims, cross-check and conflict detection. Code: `backend/research/deep.py`.

## Flow

1. **Plan.** The chat model is asked for 2-5 sub-questions as one JSON object
   `{"sub_questions": [...]}`. The reply is checked by fixed code: exactly that key, 2-5 strings,
   each 4-200 characters after cleaning, no URL, no duplicates. Any failure (provider error,
   timeout, invalid reply) falls back to a deterministic split (one sub-question per comparison
   target, else the question itself) and the result carries a caveat. The model sees only the
   question, never page text.
2. **Per sub-question.** Queries are built by the deterministic planner from the sub-question,
   searched, the best distinct pages are chosen (one page per domain first, a relevance
   pre-score, blocked URLs set aside and not counted), read, and the model proposes claims with
   quotes. Only claims whose quote occurs word for word in the page are kept; "no information"
   statements are dropped.
3. **Follow-up rounds.** While the existing decision logic reports a gap (too few relevant
   sources, open conflict, no authoritative source, stale sources) and budget remains, one
   follow-up query is generated for the weakest sub-question (the one with an open conflict, else
   the one with fewest claims). At most 2 rounds.
4. **Cross-check** and conflict detection run over all sources.
5. **Result.** Verified claims only, grouped under "Sub-question N: ...", one source numbering
   for the whole run, open conflicts and caveats as in Standard.

## Fixed budgets (constants block in `deep.py`)

| Limit | Value |
|---|---|
| Sub-questions | 5 (model plan needs at least 2) |
| Search requests per run (follow-ups included) | 10 |
| Queries per sub-question | at most 3 |
| Results per query | 5 |
| Pages tried | 20 (3 per sub-question, 2 per follow-up) |
| Follow-up rounds | 2 (7 rounds in all) |
| Wall clock | 600 s (hard timeout 660 s) |
| Verified claims | 30 (6 per round) |

**Worst case: 10 search requests per run**, plus one chat-model call for the plan and one per
round. The monthly budget `JARVIS_SEARCH_MONTHLY_LIMIT` still applies through the existing
budget guard: a run is refused up front when it is used up, and when it runs out mid-run the
remaining sub-questions and follow-ups are skipped.

## Failure and partial results

A run never invents an answer. With no verified claim it fails (`synthesis_failed`, or
`search_failed` / `timeout` when that was the cause) and stores no result. With at least one
verified claim, a stop caused by the search budget, the time limit, the page or query budget, or
a failed sub-question gives a completed result with a caveat saying what was not done.
Cancellation ends the session `cancelled` with no result.

## Progress

The Task has four steps (plan, research the sub-questions, follow up on gaps, write). The
session detail adds `progress.sub_question` and `progress.sub_questions` next to the stage code
and counters, and the Research screen shows "調べ項目 i/n".

## Choosing the level

Only a human chooses Deep: the Research screen's level list, the API (`"level": "deep"`) or
`JARVIS_CHAT_RESEARCH_LEVEL=deep` for research started from chat. Nothing selects it
automatically (the rule table in `levels.py` is not wired to any live path).

## Reuse

A completed Deep result can satisfy a later quick or standard request (the level order is
quick < standard < deep); a quick or standard result never satisfies a deep request. Freshness
rules are unchanged.

## Limits

A page already read for one sub-question is not read again for another, so claims from it are
attributed to the sub-question that read it first. Source ratings and conflict flags are
heuristics; a citation shows the quote is in the page, not that the claim is true.
