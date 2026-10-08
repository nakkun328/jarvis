# Research search contract

Search is the first stage of the R1 research path (search, read, source, citation). This
page covers the provider-neutral contract (`backend/research/search.py`), the result
normaliser (`backend/research/normalizer.py`) and the deterministic mock
(`backend/research/mock_search.py`). No live provider is implemented yet; see
[provider selection](research-provider-selection.md).

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
| `SourceType` | `official`, `docs`, `academic`, `news`, `community`, `blog`, `forum`, `unknown`. Providers leave it `unknown`; classification belongs to the later quality step. |
| `SearchError` | Carries only `reason`, a `SearchFailure`: `invalid_query`, `unauthorized`, `rate_limited`, `timeout`, `network_error`, `bad_response`, `unavailable`. It rejects free text, so upstream messages cannot be attached. |

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
