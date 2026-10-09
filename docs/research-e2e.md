# Research end-to-end tests (JAR-58)

`tests/test_research_e2e.py` drives the whole Research Engine through its public HTTP API with fakes only. No socket is opened (the `no_network` fixture makes any connection attempt fail) and no key is needed.

Path under test: `POST /api/research/sessions` -> `ResearchRunService.submit` -> task queue -> worker -> `QuickResearch` / `StandardResearch` -> citation verification -> repository -> `GET /api/research/sessions/{id}`. The chat route goes `POST /api/chat[/stream]` -> router -> `RunServiceStarter` -> the same service.

Fakes (from `tests/research_run_support.py`): a canned search provider (can fail or wait on a gate), a page transport behind the real safe reader (so URL blocking is the real code), a scripted chat model that proposes the claims it is told to, and a fake router.

## Scenarios

| Scenario | Test(s) |
| --- | --- |
| Success with several sources, Quick and Standard, citations verbatim in the page | `test_multiple_sources_give_a_cited_result_at_both_levels` |
| The task queue carries the run; the goal holds the id, never the question | `test_the_task_queue_carries_the_run_and_ends_completed` |
| Cross-check: agreeing sources are rated 1.0 | `test_agreeing_sources_are_corroborated_in_the_ratings` |
| Cross-check: a fact only one source states is cited but not corroborated | `test_a_fact_found_in_one_source_only_stays_unverified_by_cross_check` |
| Single source: few-sources caveat, agreement `None` / `agreement_no_comparison` | `test_a_single_source_result_carries_the_few_sources_caveat` |
| Conflict (60 s vs 120 s): flagged open, both sides shown, never resolved | `test_conflicting_figures_are_flagged_and_both_sides_shown` |
| Unverifiable claims dropped and reported; wrong-source quote refused | `test_a_claim_with_an_invented_quote...`, `test_a_quote_from_the_wrong_source...` |
| Nothing verifiable: failed `synthesis_failed`, no result; insufficient-evidence reply completes with the fixed notice | `test_when_nothing_can_be_verified...`, `test_an_honest_insufficient...` |
| Unreadable pages: caveat, no claim from them; all pages failing: `reader_failed` | `test_unreadable_pages...`, `test_every_page_failing...` |
| Page limits (Quick 3, Standard at most 8) | `test_quick_reads_at_most_three_pages`, `test_standard_never_reads_more...` |
| Search budget exhausted before queueing (429, nothing created) and mid-run (`search_failed`, no fetch) | `test_an_exhausted_search_budget...`, `test_a_budget_that_runs_out_mid_run...` |
| Provider failures never give an answer: search error / no hits / chat error / non-JSON reply | `test_a_failing_search_provider...`, `test_a_search_without_hits...`, `test_a_failing_chat_provider...`, `test_a_model_reply_that_is_not_json...` |
| Cancel at both levels: `cancelled`, no result, model never called, slot freed | `test_cancel_stops_the_run...` |
| SSRF: metadata/loopback/localhost hits are never fetched or stored | `test_internal_urls_from_a_search_are_never_fetched`, `test_only_internal_urls...` |
| Chat -> research: starts, completes with citations, fixed reply, no model call | `test_a_chat_message_starts_a_research...` |
| Chat skip reasons: `low_confidence`, `budget_exhausted`, `busy`; research off leaves the route unchanged | `test_chat_falls_back_to_the_main_agent_and_says_why`, `test_chat_with_research_switched_off...` |

`not_configured` and `refused` skip reasons are covered at unit level in `test_chat_research_route.py`.

## Known gaps (current behaviour is asserted)

These tests assert what happens today and carry a `known gap:` comment. When the behaviour is fixed the assertion fails on purpose; update it together with the fix.

1. Near-duplicate claims (`test_known_gap_near_duplicate_claims_are_all_kept`): two reworded copies of one fact from one source are both stored and listed. Fix would give one claim.
2. One domain dominance (`test_known_gap_one_domain_can_supply_every_source_without_a_caveat`): three pages of one domain count as three independent sources, agreement is 1.0 and the result has no caveat. A diversity caveat would appear in `result_text`.
3. Blocked URLs use page budget (`test_known_gap_blocked_urls_use_up_the_page_budget`): internal URLs in the search hits are rejected before any fetch, but each still counts as a page tried, so a flood of them crowds good sources out of the first pass.
4. Quick prints the model's unverified prose (`test_multiple_sources...[quick]`): the Quick result starts with the model's own free-text answer, then the verified claims. Standard builds the text from verified claims only.

## Running

`python -m pytest tests/test_research_e2e.py` (about 30 s; most of it is the polling worker).
