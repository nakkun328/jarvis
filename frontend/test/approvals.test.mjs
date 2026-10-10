import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { ApprovalsApiError, CONFIRM_HEADER, decide, decisionUrl, loadApprovals } from "../approvals-api.js";
import {
  decisionMessage,
  expiryText,
  isUrgent,
  listStatusText,
  normalizeApproval,
  secondsLeft,
} from "../approvals-view.js";

const ID = "11111111-1111-4111-8111-111111111111";
const raw = (overrides = {}) => ({
  id: ID,
  tool_name: "fake.write",
  summary: {
    fields: [{ name: "text", preview: "<img src=x onerror=alert(1)>" }],
    more_fields: 2,
    argument_bytes: 40,
    digest_prefix: "abcdef012345",
  },
  requested_at: "2030-01-01T12:00:00.000000Z",
  expires_at: "2030-01-01T12:05:00.000000Z",
  state: "pending",
  ...overrides,
});
const reply = (status, body) => async () => ({ status, ok: status >= 200 && status < 300, json: async () => body });

test("normalizeApproval keeps known fields as plain data and rejects malformed shapes", () => {
  const approval = normalizeApproval(raw());
  assert.equal(approval.toolName, "fake.write");
  assert.equal(approval.fields[0].preview, "<img src=x onerror=alert(1)>");
  assert.equal(approval.moreFields, 2);
  for (const bad of [
    null,
    "x",
    raw({ id: "../../etc" }),
    raw({ tool_name: 5 }),
    raw({ expires_at: "soon" }),
    raw({ summary: null }),
    raw({ summary: { fields: [{ name: 1, preview: "x" }] } }),
  ]) {
    assert.equal(normalizeApproval(bad), null);
  }
});

test("expiry text counts down and an expired request reads as denied", () => {
  const approval = normalizeApproval(raw());
  const start = Date.parse("2030-01-01T12:00:00Z");
  assert.equal(secondsLeft(approval, start), 300);
  assert.match(expiryText(approval, start), /5 分 0 秒/);
  assert.match(expiryText(approval, start + 290_000), /あと 10 秒/);
  assert.equal(isUrgent(approval, start + 280_000), true);
  assert.equal(isUrgent(approval, start), false);
  assert.match(expiryText(approval, start + 301_000), /期限切れ.*拒否/);
});

test("status and decision messages are fixed text", () => {
  assert.equal(listStatusText({ loaded: true, error: null, count: 0 }), "承認待ちはありません。");
  assert.match(listStatusText({ loaded: true, error: null, count: 2 }), /2/);
  assert.match(listStatusText({ loaded: false, error: "network", count: 0 }), /接続/);
  assert.match(decisionMessage("approve", "fake.write", "ok"), /承認しました/);
  assert.match(decisionMessage("deny", "fake.write", "ok"), /拒否しました/);
  assert.match(decisionMessage("approve", "fake.write", "expired"), /実行されません/);
  assert.match(decisionMessage("approve", "fake.write", "network"), /できませんでした/);
});

test("loadApprovals validates the answer and reports fixed error kinds", async () => {
  const list = await loadApprovals({ fetchImpl: reply(200, { approvals: [raw()] }) });
  assert.equal(list.length, 1);
  const kinds = [];
  for (const impl of [
    reply(200, { approvals: [{}] }),
    reply(200, {}),
    reply(503, {}),
    reply(500, {}),
    async () => {
      throw new TypeError("down");
    },
  ]) {
    await loadApprovals({ fetchImpl: impl }).catch((error) => {
      assert.ok(error instanceof ApprovalsApiError);
      kinds.push(error.kind);
    });
  }
  assert.deepEqual(kinds, ["format", "format", "unavailable", "server", "network"]);
});

test("decide sends a body-less POST with the confirmation header and maps answers", async () => {
  const seen = [];
  const impl = (status) => async (url, init) => {
    seen.push({ url, init });
    return { status, ok: status === 200 };
  };
  assert.equal(await decide(ID, "approve", { fetchImpl: impl(200) }), "ok");
  assert.equal(seen[0].url, `/api/approvals/${ID}/approve`);
  assert.equal(seen[0].init.method, "POST");
  assert.equal(seen[0].init.headers[CONFIRM_HEADER], "1");
  assert.equal(seen[0].init.body, undefined);
  assert.equal(await decide(ID, "deny", { fetchImpl: impl(200) }), "ok");
  assert.equal(seen[1].url, `/api/approvals/${ID}/deny`);
  const outcomes = [];
  for (const status of [410, 409, 404, 403, 500]) outcomes.push(await decide(ID, "approve", { fetchImpl: impl(status) }));
  assert.deepEqual(outcomes, ["expired", "not_pending", "not_found", "forbidden", "error"]);
  assert.equal(await decide(ID, "approve", { fetchImpl: async () => { throw new TypeError("x"); } }), "network");
});

test("the ID is encoded into the URL and any other kind means deny", () => {
  assert.equal(decisionUrl("a/b", "approve"), "/api/approvals/a%2Fb/approve");
  assert.equal(decisionUrl(ID, "anything"), `/api/approvals/${ID}/deny`);
});

test("approvals scripts never render markup", async () => {
  for (const name of ["approvals.js", "approvals-api.js", "approvals-view.js"]) {
    const source = await readFile(new URL(`../${name}`, import.meta.url), "utf8");
    assert.doesNotMatch(source, /innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(/, name);
  }
});
