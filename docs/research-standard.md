# Standard Research and follow-up queries (JAR-48, JAR-52)

Library code only. `backend/research/standard.py` runs a research session at the `standard` level; `backend/research/followup.py` builds the extra search queries it needs. Nothing here is wired to a route or to the Task queue, and no search provider is chosen yet (tests use the mock provider, a fake page transport behind the real safe reader, and a fake model).

```python
research = StandardResearch(search, reader, repository, llm, StandardLimits())
token = CancellationToken()
result = await research.run("Which is better, A or B?", cancel=token, on_progress=show)   # StandardResult
result.session.status        # completed / failed / cancelled
result.session.result_text   # verified claims + sources + open conflicts + caveats (completed only)
result.caveats               # fixed Caveat codes, the limits of this result
```

`search`, `reader`, `repository` and `llm` are the same objects Quick Research takes; `reader` may be any object with `async read(url) -> FetchedPage` (normally `PageReader` with its injected transport and resolver). `await research.resume(session_id)` continues a pending or running session; finished sessions are returned unchanged, and a session that is not at the `standard` level is refused.

## Flow

1. **planning**: planner v2 (`planner.py`, no model) gives up to `initial_queries` queries from the question.
2. **searching**: each query goes to the `SearchProvider` (results are normalised by the provider adapter; the mock goes through `normalizer.py`). Result URLs are de-duplicated by their normalised form (tracking parameters and fragments do not make a new page) and ordered by search rank.
3. **reading**: the best not-yet-tried URLs, up to the page cap of the pass, are read through the safe reader (bounded concurrency 2, per-page timeout). Each page becomes a stored source (digest only; page text stays in memory), is classified and rated (authority, freshness, relevance, `evaluation.py`), and is numbered by its position in the stored source list (ordered by retrieval time, ties by insertion), the same number the result text and the detail screen use. Two pages with the same final URL (after normalisation: fragment, `utm_*` parameters and trailing slash ignored) are one source; different pages of one site stay separate sources. Classification (`classification.py`) also knows vendor documentation hosts (`ai.google.dev`, `platform.openai.com`, `console.groq.com`, `learn.microsoft.com`, plus the `docs.`/`developer.` name pattern) as `docs` by host, documentation-like paths (`docs`, `pricing`, `api`, ...) on vendor sites such as `cloud.google.com` as `docs` by path (authority capped at 0.6), and GitHub `docs`/`README` paths as `docs` by path. Benchmark, comparison and aggregator sites (`benchlm.ai`, `docsbot.ai`, `openrouter.ai`, ...) stay type `unknown` with authority 0.25 but carry their own rule id (`benchmark_site_host`, `comparison_site_host`, `aggregator_host`) instead of `no_rule`. All host matches are exact host or subdomain matches, never substrings. Rule `subject_official_host`: when the main label of a site's registrable domain equals a distinctive Latin term of the question (or a term every issued query shares), e.g. `sqlite` in `sqlite.org`, the site is `docs` by host with authority capped at 0.6 (reason `authority_subject_official`). Whole-label match only (`sqlite.org.evil.example` and `mysqlite.org` do not match), only when no other rule decided the URL (so `/forum`, `/blog`, forum and blog hosts keep their kind), and never for open platforms such as `github.com`. Authority 0.6 meets the `no_authoritative_source` threshold, so such a page clears that caveat.

**Relevance** (`relevance_rating`) uses the Latin/ASCII technical terms of the question (case-folded, stop words removed) and of the issued queries, plus the question's CJK terms. Latin terms count in the title, the URL host/path and the text (compounds such as `wal-index` are split on the page side); with Latin terms present they carry 85% of the score (the asker's own terms 80% of that, terms only the queries added 20%, and those only when the page has one of the asker's terms), so an English official page beats a Japanese page that shares only filler words. Reading order: before the page cap is applied, hits whose title, snippet and URL score below 0.2 with the same function are moved behind the others, so they are read only if pages are left over.
4. **verifying**: the model gets only the new sources as numbered, delimited evidence and the fixed system prompt of Quick Research, and returns claims with quotes. `CitationManager` keeps a claim only when its quote is a verbatim (whitespace-normalised) substring of the cited page's extracted text; everything else is dropped and counted. Then `cross_check_and_store` writes the agreement rating of every source and `detect_and_store_conflicts` records disagreements as `open`.
5. **deciding**: `decide_additional_search` (`search_decision.py`) looks at the stored ratings, the open conflicts and the counters. On `continue` with a gap, `generate_follow_ups` builds the next queries and steps 2 to 4 repeat (an "extra round"). On `stop` the loop ends.
6. **writing**: the result text is built from verified claims only; open conflicts and unmet gaps follow as caveats.

Status goes `pending` to `running` to `completed`, `failed` (fixed `FailureReason`) or `cancelled`, always by compare-and-swap in `ResearchRepository`.

## Budgets

Ceilings are the `standard` row of `levels.LEVEL_BUDGETS`; `StandardLimits` rejects anything above them.

| Limit | Default (= ceiling) | Meaning |
| --- | --- | --- |
| `max_queries` | 5 | all queries of all passes together |
| `results_per_query` | 6 | hits kept per query |
| `max_pages` | 8 | pages tried (failed reads count), all passes together |
| `max_search_rounds` | 2 | extra rounds after the first pass |
| `total_timeout` | 300 s | hard wall clock for the whole run |

