# Research results to memory (JAR-54, JAR-55)

Research output never becomes approved memory by itself. A person can turn the verified claims of
a finished research session into **pending memory candidates** with one button; the normal memory
review then decides.

## Flow

1. On the `/research` detail of a completed session that has at least one stored claim, the
   section 「記憶の候補」 shows the button 「記憶の候補にする」.
2. Pressing it sends `POST /api/research/sessions/{id}/memory-candidates`.
3. One candidate per verified claim (at most 5 per session; the response reports `omitted`) is
   stored through the existing candidate repository with status `pending`.
4. The candidates appear on the `/memory` review screen with origin 「調査の引用」. They are
   approved or rejected exactly like any other candidate (review CLI / writer).
5. Only on approval does the existing memory writer create the Obsidian note, when a vault is
   configured. There is no second vault write path: staging never touches the vault, and the
   writer never overwrites or deletes an existing note.

## API

- `POST /api/research/sessions/{id}/memory-candidates` stages candidates. It needs a signed-in
  session (when login is on), a same-origin request and the header `X-Jarvis-Confirm: 1`, as the
  approvals endpoints do. 201 when something was created, 200 when everything already existed.
- `GET /api/research/sessions/{id}/memory-candidates` lists the candidates already staged for the
  session (`eligible` is false for a session that is not completed or has no verified claim).

Response: `{ "eligible": bool, "created": n, "omitted": n, "candidates": [ ...memory DTOs ] }`.

Refusals use fixed `detail` codes: `session_not_found` (404), `session_not_completed` (409: running,
failed or cancelled), `no_verified_claims` (409), `forbidden` / `confirm_header_required` (403),
`storage_unavailable` (503).

## What is stored

- Only stored claims are used. The free-text result and any unverified text are never read.
  A claim that is part of a still-open conflict is not offered.
- `origin` is `research`, `category` is `project`, `source` is `research:<session id>`, tag
  `research`, importance 0.5, confidence 0.6. Status is always `pending`.
- The content holds the claim, the verbatim quote (引用), the source URL (出典, with the title),
  the retrieval date (取得日) and the research session id (調査ID). The approved note body and
  front matter carry the same text and `origin: "research"` / `source`.

## Idempotency

The candidate id is derived from the claim id, so a second request for the same session creates
nothing and returns the existing candidates (including ones already approved or rejected; a
rejected candidate is not recreated).

## Guarantees and tests

Only the API handler calls the staging code; no chat, router, tool or model path does. The
tests in `tests/test_research_memory_candidates.py` cover the happy path, idempotency, refusals,
header/origin/login checks, provenance, no vault write before approval, and a source scan that
fails if other modules reference the staging code.
