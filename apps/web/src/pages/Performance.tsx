import { RefreshCw } from "lucide-react";
import { useEffect, useState } from "react";

import { RealizedChart } from "@/components/charts/RealizedChart";
import { Notice } from "@/components/Shell";
import { StatTile } from "@/components/StatTile";
import { Cell, Table } from "@/components/Table";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { api, currentLedger, type NavPoint } from "@/lib/api";
import { chainColor } from "@/lib/chains";
import { ago, fineUsd, signed, usd } from "@/lib/format";
import { useApi } from "@/lib/useApi";

// The record, read from chain at every day's close. Unit price opens at 100 and
// flows mint units, so a deposit cannot move it. A day that could not be read
// is a gap with its reason, never a zero.

const percent = (value: number | null | undefined, digits = 2) =>
  value == null ? "—" : signed(value * 100, (n) => `${n.toFixed(digits)}%`);

export function Performance() {
  const nav = useApi(() => api.nav());
  const [starting, setStarting] = useState(false);
  const data = nav.data;

  // While a backfill runs, look again every few seconds.
  useEffect(() => {
    if (!data?.backfilling) return;
    const timer = window.setTimeout(nav.reload, 4000);
    return () => window.clearTimeout(timer);
  }, [data, nav.reload]);

  async function backfill() {
    setStarting(true);
    try {
      await api.backfill();
    } finally {
      setStarting(false);
      nav.reload();
    }
  }

  const days = currentLedger(data?.days ?? []);
  const summary = data?.summary;

  return (
    <div className="space-y-8">
      <section className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Performance</h1>
          <p className="mt-1 max-w-3xl text-sm text-muted-foreground">
            What the positions returned, read from chain at each UTC day's last block. Idle cash is outside NAV;
            money moving in or out of a position is a flow, not a return. Gross of gas.
          </p>
        </div>
        <Button variant="outline" className="px-3 py-1.5 text-xs" onClick={backfill} disabled={starting || data?.backfilling}>
          <RefreshCw className={`size-3.5 ${data?.backfilling ? "animate-spin" : ""}`} />
          {data?.backfilling ? "Reading closes…" : "Read missing days"}
        </Button>
      </section>

      {nav.error && <Notice tone="destructive">{nav.error}</Notice>}

      {summary && (
        <section className="grid grid-cols-2 gap-3 lg:grid-cols-5">
          <StatTile
            label="Unit price"
            value={summary.unit_price?.toFixed(4) ?? "—"}
            sub={summary.since ? `opened at 100 on ${summary.since}` : undefined}
            tone="primary"
          />
          <StatTile label="Return" value={percent(summary.total_return)} sub="since the ledger opened" tone="success" />
          <StatTile
            label="Annualized"
            value={percent(summary.annualized_return)}
            sub={summary.annualized_return == null ? "under a week of history" : "compounded"}
          />
          <StatTile label="Yield earned" value={fineUsd(summary.yield_usd)} sub="gross of gas" />
          <StatTile label="NAV" value={usd(summary.nav_usd)} sub={summary.last_day ? `close of ${summary.last_day}` : undefined} />
        </section>
      )}

      {days.length > 0 && (
        <RealizedChart
          days={days.map((day) => ({ d: day.day, unit_price: day.unit_price, yield_usd: day.yield_usd, nav_usd: day.nav_usd }))}
        />
      )}

      {data && (
        <Card>
          <CardHeader
            title="Coverage"
            description={
              data.last_run
                ? `Last backfill ${ago(data.last_run.finished_at ?? data.last_run.started_at)} (${data.last_run.status}). History starts ${data.start_day ?? "—"}, the day the Safe was first deployed.`
                : "No backfill has run yet."
            }
          />
          <CardContent>
            <Table head={["Chain", "Days read", "Unvalued", "From", "To", "Last error"]} minWidth={640}>
              {data.chains.map((chain) => (
                <tr key={chain.chain_id}>
                  <Cell>
                    <span style={{ color: chainColor(chain.chain_id) }}>{chain.chain}</span>
                  </Cell>
                  <Cell className="tabular-nums">{chain.days_read}</Cell>
                  <Cell className="tabular-nums">{chain.days_unknown}</Cell>
                  <Cell className="tabular-nums text-muted-foreground">{chain.first_day ?? "—"}</Cell>
                  <Cell className="tabular-nums text-muted-foreground">{chain.last_day ?? "—"}</Cell>
                  <Cell className="text-warning">{chain.error ?? ""}</Cell>
                </tr>
              ))}
            </Table>
          </CardContent>
        </Card>
      )}

      {days.length > 0 && (
        <Card>
          <CardHeader title="Daily closes" description="Newest first." />
          <CardContent>
            <Table head={["Day", "NAV", "Flow", "Unit price", "Yield", ""]} minWidth={720}>
              {[...days].reverse().map((day) => (
                <DayRow key={day.day} day={day} />
              ))}
            </Table>
          </CardContent>
        </Card>
      )}
    </div>
  );
}

function DayRow({ day }: { day: NavPoint }) {
  return (
    <tr>
      <Cell className="tabular-nums">{day.day}</Cell>
      <Cell className="tabular-nums">{usd(day.nav_usd)}</Cell>
      <Cell className="tabular-nums text-muted-foreground">
        {day.flow_usd == null || Math.abs(day.flow_usd) < 0.005 ? "—" : signed(day.flow_usd, (n) => usd(n))}
      </Cell>
      <Cell className="tabular-nums">{day.unit_price?.toFixed(4) ?? "—"}</Cell>
      <Cell className={`tabular-nums ${day.yield_usd != null && day.yield_usd < 0 ? "text-destructive" : "text-success"}`}>
        {day.yield_usd == null ? "—" : signed(day.yield_usd, fineUsd)}
      </Cell>
      <Cell>
        {day.status === "opened" && <Badge variant="ghost">opened</Badge>}
        {day.status === "unknown" && (
          <span className="text-xs text-warning" title={day.reason ?? undefined}>
            {day.reason ?? "not read"}
          </span>
        )}
      </Cell>
    </tr>
  );
}
