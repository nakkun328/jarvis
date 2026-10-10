import assert from "node:assert/strict";
import test from "node:test";
import { autoApprovalInfo, detailModel, normalizeDetail, normalizeMemory } from "../memory-view.js";
import { CONFIRM_HEADER, withdrawMemory, withdrawMessage, withdrawUrl } from "../withdraw-memory.js";

const ID = "11111111-1111-4111-8111-111111111111";
const reply = (status) => async () => ({ ok: status >= 200 && status < 300, status });

test("withdraw POSTs with the confirm header and no body", async () => {
  let seen;
  const fetchImpl = async (url, init) => {
    seen = { url, init };
    return { ok: true, status: 200 };
  };
  assert.deepEqual(await withdrawMemory(ID, { fetchImpl }), { kind: "ok" });
  assert.equal(seen.url, withdrawUrl(ID));
  assert.equal(seen.init.method, "POST");
  assert.equal(seen.init.headers[CONFIRM_HEADER], "1");
  assert.equal(seen.init.body, undefined);
});

test("server answers reduce to fixed words", async () => {
  assert.equal((await withdrawMemory(ID, { fetchImpl: reply(409) })).kind, "refused");
  assert.equal((await withdrawMemory(ID, { fetchImpl: reply(403) })).kind, "forbidden");
  assert.equal((await withdrawMemory(ID, { fetchImpl: reply(404) })).kind, "not_found");
  assert.equal((await withdrawMemory(ID, { fetchImpl: reply(503) })).kind, "unavailable");
  const down = async () => {
    throw new TypeError("boom");
  };
  assert.equal((await withdrawMemory(ID, { fetchImpl: down })).kind, "network");
  assert.match(withdrawMessage("refused"), /撤回できません/);
});

const note = (extra = {}) => ({
  id: ID,
  status: "approved",
  origin: "research",
  content: "Fact\n\n引用: q\n出典: https://a.example/x (T)\n取得日: 2026-10-07\n調査ID: s",
  ...extra,
});

test("only an automatically approved research note shows the label, source and date", () => {
  const auto = normalizeMemory(note({ auto_approved: true }));
  assert.deepEqual(autoApprovalInfo(auto), {
    label: "自動承認(調査)",
    source: "https://a.example/x (T)",
    date: "2026-10-07",
  });
  assert.equal(autoApprovalInfo(normalizeMemory(note())), null);
  assert.equal(autoApprovalInfo(normalizeMemory(note({ auto_approved: "true" }))), null);
});

test("the detail history says the approval was automatic and withdrawal is offered", () => {
  const detail = normalizeDetail({
    ...note({ auto_approved: true }),
    reviews: [
      { action: "approve", automatic: true, previous_status: "pending", new_status: "approved" },
    ],
    lifecycle: [],
  });
  const model = detailModel(detail);
  assert.equal(model.canWithdraw, true);
  assert.match(model.history[0].text, /^自動で承認/);
  const retired = detailModel({ ...detail, status: "retired" });
  assert.equal(retired.canWithdraw, false);
});

test("an automatically approved chat note shows its label, verbatim quote and date", () => {
  const chat = normalizeMemory({
    id: ID,
    status: "approved",
    origin: "chat",
    auto_approved: true,
    content: "毎朝走っている\n\n引用: 毎朝走っています\n日付: 2026-10-10",
  });
  assert.deepEqual(autoApprovalInfo(chat), {
    label: "自動承認(会話)",
    source: "毎朝走っています",
    date: "2026-10-10",
    sourceLabel: "引用",
    dateLabel: "日付",
  });
  assert.equal(detailModel({ ...chat, reviews: [], lifecycle: [] }).canWithdraw, true);
  assert.equal(autoApprovalInfo({ ...chat, auto_approved: false }), null);
});
