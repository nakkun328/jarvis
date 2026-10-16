# Browser voice (JAR-99, phase R2 of the realtime design)

Status: **implemented in the chat page, no server change, no new vendor.** Push-to-talk input and spoken replies use the browser's own speech recognition and speech synthesis. The server only ever sees text, exactly as before. Speech-to-speech with server-issued tokens (phase R3) is not implemented; see [realtime-design.md](realtime-design.md).

## What it does

- **押している間だけ話す (🎤).** While the button is held (pointer, or Space/Enter on the keyboard), the browser recognises Japanese and puts the text in the message box. It is **never sent automatically**: the person can edit it and press 送信. Pressing the button also stops any speech (barge-in).
- **読み上げ.** A toggle (off by default, remembered in the browser). When on, the reply is spoken while it streams: `frontend/speech-chunker.js` cuts the growing text into chunks at 。！？, newlines and `. ! ?` plus a space, or at a length cap (40 characters for the first chunk, 80 later, cut at 、 or a space). A first chunk that has not completed after 400 ms is spoken anyway so the first sound is not held back by a long sentence. Markdown marks, code blocks and URLs are not read (code and links are replaced by "コード" and "リンク"); an unfinished code fence, link or URL at the end is held back until it is complete.
- **Stop and interruption.** 停止, 新しい会話, a new message, a failed turn, switching 読み上げ off or pressing 🎤 cancel in this order: the request is aborted, then the idle timer and the queue are cleared, then the speech engine is stopped. Late events from a cancelled utterance are ignored.
- **First-sound time.** The time from sending a message to the first sound actually starting is shown under the message box, with the median of the last 20 runs. The numbers (only numbers) are kept in the browser's storage under `jarvis.voice.firstSoundMs`; `summarizeLatency` computes median and p95. This is a client-side measurement of the browser engine, not of the model.

## Failure behaviour

| Situation | Result |
| --- | --- |
| No speech recognition in the browser | 🎤 stays hidden; text input only |
| No speech synthesis | 読み上げ stays hidden; replies are text |
| Microphone denied | Fixed notice; nothing is sent |
| Nothing recognised | Fixed notice; the message box is unchanged |
| Speech engine error | Fixed notice, the rest of the reply is text only |
| More than 1200 characters waiting to be spoken | Fixed notice, the rest of the reply is text only (text is never dropped) |

## Privacy

No audio is recorded, uploaded or stored by JARVIS. Depending on the browser, speech recognition may send audio to the browser vendor's service; that is the browser's behaviour and not controlled here. Transcripts are ordinary chat messages under the existing retention and never become approved memory without review ([memory.md](memory.md)).

## Tests

`frontend/test/speech-chunker.test.mjs` (sentence and length cuts, held-back tails, idle, markdown/code/URL) and `frontend/test/voice.test.mjs` (fake engine and recogniser: first chunk before the reply ends, ordering, cancel order, backpressure notice, error fallback, push-to-talk, latency log). No real microphone or speech engine was exercised; browser behaviour is **unverified** on real devices.
