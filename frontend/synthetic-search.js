import { searchSynthetic, validateQuestion } from "./synthetic-search-api.js";

const form = document.querySelector("#search-form");
const query = document.querySelector("#query");
const button = document.querySelector("#search");
const retry = document.querySelector("#retry");
const status = document.querySelector("#search-status");
const results = document.querySelector("#results");
const mode = document.querySelector("#mode");
let busy = false;
let lastQuestion = "";

function showMode(contractOnly) {
  mode.textContent = contractOnly
    ? "fake契約検証のみ：日本語semantic検索の品質を示しません。"
    : "固定revisionのローカルE5検索：外部APIなし。";
}

function element(tag, text) {
  const node = document.createElement(tag);
  node.textContent = text;
  return node;
}

function render(matches) {
  results.replaceChildren();
  matches.forEach((match, index) => {
    const article = element("article", "");
    article.className = "candidate";
    article.append(element("h2", `${index + 1}. ${match.fixture_id} · 現在 approved`));
    const body = element("p", match.body);
    body.className = "body";
    article.append(body);
    const metadata = element("dl", "");
    const entries = [
      ["出典 / origin", `${match.source} / ${match.origin}`],
      ["ID / 現在 revision", `${match.id} / ${match.revision}`],
      ["訂正元（人工fixture名）", match.corrects_fixture_id ?? "なし"],
      ["承認後の編集 / stale", `${match.edited_since_approval ? "あり" : "なし"} / ${match.stale}`],
      ["記憶metadata confidence / importance", `${match.memory_confidence} / ${match.importance}（検索の確信度ではありません）`],
      ["index score", `${match.index_score}（確率・confidence・回答可能性ではありません）`],
    ];
    entries.forEach(([label, value]) => metadata.append(element("dt", label), element("dd", value)));
    article.append(metadata);
    results.append(article);
  });
}

async function run(question) {
  if (busy) return;
  try { validateQuestion(question); } catch (error) {
    status.textContent = error.message;
    status.classList.add("error");
    results.replaceChildren();
    retry.hidden = true;
    return;
  }
  lastQuestion = question;
  busy = true;
  button.disabled = query.disabled = retry.disabled = true;
  retry.hidden = true;
  results.replaceChildren();
  results.setAttribute("aria-busy", "true");
  status.classList.remove("error");
  status.textContent = "人工記憶を準備・検索しています…初回は時間がかかります。";
  try {
    const result = await searchSynthetic(question);
    showMode(result.contract_only);
    render(result.matches);
    status.textContent = result.matches.length
      ? `${result.matches.length}件の検索候補。回答ではありません。質問への支持根拠を本文で確認してください。`
      : "現在の承認済み記憶の候補はありません。";
  } catch (error) {
    status.textContent = error.message;
    status.classList.add("error");
    retry.hidden = false;
  } finally {
    busy = false;
    button.disabled = query.disabled = retry.disabled = false;
    results.setAttribute("aria-busy", "false");
  }
}

form.addEventListener("submit", (event) => { event.preventDefault(); run(query.value); });
retry.addEventListener("click", () => run(lastQuestion));
fetch("/api/synthetic-search/status")
  .then((response) => { if (!response.ok) throw new Error(); return response.json(); })
  .then((state) => showMode(state.contract_only))
  .catch(() => { mode.textContent = "試験モードを確認できません。検索結果で再確認します。"; });
