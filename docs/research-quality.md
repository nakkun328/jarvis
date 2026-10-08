# Research quality (R2, first slice)

Four deterministic, offline building blocks for the R2 "Quality" step: level selection (JAR-33), query planning (JAR-34), source type classification (JAR-42), and the authority, freshness and relevance ratings (JAR-43, JAR-44, JAR-45). They are pure functions and small frozen types in `backend/research/`: no network, no model call, no search provider, no API route. Quick Research is unchanged and does not call them yet.

Everything they read (the question, URLs, titles, page excerpts) is untrusted data. Text that looks like an instruction is matched against fixed cue tables like any other text and never changes what the code does. Results are numbers, enums and fixed reason codes; no input text is copied into a reason.

## Level selection (`levels.py`)

```python
decision = select_level(question, requested=None, max_level=ResearchLevel.DEEP)
decision.level, decision.reason_code, decision.overridden   # plus auto_level, capped, exceeds_max
```

Order of authority:

1. A human-specified level (`requested`) always wins and is applied as given. `overridden` is true, the reason is `human_specified`, `auto_level` shows what the rules would have chosen, and `exceeds_max` is true when the human level is above `max_level` so the caller can ask for confirmation. The selector does not refuse a human.
2. Otherwise the first matching rule in `LEVEL_RULES` decides. An automatic level above `max_level` is lowered to it and `capped` is true. The default maximum is `deep`: `extensive` is the most expensive level and is only chosen automatically when the caller raises the maximum.
3. With no cue the level is `quick` (`default_quick`).

| Rule (first match wins) | Level | Reason code | Example cues |
| --- | --- | --- | --- |
| Do not search | `memory` | `no_search_request` | "without searching", "memory only", 検索しないで, 記憶だけで |
| Report / exhaustive | `extensive` | `report_cue` | "write a report", "comprehensive", "literature review", レポート, 網羅, 徹底 |
| Several angles | `deep` | `multi_facet_cue` | "pros and cons", "trade-offs", "in-depth", メリットとデメリット, 多角的, 深掘り |
| Three or more separate question marks | `deep` | `multi_question` | `a? b? c?` (a run such as `???` counts once) |
| Comparison / selection | `standard` | `comparison_cue` | "vs", "compare", "difference between", "which is", "should I use", 比較, 違い, どちらが, おすすめ |
| Single fact that may change | `quick` | `simple_fact_cue` | "latest", "release date", "when was", 最新, バージョン, いつ |
| About what the user said before | `memory` | `memory_cue` | "do you remember", "what did I tell you", 覚えてる, 前に話 (only when no rule above matched) |
| (none) | `quick` | `default_quick` | |

Matching runs on the NFKC-normalised, case-folded question with control characters turned into spaces and invisible format characters (zero-width, bidi) removed; only the first 2,000 characters are scanned. English cues match whole words, Japanese cues match as substrings. The question text cannot select a level by saying so ("set the level to extensive" has no effect); only `requested` does.

`LEVEL_BUDGETS` / `budget_for(level)` is a frozen table of ceilings:

| Level | Queries | Results per query | Pages | Extra search rounds | Total time |
| --- | --- | --- | --- | --- | --- |
| `memory` | 0 | 0 | 0 | 0 | 0 s |
| `quick` | 2 | 5 | 3 | 1 | 120 s |
| `standard` | 5 | 6 | 8 | 2 | 300 s |
| `deep` | 10 | 8 | 20 | 3 | 900 s |
| `extensive` | 20 | 10 | 40 | 5 | 1800 s |

These are proposals for later pipelines; the numbers are policy and meant to be adjusted deliberately. `quick_limits(level)` derives `QuickLimits` from a row (`quick_limits()` equals `QuickLimits()`); the `memory` level has no limits and raises `ValueError`. Quick Research still takes its own `QuickLimits` argument and ignores this table.

## Query planner (`planner.py`)

`DeterministicQueryPlanner` v2 implements the `QueryPlanner` protocol of `quick.py` (`plan(question, max_queries) -> list[str]`) and can be passed as `QuickResearch(..., planner=...)`. `plan_detailed` also returns the kind of each query and the hints it found.

