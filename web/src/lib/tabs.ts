export type Tab = "today" | "strategies" | "options" | "research" | "learning" | "backtests" | "settings";

/** Single source of truth for tabs. `hidden` tabs stay routable via #hash but are not shown in the bar. */
export const TABS: { id: Tab; label: string; hidden?: boolean }[] = [
  { id: "today", label: "Today" },
  { id: "strategies", label: "Strategies" },
  { id: "research", label: "Research" },
  { id: "options", label: "Options", hidden: true },
  { id: "learning", label: "Results" },
  { id: "backtests", label: "Backtests" },
  { id: "settings", label: "Settings" },
];
