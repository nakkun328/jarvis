import { ApprovalsApiError, decide, loadApprovals } from "./approvals-api.js";
import { decisionMessage, expiryText, isUrgent, listStatusText } from "./approvals-view.js";

// Lists pending approvals and sends the human's Approve/Deny. The DOM is built with textContent
// only; tool names and argument previews are untrusted data and are never parsed as markup.

const POLL_MS = 3000;
const POLL_MAX_MS = 30000;

const $ = (selector) => document.querySelector(selector);
const listBox = $("#approval-list");
const statusLine = $("#status");

const state = { approvals: [], loaded: false, error: null, failures: 0, busy: new Set(), timer: null };

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function renderStatus(message) {
  const text = message ?? listStatusText({ loaded: state.loaded, error: state.error, count: state.approvals.length });
  if (statusLine.textContent !== text) statusLine.textContent = text;
}

function buildItem(approval) {
  const item = el("li", "approval-item");
  item.dataset.id = approval.id;
  const heading = el("h2", "", approval.toolName);
  heading.id = `approval-${approval.id}`;
  item.setAttribute("aria-labelledby", heading.id);

  const expiry = el("p", "approval-expiry", expiryText(approval, Date.now()));
  expiry.dataset.expiry = "";

  const fields = el("dl", "approval-fields");
  for (const field of approval.fields) {
    fields.append(el("dt", "", field.name), el("dd", "", field.preview));
  }
  if (approval.moreFields > 0) {
    fields.append(el("dt", "", "…"), el("dd", "", `ほか ${approval.moreFields} 項目`));
  }
  const meta = el("p", "approval-meta", `照合用コード: ${approval.digestPrefix}`);

  const actions = el("div", "approval-actions");
  actions.setAttribute("role", "group");
  actions.setAttribute("aria-label", `${approval.toolName} の操作`);
  for (const [kind, label, cls] of [
    ["approve", "承認", "approve-button"],
    ["deny", "拒否", "deny-button"],
  ]) {
    const button = el("button", cls, label);
    button.type = "button";
    button.setAttribute("aria-label", `${approval.toolName} を${label}`);
    button.addEventListener("click", () => void onDecide(approval, kind));
    actions.append(button);
  }
  item.append(heading, expiry, fields, meta, actions);
  return item;
}

function renderList() {
  const existing = new Map([...listBox.children].map((node) => [node.dataset.id, node]));
  const wanted = state.approvals.map((approval) => approval.id);
  for (const [id, node] of existing) if (!wanted.includes(id)) node.remove();
  let previous = null;
  for (const approval of state.approvals) {
    let node = existing.get(approval.id);
    if (!node) {
      node = buildItem(approval);
      listBox.insertBefore(node, previous ? previous.nextSibling : listBox.firstChild);
    }
    previous = node;
  }
  tick();
}

// Only the countdown text changes each second; the buttons keep focus.
function tick() {
  const now = Date.now();
  for (const approval of state.approvals) {
    const node = listBox.querySelector(`[data-id="${CSS.escape(approval.id)}"]`);
    const expiry = node?.querySelector("[data-expiry]");
    if (!expiry) continue;
    const text = expiryText(approval, now);
    if (expiry.textContent !== text) expiry.textContent = text;
    expiry.dataset.urgent = String(isUrgent(approval, now));
    if (approval.expiresAt <= now) for (const b of node.querySelectorAll("button")) b.disabled = true;
  }
}

async function refresh() {
  try {
    state.approvals = await loadApprovals();
    state.loaded = true;
    state.error = null;
    state.failures = 0;
  } catch (error) {
    state.failures += 1;
    state.error = error instanceof ApprovalsApiError ? error.kind : "server";
  }
  renderList();
  renderStatus();
  schedule();
}

function schedule() {
  clearTimeout(state.timer);
  const delay = Math.min(POLL_MS * 2 ** Math.min(state.failures, 4), POLL_MAX_MS);
  state.timer = setTimeout(() => void refresh(), delay);
}

async function onDecide(approval, kind) {
  if (state.busy.has(approval.id)) return;
  state.busy.add(approval.id);
  const node = listBox.querySelector(`[data-id="${CSS.escape(approval.id)}"]`);
  const buttons = node ? [...node.querySelectorAll("button")] : [];
  for (const b of buttons) b.disabled = true;
  const outcome = await decide(approval.id, kind);
  state.busy.delete(approval.id);
  renderStatus(decisionMessage(kind, approval.toolName, outcome));
  if (outcome === "ok" || outcome === "expired" || outcome === "not_pending" || outcome === "not_found") {
    state.approvals = state.approvals.filter((a) => a.id !== approval.id);
    renderList();
    $("#approvals-heading").focus();
  } else {
    for (const b of buttons) b.disabled = false;
  }
}

setInterval(tick, 1000);
void refresh();
