// Picks up what a background tool task posts into the open conversation, without a reload.
// It asks the read-only messages endpoint for messages after the ones this page already knows
// (`?after=<count>`), every few seconds while the tab is visible, and stops once something new
// arrived or after a bounded number of polls. It only reads; text goes in as text nodes.
import { sessionEnded } from "./session.js";

export const POLL_MS = 3000;
export const MAX_POLLS = 400; // about 20 minutes

export function afterUrl(id, after) {
  return `/api/chat/conversations/${id}/messages?after=${after}`;
}

// env: { fetchImpl, setTimeout, clearTimeout, isHidden }. onMessages(messages) gets the new
// assistant messages ({ role, content }).
export function createMessagePoller({
  onMessages,
  fetchImpl = (url, init) => fetch(url, init),
  schedule = (fn, ms) => setTimeout(fn, ms),
  cancel = (handle) => clearTimeout(handle),
  isHidden = () => false,
  intervalMs = POLL_MS,
  maxPolls = MAX_POLLS,
}) {
  let conversationId = null;
  let known = null; // how many messages the server holds, as far as this page knows
  let timer = null;
  let polls = 0;
  let generation = 0;
  let tasksPending = false;

  function stopTimer() {
    generation += 1;
    if (timer !== null) cancel(timer);
    timer = null;
  }

  async function tick(mine) {
    timer = null;
    if (mine !== generation || conversationId === null || known === null) return;
    polls += 1;
    if (!isHidden()) {
      try {
        const response = await fetchImpl(afterUrl(conversationId, known), {
          headers: { Accept: "application/json" },
        });
        if (mine !== generation) return;
        if (response.status === 401) {
          sessionEnded();
          return;
        }
        if (response.ok) {
          const data = await response.json();
          const fresh = Array.isArray(data?.messages)
            ? data.messages.filter(
                (m) => (m?.role === "assistant" || m?.role === "user") && typeof m.content === "string",
              )
            : [];
          if (mine !== generation) return;
          if (fresh.length > 0) {
            known += fresh.length;
            tasksPending = false;
            onMessages(fresh.map(({ role, content }) => ({ role, content })));
            return; // something arrived; the next tool task starts a new watch
          }
        }
      } catch {
        // A failed poll is simply tried again.
      }
    }
    if (polls >= maxPolls) return;
    timer = schedule(() => void tick(mine), intervalMs);
  }

  function start() {
    stopTimer();
    if (conversationId === null || known === null) return;
    polls = 0;
    const mine = generation;
    timer = schedule(() => void tick(mine), intervalMs);
  }

  return {
    // The conversation and how many messages it holds now (from the restore response, or 0).
    sync(id, total) {
      stopTimer();
      conversationId = id;
      known = Number.isInteger(total) ? total : null;
    },
    // A turn of this page finished: it stored its own two messages.
    turnDone(id) {
      if (conversationId === null) conversationId = id;
      if (known === null) known = 0;
      known += 2;
    },
    // The model asked for a tool in the last turn: a task will post into the conversation.
    noteTask() {
      tasksPending = true;
    },
    // Starts polling when a task is outstanding (after the turn, never during it).
    watch() {
      if (tasksPending) start();
    },
    // Light poll for a page restored while a task may still be running.
    watchAlways() {
      tasksPending = true;
      start();
    },
    // A turn is starting: it would see its own messages, so hold still.
    stop() {
      stopTimer();
    },
    get known() {
      return known;
    },
  };
}

// Whether an `activity` SSE payload (string or object) says the model asked for a tool.
export function isToolRequested(data) {
  try {
    const payload = typeof data === "string" ? JSON.parse(data) : data;
    return payload?.stage === "tool_requested";
  } catch {
    return false;
  }
}
