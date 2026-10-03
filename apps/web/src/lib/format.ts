// Formatting rules in one place, so a number means the same thing everywhere.
// Unknown stays unknown: a missing value renders as an em dash, never as zero.

import type { TokenAmount } from "@/lib/api";

export const usd = (value: number | null | undefined, digits = 2): string =>
  value == null || !Number.isFinite(value)
    ? "—"
    : `$${value.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;

/** A token amount in its own units. Long decimals are cut, not rounded up. */
export const tokenAmount = (value: TokenAmount | null | undefined): string => {
  if (!value) return "—";
  const symbol = value.symbol ?? "units";
  if (value.raw === "max") return `all ${symbol}`;
  if (value.amount == null) return `${value.raw} raw ${symbol}`;
  const [whole = "0", fraction = ""] = value.amount.split(".");
  const grouped = Number(whole).toLocaleString("en-US");
  const kept = fraction.slice(0, 6).replace(/0+$/, "");
  return `${kept ? `${grouped}.${kept}` : grouped} ${symbol}`;
};

export const shortHash = (hash: string, lead = 8, tail = 6): string =>
  hash.length <= lead + tail ? hash : `${hash.slice(0, lead)}…${hash.slice(-tail)}`;

export const dateTime = (iso: string | null | undefined): string => {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
};

/** "4:05" for a remaining duration in milliseconds. */
export const countdown = (ms: number): string => {
  const total = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
};

/** "loop_open" → "Loop open". */
export const label = (value: string): string => {
  const words = value.replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
};
