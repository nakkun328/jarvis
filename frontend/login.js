// Login page: posts the passphrase as JSON to the same origin. Text is always set with
// textContent, nothing is stored in the browser, and the field is cleared after every attempt.
const form = document.getElementById("login-form");
const input = document.getElementById("passphrase");
const submit = document.getElementById("submit");
const message = document.getElementById("message");

const MESSAGES = {
  401: "パスフレーズが正しくありません。",
  429: "試行回数が多すぎます。しばらく待ってからやり直してください。",
};
const GENERIC = "ログインできませんでした。しばらくしてからやり直してください。";

function show(text) {
  message.textContent = text;
}

async function alreadySignedIn() {
  try {
    const response = await fetch("/api/auth/status", { credentials: "same-origin" });
    if (!response.ok) return false;
    const body = await response.json();
    return body !== null && body.authenticated === true;
  } catch {
    return false;
  }
}

async function login(event) {
  event.preventDefault();
  const passphrase = input.value;
  if (!passphrase) {
    show("パスフレーズを入力してください。");
    return;
  }
  submit.disabled = true;
  show("");
  try {
    const response = await fetch("/api/auth/login", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ passphrase }),
    });
    if (response.ok) {
      input.value = "";
      location.replace("/");
      return;
    }
    show(MESSAGES[response.status] ?? GENERIC);
  } catch {
    show(GENERIC);
  } finally {
    input.value = "";
    submit.disabled = false;
  }
}

form.addEventListener("submit", login);
alreadySignedIn().then((signedIn) => {
  if (signedIn) location.replace("/");
});
