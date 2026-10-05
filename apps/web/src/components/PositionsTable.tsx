import { PositionIcon } from "@/components/icons";
import { WeightBar } from "@/components/StatTile";
import { Badge } from "@/components/ui/badge";
import { TableScroll } from "@/components/ui/table-scroll";
import type { BookPosition } from "@/lib/api";
import { chainColor, chainName } from "@/lib/chains";
import { pct, usd, weightPct } from "@/lib/format";
import { seriesColor } from "@/lib/palette";

type Column = { label: string; cell: (position: BookPosition) => React.ReactNode; className?: string };

const COLUMNS: Column[] = [
  { label: "APY", cell: (p) => pct(p.apy), className: "font-medium text-primary" },
  {
    label: "$/year",
    cell: (p) => (p.apy == null ? "—" : usd((p.usd * p.apy) / 100)),
    className: "text-muted-foreground",
  },
];

/** The book's positions, largest first. Below `md` each position is a stacked
 *  row carrying the same columns as labelled pairs. APY is 1Tx's current rate:
 *  descriptive, not a promise. */
export function PositionsTable({ positions }: { positions: BookPosition[] }) {
  if (positions.length === 0) {
    return <p className="p-6 text-sm text-muted-foreground">No positions held.</p>;
  }
  return (
    <>
      <ul className="divide-y divide-border/40 md:hidden">
        {positions.map((position, index) => (
          <li key={position.instrument_id} className="space-y-3 px-4 py-4">
            <div className="flex items-start gap-3">
              <PositionName position={position} className="flex-1" />
              <div className="shrink-0 text-right">
                <p className="font-medium tabular-nums">{usd(position.usd)}</p>
                <p className="text-xs tabular-nums text-muted-foreground">{weightPct(position.weight)}</p>
              </div>
            </div>
            <WeightBar weight={position.weight} color={seriesColor(index)} />
            <dl className="grid grid-cols-3 gap-x-3 gap-y-2 text-xs">
              {COLUMNS.map((col) => (
                <div key={col.label} className="min-w-0">
                  <dt className="text-[10px] font-bold tracking-widest text-muted-foreground uppercase">{col.label}</dt>
                  <dd className={`mt-0.5 tabular-nums ${col.className ?? ""}`}>{col.cell(position)}</dd>
                </div>
              ))}
            </dl>
          </li>
        ))}
      </ul>

      <div className="hidden md:block">
        <TableScroll>
          <table className="w-full min-w-[640px] text-sm">
            <thead>
              <tr className="border-b text-[11px] tracking-widest text-muted-foreground uppercase">
                <th className="px-4 py-2.5 text-left font-bold">Position</th>
                <th className="px-4 py-2.5 text-right font-bold">Value</th>
                <th className="px-4 py-2.5 text-right font-bold">% of book</th>
                {COLUMNS.map((col) => (
                  <th key={col.label} className="px-4 py-2.5 text-right font-bold">
                    {col.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {positions.map((position, index) => (
                <tr key={position.instrument_id} className="border-b border-border/40 last:border-0 hover:bg-secondary/30">
                  <td className="px-4 py-3">
                    <PositionName position={position} />
                  </td>
                  <td className="px-4 py-3 text-right font-medium tabular-nums">{usd(position.usd)}</td>
                  <td className="px-4 py-3 text-right tabular-nums">
                    <div className="flex items-center justify-end gap-2">
                      <WeightBar weight={position.weight} color={seriesColor(index)} />
                      <span className="w-14 text-right">{weightPct(position.weight)}</span>
                    </div>
                  </td>
                  {COLUMNS.map((col) => (
                    <td key={col.label} className={`px-4 py-3 text-right tabular-nums ${col.className ?? ""}`}>
                      {col.cell(position)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </TableScroll>
      </div>
    </>
  );
}

function PositionName({ position, className = "" }: { position: BookPosition; className?: string }) {
  return (
    <div className={`flex min-w-0 items-center gap-3 ${className}`}>
      <PositionIcon protocol={position.protocol} chainId={position.chain_id} />
      <div className="min-w-0">
        <p className="truncate font-medium text-foreground">{position.name}</p>
        <p className="flex flex-wrap items-center gap-x-1.5 text-xs text-muted-foreground">
          <span>{position.protocol}</span>
          <span className="text-border">·</span>
          <span style={{ color: chainColor(position.chain_id) }}>{chainName(position.chain_id)}</span>
          {position.yield_token_symbol && (
            <>
              <span className="text-border">·</span>
              <span className="font-mono">{position.yield_token_symbol}</span>
            </>
          )}
          {position.leverage != null && (
            <Badge variant="ghost" className="px-1.5 py-0 text-[10px]">
              levered {position.leverage.toFixed(2)}×
            </Badge>
          )}
        </p>
      </div>
    </div>
  );
}
