import { ArrowUpRight } from "lucide-react";

import { AllocationCard } from "@/components/AllocationCard";
import { RealizedChart } from "@/components/charts/RealizedChart";
import { PositionsTable } from "@/components/PositionsTable";
import { Notice } from "@/components/Shell";
import { StatTile } from "@/components/StatTile";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { api, currentLedger } from "@/lib/api";
import { addressUrl, chainColor, chainName } from "@/lib/chains";
import { ago, pct, shortHash, usd, weightPct } from "@/lib/format";
import { useApi } from "@/lib/useApi";

// The 30-second read: what the book holds, what it advertises, what it kept.

export function Overview() {
  const book = useApi(() => api.book());
  const nav = useApi(() => api.nav());
  const realized = currentLedger(nav.data?.days ?? []);

  return (
    <div className="space-y-8">
      <section className="flex flex-wrap items-center gap-2">
        <h1 className="text-2xl font-bold tracking-tight">Overview</h1>
        {book.data && (
          <Badge variant="ghost" className="ml-auto">
            safe {shortHash(book.data.account, 6, 4)}
            {addressUrl(8453, book.data.account) && (
              <a href={addressUrl(8453, book.data.account)!} target="_blank" rel="noreferrer" className="hover:text-foreground">
                <ArrowUpRight className="size-3" />
              </a>
            )}
          </Badge>
        )}
      </section>

      {book.error && <Notice tone="destructive">Could not read the book: {book.error}</Notice>}
      {book.loading && !book.data && <p className="text-sm text-muted-foreground">Reading the book…</p>}

      {book.data && (
        <section className="grid grid-cols-2 gap-3 lg:grid-cols-5">
          <StatTile label="Total value" value={usd(book.data.total_usd)} sub={`${book.data.positions.length} positions`} />
          <StatTile
            label="Deployed"
            value={usd(book.data.deployed_usd)}
            sub={`${weightPct(book.data.deployed_usd / (book.data.total_usd || 1), 1)} of capital`}
            tone="primary"
          />
          <StatTile
            label="Idle"
            value={usd(book.data.idle_usd)}
            sub={`${book.data.idle.filter((b) => b.usd > 0).length} chains`}
            tone={book.data.idle_usd > 1 ? "warning" : "default"}
          />
          <StatTile
            label="Advertised APY"
            value={pct(book.data.blended_apy, 2)}
            sub={`${usd(book.data.income_per_year_usd)}/yr at today's rates`}
            tone="success"
          />
          <StatTile
            label="Effective positions"
            value={book.data.effective_positions?.toFixed(2) ?? "—"}
            sub="concentration, 1/Σw²"
          />
        </section>
      )}

      {realized.length > 0 && (
        <section className="space-y-3">
          <RealizedChart
            days={realized.map((day) => ({
              d: day.day,
              unit_price: day.unit_price,
              yield_usd: day.yield_usd,
              nav_usd: day.nav_usd,
            }))}
          />
          <p className="max-w-3xl px-1 text-sm text-muted-foreground">
            The APY above is what the shelf advertises today. This is what the positions actually returned, read
            from chain at each day's close.{" "}
            <a href="/performance" className="text-primary hover:underline">
              The full record
              <ArrowUpRight className="ml-0.5 inline size-3" />
            </a>
          </p>
        </section>
      )}
      {nav.data && realized.length === 0 && (
        <Notice>
          No NAV history yet.{" "}
          {nav.data.backfilling ? "The backfill is reading past closes now." : "It is filled on start and every hour."}
        </Notice>
      )}

      {book.data && <AllocationCard book={book.data} />}

      {book.data && (
        <Card>
          <CardHeader className="flex-row flex-wrap items-center justify-between gap-4 sm:flex-row">
            <div className="flex flex-col gap-1">
              <CardTitle>Positions</CardTitle>
              <CardDescription>Live balances, priced by 1Tx · read {ago(book.data.read_at)}.</CardDescription>
            </div>
            <a href="/book" className="flex items-center gap-1 text-sm font-semibold text-primary hover:underline">
              Full book <ArrowUpRight className="size-3.5" />
            </a>
          </CardHeader>
          <CardContent className="px-0 pb-0">
            <PositionsTable positions={book.data.positions} />
          </CardContent>
        </Card>
      )}

      {book.data && (
        <Card>
          <CardHeader
            title="Idle capital"
            description="Undeployed USDC per chain. A little is kept per chain for the paymaster."
          />
          <CardContent className="space-y-2">
            {book.data.idle.map((balance) => (
              <div key={balance.chain_id} className="flex items-center justify-between text-sm">
                <span style={{ color: chainColor(balance.chain_id) }}>{chainName(balance.chain_id)}</span>
                <span className="tabular-nums">{usd(balance.usd)}</span>
              </div>
            ))}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
