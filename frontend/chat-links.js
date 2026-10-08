// The one link a chat reply may contain: the Research screen's own address for a session
// (`/research#<id>`), which the fixed "research started" reply carries. Nothing else in a reply
// becomes a link, so reply text (which can be model output) stays text. Pure and DOM-free: the
// page builds the nodes from the segments with the DOM API; no markup is ever parsed.

// The path must stand alone: not the tail of a longer URL or word, and not followed by more id.
export const RESEARCH_LINK =
  /(?<![\w./:@%-])\/research#[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![\w-])/g;

// Splits `text` into [{ type: "text", text }, { type: "link", text, href }], in order. The
// concatenation of the `text` fields is always the original text. A link's href is the matched
// relative path itself (same origin by construction).
export function replySegments(text) {
  const source = typeof text === "string" ? text : "";
  const segments = [];
  let last = 0;
  for (const match of source.matchAll(RESEARCH_LINK)) {
    if (match.index > last) segments.push({ type: "text", text: source.slice(last, match.index) });
    segments.push({ type: "link", text: match[0], href: match[0] });
    last = match.index + match[0].length;
  }
  if (last < source.length) segments.push({ type: "text", text: source.slice(last) });
  return segments;
}

// Replaces the children of `container` with the reply's text nodes and, for the research path,
// anchors. Text goes in as text nodes only.
export function renderReply(doc, container, text) {
  const nodes = replySegments(text).map((segment) => {
    if (segment.type === "text") return doc.createTextNode(segment.text);
    const link = doc.createElement("a");
    link.className = "reply-link";
    link.setAttribute("href", segment.href);
    link.textContent = segment.text;
    return link;
  });
  container.replaceChildren(...nodes);
}
