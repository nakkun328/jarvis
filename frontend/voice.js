// Browser voice for the chat (docs/voice.md): push-to-talk input and spoken replies.
//
// Text stays the master stream. Speech is derived from it chunk by chunk, so a stop, a new
// message or an error cancels it in one order: (1) the caller aborts the request, (2) cancel()
// clears the idle timer and the queue, (3) the speech engine is stopped. No audio is recorded or
// stored anywhere; the recognised text is only put into the message box for the person to edit
// and send. Everything degrades to plain text when the browser has no recogniser or voice.

import { SpeechChunker } from "./speech-chunker.js";

export const IDLE_MS = 400;
// More than this much queued speech is not read: the rest of the reply is shown as text only,
// and the person is told. Text is never dropped.
export const MAX_QUEUED_CHARS = 1200;
export const LATENCY_KEY = "jarvis.voice.firstSoundMs";
export const LATENCY_KEEP = 20;

export const NOTICES = {
  noListen: "このブラウザは音声入力に対応していません。文字で入力してください。",
  micDenied: "マイクが許可されていません。ブラウザの設定を確認してください。",
  noSpeech: "音声を認識できませんでした。もう一度話すか、文字で入力してください。",
  noSpeak: "このブラウザは読み上げに対応していません。文字だけで表示します。",
  speakFailed: "読み上げに失敗したため、残りは文字だけで表示します。",
  behind: "読み上げが追いつかないため、残りは文字だけで表示します。",
};

export function supportOf(env = globalThis) {
  return {
    listen: Boolean(env.SpeechRecognition || env.webkitSpeechRecognition),
    speak: Boolean(env.speechSynthesis && env.SpeechSynthesisUtterance),
  };
}

// First-sound times, newest last, kept in the browser's storage (numbers only, no content).
export function recordLatency(storage, ms) {
  if (!Number.isFinite(ms) || ms < 0) return [];
  let list = [];
  try {
    const parsed = JSON.parse(storage?.getItem(LATENCY_KEY) ?? "[]");
    if (Array.isArray(parsed)) list = parsed.filter((n) => Number.isFinite(n));
  } catch {
    list = [];
  }
  list = [...list, Math.round(ms)].slice(-LATENCY_KEEP);
  try {
    storage?.setItem(LATENCY_KEY, JSON.stringify(list));
  } catch {
    // Storage may be full or blocked; the measurement is then only shown, not kept.
  }
  return list;
}

export function summarizeLatency(list) {
  if (!list.length) return null;
  const sorted = [...list].sort((a, b) => a - b);
  const at = (q) => sorted[Math.min(sorted.length - 1, Math.ceil(q * sorted.length) - 1)];
  return { count: sorted.length, median: at(0.5), p95: at(0.95) };
}

