// The single script every page loads for the shared shell: the header and navigation, and the
// optional service worker. Each page's own script only deals with that page's content.
import { renderShell } from "./nav.js";
import { registerServiceWorker } from "./pwa.js";

renderShell(document, location.pathname);
void registerServiceWorker(window);
