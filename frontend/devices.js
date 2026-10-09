import { DevicesApiError, loadDevices } from "./devices-api.js";
import {
  NOT_CONNECTED_TEXT,
  NOT_CONNECTED_TITLE,
  clientFacts,
  currentRows,
  serverRows,
  statusText,
} from "./devices-view.js";

// Read-only Devices screen. The DOM is built with textContent only.

const $ = (selector) => document.querySelector(selector);

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function fill(list, rows) {
  list.replaceChildren();
  for (const [name, value] of rows) list.append(el("dt", "", name), el("dd", "", value));
}

async function main() {
  const status = $("#status");
  status.textContent = statusText({ loaded: false });
  try {
    const device = await loadDevices();
    fill($("#current-fields"), currentRows(device, clientFacts()));
    fill($("#server-fields"), serverRows(device));
    $("#registered-title").textContent = NOT_CONNECTED_TITLE;
    $("#registered-text").textContent = NOT_CONNECTED_TEXT;
    status.textContent = statusText({ loaded: true });
  } catch (error) {
    status.textContent = statusText({
      loaded: false,
      error: error instanceof DevicesApiError ? error.kind : "server",
    });
  }
}

void main();
