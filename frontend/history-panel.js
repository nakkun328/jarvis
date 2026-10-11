// The 履歴 panel: a modal list of past conversations. Built with text nodes and DOM calls only
// (titles come from stored messages and are untrusted). Keyboard: Escape closes, Tab stays in the
// panel, the arrow keys move between conversations, Enter/Space choose one (native buttons).
import { formatRelative } from "./chat-history.js";

export const STATE_LOADING = "読み込み中…";
export const STATE_EMPTY = "保存された会話はまだありません。";

// `els`: { trigger, backdrop, list, state, close, more }. `handlers`: { onOpen, onSelect, onMore }.
export function createHistoryPanel(doc, els, handlers, now = () => Date.now()) {
  const { trigger, backdrop, list, state, close, more } = els;
  let itemButtons = [];
  let opened = false;

  function focusables() {
    const items = [close, ...itemButtons];
    if (!more.hidden && !more.disabled) items.push(more);
    return items.filter((element) => !element.disabled);
  }

  function open() {
    if (opened) return;
    opened = true;
    backdrop.hidden = false;
    trigger.setAttribute("aria-expanded", "true");
    handlers.onOpen();
    close.focus();
  }

  function closePanel({ restoreFocus = true } = {}) {
    if (!opened) return;
    opened = false;
    backdrop.hidden = true;
    trigger.setAttribute("aria-expanded", "false");
    if (restoreFocus) trigger.focus();
  }

  function setState(text, error = false) {
    state.textContent = text;
    state.classList.toggle("error", error);
  }

  function row(item) {
    const li = doc.createElement("li");
    const button = doc.createElement("button");
    button.type = "button";
    button.className = "history-item";
    const title = doc.createElement("span");
    title.className = "history-item-title";
    title.textContent = item.title;
    const meta = doc.createElement("span");
    meta.className = "history-item-meta";
    const when = formatRelative(item.updatedAt, now());
    meta.textContent = `${when ? `${when} ・ ` : ""}${item.count} 件`;
    button.append(title, meta);
    button.addEventListener("click", () => handlers.onSelect(item.id));
    li.append(button);
    return { li, button };
  }

  function onKeydown(event) {
    if (!opened) return;
    if (event.key === "Escape") {
      event.preventDefault();
      closePanel();
      return;
    }
    const order = focusables();
    const index = order.indexOf(doc.activeElement);
    if (event.key === "Tab" && order.length > 0) {
      const next = event.shiftKey ? index - 1 : index + 1;
      if (index === -1 || next < 0 || next >= order.length) {
        event.preventDefault();
        order[event.shiftKey ? order.length - 1 : 0].focus();
      }
    } else if ((event.key === "ArrowDown" || event.key === "ArrowUp") && itemButtons.length > 0) {
      const at = itemButtons.indexOf(doc.activeElement);
      if (at === -1 && event.key === "ArrowUp") return;
      event.preventDefault();
      const next = event.key === "ArrowDown" ? at + 1 : at - 1;
      itemButtons[Math.min(Math.max(next, 0), itemButtons.length - 1)].focus();
    }
  }

  trigger.addEventListener("click", open);
  close.addEventListener("click", () => closePanel());
  more.addEventListener("click", () => handlers.onMore());
  backdrop.addEventListener("click", (event) => {
    if (event.target === backdrop) closePanel();
  });
  backdrop.addEventListener("keydown", onKeydown);

  return {
    open,
    closePanel,
    get isOpen() {
      return opened;
    },
    showListLoading() {
      itemButtons = [];
      list.replaceChildren();
      more.hidden = true;
      setState(STATE_LOADING);
    },
    showList({ items, append, hasMore }) {
      if (!append) {
        itemButtons = [];
        list.replaceChildren();
      }
      const rows = items.map(row);
      list.append(...rows.map((entry) => entry.li));
      itemButtons.push(...rows.map((entry) => entry.button));
      more.hidden = !hasMore;
      setState(itemButtons.length === 0 ? STATE_EMPTY : "");
      if (append && rows.length > 0) rows[0].button.focus();
    },
    showListError(text) {
      setState(text, true);
      more.hidden = true;
    },
    setState,
  };
}
