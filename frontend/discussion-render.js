// DOM builders for the discussion transcript. Every server-provided string goes in through
// textContent / text nodes only; nothing here parses markup. `doc` is injected for tests.
import { PALETTE, NEUTRAL_COLOR } from "./discussion-view.js";

function make(doc, tag, className, text) {
  const node = doc.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

const colorClass = (color) => `c-${PALETTE.includes(color) ? color : NEUTRAL_COLOR}`;

export function chipNode(doc, color) {
  const chip = make(doc, "span", `chip ${colorClass(color)}`);
  chip.setAttribute("aria-hidden", "true");
  return chip;
}

// A view model from messageViewModel() -> the element to append to the transcript.
export function messageNode(doc, vm) {
  if (vm.kind === "notice") {
    const node = make(doc, "div", "notice");
    node.setAttribute("role", "note");
    node.append(make(doc, "span", "notice-meaning", `お知らせ: ${vm.meaning}`));
    if (vm.text) node.append(make(doc, "span", "notice-detail", vm.text));
    return node;
  }
  const node = make(doc, "div", `bubble ${vm.kind} ${colorClass(vm.color)}`);
  const head = make(doc, "div", "bubble-head");
  if (vm.kind === "turn") head.append(chipNode(doc, vm.color), make(doc, "span", "speaker", vm.speaker));
  else if (vm.kind === "summary") head.append(make(doc, "span", "speaker", "まとめ"));
  else if (vm.kind === "topic") head.append(make(doc, "span", "speaker", "議題"));
  else head.append(make(doc, "span", "speaker", "メッセージ"));
  node.append(head, make(doc, "p", "bubble-text", vm.text));
  if (vm.kind === "summary") node.setAttribute("role", "group");
  if (vm.kind === "summary") node.setAttribute("aria-label", "まとめ");
  return node;
}
