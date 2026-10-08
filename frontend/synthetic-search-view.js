export const FAKE_MODE_LABEL =
  "FAKE契約検証モード：偽のvectorで順序は無意味です。日本語semantic検索の品質を示しません。";
export const REAL_MODE_LABEL = "固定revisionのローカルE5検索：外部APIなし。人工記憶のみ。";
export const UNKNOWN_MODE_LABEL = "試験モードを確認できません。検索結果で再確認します。";
export const CANDIDATE_NOT_ANSWER =
  "検索候補は回答ではなく、質問への支持根拠があることも意味しません（candidate != answer evidence）。";

export function modeLabel(contractOnly) {
  if (contractOnly === true) return FAKE_MODE_LABEL;
  if (contractOnly === false) return REAL_MODE_LABEL;
  return UNKNOWN_MODE_LABEL;
}

export function resultSummary(result) {
  if (!result.matches.length) return "現在の承認済み記憶の候補はありません。";
  const fake = result.contract_only ? "FAKE vectorの順序のため無意味。" : "";
  return `${result.matches.length}件の検索候補。${fake}${CANDIDATE_NOT_ANSWER}本文で確認してください。`;
}

export function candidateTitle(match, index, contractOnly) {
  const fake = contractOnly ? "[FAKE] " : "";
  return `${fake}${index + 1}. ${match.fixture_id} · 現在の正本 approved`;
}

export function candidateRows(match) {
  return [
    ["出典 / origin", `${match.source} / ${match.origin}`],
    ["ID / 現在 revision", `${match.id} / ${match.revision}`],
    ["訂正元（人工fixture名）", match.corrects_fixture_id ?? "なし"],
    ["承認後の編集 / stale", `${match.edited_since_approval ? "あり" : "なし"} / ${match.stale}`],
    ["記憶metadata confidence / importance",
      `${match.memory_confidence} / ${match.importance}（検索の確信度ではありません）`],
    ["index score", `${match.index_score}（確率・confidence・回答可能性ではありません）`],
  ];
}
