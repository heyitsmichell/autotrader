/** CSS tone for a signed number: zero and unknown are neutral (no colour, no arrow). */
export const signTone = (value: unknown): "positive" | "negative" | "" => {
  const n = Number(value);
  if (value == null || !Number.isFinite(n) || n === 0) return "";
  return n > 0 ? "positive" : "negative";
};

const MINUS = "−";
const toNum = (value: unknown): number | null => {
  if (value == null || value === "") return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
};

/** Fixed-decimal number with thousands separators; "—" for null/NaN. */
export const num = (value: unknown, digits = 2) => {
  const n = toNum(value);
  return n == null ? "—" : n.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
};

/** Signed number with a real minus sign and "+" for gains; zero is unsigned. */
export const signed = (value: unknown, digits = 2) => {
  const n = toNum(value);
  if (n == null) return "—";
  const body = num(Math.abs(n), digits);
  if (Number(body.replace(/,/g, "")) === 0) return body;
  return `${n > 0 ? "+" : MINUS}${body}`;
};

/** USD, thousands separators, 2 decimals. Default keeps the legacy "-$1.00" form; { sign: true } gives "+$1.00" / "−$1.00". */
export const money = (value: unknown, opts: { sign?: boolean } = {}) => {
  const n = toNum(value);
  if (n == null) return "—";
  if (!opts.sign) return n.toLocaleString("en-US", { style: "currency", currency: "USD" });
  const body = `$${num(Math.abs(n), 2)}`;
  if (body === "$0.00") return body;
  return `${n > 0 ? "+" : MINUS}${body}`;
};

/** Percent; sign defaults to true (+ gains, real minus losses); digits default 2. */
export const pct = (value: unknown, opts: { sign?: boolean; digits?: number } = {}) => {
  const n = toNum(value);
  if (n == null) return "—";
  const { sign = true, digits = 2 } = opts;
  if (!sign) return `${n < 0 && num(Math.abs(n), digits) !== num(0, digits) ? MINUS : ""}${num(Math.abs(n), digits)}%`;
  return `${signed(n, digits)}%`;
};

export const score = (value: unknown) => {
  if (value == null || value === "") return "—";
  const numeric = Number(value);
  return Number.isNaN(numeric) ? "—" : `${numeric > 0 ? "+" : ""}${numeric.toFixed(2)}`;
};

export const dateTime = (value: unknown) => {
  if (!value) return "—";
  const date = new Date(String(value));
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString("en-US", {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
};

export const clockTime = (value: unknown) => {
  if (!value) return "—";
  const date = new Date(String(value));
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit" });
};

export const minutesAgo = (value: unknown, now = Date.now()) => {
  if (!value) return null;
  const date = new Date(String(value));
  if (Number.isNaN(date.getTime())) return null;
  return Math.max(0, Math.round((now - date.getTime()) / 60_000));
};

export const secondsAgo = (value: unknown, now = Date.now()) => {
  if (!value) return null;
  const date = new Date(String(value));
  if (Number.isNaN(date.getTime())) return null;
  return Math.max(0, Math.round((now - date.getTime()) / 1000));
};

export const countdown = (expiresAt: number, now: number) => {
  const remaining = Math.max(0, expiresAt - now);
  const total = Math.ceil(remaining / 1000);
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
};

const OCC = /^([A-Z.]{1,6})(\d{2})(\d{2})(\d{2})([CP])(\d{8})$/;
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** OCC option symbol in words: "XLF260102C00052000" becomes "XLF Jan 2 $52 call". Raw symbol when it does not parse. */
export const occLabel = (symbol: unknown): string => {
  const raw = String(symbol ?? "").trim().toUpperCase();
  const m = OCC.exec(raw);
  const month = m ? MONTHS[Number(m[3]) - 1] : undefined;
  if (!m || !month) return String(symbol ?? "—");
  const strike = Number(m[6]) / 1000;
  return `${m[1]} ${month} ${Number(m[4])} $${Number.isInteger(strike) ? strike : strike.toFixed(2)} ${m[5] === "C" ? "call" : "put"}`;
};

/** True for an OCC option symbol. */
export const isOccSymbol = (symbol: unknown): boolean => OCC.test(String(symbol ?? "").trim().toUpperCase());

/** Expiry (UTC midnight) encoded in an OCC symbol, or null. */
export const occExpiry = (symbol: unknown): Date | null => {
  const m = OCC.exec(String(symbol ?? "").trim().toUpperCase());
  return m ? new Date(Date.UTC(2000 + Number(m[2]), Number(m[3]) - 1, Number(m[4]))) : null;
};

export const proposalExpiry =(createdAt: string | undefined, ttlMinutes: number | undefined) => {
  if (!createdAt || !ttlMinutes) return null;
  const created = new Date(createdAt);
  if (Number.isNaN(created.getTime())) return null;
  return created.getTime() + ttlMinutes * 60_000;
};
