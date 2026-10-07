import { searchSynthetic, validateQuestion } from "./synthetic-search-api.js";
import { candidateRows, candidateTitle, modeLabel, resultSummary } from "./synthetic-search-view.js";

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
  mode.textContent = modeLabel(contractOnly);
  mode.classList.toggle("fake", contractOnly === true);
  document.title = `${contractOnly === true ? "[FAKE] " : ""}JARVIS · 人工記憶の検索試験`;
}

function element(tag, text) {
  const node = document.createElement(tag);
  node.textContent = text;
  return node;
}

function render(matches, contractOnly) {
  results.replaceChildren();
  matches.forEach((match, index) => {
    const article = element("article", "");
    article.className = contractOnly ? "candidate fake" : "candidate";
    article.append(element("h2", candidateTitle(match, index, contractOnly)));
    const body = element("p", match.body);
    body.className = "body";
    article.append(body);
    const metadata = element("dl", "");
    candidateRows(match).forEach(([label, value]) => metadata.append(element("dt", label), element("dd", value)));
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
    render(result.matches, result.contract_only);
    status.textContent = resultSummary(result);
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
  .catch(() => showMode(null));
