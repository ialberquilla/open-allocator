import { ArrowUpRight, Play } from "lucide-react";
import { useEffect, useState } from "react";

import { Notice } from "@/components/Shell";
import { StatusBadge } from "@/components/StatusBadge";
import { Cell, Table } from "@/components/Table";
import { Badge, type BadgeProps } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { api, type JobName, type JobRun } from "@/lib/api";
import { chainColor, txUrl } from "@/lib/chains";
import { ago, dateTime, label, shortHash, usd } from "@/lib/format";
import { useApi } from "@/lib/useApi";

// What happened: plans proposed, what the server executed, and the reads it
// makes on its own.

const JOBS: { name: JobName; title: string; every: string }[] = [
  { name: "nav", title: "NAV backfill", every: "hourly" },
  { name: "shelf", title: "Shelf read", every: "hourly" },
  { name: "rewards", title: "Rewards read", every: "hourly" },
];

export function Activity() {
  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Activity</h1>
        <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
          Plans your MCP client proposed, what the server executed, and the reads it makes on its own. Nothing runs
          until you approve it here or with{" "}
          <code className="text-foreground">open-allocator-ui approve &lt;hash&gt;</code>.
        </p>
      </div>
      <Plans />
      <Executions />
      <Jobs />
    </div>
  );
}

function Plans() {
  const plans = useApi(() => api.plans());
  return (
    <Card>
      <CardHeader title="Recent plans" description="Newest first. A plan expires 15 minutes after it is proposed." />
      <CardContent>
        {plans.error && <Notice tone="destructive">{plans.error}</Notice>}
        {plans.loading && !plans.data && <p className="text-sm text-muted-foreground">Loading…</p>}
        {plans.data?.length === 0 && (
          <p className="text-sm text-muted-foreground">
            No plans yet. Ask your MCP client to build an allocation and execute it.
          </p>
        )}
        {plans.data && plans.data.length > 0 && (
          <Table head={["Plan", "Kind", "Status", "Proposed"]} minWidth={520}>
            {plans.data.map((plan) => (
              <tr key={plan.plan_hash}>
                <Cell>
                  <a className="font-mono text-primary hover:underline" href={`/approve/${plan.plan_hash}`}>
                    {shortHash(plan.plan_hash)}
                  </a>
                </Cell>
                <Cell>{label(plan.kind)}</Cell>
                <Cell>
                  <StatusBadge status={plan.status} />
                </Cell>
                <Cell className="text-muted-foreground">{dateTime(plan.created_at)}</Cell>
              </tr>
            ))}
          </Table>
        )}
      </CardContent>
    </Card>
  );
}

function Executions() {
  const executions = useApi(() => api.executions());
  return (
    <Card>
      <CardHeader
        title="Executions"
        description="Each action the server's approved plans executed, newest first. What the CLI executed is in its own log, not here."
      />
      <CardContent>
        {executions.error && <Notice tone="destructive">{executions.error}</Notice>}
        {executions.data?.length === 0 && <p className="text-sm text-muted-foreground">Nothing executed yet.</p>}
        {executions.data && executions.data.length > 0 && (
          <Table head={["When", "Action", "Chain", "Instrument", "USD", "Transaction"]} minWidth={720}>
            {executions.data.map((execution) => {
              const url = txUrl(execution.chain_id, execution.tx_hash);
              return (
                <tr key={execution.id}>
                  <Cell className="text-muted-foreground">{dateTime(execution.logged_at)}</Cell>
                  <Cell>{label(execution.action_type)}</Cell>
                  <Cell>
                    <span style={{ color: chainColor(execution.chain_id) }}>{execution.chain}</span>
                  </Cell>
                  <Cell className="font-mono text-xs">{shortHash(execution.instrument_id)}</Cell>
                  <Cell className="tabular-nums">{usd(execution.usd)}</Cell>
                  <Cell className="font-mono text-xs">
                    {url ? (
                      <a className="inline-flex items-center gap-0.5 text-primary hover:underline" href={url} target="_blank" rel="noreferrer">
                        {shortHash(execution.tx_hash)}
                        <ArrowUpRight className="size-3" />
                      </a>
                    ) : (
                      shortHash(execution.tx_hash)
                    )}
                  </Cell>
                </tr>
              );
            })}
          </Table>
        )}
      </CardContent>
    </Card>
  );
}

const RUN_STATUS: Record<string, BadgeProps["variant"]> = {
  ok: "success",
  partial: "warning",
  failed: "destructive",
  running: "secondary",
};

function RunBadge({ run }: { run: JobRun }) {
  return <Badge variant={RUN_STATUS[run.status] ?? "ghost"}>{run.status}</Badge>;
}

function Jobs() {
  const jobs = useApi(() => api.jobs());
  const [asked, setAsked] = useState<JobName | null>(null);
  const data = jobs.data;

  // While a job runs, look again every few seconds.
  useEffect(() => {
    if (!data?.running.length) return;
    const timer = window.setTimeout(jobs.reload, 4000);
    return () => window.clearTimeout(timer);
  }, [data, jobs.reload]);

  async function start(name: JobName) {
    setAsked(name);
    try {
      await api.startJob(name);
    } finally {
      setAsked(null);
      jobs.reload();
    }
  }

  return (
    <Card>
      <CardHeader title="Background reads" description="Each runs on start and then hourly; a failed read keeps the last good one." />
      <CardContent className="space-y-6">
        {jobs.error && <Notice tone="destructive">{jobs.error}</Notice>}
        {data && (
          <div className="grid gap-3 sm:grid-cols-3">
            {JOBS.map((job) => {
              const last = data.latest[job.name];
              const running = data.running.includes(job.name);
              return (
                <div key={job.name} className="flex flex-col gap-2 rounded-xl border p-4">
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-semibold">{job.title}</span>
                    {last && <RunBadge run={last} />}
                  </div>
                  <span className="text-xs text-muted-foreground">
                    {last ? `Last ${ago(last.finished_at ?? last.started_at)}` : "Never run"} · {job.every}
                  </span>
                  {last?.status === "failed" && typeof last.detail?.error === "string" && (
                    <span className="text-xs text-destructive">{last.detail.error}</span>
                  )}
                  <Button
                    variant="outline"
                    className="mt-1 self-start px-3 py-1.5 text-xs"
                    onClick={() => start(job.name)}
                    disabled={running || asked === job.name}
                  >
                    <Play className="size-3" />
                    {running ? "Running…" : "Run now"}
                  </Button>
                </div>
              );
            })}
          </div>
        )}
        {data && data.runs.length > 0 && (
          <Table head={["Job", "Status", "Started", "Took"]} minWidth={480}>
            {data.runs.map((run) => (
              <tr key={run.id}>
                <Cell>{JOBS.find((job) => job.name === run.job)?.title ?? run.job}</Cell>
                <Cell>
                  <RunBadge run={run} />
                </Cell>
                <Cell className="text-muted-foreground">{dateTime(run.started_at)}</Cell>
                <Cell className="tabular-nums text-muted-foreground">{took(run)}</Cell>
              </tr>
            ))}
          </Table>
        )}
      </CardContent>
    </Card>
  );
}

function took(run: JobRun): string {
  if (!run.finished_at) return "—";
  const seconds = (new Date(run.finished_at).getTime() - new Date(run.started_at).getTime()) / 1000;
  if (!Number.isFinite(seconds)) return "—";
  return seconds < 90 ? `${seconds.toFixed(1)}s` : `${Math.round(seconds / 60)}m`;
}
