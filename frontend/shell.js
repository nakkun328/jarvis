// The single script every page loads for the shared shell: the header and navigation, the logout
// button (only while signed in with login enabled) and the optional service worker. Each page's
// own script only deals with that page's content. The login page does not load this file.
import { renderShell } from "./nav.js";
import { registerServiceWorker } from "./pwa.js";
import { mountLogout } from "./session.js";

renderShell(document, location.pathname);
void mountLogout(document);
void registerServiceWorker(window);
