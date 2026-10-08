# Research design

The research engine is planned for Phase 3. It will select a level from memory only, quick search, standard research, deep research, or extensive research. A user-specified level takes priority.

The workflow is query planning, search, source collection, evaluation, reading, cross checking, conflict detection, additional search when needed, synthesis, citations, and reusable research memory. Sources may include web search, official documentation, academic sources, forums, user files, prior research, and connected personal services as those connectors become available.

Source records should include title, publisher or source, URL, publication date when known, and retrieval date. Claims should retain links to their supporting sources. Evaluation considers authority, freshness, primary versus secondary status, relevance, and agreement. When reliable sources disagree, the response should describe the disagreement and the limits of verification.

Stored findings require freshness checks before reuse; the interval depends on how quickly the subject changes.

## Data model (R1)

Research state is stored in SQLite (schema version 6, applied by the existing migration path; v5 data is kept and a database from a newer schema is refused). The code lives in `backend/research/models.py` (frozen dataclasses and enums) and `backend/research/repository.py` (`ResearchRepository`).

- `research_sessions`: question, level (`memory`, `quick`, `standard`, `deep`, `extensive`; default `quick`), status, result text, and a failure reason. Timestamps are UTC ISO strings and IDs are UUID text.
- `research_queries`: the search queries of a session, in order (`id`, `session_id`, `text`, `position`, `created_at`).
- `research_sources`: URL, final URL, title, publisher, publication date, retrieval time, SHA-256 digest of the content read, source type, and five independent nullable ratings (authority, freshness, primary, relevance, agreement; each 0 to 1). The same final URL with the same digest is stored once per session; a repeat returns the existing row. Page text is not stored.
- `research_claims`: a claim, the supporting source, a quote of at most 500 characters, and optional quote offsets. The database requires the source to belong to the same session, so a citation cannot point across sessions.

Status transitions are compare-and-swap with an allowed-transition table: `pending` to `running` or `cancelled`; `running` to `waiting`, `completed`, `failed`, or `cancelled`; `waiting` to `running`, `cancelled`, or `failed`. `failed`, `completed`, and `cancelled` are final: the session, its queries, sources, and claims no longer change. Completion happens only through `set_result` on a running session, which stores the bounded result text. A failure stores one of a fixed set of short codes (`search_failed`, `no_results`, `reader_failed`, `synthesis_failed`, `timeout`, `budget_exceeded`, `internal_error`), never an upstream message.

The repository offers no physical deletion. Evaluation scoring and the research pipeline that fills these tables are later steps.
