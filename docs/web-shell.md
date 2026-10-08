# Web shell, Memory screen and PWA

The browser client is plain HTML and JavaScript with no build step and no inline script or style, so a strict Content-Security-Policy would work. Four pages share one shell: chat (`/`), [Tasks](tasks.md) (`/tasks`), [Research](research.md) (`/research`) and Memory (`/memory`). The app is served without authentication, so keep it on `127.0.0.1`.

## Shared layout

- `frontend/nav.js` is the only definition of the page list (`PAGES`) and of the brand and navigation. A page's HTML has an empty `<header class="app-header" data-app-header>` (a page may put its own actions inside it, such as the chat page's 新しい会話 button) and loads `/static/shell.js`. `shell.js` builds the brand and navigation into that header, marks the current page with `aria-current="page"`, and adds a 本文へ移動 skip link that moves focus to `<main>`.
- Adding a page: add one line to `PAGES`, add a route in `backend/api/app.py`, add the HTML file, and add its stylesheet to `SHELL_FILES` in `sw.js` (bump `CACHE_VERSION`). No page copies the navigation.
- `frontend/shell.css` holds the colour tokens (`--bg`, `--text`, `--accent`, ...), base rules, the header and navigation, and the components the read-only screens share (`.readonly-note`, `.badge`, `.tone-*`, `.filters`, `.list-status`). Page CSS files only hold what is specific to that page. The header is `1100px` wide on every page so the navigation does not move between pages.
- The header reserves its height in CSS, so building it from JavaScript does not shift the page.

## Phones (375 px and up)

At 600 px and below the header becomes two rows (brand and page actions, then the navigation across the full width) and stops being sticky so it does not take screen from the content. Buttons, filters and navigation links are at least 44 px tall; inline links inside text are exempt. Small print is raised to at least 12 px. Tasks and Research show one pane at a time (list or detail) below 800 px; Memory is a single column of cards.

## Memory screen (read-only)

`GET /memory` serves `frontend/memory.html`. It lists approved notes and pending candidates (pending and conflict) newest first, each with its status, category, origin (an AI inference is marked and explained in text), confidence, importance, source (provenance), note revision (first 12 characters of the SHA-256 recorded at approval; candidates have none), created and updated times, project, tags, and, for a correction candidate, the note it would replace. A banner says the screen is read-only and that only reviewed, currently approved notes reach the LLM.

There are no approve, reject, edit or retire actions here: those change what the model may read and need a permission and audit design first. Review still happens with the [local review CLI](memory-review.md).

Two things to keep in mind when reading it:

- The text shown for an approved note is the SQLite record from review time. The Obsidian note is canonical and a person may have edited it since, so this screen can differ from the current note and from what retrieval gives the model. The screen says so. It does not read the vault.
- Confidence and importance are the stored scores of the record (a person's or a detector's estimate), not verified facts.

API (`backend/api/memory.py`, GET only; any write method answers 405):

- `GET /api/memory/notes?limit=` returns `{"notes": [...]}` for `approved` records.
- `GET /api/memory/candidates?limit=&status=` returns `{"candidates": [...]}` for `pending` and `conflict` records (`status=pending` or `status=conflict` narrows it).
- `limit` is 1 to 100 (default 50), newest first by creation time. Each item has exactly: `id`, `status`, `category`, `content`, `source`, `origin`, `importance`, `confidence`, `tags`, `project`, `revision`, `supersedes_id`, `created_at`, `updated_at`. No file path, vault location, or internal column is exposed.
- Errors carry a fixed `detail` code and never repeat the request or stored text: `invalid_limit` (422), `invalid_status` (422), `storage_unavailable` (503, also for a row that no longer parses).

Memory text is untrusted data: it is placed only in JSON strings and shown with `textContent`.

`scripts/dev_memory_demo_server.py` is a dev/test-only harness (not used by the app or the gates): it starts the real app on `127.0.0.1:18931` with a new temporary database and seeds records only through `MemoryRepository`, including text that looks like HTML. It never reads `.env`, a vault or an existing database.

## PWA (installable, conservative)

`/manifest.webmanifest` (name, icons, `scope` and `start_url` `/`, standalone) and PNG/SVG icons under `/static/icons/` make the app installable. `/sw.js` is served from the root with `Service-Worker-Allowed: /` and `Cache-Control: no-cache` so its scope is `/` and a fixed worker reaches users.

The service worker is intentionally small:

- It precaches only the static app-shell files listed in `SHELL_FILES` (stylesheets, the shell scripts and icons) in a versioned cache (`jarvis-shell-vN`), and deletes caches of older versions when a new worker activates.
- For exactly those files it asks the network first and falls back to the cached copy only when the network fails.
- It never touches anything else. `/api/*`, event streams, page navigations (HTML), non-GET requests and other origins are not intercepted at all and go straight to the network. No HTML is ever cached because pages embed stored data.
- `pwa.js` registers it only where the browser allows it and the origin is trustworthy: a secure context (https) or localhost, `127.0.0.1`, `::1`.

Offline use is not supported. Without a connection the app does not open and shows the browser's own error page; the cache is a fallback for static files, not an offline mode. The server must also be reachable for every API call.

A later login layer must not be cached: authenticated pages and responses must keep being served uncached and outside the service worker (do not add them to `SHELL_FILES`, and keep `Cache-Control: no-store` on them). If a login screen or session endpoint is added, review `sw.js` in the same change so that it, and anything that depends on a session, stays out of the cache.
