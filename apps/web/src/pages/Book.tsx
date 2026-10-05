import { RefreshCw } from "lucide-react";
import { useState } from "react";

import { DonutChart } from "@/components/charts/DonutChart";
import { PositionsTable } from "@/components/PositionsTable";
import { Notice } from "@/components/Shell";
import { StatTile } from "@/components/StatTile";
import { Button } from "@/components/ui/button";
import { Cell, Table } from "@/components/Table";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { api, type BookSlice } from "@/lib/api";
import { chainColor } from "@/lib/chains";
import { ago, fineUsd, pct, usd } from "@/lib/format";
import { seriesColor } from "@/lib/palette";
import { useApi } from "@/lib/useApi";

export function Book() {
  // The server's cached read on arrival; the button asks for a fresh one.
  const [fresh, setFresh] = useState(false);
  const book = useApi(() => api.book(fresh));
  const data = book.data;
  const refresh = () => {
    setFresh(true);
    book.reload();
  };

  return (
    <div className="space-y-8">
      <section className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Book</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Every position the Safe holds, read live. APY is the current rate 1Tx reports: descriptive, not
            predictive.
          </p>
        </div>
        <Button variant="outline" className="px-3 py-1.5 text-xs" onClick={refresh} disabled={book.loading}>
          <RefreshCw className={`size-3.5 ${book.loading ? "animate-spin" : ""}`} />
          {data ? `Read ${ago(data.read_at)}` : "Reading…"}
        </Button>
      </section>

      {book.error && <Notice tone="destructive">Could not read the book: {book.error}</Notice>}
      {data && data.warnings.length > 0 && (
        <Notice tone="warning">
          <p className="font-semibold">Some of the book could not be read</p>
          <ul className="mt-1 list-disc pl-5 text-muted-foreground">
            {data.warnings.map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        </Notice>
      )}

      {data && (
        <>
          <section className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            <StatTile label="Deployed" value={usd(data.deployed_usd)} sub={`${data.positions.length} positions`} tone="primary" />
            <StatTile label="Idle" value={usd(data.idle_usd)} />
            <StatTile label="Advertised APY" value={pct(data.blended_apy)} tone="success" />
            <StatTile label="Income at today's rates" value={usd(data.income_per_year_usd)} sub="per year" />
          </section>

          <section className="grid gap-4 lg:grid-cols-2">
            <SliceCard title="By protocol" slices={data.by_protocol} />
            <SliceCard title="By chain" slices={data.by_chain} />
          </section>

          <Card>
            <CardHeader title="Positions" description="Largest first. A levered loop is valued at its equity." />
            <CardContent className="px-0 pb-0">
              <PositionsTable positions={data.positions} />
            </CardContent>
          </Card>
        </>
      )}

      <RewardsCard />
    </div>
  );
}

function RewardsCard() {
  const rewards = useApi(() => api.rewards());
  const data = rewards.data;
  return (
    <Card>
      <CardHeader
        title="Claimable rewards"
        description={
          data
            ? `${usd(data.claimable_usd)} where 1Tx quotes a swap to USDC${data.unpriced ? `; ${data.unpriced} unpriced` : ""} · read ${ago(data.read_at)}.`
            : "Incentives the positions have earned, not yet claimed."
        }
      />
      <CardContent>
        {rewards.error && <p className="text-sm text-destructive">Could not read the rewards: {rewards.error}</p>}
        {!data && !rewards.error && <p className="text-sm text-muted-foreground">Reading…</p>}
        {data && data.errors.length > 0 && (
          <p className="mb-3 text-sm text-warning">Some providers could not be read: {data.errors.join("; ")}</p>
        )}
        {data?.rewards.length === 0 && <p className="text-sm text-muted-foreground">Nothing to claim.</p>}
        {data && data.rewards.length > 0 && (
          <Table head={["Token", "Chain", "Provider", "Claimable", "Pending", "In USDC"]} minWidth={620}>
            {data.rewards.map((reward) => (
              <tr key={`${reward.chain_id}:${reward.provider}:${reward.token}`}>
                <Cell className="font-semibold">{reward.symbol}</Cell>
                <Cell>
                  <span style={{ color: chainColor(reward.chain_id) }}>{reward.chain}</span>
                </Cell>
                <Cell className="text-muted-foreground">{reward.provider}</Cell>
                <Cell className="tabular-nums">{trim(reward.claimable)}</Cell>
                <Cell className="tabular-nums text-muted-foreground">{trim(reward.pending)}</Cell>
                <Cell className="tabular-nums">
                  {reward.usd == null ? (
                    <span className="text-muted-foreground" title={`swap: ${reward.swap_status}`}>
                      unpriced
                    </span>
                  ) : (
                    fineUsd(reward.usd)
                  )}
                </Cell>
              </tr>
            ))}
          </Table>
        )}
      </CardContent>
    </Card>
  );
}

/** A normalized token amount to six decimals, cut rather than rounded. */
function trim(amount: string): string {
  const [whole = "0", fraction = ""] = amount.split(".");
  const kept = fraction.slice(0, 6).replace(/0+$/, "");
  return kept ? `${whole}.${kept}` : whole;
}

function SliceCard({ title, slices }: { title: string; slices: BookSlice[] }) {
  return (
    <Card>
      <CardHeader title={title} />
      <CardContent>
        {slices.length === 0 ? (
          <p className="text-sm text-muted-foreground">Nothing deployed.</p>
        ) : (
          <DonutChart
            segments={slices.map((slice, index) => ({
              label: slice.label,
              sub: usd(slice.usd),
              weightBps: Math.round(slice.weight * 10_000),
              color: seriesColor(index),
            }))}
          />
        )}
      </CardContent>
    </Card>
  );
}
