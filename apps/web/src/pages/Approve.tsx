import { AlertTriangle, Check, ExternalLink, Loader2, X } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { Notice } from "@/components/Shell";
import { StatTile } from "@/components/StatTile";
import { StatusBadge } from "@/components/StatusBadge";
import { Cell, Table } from "@/components/Table";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { ApiError, api, type ExecuteReview, type PlanResponse, type ReviewBundle } from "@/lib/api";
import { addressUrl, chainName, txUrl } from "@/lib/chains";
import { countdown, dateTime, label, shortHash, tokenAmount, usd } from "@/lib/format";

type Decision = "idle" | "confirming" | "approving" | "rejecting";

function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active]);
  return now;
}

export function Approve({ hash }: { hash: string }) {
  const [plan, setPlan] = useState<PlanResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [decision, setDecision] = useState<Decision>("idle");
  const [decisionError, setDecisionError] = useState<string | null>(null);

  const load = useCallback(() => {
    api.plan(hash).then(
      (loaded) => {
        setPlan(loaded);
        setLoadError(null);
      },
      (reason: Error) => setLoadError(reason.message),
    );
  }, [hash]);

  useEffect(load, [load]);

  const pending = plan?.status === "pending";
  const now = useNow(pending);
  const remaining = plan ? new Date(plan.expires_at).getTime() - now : 0;
  const expired = pending && remaining <= 0;
  const blocked = (plan?.review?.blockers.length ?? 0) > 0;

  const decide = async (action: "approve" | "reject") => {
    setDecision(action === "approve" ? "approving" : "rejecting");
    setDecisionError(null);
    try {
      await (action === "approve" ? api.approve(hash) : api.reject(hash));
    } catch (reason) {
      setDecisionError(
        reason instanceof ApiError && reason.code
          ? `${reason.message} (${reason.code})`
          : (reason as Error).message,
      );
    } finally {
      setDecision("idle");
      load();
    }
  };

  if (loadError) {
    return (
      <Notice tone="destructive">
        Could not load plan <span className="font-mono">{shortHash(hash)}</span>: {loadError}
      </Notice>
    );
  }
  if (!plan) return <p className="text-sm text-muted-foreground">Loading plan…</p>;

  const review = plan.review;
  const busy = decision === "approving" || decision === "rejecting";

  return (
    <div className="flex flex-col gap-6 pb-32">
      <header className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-2xl font-bold tracking-tight">{label(plan.kind)} plan</h1>
          <StatusBadge status={expired ? "expired" : plan.status} />
          {pending && !expired && (
            <Badge variant="ghost" className="tabular-nums">
              expires in {countdown(remaining)}
            </Badge>
          )}
        </div>
        <p className="font-mono text-xs break-all text-muted-foreground">{plan.plan_hash}</p>
        {pending && (
          <p className="max-w-2xl text-sm text-muted-foreground">
            This is the stored plan, read by the server, not the model's description of it. Approving sends exactly
            these transactions after the server re-checks it against your policy.
          </p>
        )}
      </header>

      {plan.status === "applied" || plan.status === "failed" || plan.status === "rejected" ? (
        <Outcome plan={plan} />
      ) : null}
      {plan.status === "applying" && <Notice>Approved and being sent. Reload for the outcome.</Notice>}

      {review ? <Review review={review} /> : <Notice tone="warning">This plan cannot be summarised: {plan.review_error}. Inspect the raw plan below before approving.</Notice>}

      <Card>
        <CardHeader title="Raw plan" description={`Proposed ${dateTime(plan.created_at)}. The hash covers this document.`} />
        <CardContent>
          <details>
            <summary className="cursor-pointer text-sm text-primary">Show JSON</summary>
            <pre className="table-scroll mt-3 max-h-[28rem] overflow-auto rounded-lg bg-background p-3 text-xs">
              {JSON.stringify(plan.plan, null, 2)}
            </pre>
          </details>
        </CardContent>
      </Card>

      {pending && (
        <div className="fixed inset-x-0 bottom-0 border-t bg-background/95 backdrop-blur">
          <div className="mx-auto flex max-w-5xl flex-col gap-3 px-4 py-4 sm:flex-row sm:items-center sm:justify-between">
            <div className="text-sm">
              {decisionError ? (
                <span className="text-destructive">{decisionError}</span>
              ) : expired ? (
                <span className="text-muted-foreground">Expired. Ask your MCP client to plan again.</span>
              ) : blocked ? (
                <span className="text-warning">This plan has blockers and is expected to fail as it stands.</span>
              ) : decision === "confirming" ? (
                <span>
                  Send {review?.transactions ?? "these"} transaction{review?.transactions === 1 ? "" : "s"} from{" "}
                  <span className="font-mono">{review ? shortHash(review.account) : "your account"}</span>?
                </span>
              ) : (
                <span className="text-muted-foreground">Nothing is sent until you approve.</span>
              )}
            </div>
            <div className="flex gap-2">
              {decision === "confirming" ? (
                <>
                  <Button variant="ghost" onClick={() => setDecision("idle")}>
                    Cancel
                  </Button>
                  <Button onClick={() => decide("approve")} autoFocus>
                    <Check className="size-4" /> Confirm and send
                  </Button>
                </>
              ) : (
                <>
                  <Button variant="outline" disabled={busy || expired} onClick={() => decide("reject")}>
                    {decision === "rejecting" ? <Loader2 className="size-4 animate-spin" /> : <X className="size-4" />}
                    Reject
                  </Button>
                  <Button disabled={busy || expired} onClick={() => setDecision("confirming")}>
                    {decision === "approving" ? (
                      <>
                        <Loader2 className="size-4 animate-spin" /> Sending…
                      </>
                    ) : (
                      <>
                        <Check className="size-4" /> Approve
                      </>
                    )}
                  </Button>
                </>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function Review({ review }: { review: ExecuteReview }) {
  const chains = [...new Set(review.bundles.map((bundle) => bundle.chain_id))];
  const unfunded = review.funding.filter((item) => !item.ok);
  return (
    <>
      {review.blockers.length > 0 && (
        <Notice tone="destructive">
          <p className="mb-2 flex items-center gap-2 font-semibold">
            <AlertTriangle className="size-4" /> Blockers
          </p>
          <ul className="list-disc space-y-1 pl-5">
            {review.blockers.map((blocker) => (
              <li key={blocker}>{blocker}</li>
            ))}
          </ul>
        </Notice>
      )}

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatTile label="Deposits" value={usd(review.deposit_usd)} sub={`target ${usd(review.target_usd)}`} />
        <StatTile label="Transactions" value={review.transactions} sub={`${review.bundles.length} bundle${review.bundles.length === 1 ? "" : "s"}`} />
        <StatTile label="Chains" value={chains.length} sub={chains.map(chainName).join(", ") || "—"} />
        <StatTile
          label="Funding"
          value={unfunded.length === 0 ? "Covered" : "Short"}
          tone={unfunded.length === 0 ? "success" : "destructive"}
          sub={unfunded.length === 0 ? "balances read at planning" : `${unfunded.length} shortfall(s)`}
        />
      </div>

      <Card>
        <CardHeader title="Account" />
        <CardContent>
          <Address chainId={review.bundles[0]?.chain_id} address={review.account} />
        </CardContent>
      </Card>

      <Card>
        <CardHeader
          title="Policy"
          description="The result when the plan was built. Approval checks it again against the server's policy on today's shelf."
        />
        <CardContent>
          {review.policy.ok ? (
            <Badge variant="success">No violations</Badge>
          ) : (
            <Table head={["Rule", "Entity", "Limit", "Actual"]} minWidth={480}>
              {review.policy.violations.map((violation, index) => (
                <tr key={index}>
                  <Cell>{violation.rule}</Cell>
                  <Cell className="font-mono text-xs">{violation.entity}</Cell>
                  <Cell numeric>{String(violation.limit)}</Cell>
                  <Cell numeric className="text-destructive">
                    {String(violation.actual)}
                  </Cell>
                </tr>
              ))}
            </Table>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader title="Legs" description="What the allocation asked for, and what the plan deposits." />
        <CardContent>
          <Table head={["#", "Instrument", "Target", "Planned", "Leverage"]} minWidth={560}>
            {review.legs.map((leg) => (
              <tr key={leg.leg_index}>
                <Cell numeric className="text-muted-foreground">
                  {leg.leg_index}
                </Cell>
                <Cell className="font-mono text-xs">{leg.instrument_id}</Cell>
                <Cell numeric>{usd(leg.target_usd)}</Cell>
                <Cell numeric>
                  {leg.deposit_usd == null ? <Badge variant="ghost">skipped</Badge> : usd(leg.deposit_usd)}
                </Cell>
                <Cell numeric>{leg.leverage == null ? "—" : `${leg.leverage}×`}</Cell>
              </tr>
            ))}
          </Table>
        </CardContent>
      </Card>

      <Card>
        <CardHeader
          title="Transactions"
          description="Each bundle is submitted atomically, in this order. Amounts are the plan's own quotes."
        />
        <CardContent>
          <Table head={["Action", "Chain", "Instrument", "Spends", "Expected", "Steps"]} minWidth={760}>
            {review.bundles.map((bundle) => (
              <BundleRow key={bundle.bundle_id} bundle={bundle} />
            ))}
          </Table>
        </CardContent>
      </Card>

      {review.loops.length > 0 && <Loops loops={review.loops} />}

      <Card>
        <CardHeader title="Funding" description="Balances read when the plan was built." />
        <CardContent>
          <Table head={["Chain", "Required", "Available", "Shortfall", ""]} minWidth={560}>
            {review.funding.map((item, index) => (
              <tr key={index}>
                <Cell>{chainName(item.chain_id)}</Cell>
                <Cell numeric>
                  {tokenAmount(item.required)}
                  {item.includes_gas_charge && <span className="ml-1 text-xs text-muted-foreground">incl. gas</span>}
                </Cell>
                <Cell numeric>{item.available ? tokenAmount(item.available) : "unknown"}</Cell>
                <Cell numeric className={item.ok ? "text-muted-foreground" : "text-destructive"}>
                  {tokenAmount(item.shortfall)}
                </Cell>
                <Cell>{item.ok ? <Badge variant="success">ok</Badge> : <Badge variant="destructive">short</Badge>}</Cell>
              </tr>
            ))}
          </Table>
        </CardContent>
      </Card>

      {review.notes.length > 0 && (
        <Card>
          <CardHeader title="Notes" description="Sizing and routing decisions made while planning." />
          <CardContent>
            <ul className="list-disc space-y-1 pl-5 text-sm text-muted-foreground">
              {review.notes.map((note, index) => (
                <li key={index}>{note}</li>
              ))}
            </ul>
          </CardContent>
        </Card>
      )}
    </>
  );
}

function BundleRow({ bundle }: { bundle: ReviewBundle }) {
  return (
    <tr>
      <Cell>
        <span className="font-semibold">{label(bundle.action)}</span>
        {bundle.bridge && (
          <span className="mt-0.5 block text-xs text-muted-foreground">
            to {chainName(bundle.bridge.to_chain_id)} · {bundle.bridge.fast ? "fast" : "standard"} · max fee{" "}
            {tokenAmount(bundle.bridge.max_fee)}
          </span>
        )}
      </Cell>
      <Cell>{chainName(bundle.chain_id)}</Cell>
      <Cell className="font-mono text-xs">{bundle.instrument_id}</Cell>
      <Cell numeric>{tokenAmount(bundle.amount_in)}</Cell>
      <Cell numeric>
        {tokenAmount(bundle.expected_out)}
        {bundle.min_out && <span className="block text-xs text-muted-foreground">min {tokenAmount(bundle.min_out)}</span>}
      </Cell>
      <Cell className="text-xs text-muted-foreground">
        {bundle.steps.map(label).join(" → ")}
        {bundle.expires_at != null && (
          <span className="block">quote until {dateTime(new Date(bundle.expires_at * 1000).toISOString())}</span>
        )}
      </Cell>
    </tr>
  );
}

type Loop = Record<string, unknown>;

const num = (value: unknown, digits = 2): string =>
  typeof value === "number" && Number.isFinite(value) ? value.toFixed(digits) : "—";

function Loops({ loops }: { loops: Loop[] }) {
  return (
    <Card>
      <CardHeader
        title="Levered loops"
        description="Leverage and health factor as modelled here and as 1Tx simulated them."
      />
      <CardContent>
        <Table head={["Action", "Chain", "Pool", "Leverage", "Health factor", ""]} minWidth={720}>
          {loops.map((loop, index) => {
            const blockers = Array.isArray(loop.blockers) ? (loop.blockers as string[]) : [];
            return (
              <tr key={index}>
                <Cell>{label(String(loop.action ?? "—"))}</Cell>
                <Cell>{chainName(typeof loop.chain_id === "number" ? loop.chain_id : null)}</Cell>
                <Cell className="font-mono text-xs">
                  {String(loop.protocol ?? "")} {String(loop.pool ?? "")}
                </Cell>
                <Cell numeric>
                  {num(loop.requested_leverage)}× req · {num(loop.simulated_leverage)}× sim
                </Cell>
                <Cell numeric>
                  {num(loop.modelled_health_factor)} model · {num(loop.simulated_health_factor)} sim
                </Cell>
                <Cell>
                  {loop.confirmable === false ? (
                    <Badge variant="destructive" title={blockers.join("; ")}>
                      blocked
                    </Badge>
                  ) : (
                    <Badge variant="ghost">ok</Badge>
                  )}
                </Cell>
              </tr>
            );
          })}
        </Table>
      </CardContent>
    </Card>
  );
}

type SentStep = {
  step_index?: number;
  instrument_id?: string;
  status?: string;
  step?: { chain_id?: number; kind?: string } | null;
  receipt?: { transaction_hash?: string; execution_status?: string } | null;
};

function Outcome({ plan }: { plan: PlanResponse }) {
  if (plan.status === "rejected") {
    return <Notice>Rejected. Nothing was sent.</Notice>;
  }
  if (plan.status === "failed") {
    return (
      <Notice tone="destructive">
        <p className="font-semibold">Not applied</p>
        <p className="mt-1 break-words">{plan.error}</p>
      </Notice>
    );
  }
  const result = plan.result ?? {};
  const steps = (Array.isArray(result.steps) ? result.steps : []) as SentStep[];
  const messages = (Array.isArray(result.messages) ? result.messages : []) as string[];
  const sent = steps.filter((step) => step.receipt?.transaction_hash);
  return (
    <Card>
      <CardHeader title="Outcome" description={`Execution status: ${String(result.status ?? "unknown")}`} />
      <CardContent className="flex flex-col gap-4">
        {sent.length > 0 && (
          <Table head={["Step", "Chain", "Instrument", "Transaction"]} minWidth={560}>
            {sent.map((step, index) => {
              const hash = step.receipt?.transaction_hash ?? "";
              const url = txUrl(step.step?.chain_id, hash);
              return (
                <tr key={index}>
                  <Cell>{label(step.step?.kind ?? "step")}</Cell>
                  <Cell>{chainName(step.step?.chain_id)}</Cell>
                  <Cell className="font-mono text-xs">{step.instrument_id}</Cell>
                  <Cell className="font-mono text-xs">
                    {url ? (
                      <a className="inline-flex items-center gap-1 text-primary hover:underline" href={url} target="_blank" rel="noreferrer">
                        {shortHash(hash)} <ExternalLink className="size-3" />
                      </a>
                    ) : (
                      shortHash(hash)
                    )}
                  </Cell>
                </tr>
              );
            })}
          </Table>
        )}
        {messages.length > 0 && (
          <ul className="list-disc space-y-1 pl-5 text-sm text-muted-foreground">
            {messages.map((message, index) => (
              <li key={index}>{message}</li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

function Address({ chainId, address }: { chainId: number | undefined; address: string }) {
  const url = addressUrl(chainId, address);
  return url ? (
    <a className="inline-flex items-center gap-1 font-mono text-sm break-all text-primary hover:underline" href={url} target="_blank" rel="noreferrer">
      {address} <ExternalLink className="size-3 shrink-0" />
    </a>
  ) : (
    <span className="font-mono text-sm break-all">{address}</span>
  );
}
