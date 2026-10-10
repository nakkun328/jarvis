# Search provider selection (JAR-36 decision document)

Status: proposal for maintainer decision. A Tavily adapter exists but is off by default,
unwired, and has not been run against the real service ([research-search.md](research-search.md#tavily-adapter-off-by-default));
the owner's first trial and terms check decide whether it is adopted. The `SearchProvider`
contract and the mock are in place; this page compares candidates for the first live adapter.

**Evidence limit.** This was written without network access, from general knowledge of
the vendors as of mid-2026 and not checked against their current documentation. Every
pricing, quota, terms, retention, availability and feature statement below is an
unverified recollection: **verify before adoption**. Where a vendor's plan has changed
recently, the claim may already be wrong. Nothing here is a quotation of terms.

## Update 2026-10-08: checked against vendor pages and current articles

The table below replaces the recollections for the free-tier facts; everything else in this page is still unverified. Figures are from the vendors' own pricing pages where noted, otherwise from third-party comparisons (marked *secondary*). Terms of service, query retention and Japanese result quality were **not** verified for any provider and must still be checked before adoption.

| Provider | Free allowance now | Card needed | Notes |
| --- | --- | --- | --- |
| Tavily | 1,000 credits per month, recurring (vendor page); basic search = 1 credit (*secondary*) | No | Plain REST. Request neither raw content nor the generated answer. A hard ceiling is built in: with no card, the free tier simply stops. |
| Exa | $10 in credits at signup, reset to $10 on the 1st of each month (vendor page); Instant search is $4 per 1,000, so about 2,500 searches (vendor page) | No | Returns page contents on request: do not use them as evidence. Japanese quality unchecked. |
| Brave Search API | The no-card free plan was removed in early 2026; $5 of credits per month, about 1,000 queries (vendor page, *secondary*), then $5 per 1,000 | **Yes** (identity check) | Storing results to train or tune an LLM needs a plan that grants storage rights (vendor page); our use (titles, URLs and snippets as source records) is not obviously that, but confirm. A card means charges are possible past the credits. |
| Serper | 2,500 free queries once, no expiry stated (vendor page) | No | Returns Google results through a reseller: terms risk (see below). |
| Linkup | 4,000 free queries once (vendor page); a monthly top-up is *secondary* only | Unclear | Search $0.005 to $0.006 per request. |
| Jina (`s.jina.ai`) | 10M tokens once, at least 10,000 tokens per search, so about 1,000 searches; non-commercial licence (*secondary*) | Unclear | One-time only. |
| Google Custom Search JSON API | 100 free queries per day | n/a | **Closed to new customers; discontinued on 2027-01-01** (*secondary*). Do not plan on it. |
| SearXNG (self-hosted) | No licence cost | No | Has a JSON API, language and category parameters; upstream engines may throttle the host; a private single-user instance can relax the limiter (*secondary*). The enabled engines still receive each query. |

### Updated recommendation (for the maintainer to confirm)

1. **First trial: Tavily free tier.** Recurring 1,000 credits a month, no card (so no way to be billed by surprise), plain REST with a bearer token, and it fits a personal tool: 1,000 basic searches a month is about 30 a day. Use title, URL, snippet and date only.
2. **Second choice: Exa free credits** (about 2,500 Instant searches a month, no card).
3. **Brave** moves down: it needs a card and bills past the credits unless a spending cap exists (not verified).
4. **SearXNG** stays the no-account option if a third-party account is unacceptable.
5. Not recommended: Google Custom Search (closed), scraped-Google resellers such as Serper (terms risk), one-time grants (Linkup, Jina) as a base.

Quota check: Standard Research uses up to 5 queries per run (see [research-standard.md](research-standard.md)), so 1,000 credits is roughly 200 Standard runs a month; Quick uses fewer.

Sources: the Tavily, Exa and Brave pricing pages, the Serper and Linkup home and pricing pages, and comparison articles from 2026. Re-check them on the day of adoption: free plans have changed repeatedly this year.

## Requirements that drive the choice

- Returns title, URL and snippet for a text query, ideally with a publication date.
- Works for Japanese queries and Japanese-language results.
- Terms permit an application to store and display titles, URLs and snippets as research
  source records, and to pass snippets to an LLM.
- Predictable cost with a hard monthly cap; a free or low-cost tier for a personal tool.
- Key kept in an environment variable and sent only to the vendor.
- Minimal and understood disclosure: **every search sends the query text to the provider**
  (and, for metasearch, to the engines behind it). Queries are derived from the user's
  question and could contain personal details.

## Comparison

All cells are claims to verify before adoption.

| Candidate | Cost and quota (verify) | Terms to check (verify) | Freshness and dates (verify) | Query privacy | Japanese (verify) | Key handling |
| --- | --- | --- | --- | --- | --- | --- |
| Brave Search API | Paid per-request plans; a small free or credit tier existed and has changed before. Per-second rate limit on lower plans. | Whether results may be stored and shown, and whether LLM use needs a specific plan. | Own independent index; results often carry an age or date field, sometimes relative text. | Query goes to Brave only. Check retention and logging policy. | Country and language parameters, Japan locale reported supported. | One subscription token sent in a request header. |
| Tavily | Credit-based; a monthly free allotment was offered. Advanced searches cost more credits. | Terms for storing returned content; vendor-side retention of queries. | Aggregates other sources; optional recency filters; may return page content. | Query goes to Tavily and possibly onward to its sources. | Works on multilingual queries; quality unverified. | Bearer key. |
| Google Custom Search JSON API | Small free daily quota, paid beyond it up to a daily cap. | **Availability for new users is doubtful**: Google was reported to be closing this API to new customers and retiring it. Confirm before any work. | Google index, but only the engine's configured scope; date metadata inconsistent. | Query goes to Google, tied to a Cloud project. | Strong. | API key plus engine id as query parameters (key can leak in URLs and proxy logs). |
| Bing Web Search API | **Retired by Microsoft in 2025** per our recollection. Replacement is an Azure agent grounding feature, not a general SERP API. | Grounding terms restrict how results are used and shown. | n/a | Query goes to Microsoft via Azure. | Strong. | Azure credentials. |
| SearXNG, self-hosted | No licence cost; local hosting and upkeep. No vendor quota, but upstream engines may throttle or block the host. | Scrapes upstream engines whose own terms may not allow automated use. | As fresh as the engines it queries; date fields vary by engine. | Query leaves the machine to whichever upstream engines are enabled, from your IP. No third-party account, so no key to leak. | Good, depends on enabled engines. | No key; JSON output must be enabled and the instance kept private (bind to loopback). |
| DuckDuckGo Instant Answer API | Free. | Intended for instant answers, not general web search. | Mostly encyclopedic abstracts and related topics. | Query goes to DuckDuckGo. | Limited. | None. |
| Exa, Serper, SerpAPI, Kagi, Mojeek (not assessed) | Various. | Scraped-Google resellers carry legal and terms risk; others unreviewed. | Unreviewed. | Query goes to the vendor. | Unreviewed. | Header or parameter key. |

Notes on specific candidates:

- **Google Custom Search** should be treated as unavailable until its current status for
  new accounts is confirmed. Even if available, the configured-engine model and key-in-URL
  pattern fit less well than a plain header token.
- **Bing** should not be planned on; if Microsoft grounding is revisited, confirm it can
  return plain result lists rather than generated answers.
- **Tavily** offers convenient LLM-oriented fields such as extracted page content and a
  generated answer. JARVIS must not use them as evidence: page text is to come only from
  the Reader, which enforces the fetch policy and records the content digest, and a
  vendor-generated summary has no source we can trace. If adopted, use only title, URL,
  snippet and date, and request neither raw content nor an answer.
- **DuckDuckGo Instant Answer** cannot return ranked web results. Unofficial scraping
  libraries for DuckDuckGo's HTML search are brittle and may violate its terms; do not use
  them.
- **SearXNG** is the only option with no vendor account, but it moves privacy rather than
  removing it: the enabled engines still receive the query, and operations (updates,
  blocked IPs, captcha pages, result-format drift) become the maintainer's job. It is a
  good later option if a no-vendor stance matters more than convenience.

## Recommendation

For the first live trial (JAR-36), adopt **Brave Search API**, subject to the checks below.
Reasons: it is a plain REST API with a header token and a documented rate limit, it is an
independent index rather than a scraper of another engine, queries go to one clearly
identified party, it exposes country and language parameters suited to Japanese queries,
and it returns structured fields that map cleanly onto `FieldMap`. Keep the adapter small
and behind `SearchProvider` so the choice is reversible; **Tavily** is the second choice if
Brave's terms or free tier do not fit, and **SearXNG** the alternative if no third-party
account is acceptable.

This is a recommendation from recollection, not a finding. If verification shows terms
that forbid storing snippets, a plan that no longer fits, or poor Japanese results, move to
the next candidate.

## Verify before adoption

1. Current plans, free allowance, price and rate limits for the chosen provider, and
   whether a hard spending cap can be set on the account.
2. Terms on storing titles, URLs and snippets, caching, display, and passing them to an LLM.
3. Query retention, logging and training-use policy; where requests are processed.
4. Behaviour for Japanese queries: language and locale parameters, result quality, and date
   fields. Run a handful of representative Japanese and English queries by hand.
5. The shape of date fields (absolute versus relative) so the `FieldMap` and the timestamp
   rules in the normaliser fit.
6. Whether the API key can be restricted or rotated.

## Adapter requirements for JAR-36 (once approved)

- Key from an environment variable (proposed name `JARVIS_SEARCH_API_KEY`), read at
  startup, sent only in the vendor's documented header, never logged, returned or put in a
  URL. The adapter is disabled when the key is absent, with a clear configuration error.
- A search budget per research session and per day, and a bounded retry on `rate_limited`
  with backoff; the research engine, not the adapter, owns retries.
- Query hygiene: send only the planned search text, bounded to 500 characters, and none of
  the conversation or memory context. Decide whether queries derived from private notes
  need a redaction step or explicit consent.
- Timeouts, no redirect following, a response size cap, and failures mapped to
  `SearchFailure` without upstream text.
- A manual opt-in smoke test that is never part of CI.

## Maintainer decisions required

1. **Provider**: accept Brave Search API for the first trial, or choose another.
2. **Account and billing**: who creates the account, accepts the vendor terms, and sets the
   monthly spending cap. Account creation and terms acceptance must be done by the
   maintainer; this work cannot do them.
3. **Privacy stance**: confirm it is acceptable that research queries leave the machine to
   the provider, and whether any categories of query must never be sent.
4. **Key location**: the environment variable name and where the key is stored on the
   maintainer's machine.
5. **Retention**: whether snippets may be stored in the local research tables, and for how
   long, given the vendor's terms.
