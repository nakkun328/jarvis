import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { DevicesApiError, loadDevices } from "../devices-api.js";
import { clientFacts, currentRows, normalizeDevices, serverRows, statusText } from "../devices-view.js";

const body = (overrides = {}) => ({
  current: { browser: "Firefox", os: "Linux", connection: "local", scheme: "http" },
  server: {
    login_enabled: false,
    providers: { chat: "none", search: "none" },
    features: { router: "off", research: false, shell: false, model_choices: false },
  },
  registered_devices: { available: false, reason: "no_device_registry", devices: [] },
  ...overrides,
});
const reply = (status, payload) => async () => ({
  status,
  ok: status >= 200 && status < 300,
  json: async () => payload,
});

test("normalizeDevices maps a valid body and rejects a malformed one", () => {
  const device = normalizeDevices(body());
  assert.equal(device.browser, "Firefox");
  assert.equal(device.secureTransport, false);
  assert.equal(device.chatProvider, "未設定");
  assert.equal(device.router, "無効");
  assert.equal(normalizeDevices({}), null);
  assert.equal(normalizeDevices(body({ registered_devices: { available: true, devices: [] } })), null);
});

test("untrusted strings are reduced to safe family names", () => {
  const device = normalizeDevices(body({ current: { browser: "<img onerror=x>", os: 5, connection: "x", scheme: "https" } }));
  assert.equal(device.browser, "判別できません");
  assert.equal(device.os, "判別できません");
  assert.equal(device.connection, "判別できません");
  assert.equal(device.secureTransport, true);
});

test("rows are labelled pairs and never invent device data", () => {
  const device = normalizeDevices(body());
  const facts = clientFacts({ isSecureContext: true, navigator: { onLine: true }, matchMedia: () => ({ matches: true }) });
  assert.deepEqual(facts, { secureContext: true, installed: true, online: true });
  assert.ok(currentRows(device, facts).every((row) => row.length === 2));
  assert.equal(serverRows(device)[0][1], "無効（ローカル専用）");
});

test("loadDevices reports fixed error kinds", async () => {
  assert.equal((await loadDevices({ fetchImpl: reply(200, body()) })).os, "Linux");
  for (const [fetchImpl, kind] of [
    [reply(500, {}), "server"],
    [reply(200, { nope: 1 }), "format"],
    [async () => { throw new TypeError("down"); }, "network"],
  ]) {
    await assert.rejects(loadDevices({ fetchImpl }), (e) => e instanceof DevicesApiError && e.kind === kind);
  }
  assert.equal(statusText({ loaded: false, error: "network" }), "端末情報を読み込めませんでした。");
});

test("device scripts use text nodes only and never write", async () => {
  const forbidden = /innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(|new Function|\bmethod\s*:|localStorage/;
  for (const name of ["devices.js", "devices-api.js", "devices-view.js"]) {
    assert.doesNotMatch(await readFile(new URL(`../${name}`, import.meta.url), "utf8"), forbidden, name);
  }
});
