import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

export function StatTile({
  label,
  value,
  sub,
  tone = "default",
  className,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  tone?: "default" | "primary" | "success" | "warning" | "destructive";
  className?: string;
}) {
  const toneClass = {
    default: "text-foreground",
    primary: "text-primary",
    success: "text-success",
    warning: "text-warning",
    destructive: "text-destructive",
  }[tone];

  return (
    <div className={cn("rounded-xl border bg-card p-4", className)}>
      <p className="text-[11px] font-bold tracking-widest text-muted-foreground uppercase">
        {label}
      </p>
      <p className={cn("mt-1.5 text-2xl font-bold tabular-nums", toneClass)}>{value}</p>
      {sub && <p className="mt-1 text-xs text-muted-foreground">{sub}</p>}
    </div>
  );
}

/** A thin proportion bar, used inside table rows where a chart would be noise. */
export function WeightBar({ weight, color }: { weight: number; color?: string }) {
  return (
    <span className="inline-block h-1.5 w-full max-w-24 overflow-hidden rounded-full bg-muted align-middle">
      <span
        className="block h-full rounded-full"
        style={{
          width: `${Math.min(100, Math.max(0, weight * 100))}%`,
          backgroundColor: color ?? "hsl(var(--primary))",
        }}
      />
    </span>
  );
}
