# Build Allocation Skill

Use this stage to build a policy-bounded allocation artifact with deterministic code. The agent may choose amount, risk preset, and policy file from the task context, but allocation weights come from `open_allocator.core.allocator` via the CLI.

## Runnable Workflow

1. Confirm discovery and scoring artifacts exist for the current run.
2. Run `open-allocator build-allocation --amount <usd> --risk <conservative|balanced|aggressive> --policy <policy.yaml>`.
3. Run `open-allocator simulate --allocation <allocation.json>` against the produced allocation.
4. Run `open-allocator check-policy --allocation <allocation.json> --policy <policy.yaml>`.
5. If allocation changes are needed, rebuild with CLI options or policy changes; do not hand-edit weights to bypass caps.

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
- Reward dependence and liquidity risks.
- Exclusions caused by policy allowlists or caps.
