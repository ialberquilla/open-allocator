import { useMemo, useState, type KeyboardEvent, type PointerEvent } from "react";

import { OK, BAD, MUTED } from "@/lib/palette";
import { fineUsd, signed, usd } from "@/lib/format";

// The realized record: one series at a time, over a chosen window.
//
// Share price is the default and the honest headline. It opens at $100 and flows
// mint units, so a deposit cannot move it, and unlike `twr_cum` it never
// re-bases after an unexplained day, so it draws as one continuous record.
//
// Lines do NOT baseline at zero: the series lives within a few basis points of
// its start, and a zero-anchored axis would draw it flat. A day without a value
// breaks the line instead of being bridged.
//
// Realized APY is the same share price, differenced per bucket and annualized.
// It is gross of gas: the paymaster's charge is not taken off the return.

export type RealizedDay = {
  d: string;
  unit_price: number | null;
  yield_usd: number | null;
  nav_usd: number | null;
};

type Metric = "price" | "apy" | "book";
type Period = "1W" | "1M" | "3M" | "All";

const METRICS: { id: Metric; label: string }[] = [
  { id: "price", label: "Share price" },
  { id: "apy", label: "Realized APY" },
  { id: "book", label: "NAV" },
];

const PERIODS: { id: Period; days: number }[] = [
  { id: "1W", days: 7 },
  { id: "1M", days: 30 },
  { id: "3M", days: 90 },
  { id: "All", days: Infinity },
];

const W = 720;
const H = 240;
const PAD = { top: 16, right: 16, bottom: 28, left: 64 };
const PLOT_W = W - PAD.left - PAD.right;
const PLOT_H = H - PAD.top - PAD.bottom;

const apyPct = (v: number) => signed(v, (n) => `${n.toFixed(2)}%`);
const shortDay = (d: string) => d.slice(5).replace("-", "/");

// Every metric formats its own axis: a share price moving in the fourth decimal
// and a book value in whole dollars cannot share one formatter.
const FORMAT: Record<
  Metric,
  { value: (v: number) => string; tick: (v: number) => string }
> = {
  price: { value: (v) => `$${v.toFixed(4)}`, tick: (v) => v.toFixed(3) },
  apy: { value: apyPct, tick: (v) => `${v.toFixed(1)}%` },
  book: { value: (v) => usd(v), tick: (v) => v.toFixed(2) },
};

/** A window is offered once the record is longer than the window below it —
 *  a 3M window over a few days of history is just "All" under another name. */
function availablePeriods(length: number): Period[] {
  return PERIODS.filter(
    (p, i) => p.id === "All" || i === 0 || length > (PERIODS[i - 1]?.days ?? 0),
  ).map((p) => p.id);
}

/**
 * Simple-annualized return between two closes, in percent. Simple rather than
 * compounded: compounding one good week to a year turns a rounding error into a
 * headline, and this series is read week against week, not as a promise.
 */
function annualized(days: RealizedDay[], from: number, to: number): number | null {
  const start = days[from]?.unit_price;
  const end = days[to]?.unit_price;
  if (start == null || end == null || to <= from) return null;
  return ((end / start - 1) * 365 * 100) / (to - from);
}

type Point = { label: string; value: number | null; note?: string };

/**
 * Buckets of `size` closes ending at the LAST close, so the newest bucket is
 * always complete and any short one is the oldest. Each bucket is measured from
 * the close BEFORE it, so its first day's return counts; the very first bucket
 * of the record has no such close and starts one day in.
 */
function apyBuckets(days: RealizedDay[], windowStart: number, size: number): Point[] {
  const points: Point[] = [];
  for (let end = days.length - 1; end >= windowStart; end -= size) {
    const first = Math.max(windowStart, end - size + 1);
    let from = first - 1;
    // Anchor on the nearest priced close at or before the bucket opens.
    while (from >= 0 && days[from]?.unit_price == null) from -= 1;
    if (from < 0) from = first;
    let to = end;
    while (to > from && days[to]?.unit_price == null) to -= 1;
    const span = end - first + 1;
    points.unshift({
      label:
        size === 1
          ? shortDay(days[end]?.d ?? "")
          : `${shortDay(days[first]?.d ?? "")}–${shortDay(days[end]?.d ?? "")}`,
      value: annualized(days, from, to),
      note: size > 1 && span < size ? `${span} of ${size} days` : undefined,
    });
  }
  return points;
}

