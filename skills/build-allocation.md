# Build Allocation Skill

Use this stage to build a policy-bounded allocation artifact with deterministic code. The agent may choose amount, risk preset, and policy file from the task context, but allocation weights come from `open_allocator.core.allocator` via the CLI.

## Runnable Workflow

1. Confirm discovery and scoring artifacts exist for the current run.
2. Choose a construction rule — see [Construction Rules](#construction-rules)
   below. The default is a choice like any other, not a neutral starting point.
3. Run `open-allocator build-allocation --amount <usd> --risk <conservative|balanced|aggressive> --strategy <name> --policy <policy.yaml>`.
4. Run `open-allocator simulate --allocation <allocation.json>` against the produced allocation.
5. Run `open-allocator check-policy --allocation <allocation.json> --policy <policy.yaml>`.
6. If allocation changes are needed, rebuild with CLI options or policy changes; do not hand-edit weights to bypass caps.

## Construction Rules

`--strategy`, parameterized by repeatable `--strategy-param key=value`:

| Ask | Strategy | Params |
| --- | --- | --- |
| Highest scored yield | `score_weighted` (default) | — |
| Most independent book | `decorrelated` | `top_n` (greedy selection; omit to re-weight only), `unknown_correlation` (default `1.0` — unmeasurable pairs charged as fully correlated) |
| A declared risk budget | `sleeves` / `ladder` | `tiers`: list of `{name, min_score, max_score, weight, strategy?}` |
| No opinion | `equal_weight` | — |
| Volatility-balanced | `risk_parity` / `inverse_vol` | — |
| Core plus bets | `core_satellite` | `core_weight`, `core_count`, `core_selector`, `satellite_selector` |

Independence and yield trade against each other, and the exchange rate depends on
the live shelf. When the task does not fix a preference, build more than one and
present the measured difference rather than choosing for the human.

Known interaction: `--max-positions` truncates *after* tier allocation, so with
`sleeves` it can collapse the book toward the top tier instead of thinning each
tier against its budget. The `min_effective_positions` floor catches the result.

## Measured Diversification

`simulate` reports a `diversification` block that label caps cannot replace:

- `effective_positions` — inverse-HHI over the correlation matrix; the
  independent-bet count. An **upper bound** on independence: shared collateral,
  depeg, principal and bridge risk are in none of these numbers.
- `median_tail_lift` — how much more often held pairs have a bad day on the
  *same* day than independence implies. Weight-blind: it medians over the set of
  held pairs, so it discriminates between position *sets*, not weightings.
- `unmeasured_weight_bps` — weight in names with no measurable history. A
  coverage number: how much of the book the metrics above could not see.
- `hidden_tail_pairs` — pairs correlation calls diversified that co-crash anyway.

`min_effective_positions` in `policy.yaml` is the only **floor** among the caps,
and it fails closed — unmeasurable independence is rejected, and instruments too
new to score are charged as fully correlated. Rebuild to earn it; never loosen
the policy to pass it.

## Economic Viability

`build-allocation` attaches a `metadata.cost_estimate` block: estimated gas
(per signed tx on the source chain), CCTP fast-transfer bridge fee on bridged
notional, max slippage, `cost_pct_of_deploy`, `net_apy_pct_year1`,
`breakeven_days`, and a `verdict` (`ok` / `marginal` / `uneconomic`). A
non-`ok` verdict also raises a `viability:<verdict>` warning. This is the
net-of-cost check the 1Tx gross simulation cannot give — read it before
recommending a deploy, especially at small sizes where fixed costs dominate.
Pass `--source-chain-id <id>` (the chain the wallet's USDC sits on, from
`wallet-status`) for an accurate bridge count; it defaults to the chain holding
the largest share of the deploy.

## Quality Bar

- Allocation JSON validates against `schemas/allocation.schema.json`.
- The allocation metadata includes policy result, candidate/exclusion context, and concentration warnings when emitted.
- `cost_estimate.verdict` is `ok`, or a `marginal`/`uneconomic` verdict is explicitly justified in the announcement.
- Simulation output is reviewed for blended APY, concentration, liquidity, reward-share, and failure-cost signals.
- The `diversification` block is reviewed, and `unmeasured_weight_bps` is disclosed whenever it is material — the independence metrics are only as good as their coverage.
- The chosen `--strategy` is named in the announcement, with the trade-off it implies stated plainly.
- `check-policy` returns `ok: true` before any transaction planning.

## Relevant CLI Commands

- `open-allocator build-allocation --amount <usd> --risk balanced --policy <policy.yaml>`
- `open-allocator simulate --allocation <allocation.json>`
- `open-allocator check-policy --allocation <allocation.json> --policy <policy.yaml>`

## Produced Artifacts

- Allocation JSON.
- Simulation JSON.
- Policy-result JSON.
- Candidate exclusion notes from allocation metadata.

## Safety Gates

- Policy violations block transaction planning.
- `max_deploy_per_cycle_usd` and concentration caps are enforced by code, not prompt judgment.
- APY language remains descriptive even when simulation reports blended APY.
- Do not hardcode protocol or chain universes; the builder reads live discovery and narrows by policy.

## Review Focus

- Policy conformance.
- Concentration by instrument, protocol, curator, and chain.
- Measured diversification: effective positions, tail lift, and metric coverage.
- Reward dependence and liquidity risks.
- Exclusions caused by policy allowlists or caps, and any `sleeve_empty` or `cap_clamped` warnings.
