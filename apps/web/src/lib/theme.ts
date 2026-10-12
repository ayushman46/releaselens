export type Theme = "dark" | "light";

export const THEME_STORAGE_KEY = "rl-theme";

/**
 * Runs in <head> before first paint. The server always renders `class="dark"` (dark-first);
 * this only removes it when the visitor previously chose the light theme.
 */
export const themeInitScript = `(function(){try{if(localStorage.getItem(${JSON.stringify(
  THEME_STORAGE_KEY,
)})==="light"){document.documentElement.classList.remove("dark")}}catch(e){}})();`;

export function applyTheme(theme: Theme): void {
  document.documentElement.classList.toggle("dark", theme === "dark");
  try {
    localStorage.setItem(THEME_STORAGE_KEY, theme);
  } catch {
    // Storage can be unavailable (private mode, blocked site data); the toggle still works.
  }
}
