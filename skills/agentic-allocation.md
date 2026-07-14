# Agentic Allocation Skill

Use this stage when the agent authors its own allocation — choosing a strategy,
parameterizing it, screening the universe on risk, and backtesting a proposal —
then hands the **deterministic** executor a validated artifact. Free-form agent
reasoning decides; the audited engine executes, under policy. Agent code never
becomes the execution path.

The two planes:

- **Research/decision plane — agentic.** The agent freely inspects metrics,
  screens, and backtests to decide an allocation and justify it.
- **Execution plane — deterministic and policy-gated.** The decision leaves the
  research plane only as an **allocation-spec** (explicit weights, or a
  named+parameterized strategy) that `build-allocation` materializes and
  `check-policy` gates.

## Runnable Workflow

1. **Discover + surface risk.** `open-allocator list-vaults` and
   `open-allocator score-vault --instrument-id <id>` now carry a `risk_metrics`
   block (Sharpe, Sortino, max-drawdown, downside-deviation, realized vs.
   advertised delivery gap). `Unknown` means insufficient history — never guess
   past it.
2. **Screen (advisory).** `open-allocator screen --min-sharpe … --max-drawdown …
   --max-reward-dependence … --min-history-days … --screen-curator …` narrows
   the universe. Screening can only shrink the candidate set; policy still runs
   downstream.
3. **Choose a strategy** from the vetted library (`score_weighted`,
   `equal_weight`, `risk_parity`/`inverse_vol`, `core_satellite`,
   `sleeves`/`ladder`) or decide explicit weights.
4. **Emit an allocation-spec** (see `schemas/allocation-spec.schema.json`):
   either `{ "weights": { … } }` or
   `{ "strategy": …, "params": …, "selection": … }`.
5. **Materialize + gate.**
   `open-allocator build-allocation --spec <spec.json> --amount <usd> --policy <policy.yaml>`
   then `open-allocator check-policy --allocation <allocation.json>`.
6. **Backtest (read-only).**
   `open-allocator backtest --allocation <allocation.json>` compounds the
   proposal's `apy_series` into a NAV curve vs. a TVL-weighted benchmark
   (Sharpe, max-drawdown, beat-rate) to compare before proposing.
7. **Announce → execute** per the standard confirmation discipline.

## Allocation-Spec Examples

Strategy mode:

```json
{
  "amount_usd": 100000,
  "strategy": "core_satellite",
  "params": { "core_weight": 0.8, "core_count": 3 },
  "selection": { "min_history_days": 14, "max_reward_dependence": 0.5 }
}
```

Explicit-weights mode (agent invents its own vector; pins take the wheel,
policy validates the result):

```json
{ "weights": { "base-aave-usdc": 0.6, "arbitrum-morpho-usdc": 0.4 } }
```

## Quality Bar

- Spec validates against `schemas/allocation-spec.schema.json`; allocation JSON
  validates against `schemas/allocation.schema.json`.
- `check-policy` returns `ok: true` before any transaction planning — no
  strategy, screen, or spec can loosen policy.
- Every weight remains explainable from visible inputs (scores, risk metrics,
  strategy params).

## Safety Gates

- Strategies are vetted Python selected/parameterized by the agent — never
  agent-authored executable code on the execution path.
- Screening is advisory narrowing, not policy; policy runs on whatever survives.
- Backtest and risk metrics are **yield-path only** and descriptive, not
  predictive: they exclude principal/depeg/contract/bridge/withdrawal loss, are
  bounded by the 1Tx history window, and are biased by its market regime.
- `Unknown` stays `Unknown`.
