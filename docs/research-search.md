# Research search contract

Search is the first stage of the R1 research path (search, read, source, citation). This
page covers the provider-neutral contract (`backend/research/search.py`), the result
normaliser (`backend/research/normalizer.py`), the deterministic mock
(`backend/research/mock_search.py`) and the optional Tavily adapter
([below](#tavily-adapter-off-by-default)). Search is off by default and is called only by a
research the owner requests; see [provider selection](research-provider-selection.md).

Search results are untrusted data. A title or snippet is never an instruction, and a result
URL is only a candidate: the Reader decides whether it may be fetched.

## Contract

`SearchProvider` is a `Protocol` with one method:

```python
async def search(self, query: SearchQuery) -> Sequence[SearchResult]: ...
```

| Type | Fields and rules |
| --- | --- |
| `SearchQuery` | `text` 1-500 characters and not blank; `max_results` 1-20 (default 5); `language` optional tag such as `ja` or `en-US`; `recency_days` optional 1-3650. Validated on construction. |
| `SearchResult` | `title`, `url` (absolute http(s), no credentials, at most 2048 characters), `snippet`, `rank` (1-based), `provider`, `retrieved_at` (aware UTC), `published_at` (aware UTC or `None`), `source_type` (default `unknown`). Frozen and validated on construction. |
| `SourceType` | Defined once in `backend/research/models.py` and imported by the search contract (`backend.research.search.SourceType` still resolves). Values: `official`, `docs`, `academic`, `news`, `community`, `blog`, `forum`, `unknown`. Providers leave it `unknown`; classification belongs to the later quality step. |
| `SearchError` | Carries only `reason`, a `SearchFailure`: `invalid_query`, `unauthorized`, `rate_limited`, `timeout`, `network_error`, `bad_response`, `unavailable`, `quota_exhausted` (the vendor reports its plan quota used up), `quota_exhausted_local` (our own monthly guard refused; no request was made). It rejects free text, so upstream messages cannot be attached. |

Semantics providers must keep:

- A genuine zero-hit search returns an empty sequence. A failure raises `SearchError`.
  Neither may be disguised as the other, and results are never invented.
- Results are ordered by rank, ranks are 1..n, and URLs are unique.
- Raise `SearchError(...) from None` when translating an upstream exception, so its text
  (which may echo the query or a credential) is not chained.
- Log counts and `query_digest(text)` only. Never log query text, page text, headers or keys.

## Normaliser

`normalize_results(raw_results, *, provider, retrieved_at, max_results=20, field_map=...)`
returns `(results, NormalizationReport)`. `raw_results` must be the provider's list of
entries (a non-list raises `TypeError`; the adapter treats a missing array as
`bad_response`). The adapter supplies `retrieved_at` from its clock; the normaliser never
reads the current time.

`FieldMap` lists candidate raw keys per field in priority order. The defaults cover common
names (`url`/`link`/`href`, `title`/`name`, `snippet`/`description`/`content`, `rank`/
`position`, several date keys); a provider overrides only what differs.

### Per-entry rules

| Field | Rule |
| --- | --- |
| entry | Not a mapping: dropped (`not_a_mapping`). |
| `url` | Required. Missing: `missing_url`. Normalised as below, otherwise dropped with the rejection reason. |
| `title` | Cleaned and bounded to 200 characters. Missing or empty after cleaning: the URL host. |
| `snippet` | Cleaned and bounded to 500 characters. Missing: empty string. |
| `published_at` | First parseable candidate key, as aware UTC; otherwise `None`. |
| `rank` | Positive integer (or integer-like string) from the provider; otherwise the 1-based input position. |
| `source_type` | Always `unknown`. |

### URL normalisation

Accepted and canonicalised: scheme and host lowercased, internationalised hosts converted
to punycode, one trailing host dot removed, default ports (80 for http, 443 for https)
dropped, an empty path becomes `/`, fragments removed, percent-escapes upper-cased and
non-ASCII path or query text percent-encoded, and tracking parameters removed (`utm_*`,
`fbclid`, `gclid`, `dclid`, `gbraid`, `wbraid`, `msclkid`, `yclid`, `igshid`, `mc_cid`,
`mc_eid`, `_hsenc`, `_hsmi`). Remaining query parameters keep their order, so URLs that
differ only in parameter order are not merged. The result is idempotent.

Rejected: non-string or empty input (`missing_url`/`invalid_url`), relative or
scheme-less URLs and whitespace or control characters (`invalid_url`), any scheme other
than http/https (`unsupported_scheme`), any `user@` or `user:pass@` part (`userinfo_url`),
a bad or zero port (`invalid_url`), and more than 2048 characters (`url_too_long`).

The normaliser does not decide whether a host is safe to fetch. Loopback, private-range and
metadata addresses are accepted here and must be refused by the Reader's own policy.

### Text cleanup

Control characters, surrogates, private-use characters and invisible direction/zero-width
characters (bidi overrides, zero-width space, BOM) are removed; every whitespace run
becomes one space; the result is trimmed and cut at the bound with a final ellipsis.
Non-string values count as missing. The words are otherwise unchanged, so text that reads
like an instruction stays inert data, bounded in length. HTML tags and entities are not
stripped or decoded; consumers must treat these fields as plain text and escape them when
rendering.

### Timestamps

`parse_timestamp` accepts ISO-8601 (including date-only and `Z`), epoch seconds or
milliseconds (number or digit string), and RFC 2822. Naive values are interpreted as UTC.
Unparseable input, relative text such as "2 days ago", years outside 1970-2100, and
(inside `normalize_results`) values later than `retrieved_at` all yield `None`. It never
substitutes the current time.

### Ordering and the report

Entries are de-duplicated by normalised URL, keeping the best (lowest) rank with ties
going to the earlier entry. The survivors are sorted by rank, cut to `max_results`, and
renumbered 1..n. Nothing is removed silently: `NormalizationReport` holds
`received_count`, `dropped_count` and a read-only `reasons` mapping from a fixed code
(`not_a_mapping`, `missing_url`, `invalid_url`, `unsupported_scheme`, `userinfo_url`,
`url_too_long`, `duplicate_url`, `over_limit`) to a count. `dropped_count` is the sum of
the counts. The report never contains entry content.

## Mock provider

`MockSearchProvider(responses, failures=...)` serves canned raw hits per query text. Query
matching ignores case and repeated whitespace. An empty list is an explicit zero-hit
result and an unlisted query also returns none. `failures` maps a query to a
`SearchFailure`. Hits pass through `normalize_results`, so tests exercise the real
pipeline, and `last_report` exposes what was dropped. `calls` records the queries. It never
touches the network, the clock or `asyncio.sleep`; `retrieved_at` is a fixed constant unless
overridden. `MockSearchProvider.from_mapping({...})` builds one from JSON-style data.

## Plugging in a real provider

1. Write a class with `async def search(self, query: SearchQuery)` in its own module.
   Inject the HTTP client (an `httpx.AsyncClient`) and the key; read the key from an
   environment variable at startup, send it only in the header the vendor documents, and
   never log or return it.
2. Set explicit timeouts, do not follow redirects, and cap the response size.
3. Map failures to `SearchFailure`: 400 to `invalid_query`, 401/403 to `unauthorized`,
   429 to `rate_limited`, timeouts to `timeout`, connection errors to `network_error`,
   5xx to `unavailable`, and non-JSON or unexpected shapes to `bad_response`. Use
   `raise SearchError(...) from None`.
4. Extract the entry list from the payload and call `normalize_results` with the provider
   name, a `FieldMap` for its key names, `query.max_results`, and `retrieved_at` from the
   adapter's clock. Return the results; do not return the vendor's own summary or answer
   text as evidence.
5. Translate `language` and `recency_days` into the vendor's parameters where supported,
   and say which are ignored.
6. Test with a fake transport and a fixture shaped like
   `tests/fixtures/research-search-raw-v1.json` (artificial shapes, not real vendor output). Tests make no
   network calls.

## Tavily adapter (off by default)

`backend/research/tavily.py` implements the contract for Tavily
(`POST https://api.tavily.com/search`, `Authorization: Bearer <key>`). It is used only by
a research the owner requests from the Research screen, and only when research is switched
on (see [Using it from the Research screen](#using-it-from-the-research-screen)); chat never
searches. The library, a factory (`backend/research/search_factory.py`) and the owner's
trial script exist. JAR-36 stays open until the owner has made the first real call and
checked the terms.

### What is sent and what is read

Sent to Tavily: **the query text only** (whitespace collapsed, at most 400 characters; a
longer query is refused locally), plus fixed parameters. The body is exactly:

```json
{"query": "...", "search_depth": "basic", "topic": "general",
 "max_results": 5, "include_answer": false, "include_raw_content": false}
```

`time_range` (`day`/`week`/`month`/`year`, the smallest bucket covering `recency_days`;
none beyond a year) is added when `recency_days` is set. `search_depth` is always `basic`
(one credit; `advanced` costs two). `max_results` is the query's value capped at 10. Images
and favicons are never requested. `language` is not sent (the vendor's accepted values are
unverified), so the result language follows the query.

Read from each result: `title`, `url`, `content` (as the snippet, cleaned and cut to 500
characters) and `published_date`. Nothing else is copied: not the relevance score (rank is
the vendor's order), not `answer`, not `raw_content`, not images. A vendor summary or page
text is never evidence; page text comes only from the Reader. Entries pass through the same
normaliser as every provider (http/https only, credentials and tracking parameters
removed, duplicates merged, control characters stripped). Results stay untrusted data.

### Errors, timeouts and retries

| Condition | `SearchFailure` |
| --- | --- |
| 400, 422 | `invalid_query` |
| 401, 403 | `unauthorized` |
| 429 | `rate_limited` |
| 432, 433 (plan or pay-as-you-go limit) | `quota_exhausted` |
| 5xx | `unavailable` |
| timeout (10 s per request, 25 s total) | `timeout` |
| connection failure | `network_error` |
| other statuses, redirects (not followed), non-JSON, wrong shape, body over 1 MB | `bad_response` |

Only a 5xx or a timeout is retried, once, after 0.5 s and only if the 25-second deadline
allows; 4xx and network errors are never retried. Vendor error text, the key and the
request body are never echoed in exceptions or logs. Logs hold fixed event names
(`search_ok`, `search_retry`, `search_failed`), the provider name, a result count or a
fixed failure code; never query text. The key lives in a wrapper whose repr is `<redacted>`
and is excluded from the `Settings` repr.

### Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `JARVIS_SEARCH_PROVIDER` | `none` | `none` or `tavily`. |
| `JARVIS_SEARCH_API_KEY` | unset | Required when the provider is `tavily`; startup fails with a `ConfigError` if it is missing or blank. |
| `JARVIS_SEARCH_MONTHLY_LIMIT` | `800` | Positive whole number. 800 is a proposal, below the free plan's 1,000 credits per month, leaving headroom for trial runs and retries. |

`create_search_provider(settings, repository=...)` returns `None` when the provider is
`none`. For `tavily` it returns the adapter wrapped in the budget guard and refuses to
build one without a usage counter (a `repository` or an explicit `usage_counter`).

### Local monthly budget guard

`BudgetedSearchProvider` (`backend/research/search_budget.py`) refuses a search with
`quota_exhausted_local`, without any network call, when the queries recorded in the
`research_queries` table during the current UTC calendar month plus the searches in flight
reach `JARVIS_SEARCH_MONTHLY_LIMIT`. The count is a read-only SQL count behind
`ResearchRepository.count_queries_in_month`; there is no schema change. If the counter
fails, the guard fails closed (`unavailable`). If a caller records the query before it
searches, that query is already counted and the effective ceiling is one lower.

**This is not the vendor's credit balance.** It counts only queries JARVIS recorded. It
does not see searches made by the trial script or by other clients with the same key,
retries the vendor may bill, the plan's real reset date, or a changed allowance. Check the
balance in the Tavily dashboard, and set the limit from it.

### Owner setup (first real call)

1. Create a Tavily account on the free plan yourself, and create an API key in its
   dashboard. Do not paste the key into chat, an issue, a pull request or any file in the
   repository.
2. In your own terminal, set the variables for that shell only. The first line reads the key
   without echoing it or writing it to shell history:

   ```sh
   read -rs JARVIS_SEARCH_API_KEY && export JARVIS_SEARCH_API_KEY
   export JARVIS_SEARCH_PROVIDER=tavily
   ```

   To make them permanent, put the two lines in a private shell profile that is not in a
   Git repository. Leave `JARVIS_SEARCH_PROVIDER` unset (or `none`) to keep search off.
3. Dry run first; it uses a fake transport and needs no key:

   ```sh
   python scripts/search_trial.py --dry-run
   ```

4. Make the real call (each query costs one credit; at most 10 queries per run):

   ```sh
   python scripts/search_trial.py                      # three built-in Japanese queries
   python scripts/search_trial.py "your query" "another"
   ```

   It prints only rank, title, URL, snippet length and published date, plus the credits the
   vendor reports for each call. It never prints the key, the request body or the raw
   response, reads the key only from the environment, stops at the first error and exits
   non-zero with a fixed message such as `Search failed (unauthorized). Stopped.` The
   `--max-results` option (1-10, default 3) limits the results per query. Trial queries are
   not recorded in JARVIS' database, so the monthly guard does not count them.
5. Nothing ran against the real service while this adapter was written; every test uses a
   fake transport and fails on a real socket. Until you have run step 4 yourself, treat the
   field names and the parameters above as unverified against live behaviour.

The requests ignore proxy environment variables (`trust_env` is off) and do not follow
redirects.

### Using it from the Research screen

The Research screen can start a Quick or Standard research. It stays off until all three of
these are set in the shell that starts JARVIS (the key is read without echo, never written
to a file in the repository):

```sh
read -rs JARVIS_SEARCH_API_KEY && export JARVIS_SEARCH_API_KEY   # your own Tavily key
export JARVIS_SEARCH_PROVIDER=tavily                             # a search provider
export JARVIS_RESEARCH_ENABLED=1                                 # the switch
```

A chat provider must be configured too (the one chat uses; the research model calls go to
it). With any of the three missing, or an invalid `JARVIS_RESEARCH_ENABLED`, the form is
disabled and says why (an invalid value stops startup). `JARVIS_SEARCH_MONTHLY_LIMIT` (default
800) is the local cap described above; a request is refused with `search_budget_exhausted`
before anything is queued once it is reached.

What is sent where, per research:

- **To Tavily**: search queries built from the question only (never from memory notes): the
  question itself (a query longer than 400 characters is refused locally; the shorter keyword
  query still runs) and, for Standard, up to a few reworded or follow-up queries. A Quick
  research sends at most 2 queries and a Standard at most 5, each one credit. The vendor may
  retain and use them (see below). The screen says so next to the start button.
- **To the pages' own sites**: a plain GET for up to 3 (Quick) or 8 (Standard) result pages,
  through the safe reader (public addresses only, redirects re-checked, no cookies, size and
  time bounds).
- **To the chat provider**: the question and bounded excerpts of the fetched pages. Memory
  notes are not included.

Limits: one research at a time, at most 2 or 5 queries, 3 or 8 pages, 120 s or 300 s. Nothing
runs unless you press the button; there is no retry and no background research.

To stop a research, press 調査を取り消す on the screen (or `POST
/api/research/sessions/{id}/cancel`). A queued research stops at once; a running one stops at
its next checkpoint, so a query already sent still counts against the credits. Stopping
JARVIS also stops the run; the session is marked failed or cancelled and is never re-run.
Check the credit balance in the Tavily dashboard: the local counter only knows what JARVIS
recorded.

Nothing in the automated tests or the browser checks used a real key, the real service or a
real model; they use a fake search provider, a fake page transport and a scripted model. The
first live research is yours to run.

### Checks that remain with the owner

Read the vendor's current terms and privacy policy before adopting it, and record the
answers (see [provider selection](research-provider-selection.md#verify-before-adoption)):

- May titles, URLs and snippets be stored as research source records, shown in the UI and
  passed to an LLM?
- How long does Tavily retain query text, whether it is used for training, and where
  requests are processed?
- Is the free allowance still 1,000 credits per month, is there a hard cap or a way to
  prevent pay-as-you-go charges, and how are credits counted?
- Result quality for Japanese queries (the trial in step 4 is the first evidence).

### What was found on 2026-10-08 (not legal advice; re-read the pages before relying on this)

First live trial (the owner's own key, three Japanese queries: a weather forecast, Python 3.13 features, Mount Fuji routes): each returned three relevant Japanese results with title, URL and a snippet capped at about 500 characters. `published_date` was absent for these general-topic results, and the vendor did not report `usage.credits` for the basic call, so the local budget guard (which counts queries JARVIS recorded) is the only counter. Time-sensitive pages such as a forecast return a current page, not today's content: the Reader must fetch the page and the claim must come from its text.

Reading the vendor's terms and privacy policy:

- **Storing results**: not addressed explicitly. The terms grant a revocable right to use the API for internal business purposes and forbid redistribution and sublicensing. Storing titles, URLs and snippets as private research records for one owner looks consistent with that, but it is not confirmed in writing.
- **Use with an LLM**: allowed for the owner's own application; the user must verify outputs, and the AI features must not be used for safety-critical, medical, legal or financial decisions.
- **Queries**: the privacy policy says data is kept for the period needed to provide and improve the service and that query data may be used to improve future responses; the terms say that, for the AI functionality, input may be used for training. No retention period is stated and **no zero-retention option** is offered. Processing is in the United States.
- **Free plan**: no personal/commercial distinction is stated.

Consequence for JARVIS: **a search query leaves the machine and may be kept and used by the vendor.** Queries are built from the user's question only (never from memory notes), but a question can contain personal details. Until the owner decides otherwise: do not put memory content in queries, keep the Research screen and chat copy honest that web research sends the question text to a third party, and prefer a self-hosted SearXNG (queries still reach upstream engines, but no single vendor account holds them) or another provider if that is not acceptable.
