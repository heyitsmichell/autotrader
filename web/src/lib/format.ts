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

const ET = "America/New_York";
const toDate = (value: unknown): Date | null => {
  if (value == null || value === "") return null;
  const d = value instanceof Date ? value : new Date(typeof value === "number" ? value : String(value));
  return Number.isNaN(d.getTime()) ? null : d;
};
const etFmt = (opts: Intl.DateTimeFormatOptions) => new Intl.DateTimeFormat("en-US", { timeZone: ET, ...opts });
const FMT_DATE = etFmt({ month: "short", day: "numeric" });
const FMT_DATE_Y = etFmt({ month: "short", day: "numeric", year: "numeric" });
const FMT_DT = etFmt({ month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
const FMT_CLOCK = etFmt({ hour: "2-digit", minute: "2-digit" });
const FMT_KEY = new Intl.DateTimeFormat("en-CA", { timeZone: ET, year: "numeric", month: "2-digit", day: "2-digit" });

/** "Nov 26" in US Eastern time; "—" for bad input. */
export const etDate = (value: unknown, opts: { year?: boolean } = {}) => {
  const d = toDate(value);
  return d ? (opts.year ? FMT_DATE_Y : FMT_DATE).format(d) : "—";
};
/** "Nov 26, 09:30 AM" in US Eastern time (no zone suffix; callers label "ET"). */
export const etDateTime = (value: unknown) => {
  const d = toDate(value);
  return d ? FMT_DT.format(d) : "—";
};
/** "09:30 AM" in US Eastern time. */
export const etClock = (value: unknown) => {
  const d = toDate(value);
  return d ? FMT_CLOCK.format(d) : "—";
};
/** YYYY-MM-DD in America/New_York, or "" for bad input. */
export const etDayKey = (value: unknown = Date.now()) => {
  const d = toDate(value);
  return d ? FMT_KEY.format(d) : "";
};
export const isSameEtDay = (a: unknown, b: unknown = Date.now()) => {
  const ka = etDayKey(a);
  return ka !== "" && ka === etDayKey(b);
};
/** Weekdays elapsed between two ET calendar days (start day excluded). */
export const weekdaysSinceEt = (from: unknown, now: unknown = Date.now()): number | null => {
  const ka = etDayKey(from), kb = etDayKey(now);
  if (!ka || !kb) return null;
  const parse = (k: string) => { const [y, m, d] = k.split("-").map(Number); return Date.UTC(y, m - 1, d); };
  let t = parse(ka);
  const end = parse(kb);
  let n = 0;
  while (t < end) {
    t += 86_400_000;
    const w = new Date(t).getUTCDay();
    if (w !== 0 && w !== 6) n++;
  }
  return n;
};

export const dateTime = etDateTime;
export const clockTime = etClock;

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

/** OCC option symbol in words: "QQQ261218P00580000" becomes "QQQ $580 put, Dec 18". Raw symbol when it does not parse. */
export const occLabel = (symbol: unknown): string => {
  const raw = String(symbol ?? "").trim().toUpperCase();
  const m = OCC.exec(raw);
  const month = m ? MONTHS[Number(m[3]) - 1] : undefined;
  if (!m || !month) return String(symbol ?? "—");
  const strike = Number(m[6]) / 1000;
  return `${m[1]} $${Number.isInteger(strike) ? strike : strike.toFixed(2)} ${m[5] === "C" ? "call" : "put"}, ${month} ${Number(m[4])}${2000 + Number(m[2]) === new Date().getFullYear() ? "" : ` ${2000 + Number(m[2])}`}`;
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

/** "XLF 52C Jan 2" from an OCC symbol, or null when it does not parse. */
export const occShort = (symbol: unknown): string | null => {
  const m = OCC.exec(String(symbol ?? "").trim().toUpperCase());
  const month = m ? MONTHS[Number(m[3]) - 1] : undefined;
  if (!m || !month) return null;
  const strike = Number(m[6]) / 1000;
  return `${m[1]} ${Number.isInteger(strike) ? strike : strike.toFixed(2)}${m[5]} ${month} ${Number(m[4])}`;
};

const SHORT_MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
/** "2025-11-26" -> "Nov 26"; input returned unchanged when it does not parse. */
export const shortDate = (d: string | null | undefined, year = false): string => {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(d ?? "");
  return m ? `${SHORT_MON[Number(m[2]) - 1]} ${Number(m[3])}${year ? `, ${m[1]}` : ""}` : d ?? "—";
};
