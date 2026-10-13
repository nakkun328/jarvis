// Follows a running discussion: every ~2 s (while the tab is visible) it reads the task status
// and the messages after the ones already shown. It stops by itself when the task is finished
// (after one last read of the messages) and after a bounded number of polls. It only reads.
import { DiscussionApiError, loadMessages, loadTaskStatus } from "./discussion-api.js";
import { isActiveStatus } from "./discussion-view.js";

export const POLL_MS = 2000;
export const MAX_POLLS = 900; // about 30 minutes

// callbacks: onMessages(messages), onStatus(task), onFinished(task), onError(error), onGiveUp().
export function createDiscussionPoller({
  conversationId,
  taskId,
  known = 0,
  callbacks,
  api = { loadMessages, loadTaskStatus },
  schedule = (fn, ms) => setTimeout(fn, ms),
  cancel = (handle) => clearTimeout(handle),
  isHidden = () => false,
  intervalMs = POLL_MS,
  maxPolls = MAX_POLLS,
}) {
  let timer = null;
  let polls = 0;
  let stopped = false;
  let count = known;
  let running = false;

  async function readMessages() {
    const { messages, total } = await api.loadMessages(conversationId, count);
    if (stopped) return;
    count = total;
    if (messages.length > 0) callbacks.onMessages(messages);
  }

  async function tick() {
    timer = null;
    if (stopped || running) return;
    running = true;
    polls += 1;
    let finished = false;
    try {
      if (!isHidden()) {
        // Status first: when it is finished, the messages read next are complete.
        let task = null;
        try {
          task = await api.loadTaskStatus(taskId);
        } catch (error) {
          if (error instanceof DiscussionApiError && error.kind === "http" && error.status === 404) throw error;
          callbacks.onError?.(error);
        }
        if (stopped) return;
        if (task !== null) {
          callbacks.onStatus(task);
          finished = !isActiveStatus(task.status);
        }
        await readMessages();
        if (stopped) return;
        if (finished) {
          stopped = true;
          callbacks.onFinished(task);
          return;
        }
      }
    } catch (error) {
      if (stopped) return;
      callbacks.onError?.(error);
    } finally {
      running = false;
    }
    if (stopped) return;
    if (polls >= maxPolls) {
      stopped = true;
      callbacks.onGiveUp?.();
      return;
    }
    timer = schedule(() => void tick(), intervalMs);
  }

  return {
    start() {
      if (stopped || timer !== null || running) return;
      timer = schedule(() => void tick(), 0);
    },
    stop() {
      stopped = true;
      if (timer !== null) cancel(timer);
      timer = null;
    },
    // Reads right away (after the stop button, for example) instead of waiting for the timer.
    async nudge() {
      if (stopped) return;
      if (timer !== null) cancel(timer);
      timer = null;
      await tick();
    },
    get known() {
      return count;
    },
    get stopped() {
      return stopped;
    },
  };
}
