import { useState } from "react";

import { DonutChart } from "@/components/charts/DonutChart";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import type { Book } from "@/lib/api";
import { chainName } from "@/lib/chains";
import { usd } from "@/lib/format";
import { seriesColor } from "@/lib/palette";

// Allocation as one ring, sliced by position, protocol or chain. Weights are of
// deployed capital: idle USDC is a funding state, not an allocation.

type View = "position" | "protocol" | "chain";

const VIEWS: { id: View; label: string }[] = [
  { id: "position", label: "Position" },
  { id: "protocol", label: "Protocol" },
  { id: "chain", label: "Chain" },
];

export function AllocationCard({ book }: { book: Book }) {
  const [view, setView] = useState<View>("position");
  const rows =
    view === "position"
      ? book.positions.map((p) => ({ label: p.name, sub: `${p.protocol} · ${chainName(p.chain_id)}`, weight: p.weight }))
      : (view === "protocol" ? book.by_protocol : book.by_chain).map((s) => ({
          label: s.label,
          sub: usd(s.usd),
          weight: s.weight,
        }));
  return (
    <Card>
      <CardHeader className="flex-row flex-wrap items-start justify-between gap-3 sm:flex-row">
        <div className="flex flex-col gap-1">
          <CardTitle>Allocation</CardTitle>
          <CardDescription>Of {usd(book.deployed_usd)} deployed; idle cash is not a slice.</CardDescription>
        </div>
        <div role="radiogroup" aria-label="Slice by" className="flex rounded-lg border border-border/60 bg-background/50 p-0.5">
          {VIEWS.map((option) => (
            <button
              key={option.id}
              type="button"
              role="radio"
              aria-checked={option.id === view}
              onClick={() => setView(option.id)}
              className={`rounded-md px-3 py-1.5 text-xs font-bold transition-colors ${
                option.id === view ? "bg-muted text-foreground" : "text-muted-foreground hover:text-foreground"
              }`}
            >
              {option.label}
            </button>
          ))}
        </div>
      </CardHeader>
      <CardContent>
        {rows.length === 0 ? (
          <p className="text-sm text-muted-foreground">Nothing deployed.</p>
        ) : (
          <DonutChart
            size={150}
            centerLabel={usd(book.deployed_usd, 0)}
            centerSub="deployed"
            segments={rows.map((row, index) => ({
              label: row.label,
              sub: row.sub,
              weightBps: Math.round(row.weight * 10_000),
              color: seriesColor(index),
            }))}
          />
        )}
      </CardContent>
    </Card>
  );
}
