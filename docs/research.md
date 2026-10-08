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

The repository offers no physical deletion. Evaluation scoring is a later step. The Quick Research pipeline that fills these tables is described in [research-quick.md](research-quick.md).

Level selection, the deterministic query planner, source type classification and the authority, freshness and relevance ratings (R2, not yet wired into Quick Research) are described in [research-quality.md](research-quality.md).

## Research screen (read-only)

`GET /research` serves a read-only screen over stored research sessions, backed by two read-only endpoints in `backend/api/research.py` (`create_research_router(ResearchRepository(database))`):

- `GET /api/research/sessions` lists sessions (`limit` 1 to 100, default 50; optional `status`). Rows are summaries: id, question, level, status, failure reason code, whether a result exists, and timestamps. The repository returns the oldest sessions first and has no paging, so a list that hits the limit covers only the oldest sessions; the screen says so and asks the server for the chosen status instead.
- `GET /api/research/sessions/{id}` returns one session with its result text, queries, sources (title, URL, final URL, publisher, source type, the five ratings, dates) and claims. Each claim carries its stored quote, optional offsets, and the id of the source it cites. The content digest is not exposed.

Responses are built from explicit field allowlists. An unknown or malformed id is `404 session_not_found`; a bad `limit` or `status` is `422 invalid_limit` or `422 invalid_status`; a storage failure is `503 storage_unavailable`. Error bodies are fixed codes and never repeat the request or stored text. There are no write routes, and the endpoints never search, fetch pages, or call a model.

The screen is plain HTML and JavaScript (`frontend/research.html`, `research.js`, `research-api.js`, `research-view.js`, `research.css`). It polls every 5 seconds (with backoff on errors), refreshes a running session's detail, and stops re-reading a session once it is finished. All stored text is rendered as text nodes. A source URL becomes a link only for `http` and `https`, with `rel="noopener noreferrer"` and `target="_blank"`; anything else is plain text. A banner states that the page is local and read-only, that a citation only means the quote was found verbatim in the stored source text (not that the claim is true), and that the 0 to 1 ratings are heuristic.

Nothing creates research sessions yet: no search provider is decided and no pipeline is wired to the application, so a fresh database shows an empty list. To look at the screen with fake data, run `python scripts/dev_research_demo_server.py` (loopback only, port 18921 by default, a temporary SQLite file seeded through `ResearchRepository`, never your own database or `.env`) and open the printed URL. It includes sessions whose text contains HTML and script-like strings to show that they render inertly. Stop it with Ctrl-C or SIGTERM; the temporary database is removed.
