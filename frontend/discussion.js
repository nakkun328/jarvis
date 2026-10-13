import { createScreen } from "./discussion-screen.js";

const screen = createScreen({
  doc: document,
  search: location.search,
  schedule: (fn, ms) => setTimeout(fn, ms),
  cancel: (handle) => clearTimeout(handle),
  isHidden: () => document.hidden,
  replaceUrl: (url) => history.replaceState(null, "", url),
  navigate: (url) => location.assign(url),
});
void screen.init();
