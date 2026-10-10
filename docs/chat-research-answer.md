# Answering a chat turn from research (opt-in)

Without this feature, a chat turn the router sends to research replies with a fixed message and a link to the Research screen; the research runs on its own ([chat.md](chat.md#research-from-chat)). With `JARVIS_CHAT_RESEARCH_ANSWER=1` the turn closes the loop: it waits for the research, then answers the user's question from what the research verified.

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `JARVIS_CHAT_RESEARCH_ANSWER` | `0` | On: wait for the started research and answer from it. Off: exactly the behavior described in chat.md. Needs the same setup as research from chat (router, research, search and chat providers). |
| `JARVIS_CHAT_RESEARCH_ANSWER_TIMEOUT_SECONDS` | `180` | How long one turn waits. Whole number, 20 to 900. |
| `JARVIS_CHAT_RESEARCH_FALLBACK_MAIN` | `0` | On: when the research gave nothing to answer from, the Main Agent answers, labelled as unsupported. Off: a fixed message. |

`/doctor` shows these as flags and numbers on the chat-to-research row.

## The turn

1. `received`, `routing`, `route_selected` (`route: research`), `researching{started}` as before. The question sent to the search service is still the user's message only.
2. The turn polls the research session and the run's progress, the same data the Research screen reads, about once a second. Each stage the run enters is reported as `researching{planning|searching|reading|verifying|writing}`. No new event kinds exist.
3. When the run completes, the stored, quote-verified claims and their sources are read. `generating` is emitted and the chat model (the request's model choice, or the default) streams the answer.
4. The answer is followed by text built by code: caveats, the source list and the Research link. Then `done`.

If the client aborts (Stop, closed tab), waiting stops and nothing is saved for the turn. The research itself keeps running in the background and stays visible on the Research screen.

## What the model sees

Only the verified claims: claim text, the source number, and for each source its title, URL, publication date and retrieval date; open conflicts between claims (kind and which claims/sources); and fixed caveat codes (`few_sources` when fewer than three sources back the claims, `open_conflicts`). Never the free-text research result, never quotes or page text, never unverified text. At most 40 claims are sent.

Claim and source text come from web pages, so it is data. It is sent as one JSON object in its own user message, labelled as data to be reported and not obeyed. The system message holds only fixed rules: answer in Japanese from the claims only, cite sources as `[n]`, say plainly what is unknown or conflicting, mention conflicts and caveats, add no facts, add no source list. The user's own message is last and separate.

## What code guarantees

- **Citations.** Every `[n]` the model writes is checked while streaming against the real source numbers. A number that does not exist is removed from the text (also when split across stream chunks).
- **Source list.** Number, title, URL and retrieval date for every source used, built from stored data, never from model text. It is appended even when the model cited nothing. Caveat lines (few sources, unresolved conflicts) are added by code too.
- **Link.** `/research#<id>` is always included.

## Fallbacks

Nothing is invented. Fixed Japanese messages, each with the Research link:

| Situation | Reply |
| --- | --- |
| Timeout | The research did not finish within the limit and is still running in the background. No model call. |
| Failed, cancelled, or completed with no verified claims | Says so with a fixed reason (for example no search results, cancelled). States that unverified things are not answered as fact. No model call. |
| Research state unreadable | A fixed "could not check the state" message. |
| Model returns no text | A fixed line plus the source list built by code. |

**Decision on model memory.** The default does not answer from the model's own knowledge when research failed. If the owner sets `JARVIS_CHAT_RESEARCH_FALLBACK_MAIN=1`, the failed, cancelled and no-claims cases (not the timeout, because the research may still finish) are answered by the Main Agent, and the reply starts with the line 「この回答は調査の裏付けがありません。」 so it cannot be mistaken for a researched answer.

Budget, busy, not-configured and refused starts are unchanged: the Main Agent answers and `route_selected` carries `research_skip`.

## Storage and memory

The final answer (or fixed message) is stored as the turn's assistant message like any other. Automatic chat memory is not run for a turn that started a research, including this one. Logs carry fixed event names and error types only; no question, claim, title, URL or key is logged or sent in events.

## Frontend

The Activity view already renders the `researching` steps. The reply, including the source list, is shown as text nodes; the only link made from a reply is `/research#<id>`, by `frontend/chat-links.js`. Source URLs and titles appear as plain text, so a hostile title cannot become markup or a link.
