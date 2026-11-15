/* Applies the saved theme before the first paint, so there is no flash.
   Kept tiny and separate from app.js because app.js is deferred. */
(() => {
  "use strict";

  const KEY = "playify.theme";
  const meta = { dark: "#0a0b10", light: "#f4f5f9" };

  const stored = (() => {
    try {
      return localStorage.getItem(KEY);
    } catch (_) {
      return null;
    }
  })();

  const prefersLight =
    window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches;

  const theme = stored === "light" || stored === "dark" ? stored : prefersLight ? "light" : "dark";

  document.documentElement.dataset.theme = theme;
  const color = document.querySelector('meta[name="theme-color"]');
  if (color) color.setAttribute("content", meta[theme]);
})();
