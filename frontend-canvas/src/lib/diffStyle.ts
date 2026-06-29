import type { ChangeColor } from "./types";

// The Diff Engine's colour convention (spec §1): green=add, red=drop, yellow=change, blue=rename.
// One place maps each to Tailwind classes so the diff list, the canvas tint and the approve summary
// all speak the same visual language (spec §4 — "same visual language as the previous milestones").

export const CHANGE_LABEL: Record<ChangeColor, string> = {
  green: "added",
  red: "removed",
  yellow: "changed",
  blue: "renamed",
};

export const CHANGE_DOT: Record<ChangeColor, string> = {
  green: "bg-emerald-500",
  red: "bg-rose-500",
  yellow: "bg-amber-500",
  blue: "bg-sky-500",
};

export const CHANGE_TEXT: Record<ChangeColor, string> = {
  green: "text-emerald-600 dark:text-emerald-400",
  red: "text-rose-600 dark:text-rose-400",
  yellow: "text-amber-600 dark:text-amber-400",
  blue: "text-sky-600 dark:text-sky-400",
};

/** Left-border + faint background tint for a changed node/row on the canvas. */
export const CHANGE_NODE: Record<ChangeColor, string> = {
  green: "!border-emerald-500 ring-1 ring-emerald-500/40",
  red: "!border-rose-500 ring-1 ring-rose-500/40",
  yellow: "!border-amber-500 ring-1 ring-amber-500/40",
  blue: "!border-sky-500 ring-1 ring-sky-500/40",
};

export const CHANGE_ROW: Record<ChangeColor, string> = {
  green: "bg-emerald-500/10",
  red: "bg-rose-500/10",
  yellow: "bg-amber-500/10",
  blue: "bg-sky-500/10",
};

/** Normalise the engine's free-form colour strings to our four canonical colours. */
export function asChangeColor(color: string): ChangeColor {
  if (color === "green" || color === "red" || color === "yellow" || color === "blue") return color;
  return "yellow";
}