Further knobs, all bounded: `initial_queries` 3, `follow_ups_per_round` 1, `first_pass_pages` 5 (the rest of the page budget is kept for extra rounds), `pages_per_round` 2, per-search/page/model timeouts, evidence size, `max_claims_per_round` 10, `max_total_claims` 20. New rounds stop at `soft_deadline_fraction` (0.8) of the wall clock so that a started round can finish; the hard `asyncio` timeout at the full budget ends the run as `timeout` and stores no result. The model is called at most once per pass (at most 3 times), and only when the pass found a new source.

A cancellation token is checked before every search, between page reads, and before every step. A cancelled run ends `cancelled` with no result text, even when the first pass had already verified claims, so a partial answer is never shown as an answer. Task cancellation (`asyncio.CancelledError`) records `cancelled` and re-raises.

## Progress

`on_progress(ProgressEvent)` is called, synchronously, with a fixed stage code `planning`, `searching`, `reading`, `verifying` or `writing`, the round index (0 is the first pass) and four counters (queries run, pages tried, sources, verified claims). Events carry no query, URL or page text. An exception in the callback is logged by type and ignored.

## Failures and caveats

First pass: every provider, reader or model problem ends the session `failed` with a fixed code and stores no result: all searches failed `search_failed`, no hits `no_results`, no page readable `reader_failed`, bad or refused model reply or no claim surviving verification without the model saying the sources fall short `synthesis_failed`, oversize reply or result `budget_exceeded`, wall clock `timeout`, anything unexpected `internal_error`. Messages from upstream are never stored.

After the first pass the same problems do not discard what was verified; they are listed in the result as caveats (`follow_up_search_failed`, `follow_up_incomplete`, `reads_failed`, `search_partial`, `claims_removed`). `same_site_sources` is added when two of the sources listed in the result (claims and conflicts) are pages of one registrable domain but not all of them are (`single_domain` covers the all-one-site case); the cross-check already treats such pages as one speaker. The `Sources:` list contains every source number that appears anywhere in the text: cited claim sources and the sources named by the open conflicts shown. A proposed claim that only states missing information (for example "cannot be confirmed from the provided materials", `確認できません`, `記載されていません`, "not specified") is dropped (`absence_statement`) and is never a verified claim; if nothing else is verified it counts as the model saying the sources fall short (`no_verified_claims`). `single_domain` is added when two or more sources carry the verified claims but all share one registrable domain: such pages are not independent confirmation. Pages of one domain are also not compared with each other by the cross-check (their `agreement` is `None`), and in a claim's rating one domain counts as one speaker. Blocked URLs are not counted in `pages_tried`; the best hit of each domain is picked first when the page limit applies. The other caveats come from the stop: each unmet gap of `search_decision` (`few_relevant_sources`, `unresolved_conflicts`, `no_authoritative_source`, `stale_sources`) is listed, together with the budget that stopped the search (`time_budget_exhausted`, `page_budget_exhausted`, `query_budget_exhausted`, `search_rounds_exhausted`, `no_new_sources`) or `no_follow_up_query`. A stop caused by the budget is therefore never presented as completeness. If the model states that the sources do not support an answer and nothing is verified, the session completes with `no_verified_claims`.

Open conflicts are always printed, both sides with their source numbers. Nothing in this module resolves, closes or hides a conflict; only `ResearchRepository.resolve_conflict` does, on an explicit call.

## Follow-up queries (JAR-48)

`generate_follow_ups(question, gaps, existing_queries=..., source_titles=..., max_queries=...)` is pure and deterministic. Each query is `<subject> <fixed phrase>`: the subject is the question's own content terms (planner v2 keywords, at most 6), the phrase comes from a template table per gap (English, or Japanese for a Japanese question), for example `official documentation` for `no_authoritative_source` and `unresolved_conflicts`, `latest` / `release notes` for `stale_sources`, `explained` for `few_relevant_sources`. Some templates add up to two short terms from the titles of sources already found.

- Bounds: at most 3 queries per call, 160 characters each, fixed phrase kept whole.
- De-duplication: a candidate is skipped when its set of case-folded words equals that of a query already run or of an earlier candidate; the next template is tried.
- Reason: every query carries the one gap it answers and a template id.
- Untrusted input: only titles are page-derived, never page bodies or snippets. A title term must be plain letters/digits (or a katakana/kanji run) of 3 to 24 characters, not already in the question, not a generic word (`docs`, `official`, ...) and not an instruction-like word (`ignore`, `system`, `prompt`, ...). URLs, operators, quotes and sentences cannot pass. This is defence in depth: a query only goes to a search engine, never to a model, and its size is bounded either way.

## Verified and heuristic

Verified (checked by code against the page text that was read): each claim's quote occurs word for word in its source; the source list is built from stored records, and any URL in claim text that is not a stored source URL is replaced.

Heuristic (no statement about truth): the source type and its authority, freshness decay and relevance (lexical overlap), the agreement rating, conflict flags (number, date and negation mismatches on the surface of sentences), the thresholds of the stop decision, and the keyword-based planner and follow-up terms. Claim text is written by the model and constrained only by its quote; the free-text answer of the model is discarded.

## Limits

- One model call per pass sees only that pass's new sources, so a claim that needs two sources from different passes is not proposed; the cross-check and conflict detection do compare them.
- Sources in different languages or paraphrases without shared words are invisible to the cross-check, and a different year of the same statistic can be flagged as a conflict.
- Follow-up queries are keyword templates, not reasoning; a gap can stay open after the budget is spent, and the result then says so.
- Page text is capped (20,000 characters per page at the reader, 4,000 per source in the evidence), so a quote past the cap cannot be cited.
- Resuming a `running` session re-runs it from the start; stored queries, sources and claims are reused, not duplicated, but model and search calls are made again.
- No live search, page fetch or model has been used with this code; all verification is with fakes.
