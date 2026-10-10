# Web shell, Memory screen and PWA

The browser client is plain HTML and JavaScript with no build step and no inline script or style, so a strict Content-Security-Policy would work. Five pages share one shell: chat (`/`), [Tasks](tasks.md) (`/tasks`), [Approvals](tool-confirmation.md) (`/approvals`), [Research](research.md) (`/research`) and Memory (`/memory`). Without the [login layer](auth.md) the app is served without authentication, so keep it on `127.0.0.1`; with it, a logout button appears in the header (below).

## Shared layout

- `frontend/nav.js` is the only definition of the page list (`PAGES`) and of the brand and navigation. A page's HTML has an empty `<header class="app-header" data-app-header>` (a page may put its own actions inside it, such as the chat page's 新しい会話 button) and loads `/static/shell.js`. `shell.js` builds the brand and navigation into that header, marks the current page with `aria-current="page"`, and adds a 本文へ移動 skip link that moves focus to `<main>`.
- Adding a page: add one line to `PAGES`, add a route in `backend/api/app.py`, add the HTML file, and add its stylesheet to `SHELL_FILES` in `sw.js` (bump `CACHE_VERSION`). No page copies the navigation.
- `frontend/shell.css` holds the colour tokens (`--bg`, `--text`, `--accent`, ...), base rules, the header and navigation, and the components the read-only screens share (`.readonly-note`, `.badge`, `.tone-*`, `.filters`, `.list-status`). Page CSS files only hold what is specific to that page. The header is `1100px` wide on every page so the navigation does not move between pages.
- The header reserves its height in CSS, so building it from JavaScript does not shift the page.

## Phones (375 px and up)

At 600 px and below the header becomes two rows (brand and page actions, then the navigation across the full width) and stops being sticky so it does not take screen from the content. Buttons, filters and navigation links are at least 44 px tall; inline links inside text are exempt. Small print is raised to at least 12 px. Tasks and Research show one pane at a time (list or detail) below 800 px; Memory is a single column of cards.

## Login session in the header

`frontend/session.js` (loaded by `shell.js`, never by the login page) does two things, both inert when login is off:

- It asks `GET /api/auth/status`. Only when the answer is `{"authenticated": true}` it adds a **ログアウト** button to the header; the button sends `POST /api/auth/logout` and goes to `/login`. With login off the route does not exist (404), so there is no button.
- The chat page keeps only the current conversation id in `localStorage` and restores that conversation when it is opened again, so moving to another page and back no longer starts a new conversation (see [chat](chat.md#restoring-a-conversation)). Chat reply links to `/research#<id>` open in a new tab.
- Fetch helpers (`chat-api.js`, `tasks-api.js`, `approvals-api.js`, `research-api.js`, `research-run-api.js`, `memory-api.js`, `tasks-stream.js`) call `sessionEnded()` when a response is `401`: the page goes to `/login` once (never from `/login` itself, never a second time) and the screen shows a fixed "session ended" message while it leaves. A task event stream stops instead of reconnecting.

## Memory screen (read-only)

`GET /memory` serves `frontend/memory.html`. It lists approved notes and pending candidates (pending and conflict) newest first, each with its status, category, origin (an AI inference is marked and explained in text), confidence, importance, source (provenance), note revision (first 12 characters of the SHA-256 recorded at approval; candidates have none), created and updated times, project, tags, and, for a correction candidate, the note it would replace. Each card has a 詳細を見る button, and a search box filters both lists (below). A banner says the screen is read-only and that only reviewed, currently approved notes reach the LLM.

There are no approve, reject, edit or retire actions here: those change what the model may read and need a permission and audit design first. Review still happens with the [local review CLI](memory-review.md).

### Search and detail

- The search box (debounced 300 ms; Enter searches at once, Escape or クリア clears) asks both lists with `?q=`. Words are separated by spaces and **all** must appear in the content, project, category or a tag; English letters match regardless of case, other scripts match exactly. A query the API would refuse (over 100 characters, control or line-break characters, more than 8 words) is explained on the page and not sent. The search covers the stored review records only, not the current Obsidian text.
- A card's 詳細を見る opens a detail page in the same screen (`/memory#<id>`: the address bar decides, so Back and a copied link work; focus moves to the heading and returns to the button on the way back; the 一覧に戻る button leaves). It shows the full record, the full note revision, the links to the note it corrects or the note that replaced it (each opens that record), and a time-ordered history of review decisions and replacements with their revisions. Who decided and why (free text typed at the review CLI) is not exposed.
- Everything is still read-only. There is no approve, reject, edit, retire or delete action anywhere.

Two things to keep in mind when reading it:

- The text shown for an approved note is the SQLite record from review time. The Obsidian note is canonical and a person may have edited it since, so this screen can differ from the current note and from what retrieval gives the model. The screen says so. It does not read the vault.
- Confidence and importance are the stored scores of the record (a person's or a detector's estimate), not verified facts.

API (`backend/api/memory.py`, GET only; any write method answers 405):

- `GET /api/memory/notes?limit=` returns `{"notes": [...]}` for `approved` records.
- `GET /api/memory/candidates?limit=&status=` returns `{"candidates": [...]}` for `pending` and `conflict` records (`status=pending` or `status=conflict` narrows it).
- `?q=` on either list: a bounded search (see *Search and detail*). `q` is at most 100 characters; control, surrogate, line- and paragraph-separator characters and more than 8 words are refused with `422 invalid_query`. Blank means no search. Matching is a plain, case-insensitive-for-ASCII substring test with the word bound as a parameter (`instr`), so `%`, `_`, quotes and backslashes mean only themselves; it covers content, project, category and each tag, newest first, with the same `limit`.
- `GET /api/memory/notes/{id}` returns one record that was ever approved (`approved`, `superseded` or `retired`) and `GET /api/memory/candidates/{id}` one `pending` or `conflict` record. The id must be the canonical lowercase hyphenated UUID; any other spelling, an unknown id, or a record in another state answers `404 {"detail":"not_found"}`. The body is the list item plus `replaced_by_id` (the note that took its place), `lifecycle` (`action`, `related_id`, `occurred_at`, `revision` of replacements and retirements) and `reviews` (`action`, `previous_status`, `new_status`, `occurred_at`, `revision`). The actor and reason columns are not exposed.
- `limit` is 1 to 100 (default 50), newest first by creation time. Each item has exactly: `id`, `status`, `category`, `content`, `source`, `origin`, `importance`, `confidence`, `tags`, `project`, `revision`, `supersedes_id`, `created_at`, `updated_at`. No file path, vault location, or internal column is exposed.
- Errors carry a fixed `detail` code and never repeat the request or stored text: `invalid_limit` (422), `invalid_status` (422), `invalid_query` (422), `not_found` (404, on the detail routes), `storage_unavailable` (503, also for a row that no longer parses).

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

The login layer must not be cached: authenticated pages and responses are served uncached and outside the service worker (do not add them to `SHELL_FILES`, and keep `Cache-Control: no-store` on them). `SHELL_FILES` holds only static code that contains no user data (`session.js` is one of them). A `401`, a redirect, an HTML answer or a `no-store` response is never written to the cache, an install that gets refused fails instead of caching the refusal, and the cached copy answers only when the network itself fails, never a refusal.

### With login enabled

The browser fetches `/manifest.webmanifest` and the icons without the session cookie, so they would be `401` and the app could not be installed. They are therefore public by **exact path** (`/manifest.webmanifest`, `/static/icons/icon.svg`, `/static/icons/icon-192.png`, `/static/icons/icon-512.png` and `/static/icons/icon-maskable-512.png`; no directory or prefix rule), as they hold nothing sensitive. `crossorigin="use-credentials"` on the manifest link was not used because it would not cover the icon and install-time fetches. `/sw.js` and the shell scripts keep requiring the session. The reasoning and threat model are in [auth](auth.md#login-and-the-installable-app-pwa).

## Static file caching

`/static/*` is served with `Cache-Control: no-cache`: the browser keeps its copy but must revalidate it (a cheap `304` through the ETag/Last-Modified validators) before every use. The pages are made of many small ES modules that import each other; with the default heuristic caching a browser could keep an old module next to a new one after an update, which breaks the whole import graph (blank activity view, missing model selector). The service worker's shell files are versioned separately (see `CACHE_VERSION` in `frontend/sw.js`).