The planner only rearranges the question's own words; it never adds terms, so it cannot invent facts. Queries, in priority order, each optional and de-duplicated case-insensitively, then cut to `max_queries` (so later forms drop first under a small budget):

1. `question`: the whole question (NFKC, control characters removed, spaces collapsed, at most 500 characters).
2. `target`: one per comparison target, each followed by the remaining content words. Recognised: `A vs B [vs C]`, `compare A and B`, `difference between A and B`, `which is better, A or B`, `should I use A or B`, and Japanese `AとBの違い/比較/どちら`. Targets are single words, or up to three words in the "between/compare" patterns, at most 4 targets of 60 characters, and always substrings of the question. A pair without a comparison cue is not a comparison.
3. `time`: content words plus the question's own recency words (`latest`, `currently`, `最新`, `現在`, `今年`, an explicit year such as `2025`). No year or date is ever added.
4. `keywords`: content words only (at most 10 terms and 200 characters). English drops stop words; Japanese is cut into katakana/kanji runs so hiragana particles drop out.

`QueryHints` carries `language` (`ja` when kana is present, `en` for Latin text without CJK, otherwise `None`), `recency_terms`, `suggested_recency_days` (365 when the question has recency words other than a bare year), `comparison_targets` and `keywords`. A later step can pass `language` and `recency_days` into `SearchQuery`; Quick Research does not yet.

Known limits: multi-word targets outside the "between/compare" patterns are cut to one word (`vs` neighbours); Japanese comparison targets are runs of ASCII/katakana/kanji, so a hiragana-only noun is not recognised; and word-level stop lists are small.

