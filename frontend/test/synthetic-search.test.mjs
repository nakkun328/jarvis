import assert from "node:assert/strict";
import test from "node:test";
import { searchSynthetic, SearchError } from "../synthetic-search-api.js";

test("empty and oversized queries never request; 4000 code points are accepted", async () => {
  for (const query of ["", " \n", "a".repeat(4001)]) {
    await assert.rejects(searchSynthetic(query, () => assert.fail("must not request")), SearchError);
  }
  let body;
  const result = await searchSynthetic("🙂".repeat(4000), async (url, options) => {
    assert.equal(url, "/api/synthetic-search");
    body = JSON.parse(options.body);
    return Response.json({ matches: [], contract_only: true, support_assessment: "not_assessed" });
  });
  assert.deepEqual(Object.keys(body), ["query"]);
  assert.equal([...body.query].length, 4000);
  assert.deepEqual(result.matches, []);
});

test("network/provider failures are redacted and explicit next request retries", async () => {
  let calls = 0;
  const fetchImpl = async () => {
    calls += 1;
    if (calls === 1) throw new Error("secret path");
    return Response.json({ matches: [], contract_only: false, support_assessment: "not_assessed" });
  };
  await assert.rejects(searchSynthetic("質問", fetchImpl), (error) => {
    assert(error instanceof SearchError);
    assert(!error.message.includes("secret"));
    return true;
  });
  assert.equal((await searchSynthetic("質問", fetchImpl)).support_assessment, "not_assessed");
  assert.equal(calls, 2);
  await assert.rejects(searchSynthetic("質問", async () => new Response("private path", { status: 503 })), SearchError);
});

test("malformed results fail without presenting candidates as answers", async () => {
  for (const payload of [null, {}, { matches: [], contract_only: false, support_assessment: "answerable" }]) {
    await assert.rejects(searchSynthetic("質問", async () => Response.json(payload)), SearchError);
  }
  await assert.rejects(searchSynthetic("質問", async () => new Response("not json")), SearchError);
});
