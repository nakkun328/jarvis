// The one definition of the app's pages and of the header every page shares.
//
// Adding a screen is one line in PAGES (plus its route and HTML file). The header, the navigation,
// the current-page marker and the skip link are all built from that list, so no page copies them.
// Text is only ever placed with textContent; nothing here parses markup.

export const BRAND = { name: "JARVIS", tagline: "Personal assistant", mark: "J" };

export const PAGES = [
  { path: "/", label: "チャット" },
  { path: "/tasks", label: "タスク" },
  { path: "/approvals", label: "承認" },
  { path: "/research", label: "リサーチ" },
  { path: "/memory", label: "記憶" },
];

export const NAV_LABEL = "画面の切り替え";
export const SKIP_LABEL = "本文へ移動";
const MAIN_ID = "main-content";

// "/tasks/" and "/tasks" are the same page; the empty path is the root.
export function normalizePath(pathname) {
  if (typeof pathname !== "string" || pathname === "") return "/";
  const trimmed = pathname.length > 1 ? pathname.replace(/\/+$/, "") : pathname;
  return trimmed === "" ? "/" : trimmed;
}

// Exactly one item is current when the path is a known page, none otherwise.
export function navModel(pathname, pages = PAGES) {
  const here = normalizePath(pathname);
  return pages.map((page) => ({ path: page.path, label: page.label, current: page.path === here }));
}

function make(doc, tag, className, text) {
  const node = doc.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

export function buildBrand(doc) {
  const brand = make(doc, "div", "brand");
  brand.setAttribute("aria-label", BRAND.name);
  const mark = make(doc, "span", "brand-mark", BRAND.mark);
  mark.setAttribute("aria-hidden", "true");
  const text = make(doc, "div");
  text.append(make(doc, "strong", "", BRAND.name), make(doc, "span", "", BRAND.tagline));
  brand.append(mark, text);
  return brand;
}

export function buildNav(doc, pathname, pages = PAGES) {
  const nav = make(doc, "nav", "app-nav");
  nav.setAttribute("aria-label", NAV_LABEL);
  for (const item of navModel(pathname, pages)) {
    const link = make(doc, "a", "nav-link", item.label);
    link.setAttribute("href", item.path);
    if (item.current) link.setAttribute("aria-current", "page");
    nav.append(link);
  }
  return nav;
}

// Fills the page's <header data-app-header> (any actions the page put there stay after the
// navigation) and adds a skip link that jumps to the page's <main>. Safe to call twice.
export function renderShell(doc, pathname) {
  const header = doc.querySelector("[data-app-header]");
  if (!header || header.querySelector(".app-nav")) return false;
  header.prepend(buildBrand(doc), buildNav(doc, pathname));
  const main = doc.querySelector("main");
  if (main) {
    if (!main.id) main.id = MAIN_ID;
    main.setAttribute("tabindex", "-1");
    const skip = make(doc, "a", "skip-link", SKIP_LABEL);
    skip.setAttribute("href", `#${main.id}`);
    doc.body.prepend(skip);
  }
  return true;
}
