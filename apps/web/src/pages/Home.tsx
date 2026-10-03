import { useEffect, useState } from "react";

import { Notice } from "@/components/Shell";
import { StatusBadge } from "@/components/StatusBadge";
import { Cell, Table } from "@/components/Table";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { api, type PlanSummary } from "@/lib/api";
import { dateTime, label, shortHash } from "@/lib/format";

export function Home() {
  const [plans, setPlans] = useState<PlanSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.plans().then(setPlans, (reason: Error) => setError(reason.message));
  }, []);

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Approvals</h1>
        <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
          Plans your MCP client proposed. Nothing runs until you approve it here or with{" "}
          <code className="text-foreground">open-allocator-ui approve &lt;hash&gt;</code>.
        </p>
      </div>
      {error && <Notice tone="destructive">{error}</Notice>}
      <Card>
        <CardHeader title="Recent plans" description="Newest first. A plan expires 15 minutes after it is proposed." />
        <CardContent>
          {plans === null && !error && <p className="text-sm text-muted-foreground">Loading…</p>}
          {plans?.length === 0 && (
            <p className="text-sm text-muted-foreground">
              No plans yet. Ask your MCP client to build an allocation and execute it.
            </p>
          )}
          {plans && plans.length > 0 && (
            <Table head={["Plan", "Kind", "Status", "Proposed"]} minWidth={520}>
              {plans.map((plan) => (
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
    </div>
  );
}