export function RealizedChart({ days }: { days: RealizedDay[] }) {
  const periods = availablePeriods(days.length);
  const [metric, setMetric] = useState<Metric>("price");
  const [period, setPeriod] = useState<Period>("All");
  const [active, setActive] = useState<number | null>(null);

  const windowDays = PERIODS.find((p) => p.id === period)?.days ?? Infinity;
  const windowStart = Number.isFinite(windowDays)
    ? Math.max(0, days.length - windowDays)
    : 0;
  const slice = useMemo(() => days.slice(windowStart), [days, windowStart]);

  // A week is the natural bucket; inside a one-week window that would be a
  // single bar, so 1W falls back to one bar per close.
  const bucket = period === "1W" ? 1 : 7;
  const bucketName = bucket === 1 ? "day" : "week";

  const points: Point[] = useMemo(() => {
    if (metric === "apy") return apyBuckets(days, windowStart, bucket);
    return slice.map((day) => ({
      label: shortDay(day.d),
      value: metric === "price" ? day.unit_price : day.nav_usd,
    }));
  }, [metric, days, slice, windowStart, bucket]);

  const bars = metric === "apy";
  const values = points.map((p) => p.value);
  const known = values.filter((v): v is number => v != null && Number.isFinite(v));
  const fmt = FORMAT[metric];

  const earned = slice.reduce((sum, day) => sum + (day.yield_usd ?? 0), 0);
  const unexplained = slice.filter((day) => day.unit_price == null).length;

  const first = known[0];
  const last = known.at(-1);
  const lo = known.length ? Math.min(...known) : 0;
  const hi = known.length ? Math.max(...known) : 0;

  // Bars grow from zero, so zero is in the domain.
  const extent = bars ? [...known, 0] : known;
  const dLo = extent.length ? Math.min(...extent) : 0;
  const dHi = extent.length ? Math.max(...extent) : 0;
  // Pad the domain so the extremes are not welded to the frame, and keep a flat
  // series from collapsing into a zero-height plot.
  const pad = Math.max((dHi - dLo) * 0.12, Math.abs(dHi) * 1e-5, 1e-4);
  const min = bars && dLo === 0 ? 0 : dLo - pad;
  const max = dHi + pad;

  const n = points.length;
  const slot = PLOT_W / Math.max(n, 1);
  const x = (i: number) =>
    bars
      ? PAD.left + slot * (i + 0.5)
      : PAD.left + (n <= 1 ? PLOT_W / 2 : (i / (n - 1)) * PLOT_W);
  const y = (v: number) => PAD.top + (1 - (v - min) / (max - min)) * PLOT_H;

  // Split on nulls: each run is its own line and its own fill.
  const runs: { x: number; y: number }[][] = [];
  values.forEach((v, i) => {
    if (v == null) return void runs.push([]);
    if (runs.length === 0) runs.push([]);
    runs[runs.length - 1]?.push({ x: x(i), y: y(v) });
  });
  const segments = runs.filter((run) => run.length > 0);
  const line = (run: { x: number; y: number }[]) =>
    run
      .map((p, i) => `${i === 0 ? "M" : "L"} ${p.x.toFixed(2)} ${p.y.toFixed(2)}`)
      .join(" ");
  const area = (run: { x: number; y: number }[]) =>
    `${line(run)} L ${run.at(-1)!.x.toFixed(2)} ${PAD.top + PLOT_H} L ${run[0]!.x.toFixed(2)} ${PAD.top + PLOT_H} Z`;

  const change = first != null && last != null ? last - first : null;
  const tone = (change ?? 0) >= 0 ? OK : BAD;
  const changeLabel =
    change == null
      ? "—"
      : metric === "price"
        ? signed((change / first!) * 10_000, (v) => `${v.toFixed(1)} bps`)
        : signed(change, (v) => usd(v));

  // Realized APY over the whole window: one number, measured the same way as
  // the bars, from the close before the window to the last priced close.
  const windowApy = useMemo(() => {
    let from = windowStart - 1;
    while (from >= 0 && days[from]?.unit_price == null) from -= 1;
    if (from < 0) from = windowStart;
    let to = days.length - 1;
    while (to > from && days[to]?.unit_price == null) to -= 1;
    return annualized(days, from, to);
  }, [days, windowStart]);

  const stats: {
    label: string;
    value: string;
    sub?: string;
    subTone?: string;
  }[] = bars
    ? [
        {
          label: `Worst ${bucketName}`,
          value: known.length ? fmt.value(lo) : "—",
        },
        {
          label: `Realized APY · ${period}`,
          value: windowApy == null ? "—" : apyPct(windowApy),
        },
        {
          label: `Best ${bucketName}`,
          value: known.length ? fmt.value(hi) : "—",
        },
      ]
    : [
        { label: `Low · ${period}`, value: known.length ? fmt.value(lo) : "—" },
        {
          label: "Current",
          value: last != null ? fmt.value(last) : "—",
          sub: changeLabel,
          subTone: change == null ? undefined : tone,
        },
        {
          label: `High · ${period}`,
          value: known.length ? fmt.value(hi) : "—",
        },
      ];

  const ticks = bars
    ? [...new Set([max - pad, 0, ...(dLo < 0 ? [min + pad] : [])])]
    : [max - pad, (min + max) / 2, min + pad];
  // At most ~7 x labels, always including the last. Bucket ranges are long, so
  // bars label by the bucket's closing day.
  const every = Math.max(1, Math.ceil(n / 7));
  const labelled = (i: number) =>
    i === n - 1 || (i % every === 0 && n - 1 - i >= every / 2);
  const axisLabel = (p: Point) => p.label.split("–").at(-1)!;
  const showDots = !bars && n <= 31;

  const shownIndex = active ?? n - 1;
  const shown = points[shownIndex];

  function move(event: PointerEvent<SVGSVGElement>) {
    const rect = event.currentTarget.getBoundingClientRect();
    const inPlot = (((event.clientX - rect.left) / rect.width) * W - PAD.left) / PLOT_W;
    const index = bars ? Math.floor(inPlot * n) : Math.round(inPlot * (n - 1));
    setActive(Math.max(0, Math.min(n - 1, index)));
  }

  function key(event: KeyboardEvent<SVGSVGElement>) {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const from = active ?? n - 1;
    setActive(Math.max(0, Math.min(n - 1, from + (event.key === "ArrowRight" ? 1 : -1))));
  }

  const metricLabel = METRICS.find((m) => m.id === metric)!.label;
  const barWidth = Math.min(slot * 0.62, 40);

  return (
    <div className="overflow-hidden rounded-2xl border bg-card/60">
      <div className="flex flex-wrap items-center justify-between gap-3 px-5 pt-5 sm:px-6">
        <p className="text-xs font-bold tracking-widest text-primary uppercase">
          Realized to date
        </p>
        <Segmented
          label="Metric"
          options={METRICS.map((m) => ({ id: m.id, label: m.label }))}
          value={metric}
          onChange={(id) => {
            setMetric(id);
            setActive(null);
          }}
        />
      </div>

      <div className="mt-4 grid grid-cols-3 border-y border-border/60">
        {stats.map((stat, i) => (
          <Stat key={stat.label} {...stat} border={i > 0} />
        ))}
      </div>

      <div className="px-3 pt-3 sm:px-4">
        <div className="flex items-baseline justify-end gap-2 px-2 text-xs">
          <span className="text-muted-foreground">
            {shown?.label}
            {shown?.note && ` · ${shown.note}`}
          </span>
          <span className="font-bold tabular-nums text-foreground">
            {shown?.value == null ? "no close" : fmt.value(shown.value)}
          </span>
        </div>

        {known.length === 0 ? (
          <p className="py-16 text-center text-sm text-muted-foreground">
            No {metricLabel.toLowerCase()} recorded in this window.
          </p>
        ) : (
          <svg
            role="img"
            aria-label={
              bars
                ? `Realized APY per ${bucketName}, ${points[0]?.label} to ${points.at(-1)?.label}`
                : `${metricLabel}, ${points[0]?.label} to ${points.at(-1)?.label}: ${changeLabel}`
            }
            viewBox={`0 0 ${W} ${H}`}
            className="w-full outline-none"
            tabIndex={0}
            onPointerMove={move}
            onPointerLeave={() => setActive(null)}
            onKeyDown={key}
            onBlur={() => setActive(null)}
          >
            <defs>
              <linearGradient id="realized-fill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={tone} stopOpacity="0.22" />
                <stop offset="100%" stopColor={tone} stopOpacity="0" />
              </linearGradient>
            </defs>

            {ticks.map((t) => (
              <g key={t}>
                <line
                  x1={PAD.left}
                  y1={y(t)}
                  x2={W - PAD.right}
                  y2={y(t)}
                  stroke={bars && t === 0 ? MUTED : "hsl(var(--border))"}
                  strokeDasharray={bars && t === 0 ? undefined : "2 4"}
                  opacity={bars && t === 0 ? 1 : 0.6}
                />
                <text
                  x={PAD.left - 10}
                  y={y(t) + 3.5}
                  textAnchor="end"
                  className="fill-muted-foreground text-[10px] tabular-nums"
                >
                  {fmt.tick(t)}
                </text>
              </g>
            ))}

            {bars && active != null && (
              <rect
                x={PAD.left + slot * active}
                y={PAD.top}
                width={slot}
                height={PLOT_H}
                fill="hsl(var(--muted))"
                opacity={0.35}
              />
            )}

            {bars &&
              points.map((p, i) => {
                if (p.value == null) return null;
                const top = y(Math.max(p.value, 0));
                const height = Math.max(Math.abs(y(p.value) - y(0)), 1.5);
                const hovered = i === active;
                return (
                  <g key={p.label}>
                    <rect
                      x={x(i) - barWidth / 2}
                      y={top}
                      width={barWidth}
                      height={height}
                      rx={Math.min(3, barWidth / 4)}
                      fill={p.value < 0 ? BAD : OK}
                      // A short bucket is a partial measurement — shown, but quieter.
                      opacity={p.note ? 0.45 : i === shownIndex ? 1 : 0.85}
                    />
                    {hovered && (
                      <text
                        x={x(i)}
                        y={p.value < 0 ? top + height + 13 : top - 6}
                        textAnchor="middle"
                        className="fill-foreground text-[11px] font-bold tabular-nums"
                      >
                        {fmt.value(p.value)}
                      </text>
                    )}
                  </g>
                );
              })}

            {!bars &&
              segments.map((run, i) => (
                <g key={i}>
                  {run.length > 1 && <path d={area(run)} fill="url(#realized-fill)" />}
                  <path
                    d={line(run)}
                    fill="none"
                    stroke={tone}
                    strokeWidth="2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                </g>
              ))}

            {showDots &&
              values.map((v, i) =>
                v == null ? null : (
                  <circle key={points[i]?.label ?? i} cx={x(i)} cy={y(v)} r="2.5" fill={tone} />
                ),
              )}

            {!bars && active != null && (
              <line
                x1={x(active)}
                y1={PAD.top}
                x2={x(active)}
                y2={PAD.top + PLOT_H}
                stroke={MUTED}
                strokeWidth="1"
              />
            )}
            {!bars && shown?.value != null && (
              <circle
                cx={x(shownIndex)}
                cy={y(shown.value)}
                r="5"
                fill={tone}
                stroke="hsl(var(--card))"
                strokeWidth="2"
              />
            )}

            {points.map((p, i) =>
              labelled(i) ? (
                <text
                  key={`x-${p.label}`}
                  x={x(i)}
                  y={H - 8}
                  textAnchor={
                    bars ? "middle" : i === n - 1 ? "end" : i === 0 ? "start" : "middle"
                  }
                  className={`text-[10px] tabular-nums ${
                    i === shownIndex ? "fill-foreground" : "fill-muted-foreground"
                  }`}
                >
                  {axisLabel(p)}
                </text>
              ) : null,
            )}
          </svg>
        )}
      </div>

      <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border/60 px-5 py-3 text-xs sm:px-6">
        <div className="flex flex-wrap gap-x-5 gap-y-1">
          <span className="text-muted-foreground">
            yield{" "}
            <span className="font-medium tabular-nums text-success">{signed(earned, fineUsd)}</span>
          </span>
          <span className="text-muted-foreground">gross of gas</span>
          {unexplained > 0 && (
            <span className="text-muted-foreground">
              {unexplained} day{unexplained === 1 ? "" : "s"} without a price
            </span>
          )}
        </div>
        <Segmented
          label="Period"
          size="sm"
          options={periods.map((id) => ({ id, label: id }))}
          value={periods.includes(period) ? period : "All"}
          onChange={(id) => {
            setPeriod(id);
            setActive(null);
          }}
        />
      </div>
    </div>
  );
}