export function createVoice({
  env = globalThis,
  now = () => Date.now(),
  setTimer = (fn, ms) => setTimeout(fn, ms),
  clearTimer = (id) => clearTimeout(id),
  onNotice = () => {},
  onFirstSound = () => {},
  onListening = () => {},
} = {}) {
  const support = supportOf(env);
  let speaking = false;
  let chunker = new SpeechChunker();
  let queue = [];
  let queuedChars = 0;
  let playing = false;
  let textOnly = false;
  let started = 0;
  let measured = false;
  let idleTimer = null;
  let lastFull = "";
  let recognition = null;
  let generation = 0;

  function stopIdle() {
    if (idleTimer !== null) clearTimer(idleTimer);
    idleTimer = null;
  }

  function degrade(notice) {
    if (!textOnly) onNotice(notice);
    textOnly = true;
    queue = [];
    queuedChars = 0;
  }

  function playNext(token) {
    if (token !== generation || playing || !queue.length) return;
    const text = queue.shift();
    queuedChars -= text.length;
    let utterance;
    try {
      utterance = new env.SpeechSynthesisUtterance(text);
      utterance.lang = "ja-JP";
    } catch {
      degrade(NOTICES.speakFailed);
      return;
    }
    playing = true;
    utterance.onstart = () => {
      if (token !== generation || measured) return;
      measured = true;
      onFirstSound(now() - started);
    };
    utterance.onend = () => {
      if (token !== generation) return;
      playing = false;
      playNext(token);
    };
    utterance.onerror = () => {
      if (token !== generation) return;
      playing = false;
      degrade(NOTICES.speakFailed);
    };
    try {
      env.speechSynthesis.speak(utterance);
    } catch {
      playing = false;
      degrade(NOTICES.speakFailed);
    }
  }

  function enqueue(chunks) {
    for (const chunk of chunks) {
      if (textOnly) return;
      if (queuedChars + chunk.length > MAX_QUEUED_CHARS) {
        degrade(NOTICES.behind);
        return;
      }
      queue.push(chunk);
      queuedChars += chunk.length;
    }
    playNext(generation);
  }

  const api = {
    support,
    get speaking() {
      return speaking;
    },
    setSpeaking(value) {
      const next = Boolean(value);
      if (next && !support.speak) {
        onNotice(NOTICES.noSpeak);
        return false;
      }
      speaking = next;
      if (!speaking) api.cancel();
      return speaking;
    },
    // A new turn is about to start: measure from here to the first sound.
    beginTurn() {
      api.cancel();
      started = now();
      measured = false;
    },
    // The reply has grown to `full`. Speaks every complete chunk; a half-finished first chunk is
    // spoken after IDLE_MS so the first sound is not held back by a long sentence.
    feed(full) {
      if (!speaking || textOnly) return;
      lastFull = full;
      enqueue(chunker.push(full));
      stopIdle();
      if (chunker.emitted === 0) {
        const token = generation;
        idleTimer = setTimer(() => {
          idleTimer = null;
          if (token === generation && speaking && !textOnly) enqueue(chunker.idle(lastFull));
        }, IDLE_MS);
      }
    },
    // The reply is complete.
    finish(full) {
      if (!speaking || textOnly) return;
      stopIdle();
      enqueue(chunker.flush(full));
    },
    // Stop speaking and drop what has not been spoken. Safe to call at any time.
    cancel() {
      generation += 1;
      stopIdle();
      queue = [];
      queuedChars = 0;
      playing = false;
      textOnly = false;
      chunker = new SpeechChunker();
      lastFull = "";
      if (support.speak) {
        try {
          env.speechSynthesis.cancel();
        } catch {
          // Nothing left to stop.
        }
      }
    },
    // Push-to-talk: begin recognising. Playback is cancelled first (button down = barge-in).
    // `onText(text, final)` receives the recognised text for the message box; it is never sent
    // automatically.
    startListening(onText) {
      if (!support.listen) {
        onNotice(NOTICES.noListen);
        return false;
      }
      if (recognition) return true;
      api.cancel();
      const Recognition = env.SpeechRecognition || env.webkitSpeechRecognition;
      let heard = false;
      let current;
      try {
        current = new Recognition();
        current.lang = "ja-JP";
        current.interimResults = true;
        current.continuous = false;
      } catch {
        onNotice(NOTICES.noListen);
        return false;
      }
      current.onresult = (event) => {
        let text = "";
        let final = true;
        for (const result of Array.from(event.results ?? [])) {
          text += result[0]?.transcript ?? "";
          if (!result.isFinal) final = false;
        }
        if (text.trim()) heard = true;
        onText(text, final);
      };
      current.onerror = (event) => {
        const code = event?.error;
        if (code === "not-allowed" || code === "service-not-allowed") onNotice(NOTICES.micDenied);
        else if (code !== "aborted") onNotice(NOTICES.noSpeech);
        heard = true;
      };
      current.onend = () => {
        if (recognition === current) recognition = null;
        if (!heard) onNotice(NOTICES.noSpeech);
        onListening(false);
      };
      recognition = current;
      try {
        current.start();
      } catch {
        recognition = null;
        onNotice(NOTICES.noListen);
        return false;
      }
      onListening(true);
      return true;
    },
    stopListening() {
      try {
        recognition?.stop();
      } catch {
        recognition = null;
      }
    },
    get listening() {
      return recognition !== null;
    },
  };
  return api;
}
