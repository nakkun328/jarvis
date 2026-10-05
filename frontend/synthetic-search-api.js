export class SearchError extends Error {}

export function validateQuestion(query) {
  if (typeof query !== "string" || !query.trim()) throw new SearchError("空入力です。質問を入力してください。");
  if ([...query].length > 4000) throw new SearchError("質問は4000文字以内にしてください。");
}

export async function searchSynthetic(query, fetchImpl = fetch) {
  validateQuestion(query);
  let response;
  try {
    response = await fetchImpl("/api/synthetic-search", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query }),
    });
  } catch {
    throw new SearchError("サーバーへ接続できません。起動状態を確認し、再試行してください。");
  }
  if (!response.ok) throw new SearchError("検索できませんでした。固定cacheと依存環境を確認し、再試行してください。");
  let result;
  try { result = await response.json(); } catch {
    throw new SearchError("検索結果を読み取れませんでした。再試行してください。");
  }
  if (!Array.isArray(result?.matches) || typeof result.contract_only !== "boolean"
      || result.support_assessment !== "not_assessed") {
    throw new SearchError("検索結果の形式が正しくありません。再試行してください。");
  }
  return result;
}
