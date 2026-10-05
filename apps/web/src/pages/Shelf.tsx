import { ArrowDown, ArrowUp, RefreshCw, Search } from "lucide-react";
import { useMemo, useState } from "react";

import { PositionIcon } from "@/components/icons";
import { Notice } from "@/components/Shell";
import { StatTile, WeightBar } from "@/components/StatTile";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { TableScroll } from "@/components/ui/table-scroll";
import { api, type ShelfVault } from "@/lib/api";
import { chainColor, chainName } from "@/lib/chains";
import { ago, pct, signed, usd } from "@/lib/format";
import { useApi } from "@/lib/useApi";
import { cn } from "@/lib/utils";

type SortKey = "score" | "apy" | "realized_apy" | "tvl_usd" | "sharpe" | "volatility" | "history_days";

type Column = {
  key: SortKey;
  label: string;
  title: string;
  cell: (vault: ShelfVault) => React.ReactNode;
  className?: string;
};

const compactUsd = (value: number): string =>
  value >= 1e9
    ? `$${(value / 1e9).toFixed(2)}B`
    : value >= 1e6
      ? `$${(value / 1e6).toFixed(1)}M`
      : value >= 1e3
        ? `$${(value / 1e3).toFixed(0)}K`
        : usd(value, 0);

const fixed = (value: number | null | undefined, digits = 2): string =>
  value == null || !Number.isFinite(value) ? "—" : value.toFixed(digits);

const COLUMNS: Column[] = [
  {
    key: "apy",
    label: "APY",
    title: "Advertised by 1Tx today; a fixed-term row's is the rate locked to maturity",
    cell: (v) => <ApyCell vault={v} />,
  },
  {
    key: "realized_apy",
    label: "Realized",
    title: "Compounded mean of the APY history, and its gap to the advertised mean",
    cell: (v) => (
      <div>
        <p>{pct(v.realized_apy)}</p>
        {v.delivery_gap != null && Math.abs(v.delivery_gap) >= 0.005 && (
          <p className="text-[11px] text-muted-foreground">{signed(v.delivery_gap, (n) => `${n.toFixed(2)} pts`)}</p>
        )}
      </div>
    ),
  },
  { key: "tvl_usd", label: "TVL", title: "Total value locked", cell: (v) => compactUsd(v.tvl_usd) },
  { key: "sharpe", label: "Sharpe", title: "Of the APY history, over a 3% risk-free rate", cell: (v) => fixed(v.sharpe) },
  {
    key: "volatility",
    label: "APY vol",
    title: "Standard deviation of the APY, in percentage points",
    cell: (v) => (v.volatility == null ? "—" : `${v.volatility.toFixed(2)} pts`),
  },
  {
    key: "history_days",
    label: "History",
    title: "APY observations the metrics read, over the last 180 days",
    cell: (v) => (v.history_days == null ? "—" : `${v.history_days} obs`),
    className: "text-muted-foreground",
  },
];

/** Higher is better for every key but volatility, whose best is lowest. */
const DESCENDING: Record<SortKey, boolean> = {
  score: true,
  apy: true,
  realized_apy: true,
  tvl_usd: true,
  sharpe: true,
  volatility: false,
  history_days: true,
};

function sorted(vaults: ShelfVault[], key: SortKey, descending: boolean): ShelfVault[] {
  // Unknown sorts last in either direction: it is not the best or the worst.
  return [...vaults].sort((a, b) => {
    const x = a[key];
    const y = b[key];
    if (x == null && y == null) return 0;
    if (x == null) return 1;
    if (y == null) return -1;
    return descending ? y - x : x - y;
  });
}

