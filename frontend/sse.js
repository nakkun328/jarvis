// Reads SSE fields even when UTF-8 characters or line endings span network chunks.
export async function readEventStream(stream, onEvent) {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let line = "";
  let event = "message";
  let data = [];
  let skipLF = false;
  let completed = false;

  function dispatch() {
    if (data.length) onEvent({ event, data: data.join("\n") });
    event = "message";
    data = [];
  }

  function finishLine() {
    if (!line) {
      dispatch();
    } else if (!line.startsWith(":")) {
      const colon = line.indexOf(":");
      const field = colon < 0 ? line : line.slice(0, colon);
      let value = colon < 0 ? "" : line.slice(colon + 1);
      if (value.startsWith(" ")) value = value.slice(1);
      if (field === "event") event = value;
      if (field === "data") data.push(value);
    }
    line = "";
  }

  function feed(text) {
    for (const char of text) {
      if (skipLF) {
        skipLF = false;
        if (char === "\n") continue;
      }
      if (char === "\r") {
        finishLine();
        skipLF = true;
      } else if (char === "\n") {
        finishLine();
      } else {
        line += char;
      }
    }
  }

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      feed(decoder.decode(value, { stream: true }));
    }
    completed = true;
    feed(decoder.decode());
    if (line) finishLine();
    dispatch();
  } finally {
    if (!completed) await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