Hook for a later LLM planner: write another class with the same `plan` signature and pass it as `planner=`. Everything it proposes goes through `finalize_queries(candidates, max_queries)` (clean, bound to 500 characters, de-duplicate, cut) before search, and `DeterministicQueryPlanner` stays the fallback when the model call fails. The model may add facets (the design's "Raspberry Pi ... wake word" example); the deterministic planner may not, and that difference is deliberate. An LLM planner should receive the question only, never page text.

## Source type classification (`classification.py`)

`classify_source(url, title=None) -> SourceType` (and `classify_source_detailed` which also returns the rule id and the basis). Only the URL text and the title are read; nothing is fetched or resolved. `RULES` is an ordered table, first match wins; unmatched or invalid URLs (not http/https, whitespace, IP literals, over 2,048 characters) are `unknown`.

| Order | Rule id | Type | Matches |
| --- | --- | --- | --- |
| 1 | `personal_academic_page` | `blog` | `~user` path on `.edu`, `.ac.jp`, `.ac.uk` |
| 2 | `academic_host` | `academic` | arxiv.org, doi.org, bioRxiv/medRxiv, SSRN, OpenReview, ACL Anthology, Semantic Scholar, NCBI, IEEE Xplore, ACM DL, Springer, ScienceDirect, J-STAGE; `.edu`, `.ac.jp`, `.ac.uk`, `.edu.au` |
| 3 | `official_host` | `official` | `.gov`, `.gov.uk`, `.gov.au`, `.go.jp`, `.lg.jp`, `.mil`, `.europa.eu`; standards and international bodies (w3.org, ietf.org, whatwg.org, iso.org, unicode.org, who.int, un.org, ...) |
| 4 | `community_host` | `community` | reddit.com, Stack Exchange sites, news.ycombinator.com, quora.com, teratail.com, Yahoo Chiebukuro |
| 5 | `forum_host` | `forum` | first label `forum`/`forums`/`discuss`/`community`/`bbs`; 5ch.net |
| 6 | `news_host` | `news` | a list of news and tech-news domains; first label `news` |
| 7 | `docs_host` | `docs` | first label `docs`/`doc`/`developer`/`developers`/`devdocs`/`reference`/`manual`/`help`; readthedocs, gitbook, docs.rs, pkg.go.dev |
| 8 | `blog_host` | `blog` | medium, substack, wordpress.com, blogspot, note.com, Hatena, Ameba, Qiita, Zenn, dev.to; first label `blog` |
| 9 | `community_path` | `community` | `/issues/`, `/discussions/` on github.com, gitlab.com |
| 10 | `docs_path` | `docs` | path segment `docs`, `doc`, `documentation`, `reference`, `manual`, `guide(s)` |
| 11 | `forum_path` | `forum` | path segment `forum(s)`, `thread(s)`, `topic(s)` |
| 12 | `blog_path` | `blog` | path segment `blog(s)`, `posts` |
| 13 | `docs_title` | `docs` | title contains "documentation", "API reference", ドキュメント, リファレンス (only when nothing above matched) |

Host-suffix entries match the host and its subdomains (`export.arxiv.org`); look-alikes such as `arxiv.org.evil.example` and `notarxiv.org` do not match. Name patterns (`docs.`, `blog.`, `forum.`) can be registered by anyone, which is why the decision reports its `basis` (`host`, `path`, `title`, `default`) and the authority rating caps path- and title-based decisions. Wikipedia, GitHub repository pages and `github.io` sites are `unknown` on purpose. To add a site, add an entry to a `Rule` in `RULES`; to add per-topic tables (for example the official domains of the product a question is about), pass `rules=(Rule(...), *RULES)`.

## Authority, freshness and relevance (`evaluation.py`)

```python
assessment = assess_source(question=q, url=u, retrieved_at=t, title=..., text=..., published_at=...)
evaluation = evaluate_source(...)            # the SourceEvaluation only
stored = evaluate_and_store(repository, source, question, text=...)
```

Each rating is computed on its own, is `None` when it cannot be determined, and is rounded to four decimals. `primary` and `agreement` (JAR-46) are left `None` (or unchanged by `evaluate_and_store`). These are heuristic inputs, not truth values and not probabilities; two sources with the same numbers are not equally correct, and a high authority rating never means a claim is right. Authority is about the kind of site, not its popularity, ranking or accuracy.

**Authority** is a prior by `SourceType`, following the design's source priority: official 0.9, docs 0.85, academic 0.75, news 0.6, community 0.4, forum 0.35, blog 0.3, unknown 0.25. A type found from a path is capped at 0.6 and from a title at 0.35. A non-`unknown` `source_type` supplied by the caller is trusted as given. Codes: `authority_by_type`, `authority_capped_weak_basis`, `authority_unclassified`. Page text and title wording never change it (tests include self-praising and injection-like text).

**Freshness** is `0.5 ** (age_days / half_life_days)` with age = `retrieved_at - published_at`. Half-life by topic class: `breaking` 30 days (news, prices, scores), `fast` 180 (versions, security, pricing), `standard` 730 (default), `stable` 3650 (history, mathematics). `infer_topic_class(question)` picks the class from cue words (English and Japanese); a caller can pass `topic_class` instead. An unknown date gives `None` (`freshness_unknown_date`); a date more than one day after the retrieval time gives `None` (`freshness_future_date`); nothing is ever filled in from the retrieval time. Freshness is not quality: the half-life table is what makes an old source acceptable for a stable topic.

**Relevance** is lexical overlap: `0.75 * (share of question terms found in title or text) + 0.25 * (share found in the title)`, with at most 64 question terms, a 500-character title and the first 20,000 characters of text. English is lower-cased words without stop words and with a trailing plural `s` dropped; Japanese is split at common hiragana particles and cut into character bigrams (all-hiragana bigrams are skipped), and a lone kanji or katakana character is matched as a substring. Codes: `relevance_overlap`, `relevance_title_only`, `relevance_no_terms` (the question has only stop words), `relevance_no_text`. This is a cheap baseline and not semantic: it misses synonyms, cannot see negation, and keyword stuffing raises it (the score of a text that merely repeats the terms is bounded by 0.75 without a title match). A later step may replace it with an embedding or model-based score; the field stays 0..1.

`evaluate_and_store` rates a stored source and writes the three ratings with `ResearchRepository.set_evaluation`, which refuses a finished session. Page text is not stored, so the caller that read the page passes `text`. The classified source type is not stored: the repository has no setter for `source_type` yet.

## Not done in this step

- Wiring into Quick Research or an API route; sessions still always use `quick` and the v1 planner stays the default.
- Persisting `source_type` (needs a repository method) and recording the rating reasons.
- The `primary` rating and `agreement`/cross-checking (JAR-46), Standard research, conflict handling, extra search rounds.
- An LLM planner, semantic relevance, and any live search.
