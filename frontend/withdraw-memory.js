// 「自動承認の撤回」: retires one automatically approved research memory. Only a person's click on
// the Memory detail screen calls withdrawMemory. The POST sends the fixed confirmation header (a
// cross-site page cannot add it) and no body; the server refuses any note that a person approved
// or that is not research-origin. Nothing is deleted. Messages are fixed Japanese strings.
import { sessionEnded } from "./session.js";

export const CONFIRM_HEADER = "X-Jarvis-Confirm";
export const WITHDRAW_LABEL = "この自動承認を撤回";
export const WITHDRAW_CONFIRM =
  "この記憶を撤回します。以後 JARVIS はこの記憶を使いません(記録とノートは残ります)。よろしいですか?";

export const withdrawUrl = (id) => `/api/memory/notes/${encodeURIComponent(id)}/withdraw`;

const FAILURES = {
  unauthorized: "ログインの有効期限が切れました。",
  forbidden: "この操作は許可されませんでした。",
  not_found: "この記憶は見つかりませんでした。",
  refused: "この記憶は撤回できません(自動承認された調査由来の記憶だけが対象です)。",
  unavailable: "記憶の保存先を読み取れません。しばらくしてからもう一度お試しください。",
  network: "サーバーに接続できません。",
  error: "サーバーでエラーが発生しました。",
};

export const withdrawMessage = (kind) => FAILURES[kind] ?? FAILURES.error;

// Resolves to { kind: "ok" } or { kind: <fixed failure word> }; it never throws for server answers.
export async function withdrawMemory(id, { fetchImpl = fetch, signal } = {}) {
  let response;
  try {
    response = await fetchImpl(withdrawUrl(id), {
      method: "POST",
      headers: { Accept: "application/json", [CONFIRM_HEADER]: "1" },
      signal,
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    return { kind: "network" };
  }
  if (response.status === 401) {
    sessionEnded();
    return { kind: "unauthorized" };
  }
  if (response.status === 403) return { kind: "forbidden" };
  if (response.status === 404) return { kind: "not_found" };
  if (response.status === 409) return { kind: "refused" };
  if (!response.ok) return { kind: response.status === 503 ? "unavailable" : "error" };
  return { kind: "ok" };
}
