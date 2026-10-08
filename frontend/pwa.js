// Service worker registration, deliberately conservative.
//
// The worker (sw.js) only keeps a fallback copy of the static app-shell files. It never touches
// /api/*, event streams or pages, and offline use is not supported (docs/web-shell.md). It is
// registered only where the browser allows it and the origin is trustworthy: a secure context
// (https) or the local machine (localhost, 127.0.0.1, ::1).

export const WORKER_URL = "/sw.js";
export const WORKER_SCOPE = "/";

const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]", "::1"]);

export function canRegister({ isSecureContext, hostname, hasServiceWorker }) {
  if (!hasServiceWorker) return false;
  return isSecureContext === true || LOCAL_HOSTS.has(String(hostname ?? "").toLowerCase());
}

export function registerServiceWorker(env = globalThis) {
  const navigatorRef = env.navigator;
  const allowed = canRegister({
    isSecureContext: env.isSecureContext,
    hostname: env.location?.hostname,
    hasServiceWorker: Boolean(navigatorRef) && "serviceWorker" in navigatorRef,
  });
  if (!allowed) return Promise.resolve(null);
  // Wait for the page to finish loading so registration never competes with the first render.
  const register = () =>
    navigatorRef.serviceWorker
      .register(WORKER_URL, { scope: WORKER_SCOPE })
      .catch(() => null);
  if (env.document?.readyState === "complete") return register();
  return new Promise((resolve) => {
    env.addEventListener("load", () => resolve(register()), { once: true });
  });
}