export function Shelf() {
  // The server's hourly read on arrival; the button asks for a fresh one.
  const [fresh, setFresh] = useState(false);
  const shelf = useApi(() => api.shelf(fresh));
  const book = useApi(() => api.book());
  const [query, setQuery] = useState("");
  const [chain, setChain] = useState<number | null>(null);
  const [sort, setSort] = useState<{ key: SortKey; descending: boolean }>({ key: "score", descending: true });

  const data = shelf.data;
  const held = useMemo(
    () => (book.data ? new Set(book.data.positions.map((p) => p.instrument_id)) : null),
    [book.data],
  );
  const chains = useMemo(
    () => [...new Set((data?.vaults ?? []).map((v) => v.chain_id))].sort((a, b) => a - b),
    [data],
  );
  const rows = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const kept = (data?.vaults ?? []).filter(
      (v) =>
        (chain == null || v.chain_id === chain) &&
        (!needle ||
          [v.name, v.asset, v.yield_token_symbol ?? "", v.protocol, v.curator ?? "", v.chain, v.instrument_id].some((field) =>
            field.toLowerCase().includes(needle),
          )),
    );
    return sorted(kept, sort.key, sort.descending);
  }, [data, query, chain, sort]);

  const refresh = () => {
    setFresh(true);
    shelf.reload();
  };
  const sortBy = (key: SortKey) =>
    setSort((current) =>
      current.key === key ? { key, descending: !current.descending } : { key, descending: DESCENDING[key] },
    );

  return (
    <div className="space-y-8">
      <section className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Shelf</h1>
          <p className="mt-1 max-w-3xl text-sm text-muted-foreground">
            Every instrument 1Tx lists that the allocator can score, best score first. APY is descriptive, not
            predictive; the risk metrics read the APY path only, never principal, depeg or contract loss.
          </p>
        </div>
        <Button variant="outline" className="px-3 py-1.5 text-xs" onClick={refresh} disabled={shelf.loading}>
          <RefreshCw className={`size-3.5 ${shelf.loading ? "animate-spin" : ""}`} />
          {shelf.loading ? "Reading…" : data ? `Read ${ago(data.read_at)}` : "Read"}
        </Button>
      </section>

      {shelf.loading && !data && (
        <Notice>Reading the shelf. Discovery with each instrument's APY history takes about a minute.</Notice>
      )}
      {shelf.error && <Notice tone="destructive">Could not read the shelf: {shelf.error}</Notice>}
      {data && data.warnings.length > 0 && (
        <Notice tone="warning">
          <p className="font-semibold">Some of the shelf could not be read</p>
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
            <StatTile label="Instruments" value={data.vaults.length} tone="primary" />
            <StatTile label="Chains" value={chains.length} />
            <StatTile label="Protocols" value={new Set(data.vaults.map((v) => v.protocol)).size} />
            <StatTile
              label="Held"
              value={held ? data.vaults.filter((v) => held.has(v.instrument_id)).length : "—"}
              sub={book.error ? "the book could not be read" : "positions on the shelf"}
            />
          </section>

          <Card>
            <CardHeader title="Instruments" description={`${rows.length} of ${data.vaults.length} shown.`} />
            <CardContent className="space-y-3">
              <label className="flex items-center gap-2 rounded-lg border bg-background/40 px-3 py-2 text-sm focus-within:border-primary/60">
                <Search className="size-4 text-muted-foreground" />
                <input
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="Name, asset, protocol, curator or instrument id"
                  className="w-full bg-transparent outline-none placeholder:text-muted-foreground"
                />
              </label>
              <div className="flex flex-wrap gap-1.5">
                <ChainChip active={chain == null} onClick={() => setChain(null)}>
                  All chains
                </ChainChip>
                {chains.map((id) => (
                  <ChainChip key={id} active={chain === id} onClick={() => setChain(chain === id ? null : id)}>
                    <span className="size-2 rounded-full" style={{ backgroundColor: chainColor(id) }} />
                    {chainName(id)}
                  </ChainChip>
                ))}
              </div>
            </CardContent>
            <CardContent className="px-0 pb-0">
              <ShelfTable vaults={rows} held={held} sort={sort} onSort={sortBy} />
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}

function ChainChip({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={cn(
        "flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-xs font-semibold transition-colors",
        active ? "border-primary/50 bg-primary/10 text-foreground" : "text-muted-foreground hover:text-foreground",
      )}
    >
      {children}
    </button>
  );
}

function ShelfTable({
  vaults,
  held,
  sort,
  onSort,
}: {
  vaults: ShelfVault[];
  held: Set<string> | null;
  sort: { key: SortKey; descending: boolean };
  onSort: (key: SortKey) => void;
}) {
  if (vaults.length === 0) {
    return <p className="p-6 text-sm text-muted-foreground">Nothing matches.</p>;
  }
  return (
    <>
      <ul className="divide-y divide-border/40 md:hidden">
        {vaults.map((vault) => (
          <li key={vault.instrument_id} className="space-y-3 px-4 py-4">
            <div className="flex items-start gap-3">
              <VaultName vault={vault} held={held?.has(vault.instrument_id) ?? false} className="flex-1" />
              <ScoreCell score={vault.score} />
            </div>
            <dl className="grid grid-cols-3 gap-x-3 gap-y-2 text-xs">
              {COLUMNS.map((col) => (
                <div key={col.key} className="min-w-0">
                  <dt className="text-[10px] font-bold tracking-widest text-muted-foreground uppercase">{col.label}</dt>
                  <dd className={`mt-0.5 tabular-nums ${col.className ?? ""}`}>{col.cell(vault)}</dd>
                </div>
              ))}
            </dl>
          </li>
        ))}
      </ul>

      <div className="hidden md:block">
        <TableScroll>
          <table className="w-full min-w-[960px] text-sm">
            <thead>
              <tr className="border-b text-[11px] tracking-widest text-muted-foreground uppercase">
                <th className="px-4 py-2.5 text-left font-bold">Instrument</th>
                <SortHeader label="Score" title="The allocator's score, 0–1" column="score" sort={sort} onSort={onSort} />
                {COLUMNS.map((col) => (
                  <SortHeader key={col.key} label={col.label} title={col.title} column={col.key} sort={sort} onSort={onSort} />
                ))}
              </tr>
            </thead>
            <tbody>
              {vaults.map((vault) => (
                <tr key={vault.instrument_id} className="border-b border-border/40 last:border-0 hover:bg-secondary/30">
                  <td className="px-4 py-3">
                    <VaultName vault={vault} held={held?.has(vault.instrument_id) ?? false} />
                  </td>
                  <td className="px-4 py-3 text-right">
                    <ScoreCell score={vault.score} />
                  </td>
                  {COLUMNS.map((col) => (
                    <td key={col.key} className={`px-4 py-3 text-right align-top tabular-nums ${col.className ?? ""}`}>
                      {col.cell(vault)}
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

function SortHeader({
  label,
  title,
  column,
  sort,
  onSort,
}: {
  label: string;
  title: string;
  column: SortKey;
  sort: { key: SortKey; descending: boolean };
  onSort: (key: SortKey) => void;
}) {
  const active = sort.key === column;
  const Arrow = sort.descending ? ArrowDown : ArrowUp;
  return (
    <th className="px-4 py-2.5 text-right font-bold whitespace-nowrap" aria-sort={active ? (sort.descending ? "descending" : "ascending") : "none"}>
      <button
        type="button"
        title={title}
        onClick={() => onSort(column)}
        className={cn(
          "inline-flex items-center gap-1 uppercase transition-colors hover:text-foreground",
          active && "text-foreground",
        )}
      >
        {label}
        {active && <Arrow className="size-3" />}
      </button>
    </th>
  );
}

function ScoreCell({ score }: { score: number }) {
  return (
    <div className="flex items-center justify-end gap-2">
      <span className="hidden w-16 lg:inline-block">
        <WeightBar weight={score} />
      </span>
      <span className="w-9 text-right font-semibold tabular-nums text-primary">{score.toFixed(2)}</span>
    </div>
  );
}

function ApyCell({ vault }: { vault: ShelfVault }) {
  const unpriced = vault.reward_apy != null && vault.reward_apy > 0 && vault.priced_reward_apy == null;
  return (
    <div>
      <p className="font-medium text-primary">{pct(vault.apy)}</p>
      {vault.reward_apy != null && vault.reward_apy > 0 && (
        <p
          className="text-[11px] text-muted-foreground"
          title={unpriced ? "The reward is priced at its emission schedule, not a quote it would fill at; the allocator does not count it" : undefined}
        >
          {pct(vault.base_apy)} + {pct(vault.reward_apy)} rewards
          {unpriced && <span className="text-warning"> (unpriced)</span>}
        </p>
      )}
    </div>
  );
}

function VaultName({ vault, held, className = "" }: { vault: ShelfVault; held: boolean; className?: string }) {
  return (
    <div className={`flex min-w-0 items-center gap-3 ${className}`}>
      <PositionIcon protocol={vault.protocol} chainId={vault.chain_id} />
      <div className="min-w-0">
        <p className="flex flex-wrap items-center gap-1.5 font-medium text-foreground">
          <span className="truncate">{vault.name}</span>
          {held && (
            <Badge variant="success" className="px-1.5 py-0 text-[10px]">
              held
            </Badge>
          )}
          {vault.days_to_maturity != null && (
            <Badge variant="ghost" className="px-1.5 py-0 text-[10px]" title={vault.maturity ?? undefined}>
              fixed · {vault.days_to_maturity}d
            </Badge>
          )}
          {vault.levered && (
            <Badge variant="warning" className="px-1.5 py-0 text-[10px]">
              levered{vault.max_leverage != null ? ` ≤ ${vault.max_leverage.toFixed(1)}×` : ""}
            </Badge>
          )}
        </p>
        <p className="flex flex-wrap items-center gap-x-1.5 text-xs text-muted-foreground">
          <span>{vault.protocol}</span>
          <span className="text-border">·</span>
          <span style={{ color: chainColor(vault.chain_id) }}>{chainName(vault.chain_id)}</span>
          {vault.yield_token_symbol && (
            <>
              <span className="text-border">·</span>
              <span className="font-mono">{vault.yield_token_symbol}</span>
            </>
          )}
          {vault.curator && (
            <>
              <span className="text-border">·</span>
              <span>{vault.curator}</span>
            </>
          )}
        </p>
      </div>
    </div>
  );
}
