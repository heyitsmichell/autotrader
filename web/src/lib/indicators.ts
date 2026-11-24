export type CloseBar = { time: string | number; close: number };

/** Simple moving average; returns points only once n closes exist. */
export function sma<T extends CloseBar>(bars: T[], n: number): Array<{ time: T["time"]; value: number }> {
  const out: Array<{ time: T["time"]; value: number }> = [];
  let sum = 0;
  for (let i = 0; i < bars.length; i++) {
    sum += bars[i].close;
    if (i >= n) sum -= bars[i - n].close;
    if (i >= n - 1) out.push({ time: bars[i].time, value: sum / n });
  }
  return out;
}

export function lastSma(bars: CloseBar[], n: number): number | null {
  if (bars.length < n) return null;
  return bars.slice(-n).reduce((a, b) => a + b.close, 0) / n;
}

/** Percent return over the last `days` trading days, or null if not enough history. */
export function periodReturn(bars: CloseBar[], days: number): number | null {
  if (bars.length <= days) return null;
  const then = bars[bars.length - 1 - days].close;
  return then > 0 ? (bars[bars.length - 1].close / then - 1) * 100 : null;
}

export const PERIODS = { m3: 63, m6: 126, m12: 252 } as const;
