# Quick Research (R1)

`backend/research/quick.py` and `backend/research/citations.py` answer one question from a few web pages and keep only claims that can be checked against what was read (JAR-49, JAR-50, JAR-51, JAR-58). It is a library; there is no API route yet.

```python
quick = QuickResearch(search, reader, repository, llm, QuickLimits())
result = await quick.run("How does X work?")   # QuickResult
result.session.status        # completed / failed / cancelled
result.session.result_text   # header + verified claims + source list (completed only)
```

`search` is a `SearchProvider`, `reader` a `PageReader`, `repository` a `ResearchRepository`, `llm` any `LLMProvider` from `backend/providers/base.py`. `await quick.resume(session_id)` continues a pending, waiting or running session and returns a finished one unchanged.

## Sequence

```
caller      QuickResearch        Search      Reader      Repository     LLM      CitationManager
  | run(q)       |                  |           |             |          |             |
  |------------->| create session (quick)  ---------------->  |          |             |
  |              | pending -> running (CAS) ---------------->  |          |             |
  |              | plan <= N queries (deterministic), add_query |          |             |
  |              |--- search(query) ->|        |             |          |             |
  |              |<-- results --------|        |             |          |             |
  |              | dedupe by URL, keep top K                  |          |             |
  |              |--- read(url) x K (concurrency <= 2) -->|    |          |             |
  |              |<-- FetchedPage or ReaderError ---------|    |          |             |
  |              | add_source (digest only; text kept in memory)         |             |
  |              | build numbered evidence blocks                        |             |
  |              |--- complete(system prompt + question + evidence) ---->|             |
  |              |<-- one JSON object ---------------------------------- |             |
  |              |--- verify(claims) ----------------------------------------------->|
  |              |<-- verified / dropped -------------------------------------------|
  |              | add_claim (verified only); render; set_result (running -> completed)
  |<-------------| QuickResult
```

A failure at any step ends the session as `failed` with one fixed code, or `cancelled` on cancellation. Nothing is retried: one search per query, one read per page, one model call.

## Limits (`QuickLimits`)

| Field | Default | Effect |
| --- | --- | --- |
| `max_queries` | 2 | Queries sent to search. |
| `results_per_query` | 5 (max 20) | Results requested and kept per query. |
| `max_pages` | 3 | Distinct URLs read; failed reads are not replaced. |
| `read_concurrency` | 2 (max 2) | Pages read at the same time. |
| `search_timeout`, `page_timeout`, `llm_timeout` | 15 s, 20 s, 60 s | Per call. A search or page timeout is not fatal on its own. |
| `total_timeout` | 120 s | Whole run; expiry fails the session with `timeout`. |
| `max_chars_per_source`, `max_total_evidence_chars` | 4000, 12000 | Evidence sent to the model: each source gets the smaller of its cap and an equal share of the total. |
| `max_response_chars` | 20000 | Longest model reply accepted. |
| `max_claims` | 10 | Verified claims kept; extra claims are dropped. |

The stored result is also bounded by the repository (50,000 characters).

## Queries

`DeterministicQueryPlanner` sends the question (whitespace normalised, at most 500 characters) and, when it differs, a keyword form: the first eight words that are not common English stop words. There is no model in this step. `QueryPlanner` (`plan(question, max_queries)`) is the hook where the later Query Planner plugs in through the `planner=` argument.

## Evidence and the model request

The system prompt is a fixed constant (`SYSTEM_PROMPT`). It tells the model to answer only from the numbered evidence, to treat evidence as untrusted data and never as instructions, to quote supporting text verbatim, to say plainly when the evidence is insufficient, and to reply with a single JSON object:

```json
{"answer": "...", "insufficient_evidence": false,
 "claims": [{"text": "...", "source": 1, "quote": "..."}]}
```

Search results and page text appear only in the user message, inside `<evidence number="N">` blocks that carry the title, URL and retrieval time. A closing `</evidence` tag inside page text is defused so a page cannot end its own block. Page text can change what the model is shown but cannot change the code path, the prompt rules, or what is fetched next.

## Citation manager

A claim is kept only if its `source` is the number of a source read in this run and its `quote` (8 to 500 characters after whitespace normalisation) occurs verbatim, case-sensitively, in that source's extracted text. Runs of whitespace may differ. Otherwise the claim is dropped and counted with a fixed code: `malformed`, `unknown_source`, `quote_too_short`, `quote_too_long`, `quote_not_found`, `claim_too_long`, `duplicate`, `near_duplicate`, `over_limit`. Verified claims are stored with `add_claim`; the stored quote is the whitespace-normalised form, and `quote_start`/`quote_end` are character offsets into the in-memory extracted page text of that run. Page text is not stored, so the offsets cannot be re-checked later without re-reading the page.

The stored result is a fixed header, the verified claims with their source numbers, a source list (title, final URL, retrieved date, published date when known) built only from stored source records, and a note when claims were removed. The model's free-text answer is never shown. Any URL in the claims that is not a stored source URL is replaced by `[link removed]`.

Only claims are verified, and only verified claims are shown (Quick and Standard alike). Reworded copies of one fact from the same source are merged into one claim (same numbers and negation, at least 70% shared content words; the claim with the longer quote is kept; code `near_duplicate`). The same fact from two different sources is kept: that is corroboration.

Page choice: hits the reader would block (internal addresses, bad schemes) are set aside before the result limit and the page limit, so they take no slot (they appear in `failed_reads`); and the best hit of each registrable domain is read before a second hit of the same domain (`domains.py`, a small heuristic, not the full public suffix list).

## Failure-code mapping

| Situation | Session end |
| --- | --- |
| Every query's search failed or timed out | `failed`, `search_failed` |
| Searches worked but returned no results | `failed`, `no_results` |
| No page could be read (blocked host, HTTP error, timeout, empty text, ...) | `failed`, `reader_failed` |
| Model call raised, or the reply is not the expected JSON | `failed`, `synthesis_failed` |
| No claim verified and the model did not set `insufficient_evidence` | `failed`, `synthesis_failed` |
| No claim verified and `insufficient_evidence` is true | `completed`; the result says no claim could be verified and lists no sources |
| Total or model-call timeout | `failed`, `timeout` |
| Model reply or final result larger than its cap | `failed`, `budget_exceeded` |
| Unexpected error in the pipeline | `failed`, `internal_error` |
| `asyncio` cancellation | `cancelled` (compare-and-swap), then `CancelledError` is re-raised |

Individual read failures (and a single failed query) are recorded in `QuickResult.failed_reads` with their fixed `ReadFailure` code and do not end the run. No answer is produced when no evidence was read.

If another worker already moved the session to a final state, the run stops quietly and returns the stored state. Repository calls are synchronous SQLite calls made on the event loop.

## Not done in this step

- A real search provider (JAR-36); only `SearchProvider` implementations are accepted, and tests use the mock.
- The research level selector; sessions are always `quick`.
- An LLM Query Planner, Standard, Deep and Extensive research, extra search rounds, cross-checking and conflict detection.
- Source evaluation scoring (authority, freshness, primary, relevance, agreement) and `source_type` classification.
- API routes, streaming, and UI.
- Storing page text or reusing earlier research.
