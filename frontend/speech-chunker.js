// Turns a growing reply into speakable chunks (docs/voice.md). Text is the master stream: this
// only decides what to say and when; it never changes what is shown or stored.
//
// A chunk ends at a sentence end (。！？, a newline, or . ! ? followed by a space), or at the
// length cap (cut at 、 , or a space when there is one). The first chunk uses a lower cap so the
// first sound starts sooner. Markdown, code and URLs are not read aloud.

export const FIRST_CHUNK_MAX = 40;
export const CHUNK_MAX = 80;

const CODE_WORD = "コード";
const LINK_WORD = "リンク";

// The speakable form of `raw`. While the reply is still growing (`final` false), an unfinished
// tail (open code fence, half a link, a URL that may continue) is held back instead of spoken.
export function speakable(raw, final = false) {
  let text = String(raw);
  text = text.replace(/```[\s\S]*?```/g, ` ${CODE_WORD} `);
  const open = text.indexOf("```");
  if (open !== -1) text = text.slice(0, open);
  text = text.replace(/`[^`\n]*`/g, CODE_WORD);
  if (!final) text = text.replace(/`[^`\n]*$/, "");
  text = text.replace(/\[([^\]\n]*)\]\([^)\s]*\)/g, "$1");
  if (!final) text = text.replace(/\[[^\]\n]*(?:\]\([^)\s]*)?$/, "");
  if (!final) text = text.replace(/https?:\/\/\S*$/, "");
  text = text.replace(/https?:\/\/\S+/g, LINK_WORD);
  text = text.replace(/^[ \t]{0,3}(?:#{1,6}|>|[-*+]|\d+\.)[ \t]+/gm, "");
  text = text.replace(/\*+|~~/g, "");
  return text;
}

function cutPoint(text, cap) {
  const head = text.slice(0, cap);
  const at = Math.max(
    head.lastIndexOf("、"),
    head.lastIndexOf("，"),
    head.lastIndexOf(","),
    head.lastIndexOf(" "),
  );
  return at >= cap / 2 ? at + 1 : cap;
}

function sentenceEnd(text, final) {
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    if (ch === "。" || ch === "！" || ch === "？" || ch === "\n") return i + 1;
    if (ch === "." || ch === "!" || ch === "?") {
      const next = text[i + 1];
      if (next === undefined) {
        if (final) return i + 1;
      } else if (/\s/.test(next)) {
        return i + 1;
      }
    }
  }
  return -1;
}

export class SpeechChunker {
  #consumed = 0;
  #emitted = 0;

  get emitted() {
    return this.#emitted;
  }

  // New chunks available in `full` (the whole reply so far).
  push(full) {
    return this.#take(speakable(full, false), false);
  }

  // The reply is complete: everything that is left.
  flush(full) {
    return this.#take(speakable(full, true), true);
  }

  // The first chunk is taking too long to complete: speak what there is. Only before any chunk
  // has been emitted; later chunks wait for a natural boundary.
  idle(full) {
    if (this.#emitted > 0) return [];
    const rest = speakable(full, false).slice(this.#consumed);
    const chunk = rest.trim();
    if (!chunk) return [];
    this.#consumed += rest.length;
    this.#emitted += 1;
    return [chunk];
  }

  #take(text, final) {
    const chunks = [];
    for (;;) {
      const rest = text.slice(this.#consumed);
      if (!rest) break;
      const cap = this.#emitted === 0 && chunks.length === 0 ? FIRST_CHUNK_MAX : CHUNK_MAX;
      let end = sentenceEnd(rest, final);
      if (end === -1) {
        if (rest.length >= cap) end = cutPoint(rest, cap);
        else if (final) end = rest.length;
        else break;
      } else if (end > cap) {
        end = cutPoint(rest, cap);
      }
      const chunk = rest.slice(0, end).trim();
      this.#consumed += end;
      if (!chunk) continue;
      chunks.push(chunk);
      this.#emitted += 1;
    }
    return chunks;
  }
}