function Stat({
  label,
  value,
  sub,
  subTone,
  border,
}: {
  label: string;
  value: string;
  sub?: string;
  subTone?: string;
  border?: boolean;
}) {
  return (
    <div className={`px-5 py-3 sm:px-6 ${border ? "border-l border-border/60" : ""}`}>
      <p className="text-[10px] font-bold tracking-widest text-muted-foreground uppercase">
        {label}
      </p>
      <p className="mt-1 text-sm font-semibold tabular-nums sm:text-base">
        {value}
        {sub && (
          <span className="ml-2 text-xs font-bold" style={{ color: subTone }}>
            {sub}
          </span>
        )}
      </p>
    </div>
  );
}

function Segmented<T extends string>({
  label,
  options,
  value,
  onChange,
  size = "md",
}: {
  label: string;
  options: { id: T; label: string }[];
  value: T;
  onChange: (id: T) => void;
  size?: "sm" | "md";
}) {
  return (
    <div
      role="radiogroup"
      aria-label={label}
      className={
        size === "md"
          ? "flex rounded-lg border border-border/60 bg-background/50 p-0.5"
          : "flex gap-1"
      }
    >
      {options.map((option) => {
        const selected = option.id === value;
        return (
          <button
            key={option.id}
            type="button"
            role="radio"
            aria-checked={selected}
            onClick={() => onChange(option.id)}
            className={`rounded-md font-bold transition-colors ${
              size === "md" ? "px-3 py-1.5 text-xs" : "px-2 py-1 text-[11px]"
            } ${
              selected
                ? "bg-muted text-foreground"
                : "text-muted-foreground hover:text-foreground"
            }`}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}
