# Past research reuse and freshness

Before a research searches, the run looks for an earlier research it can reuse
(`backend/research/reuse.py`). The decision is deterministic: no network, no model, no
embeddings. It is recorded on the session as one fixed code and shown in the Research screen.

## Candidates

A prior session is a candidate only when all of these hold:

- it is `completed` and has at least one stored claim. A claim is stored only after its quote
  was checked against the page text, so a result that only says "no claim could be verified"
  has no claims and is never reused. Failed, cancelled and unfinished sessions never qualify;
- its level is not lower than the requested level (a Standard result serves a Quick request,
  not the other way round);
- its question is similar: after NFKC folding, lower-casing and dropping everything but
  letters and digits, the questions are equal, or their character-bigram Jaccard similarity is
  at least 0.8 (a proposal, not a measured value).

The best candidate is the most similar one, newest first on a tie. Only the newest 200
completed sessions are examined.

## Freshness policy

The topic class comes from a fixed word list (`TOPIC_VOCABULARY`: price, latest, news, today,
weather, ... and Japanese equivalents). The list is applied to the new question and to the
prior question.

| Class | Maximum age of the prior result |
| --- | --- |
| `time_sensitive` | 0: never reused |
| `stable` (everything else) | 30 days (`DEFAULT_MAX_AGE_DAYS`, a proposal) |

The age is that of the prior result's oldest source (`retrieved_at`): a result is only as
fresh as its stalest citation. A date in the future counts as stale.

## Reason codes

Stored in `research_sessions.reuse_reason` (schema v10, checked by the database):

| Code | Meaning | Searches? | Screen text |
| --- | --- | --- | --- |
| `reused_fresh` | A stable, young, verified prior result was reused. | no | 過去の調査を再利用 |
| `prior_stale` | A candidate exists but is older than the maximum age. | yes | 古いので再検索 |
| `time_sensitive_topic` | A candidate exists but the topic needs current data. | yes | 古いので再検索 |
| `no_prior_research` | No candidate. | yes | (nothing shown) |

`reuse_of` holds the prior session and `reuse_prior_at` the retrieval date of its oldest
source. For `prior_stale` and `time_sensitive_topic` the prior session is only the "previous
result" context: the new run searches, verifies and answers on its own, and the earlier answer
is never presented as fresh. The screen shows the previous date next to the label.

## What a reused answer contains

On `reused_fresh` the run copies the prior sources and verified claims into the new session
with their original `retrieved_at`, ratings, quotes and offsets, and stores the prior result
text unchanged. No query, search or page fetch happens and nothing is re-verified; the shown
date is the original retrieval date, not the date of the request. The task completes through
the normal verifier (it requires the copied claims).

## API and screen

`GET /api/research/sessions` and the detail route add `reuse`: `null` for sessions without a
decision, otherwise `{reason, previous_session_id, prior_at}`. The screen
(`frontend/research-view.js`, `reuseInfo`) maps only the three codes above to Japanese text
and renders it with `textContent`; an unknown code shows nothing.

## Limits

- Similarity is lexical; paraphrases that share few characters are not matched.
- The word list and the 30-day limit are proposals and are not configurable at run time.
- A reused answer is only as correct as the earlier run; its sources are not re-fetched.
