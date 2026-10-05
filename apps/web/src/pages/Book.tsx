import { RefreshCw } from "lucide-react";
import { useState } from "react";

import { DonutChart } from "@/components/charts/DonutChart";
import { PositionsTable } from "@/components/PositionsTable";
import { Notice } from "@/components/Shell";
import { StatTile } from "@/components/StatTile";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { api, type BookSlice } from "@/lib/api";
import { ago, pct, usd } from "@/lib/format";
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
    </div>
  );
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
