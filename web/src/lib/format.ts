/** CSS tone for a signed number: zero and unknown are neutral (no colour, no arrow). */
export const signTone = (value: unknown): "positive" | "negative" | "" => {
  const n = Number(value);
  if (value == null || !Number.isFinite(n) || n === 0) return "";
  return n > 0 ? "positive" : "negative";
};

export const money = (value: unknown) => {
  if (value == null || value === "") return "—";
  const numeric = Number(value);
  if (Number.isNaN(numeric)) return "—";
  return numeric.toLocaleString("en-US", { style: "currency", currency: "USD" });
};

export const pct = (value: unknown) => {
  if (value == null || value === "") return "—";
  const numeric = Number(value);
  return Number.isNaN(numeric) ? "—" : `${numeric > 0 ? "+" : ""}${numeric.toFixed(2)}%`;
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

export const shortTime = (value: unknown) => {
  if (!value) return "—";
  const date = new Date(String(value));
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
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

export const proposalExpiry = (createdAt: string | undefined, ttlMinutes: number | undefined) => {
  if (!createdAt || !ttlMinutes) return null;
  const created = new Date(createdAt);
  if (Number.isNaN(created.getTime())) return null;
  return created.getTime() + ttlMinutes * 60_000;
};
