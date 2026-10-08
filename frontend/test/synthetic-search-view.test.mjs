import assert from "node:assert/strict";
import test from "node:test";
import {
  candidateRows, candidateTitle, modeLabel, resultSummary,
  FAKE_MODE_LABEL, REAL_MODE_LABEL, UNKNOWN_MODE_LABEL,
} from "../synthetic-search-view.js";

const match = {
  id: "id-1", fixture_id: "mizuki-current", body: "ESP32", source: "synthetic-evaluation:x/mizuki-current",
  origin: "user_explicit", revision: "rev", memory_confidence: 0.8, importance: 0.7, index_score: 0.5,
  edited_since_approval: true, stale: false, corrects_fixture_id: "mizuki-old",
};

test("contract-only is labelled FAKE, real and unknown modes never are", () => {
  assert.equal(modeLabel(true), FAKE_MODE_LABEL);
  assert(FAKE_MODE_LABEL.startsWith("FAKE"));
  assert.equal(modeLabel(false), REAL_MODE_LABEL);
  assert(!REAL_MODE_LABEL.includes("FAKE"));
  assert.equal(modeLabel(undefined), UNKNOWN_MODE_LABEL);
  assert.equal(modeLabel("true"), UNKNOWN_MODE_LABEL);
});

test("candidates are shown as current canonical, FAKE-marked in contract-only mode", () => {
  assert(candidateTitle(match, 0, true).startsWith("[FAKE] 1. mizuki-current"));
  assert(!candidateTitle(match, 0, false).includes("FAKE"));
  assert(candidateTitle(match, 1, false).includes("現在の正本"));
});

test("summary says candidate is not answer evidence and keeps empty results explicit", () => {
  const found = resultSummary({ matches: [match], contract_only: false });
  assert(found.includes("candidate != answer evidence") && !found.includes("FAKE"));
  assert(resultSummary({ matches: [match], contract_only: true }).includes("FAKE"));
  assert.equal(resultSummary({ matches: [], contract_only: true }), "現在の承認済み記憶の候補はありません。");
});

test("metadata rows keep score and confidence away from answerability claims", () => {
  const rows = Object.fromEntries(candidateRows(match));
  assert.equal(rows["訂正元（人工fixture名）"], "mizuki-old");
  assert.equal(rows["承認後の編集 / stale"], "あり / false");
  assert(rows["index score"].includes("回答可能性ではありません"));
  assert(rows["記憶metadata confidence / importance"].includes("確信度ではありません"));
  assert.equal(Object.fromEntries(candidateRows({ ...match, corrects_fixture_id: null }))["訂正元（人工fixture名）"], "なし");
});
