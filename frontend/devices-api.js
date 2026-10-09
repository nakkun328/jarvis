// Client for the read-only devices API (GET only; errors are reduced to fixed words).
import { normalizeDevices } from "./devices-view.js";
import { sessionEnded } from "./session.js";

export const DEVICES_URL = "/api/devices";

export class DevicesApiError extends Error {
  // kind: unauthorized | network | format | server
  constructor(kind, status = null) {
    super(kind);
    this.kind = kind;
    this.status = status;
  }
}

export async function loadDevices({ fetchImpl = fetch, signal } = {}) {
  let response;
  try {
    response = await fetchImpl(DEVICES_URL, { headers: { Accept: "application/json" }, signal });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw new DevicesApiError("network");
  }
  if (response.status === 401) {
    sessionEnded();
    throw new DevicesApiError("unauthorized", 401);
  }
  if (!response.ok) throw new DevicesApiError("server", response.status);
  let body;
  try {
    body = await response.json();
  } catch {
    throw new DevicesApiError("format");
  }
  const devices = normalizeDevices(body);
  if (!devices) throw new DevicesApiError("format");
  return devices;
}
