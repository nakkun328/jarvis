import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  BUTTON_LABEL,
  CONFIRM_HEADER,
  candidatesUrl,
  loadCandidates,
  memoryPanelModel,
  stageCandidates,
} from "../stage-candidates.js";

const ID = "11111111-1111-4111-8111-111111111111";
const reply = (status, body) => async () => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => body,
});

test("POST carries the confirm header and no body; GET is plain", async () => {
  let seen;
  const fetchImpl = async (url, init) => {
    seen = { url, init };
    return { ok: true, status: 201, json: async () => ({ eligible: true, created: 2, omitted: 0, candidates: [{}, {}] }) };
  };
  const result = await stageCandidates(ID, { fetchImpl });
  assert.equal(seen.url, candidatesUrl(ID));
  assert.equal(seen.init.method, "POST");
  assert.equal(seen.init.headers[CONFIRM_HEADER], "1");
  assert.equal(seen.init.body, undefined);
  assert.deepEqual(result, {
    kind: "ok",
    eligible: true,
    count: 2,
    omitted: 0,
    created: 2,
    autoApproval: false,
    approved: 0,
  });
  await loadCandidates(ID, { fetchImpl });
  assert.equal(seen.init.method, undefined);
  assert.equal(seen.init.headers[CONFIRM_HEADER], undefined);
});

test("server answers reduce to fixed words", async () => {
  assert.equal((await stageCandidates(ID, { fetchImpl: reply(409, {}) })).kind, "refused");
  assert.equal((await stageCandidates(ID, { fetchImpl: reply(403, {}) })).kind, "forbidden");
  assert.equal((await stageCandidates(ID, { fetchImpl: reply(503, {}) })).kind, "unavailable");
  assert.equal((await stageCandidates(ID, { fetchImpl: reply(200, { x: 1 }) })).kind, "format");
  const down = async () => {
    throw new TypeError("boom");
  };
  assert.equal((await stageCandidates(ID, { fetchImpl: down })).kind, "network");
});

test("panel model shows the added state and never claims approval", () => {
  const idle = memoryPanelModel({ phase: "idle" });
  assert.equal(idle.buttonLabel, BUTTON_LABEL);
  assert.equal(idle.disabled, false);
  const added = memoryPanelModel({ phase: "added", count: 3, created: 0, omitted: 1 });
  assert.equal(added.disabled, true);
  assert.equal(added.buttonLabel, "追加済み");
  assert.match(added.status, /追加済み/);
  assert.match(added.status, /確認待ち 3 件/);
  assert.match(added.note, /自動で記憶には入りません/);
  assert.match(memoryPanelModel({ phase: "error", errorKind: "refused" }).error, /検証済み/);
});

test("panel model tells the truth when automatic approval is on", () => {
  const idle = memoryPanelModel({ phase: "idle", autoApproval: true });
  assert.match(idle.note, /自動承認が有効です/);
  assert.doesNotMatch(idle.note, /自動で記憶には入りません/);
  const added = memoryPanelModel({
    phase: "added", count: 5, created: 5, approved: 3, omitted: 2, autoApproval: true,
  });
  assert.match(added.status, /新規 5 件/);
  assert.match(added.status, /自動承認 3 件、確認待ち 2 件/);
  assert.match(added.status, /2 件は追加していません/);
});

test("the screen uses text nodes only", () => {
  const own = readFileSync(new URL("../stage-candidates.js", import.meta.url), "utf8");
  assert.equal(own.match(/\bmethod\s*:\s*["']\w+["']/g).length, 1);
  for (const file of ["../stage-candidates.js", "../research.js"]) {
    const source = readFileSync(new URL(file, import.meta.url), "utf8");
    assert.doesNotMatch(source, /innerHTML|outerHTML|insertAdjacentHTML|document\.write/);
  }
});
