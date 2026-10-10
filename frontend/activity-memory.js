// Client of the memory activity feed (GET /api/memory/activity, docs/memory.md). Chat auto-memory
// and research staging run after the reply, so the page polls this feed for a short window after
// a reply (or a finished research) to tell the owner WHEN a memory was made.
//
// Everything here is logic: parsing the payload defensively, wording, and a poller whose timers,
// fetch and visibility check are injected so Node can test it. Summaries are the owner's own
// text; they are only ever placed with textContent (the DOM adapters do that), never as markup.
import { sessionEnded } from "./session.js";

export const FEED_URL = "/api/memory/activity";
export const POLL_INTERVAL_MS = 1500;
export const POLL_WINDOW_MS = 20000;
export const RECENT_LIMIT = 5;
export const SUMMARY_MAX = 80;
export const MEMORY_PAGE = "/memory";
export const LINK_TEXT = "記憶の画面を開く";

const KINDS = ["staged", "approved", "withdrawn"];
const ORIGINS = ["chat", "research"];
const ORIGIN_TEXT = { chat: "会話", research: "調査" };
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
// Control, format (zero-width, bidi marks and isolates), line/paragraph separators, private use.
const UNSAFE = /[\p{Cc}\p{Cf}\p{Co}\p{Cs}\p{Zl}\p{Zp}]/gu;

export function cleanSummary(value) {
  if (typeof value !== "string") return "";
  const text = value.replace(UNSAFE, " ").replace(/\s+/g, " ").trim();
  const chars = [...text];
  return chars.length <= SUMMARY_MAX ? text : `${chars.slice(0, SUMMARY_MAX - 1).join("")}…`;
}

function isSeq(value) {
  return Number.isSafeInteger(value) && value >= 0;
}

// Returns { latest, configured, events } or null. Bad events are dropped one by one; a payload
// whose `latest` is not a count is rejected whole. Unknown extra fields are ignored.
export function parseFeed(body) {
  if (body === null || typeof body !== "object" || Array.isArray(body)) return null;
  if (!isSeq(body.latest) || !Array.isArray(body.events)) return null;
  const events = [];
  for (const raw of body.events) {
    if (
      raw === null || typeof raw !== "object" || !isSeq(raw.seq) || raw.seq < 1 ||
      !KINDS.includes(raw.kind) || !ORIGINS.includes(raw.origin) ||
      typeof raw.memory_id !== "string" || !UUID.test(raw.memory_id)
    ) {
      continue;
    }
    events.push({
      seq: raw.seq,
      kind: raw.kind,
      origin: raw.origin,
      memoryId: raw.memory_id.toLowerCase(),
      summary: cleanSummary(raw.summary),
    });
  }
  events.sort((a, b) => a.seq - b.seq);
  return { latest: body.latest, configured: body.configured !== false, events };
}

export function eventLine(event) {
  const where = ORIGIN_TEXT[event.origin];
  const tail = event.summary ? `: ${event.summary}` : "";
  if (event.kind === "approved") return `記憶しました(自動承認・${where})${tail}`;
  if (event.kind === "staged") return `記憶の候補を作りました(確認待ち・${where})${tail}`;
  return `記憶を取り下げました(${where})${tail}`;
}

// One polite sentence for a batch: the newest event, with a count when there are several.
export function noticeText(events) {
  if (events.length === 0) return "";
  const line = eventLine(events[events.length - 1]);
  return events.length > 1 ? `${line}(ほか ${events.length - 1} 件)` : line;
}

// The last RECENT_LIMIT events, newest first.
export function mergeRecent(recent, events) {
  const seen = new Set();
  const merged = [];
  for (const event of [...events].reverse().concat(recent)) {
    if (seen.has(event.seq)) continue;
    seen.add(event.seq);
    merged.push(event);
  }
  return merged.slice(0, RECENT_LIMIT);
}

// The /memory link, opened in a new tab.
export function memoryLink(doc) {
  const link = doc.createElement("a");
  link.setAttribute("href", MEMORY_PAGE);
  link.setAttribute("target", "_blank");
  link.setAttribute("rel", "noopener noreferrer");
  link.textContent = LINK_TEXT;
  return link;
}

// env: { fetchImpl, setTimeout, clearTimeout, now, isHidden }. Callbacks:
//   onConfig(configured)            the first time the server says whether memory is configured
//   onEvents(events, { silent })    new events (silent: the page-load backlog, not "just made")
// `filter(event)` limits which events are reported (the research screen wants research only).
export function createMemoryPoller({
  onEvents,
  onConfig = () => {},
  filter = () => true,
  intervalMs = POLL_INTERVAL_MS,
  windowMs = POLL_WINDOW_MS,
  fetchImpl = globalThis.fetch?.bind(globalThis),
  setTimeout: schedule = globalThis.setTimeout.bind(globalThis),
  clearTimeout: cancel = globalThis.clearTimeout.bind(globalThis),
  now = Date.now,
  isHidden = () => false,
} = {}) {
  let cursor = null;
  let timer = null;
  let runId = 0;
  let deadline = 0;

  async function fetchFeed() {
    if (!fetchImpl) return null;
    try {
      const response = await fetchImpl(`${FEED_URL}?after=${cursor ?? 0}`, {
        headers: { Accept: "application/json" },
        cache: "no-store",
      });
      if (response.status === 401) {
        sessionEnded();
        return null;
      }
      if (!response.ok) return null;
      return parseFeed(await response.json());
    } catch {
      return null; // network, abort or malformed JSON: try again on the next tick
    }
  }

  // Reads the feed once. With no cursor yet, this is the baseline: nothing in it is "new".
  async function pull() {
    const baseline = cursor === null;
    const feed = await fetchFeed();
    if (feed === null) return null;
    onConfig(feed.configured);
    // The server restarted (its counter went back): follow it instead of waiting forever.
    if (cursor !== null && feed.latest < cursor) cursor = feed.latest;
    const events = feed.events.filter(filter);
    if (feed.events.length > 0) cursor = Math.max(cursor ?? 0, ...feed.events.map((e) => e.seq));
    cursor = Math.max(cursor ?? 0, feed.latest);
    if (events.length > 0) onEvents(events, { silent: baseline });
    return feed;
  }

  function stop() {
    runId += 1;
    if (timer !== null) cancel(timer);
    timer = null;
  }

  return {
    // Page load: remember where the feed is, and show the backlog quietly.
    async baseline() {
      cursor = null;
      await pull();
    },
    // Moves the cursor to "now" without reporting anything (before a turn or a research starts).
    async mark() {
      const feed = await fetchFeed();
      if (feed !== null) {
        cursor = feed.latest;
        onConfig(feed.configured);
      }
    },
    // Polls every intervalMs for up to windowMs; stops early when the tab is hidden or stop()
    // is called (a new turn started). A new watch() replaces the running one.
    watch() {
      stop();
      const mine = runId;
      deadline = now() + windowMs;
      const tick = async () => {
        timer = null;
        if (mine !== runId) return;
        if (isHidden()) return;
        await pull();
        if (mine !== runId || isHidden() || now() >= deadline) return;
        timer = schedule(tick, intervalMs);
      };
      timer = schedule(tick, intervalMs);
    },
    stop,
    get cursor() {
      return cursor;
    },
    get watching() {
      return timer !== null;
    },
  };
}
