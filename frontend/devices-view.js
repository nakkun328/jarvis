// Pure helpers for the Devices screen: validate what the API sent and turn it into labelled rows.
// Nothing here touches the DOM. Every string from the server is untrusted and only ever becomes
// a text node in devices.js.

const FAMILY = /^[A-Za-z][A-Za-z0-9 ._-]{0,31}$/;
const CONNECTION_LABELS = {
  local: "このサーバーと同じ端末（ローカル接続）",
  remote: "別の端末からの接続（リモート）",
  proxied: "プロキシ経由（接続元は判別できません）",
};
const PROVIDER = /^[a-z0-9_-]{1,32}$/;

function family(value) {
  if (value === "unknown") return "判別できません";
  return typeof value === "string" && FAMILY.test(value) ? value : "判別できません";
}

function provider(value) {
  if (value === "none") return "未設定";
  return typeof value === "string" && PROVIDER.test(value) ? value : "判別できません";
}

function onOff(value) {
  return value === true ? "有効" : "無効";
}

// Returns null when the body is not the documented shape.
export function normalizeDevices(body) {
  const current = body?.current;
  const server = body?.server;
  const registered = body?.registered_devices;
  if (!current || !server || !registered) return null;
  if (typeof server.login_enabled !== "boolean") return null;
  if (!Array.isArray(registered.devices) || registered.available !== false) return null;
  return {
    browser: family(current.browser),
    os: family(current.os),
    connection: CONNECTION_LABELS[current.connection] ?? "判別できません",
    secureTransport: current.scheme === "https",
    loginEnabled: server.login_enabled,
    chatProvider: provider(server.providers?.chat),
    searchProvider: provider(server.providers?.search),
    router: provider(server.features?.router) === "off" ? "無効" : provider(server.features?.router),
    research: onOff(server.features?.research),
    shell: onOff(server.features?.shell),
    modelChoices: onOff(server.features?.model_choices),
  };
}

// Client-side facts only the browser knows. `env` is injectable so tests need no browser.
export function clientFacts(env = globalThis) {
  const standalone =
    (typeof env.matchMedia === "function" && env.matchMedia("(display-mode: standalone)").matches) ||
    env.navigator?.standalone === true;
  return {
    secureContext: env.isSecureContext === true,
    installed: standalone,
    online: env.navigator?.onLine !== false,
  };
}

const yesNo = (value) => (value ? "はい" : "いいえ");

export function currentRows(device, facts) {
  return [
    ["ブラウザ", device.browser],
    ["OS", device.os],
    ["接続", device.connection],
    ["通信の暗号化 (https)", yesNo(device.secureTransport)],
    ["セキュアコンテキスト", yesNo(facts.secureContext)],
    ["アプリとしてインストール済み", yesNo(facts.installed)],
    ["ネットワーク", facts.online ? "オンライン" : "オフライン"],
  ];
}

export function serverRows(device) {
  return [
    ["ログイン", device.loginEnabled ? "有効" : "無効（ローカル専用）"],
    ["チャット用プロバイダー", device.chatProvider],
    ["検索プロバイダー", device.searchProvider],
    ["ルーター", device.router],
    ["リサーチ", device.research],
    ["シェルツール", device.shell],
    ["モデル選択", device.modelChoices],
  ];
}

export const NOT_CONNECTED_TITLE = "登録済みの端末と最終接続時刻";
export const NOT_CONNECTED_TEXT =
  "未接続（未実装）: この版には端末の登録も接続履歴もありません。ログインは署名付き Cookie だけで、サーバーは端末を覚えていません。そのため端末一覧と最終接続時刻は表示できません。";

export function statusText({ loaded, error }) {
  if (error === "unauthorized") return "ログインの有効期限が切れました。";
  if (error) return "端末情報を読み込めませんでした。";
  return loaded ? "" : "読み込み中…";
}
