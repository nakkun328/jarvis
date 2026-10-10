// Login-session handling shared by every screen except the login page.
//
// Two jobs, both inert when login is off:
//   * a logout button in the shared header, shown only when GET /api/auth/status says this browser
//     is signed in (with login off that route does not exist, so nothing is added);
//   * one redirect to /login when a background request is refused with 401 (an expired or removed
//     session), instead of a generic error. The redirect happens at most once per page load and
//     never from the login page itself, so it cannot loop.
// Text is only ever set with textContent. Nothing is stored in the browser.

export const LOGIN_PATH = "/login";
export const STATUS_URL = "/api/auth/status";
export const LOGOUT_URL = "/api/auth/logout";
export const LOGOUT_LABEL = "ログアウト";
export const LOGOUT_BUSY_LABEL = "ログアウト中…";
export const LOGOUT_FAILED_LABEL = "ログアウトできませんでした。もう一度";
export const SESSION_ENDED_MESSAGE = "ログインの有効期限が切れました。ログイン画面に移動します。";

const BUTTON_CLASS = "logout-button";

function pathOf(location) {
  const raw = typeof location?.pathname === "string" ? location.pathname : "";
  return raw.length > 1 ? raw.replace(/\/+$/, "") : raw;
}

// One guard per page. `getLocation` is injectable so tests need no browser.
export function createSessionGuard(getLocation = () => globalThis.location) {
  let leaving = false;
  return {
    get leaving() {
      return leaving;
    },
    // Forgets a past redirect. Only for tests; a real page is left by the redirect itself.
    reset() {
      leaving = false;
    },
    // Sends the browser to the login page. Returns true only for the call that did it.
    sessionEnded() {
      const location = getLocation();
      if (leaving || !location || typeof location.replace !== "function") return false;
      if (pathOf(location) === LOGIN_PATH) return false;
      leaving = true;
      location.replace(LOGIN_PATH);
      return true;
    },
  };
}

export const pageGuard = createSessionGuard();

export const sessionEnded = () => pageGuard.sessionEnded();

// For fetch helpers: true (after starting the redirect) when the response is a 401.
export function refusedAsUnauthorized(response) {
  if (response?.status !== 401) return false;
  pageGuard.sessionEnded();
  return true;
}

// True only when login is on and this browser holds a valid session.
export async function fetchAuthenticated(fetchImpl = globalThis.fetch) {
  try {
    const response = await fetchImpl(STATUS_URL, {
      headers: { Accept: "application/json" },
      credentials: "same-origin",
    });
    if (!response.ok) return false;
    const body = await response.json();
    return body !== null && typeof body === "object" && body.authenticated === true;
  } catch {
    return false;
  }
}

// True when the session is gone afterwards: logged out now, or it had already ended (401).
export async function requestLogout(fetchImpl = globalThis.fetch) {
  try {
    const response = await fetchImpl(LOGOUT_URL, {
      method: "POST",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    return response.ok || response.status === 401;
  } catch {
    return false;
  }
}

export function buildLogoutButton(doc, { onLogout }) {
  const button = doc.createElement("button");
  button.className = BUTTON_CLASS;
  button.textContent = LOGOUT_LABEL;
  button.setAttribute("type", "button");
  button.addEventListener("click", () => onLogout(button));
  return button;
}

// Adds the logout button to the header once, when (and only when) login is on and this browser is
// signed in. Safe to call twice; resolves to whether a button is present.
export async function mountLogout(
  doc,
  { fetchImpl = globalThis.fetch, location = globalThis.location, guard = pageGuard } = {},
) {
  const header = doc.querySelector("[data-app-header]");
  if (!header) return false;
  if (header.querySelector(`.${BUTTON_CLASS}`)) return true;
  if (!(await fetchAuthenticated(fetchImpl))) return false;
  if (guard.leaving || header.querySelector(`.${BUTTON_CLASS}`)) return !guard.leaving;
  const button = buildLogoutButton(doc, {
    async onLogout(control) {
      control.disabled = true;
      control.textContent = LOGOUT_BUSY_LABEL;
      if (await requestLogout(fetchImpl)) {
        location.replace(LOGIN_PATH);
        return;
      }
      control.disabled = false;
      control.textContent = LOGOUT_FAILED_LABEL;
    },
  });
  header.append(button);
  return true;
}
