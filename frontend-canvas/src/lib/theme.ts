export type Theme = "light" | "dark";

const STORAGE_KEY = "vdb-canvas-theme";

/** Initial theme: stored preference, else the OS preference (spec §1 — "respect system preference"). */
export function initialTheme(): Theme {
  const stored = typeof localStorage !== "undefined" ? localStorage.getItem(STORAGE_KEY) : null;
  if (stored === "light" || stored === "dark") return stored;
  if (typeof matchMedia !== "undefined" && matchMedia("(prefers-color-scheme: dark)").matches) {
    return "dark";
  }
  return "light";
}

/** Apply a theme by toggling the `.dark` class on <html>; switching needs no reload (spec §1/§4). */
export function applyTheme(theme: Theme): void {
  const root = document.documentElement;
  // Briefly enable the colour transition only around an explicit switch.
  root.classList.add("theme-anim");
  root.classList.toggle("dark", theme === "dark");
  try {
    localStorage.setItem(STORAGE_KEY, theme);
  } catch {
    /* storage may be unavailable (private mode) — theme still applies for the session */
  }
  window.setTimeout(() => root.classList.remove("theme-anim"), 300);
}
