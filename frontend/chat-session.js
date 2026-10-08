import { ChatError } from "./chat-api.js";

// Mirrors the backend limit, which counts Unicode code points.
export const MAX_MESSAGE_LENGTH = 4000;

const NETWORK_MESSAGE = "サーバーに接続できませんでした。接続を確認して再試行してください。";
const ABORT_MESSAGE = "応答を停止しました。この応答は会話に保存されていません。";
const EMPTY_REPLY_MESSAGE = "応答が空でした。会話には保存されていません。再試行してください。";

export function messageLength(text) {
  return Array.from(text).length;
}

// UI-independent chat state: one request at a time, conversation id handling, abort and retry.
// `view` is the DOM adapter (see app.js); tests drive this with a fake view and a fake `send`.
//
// Rules this class enforces:
//  - only a completed (`done`) reply sets or keeps the conversation id and is shown as saved;
//  - text received before a failure/stop is handed to the view as an unsaved partial;
//  - a missing/expired conversation id is surfaced as an error, never replaced by a new one.
export class ChatSession {
  conversationId = null;
  busy = false;
  #send;
  #view;
  #controller = null;
  #retry = null;

  constructor({ send, view }) {
    this.#send = send;
    this.#view = view;
  }

  submit(raw) {
    if (this.busy) {
      this.#view.setStatus("応答中です。完了するか「停止」してから送信してください。");
      return "busy";
    }
    const text = raw.trim();
    if (!text) {
      this.#view.setStatus("メッセージを入力してください。", true);
      return "empty";
    }
    if (messageLength(text) > MAX_MESSAGE_LENGTH) {
      this.#view.setStatus(`メッセージは ${MAX_MESSAGE_LENGTH.toLocaleString("en-US")} 文字以内にしてください。`, true);
      return "too-long";
    }
    this.#view.clearInput();
    const turn = this.#view.beginTurn(text);
    void this.#run(text, turn);
    return "sent";
  }

  retry() {
    if (this.busy || !this.#retry) return false;
    const { text, turn } = this.#retry;
    turn.reset();
    void this.#run(text, turn);
    return true;
  }

  stop() {
    if (!this.busy) return false;
    this.#controller?.abort();
    return true;
  }

  // Starts a fresh conversation. Refused while a request is in flight.
  reset() {
    if (this.busy) return false;
    this.conversationId = null;
    this.#dropRetry();
    return true;
  }

  #dropRetry() {
    this.#retry?.turn.clearRetry();
    this.#retry = null;
  }

  async #run(text, turn) {
    this.#dropRetry();
    const controller = new AbortController();
    this.#controller = controller;
    this.busy = true;
    this.#view.setBusy(true);
    this.#view.setStatus("JARVIS が応答しています…");
    let reply = "";
    try {
      const result = await this.#send({
        message: text,
        conversationId: this.conversationId,
        signal: controller.signal,
        onDelta(delta) {
          reply += delta;
          turn.showText(reply);
        },
      });
      if (!reply.trim()) {
        this.#fail(text, turn, reply, EMPTY_REPLY_MESSAGE, "empty", true);
        return;
      }
      this.conversationId = result.conversation_id;
      turn.complete(result);
      this.#view.setStatus("");
    } catch (error) {
      if (controller.signal.aborted) {
        this.#fail(text, turn, reply, ABORT_MESSAGE, "aborted", true);
        this.#view.setStatus("停止しました。");
      } else if (error instanceof ChatError) {
        this.#fail(text, turn, reply, error.message, error.kind, error.retryable);
      } else {
        this.#fail(text, turn, reply, NETWORK_MESSAGE, "network", true);
      }
    } finally {
      this.#controller = null;
      this.busy = false;
      this.#view.setBusy(false);
    }
  }

  #fail(text, turn, partial, message, kind, retryable) {
    turn.fail({ message, partial, kind, retryable });
    if (retryable) this.#retry = { text, turn };
    if (kind !== "aborted") this.#view.setStatus("応答を完了できませんでした。", true);
  }
}
