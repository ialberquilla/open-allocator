# Capabilities — what this allocator can be asked to do

The knob surface, written as *mechanism* rather than as results. Nothing here
quotes a measured number: the live shelf changes daily and every added
instrument moves every figure, so a number in a doc is stale the week after it
is written. What does not change is which questions the library can answer and
which knob answers each one.

Read this before re-deriving anything by hand. If a capability is missing here,
it is missing from the code — not from the doc.

## The choice this library exists to expose

There is no single correct allocation, so the allocator does not pick one. It
prices the trade-off and hands you the controls:

| If you want | Set | Mechanism |
| --- | --- | --- |
| The highest scored yield | `--strategy score_weighted` (default) | Weight by composite score, tilted by `--apy-weight` / `--score-power` |
| The most independent book | `--strategy decorrelated` | Weight divided by measured correlation load; optional greedy `top_n` selection |
| A risk budget you declare | `--strategy sleeves` | Score-tiered buckets, each with its own target weight and sub-strategy |
| Nothing to decide | `--strategy equal_weight` | Equal weights, the honest baseline |
| Volatility-balanced | `--strategy risk_parity` / `inverse_vol` | Weight inverse to APY volatility |
| A core plus bets | `--strategy core_satellite` | Split into a core and a satellite, each with its own selector |

Concentration and yield pull against each other. Raising independence generally
costs some blended APY, and the size of that cost depends on the shelf and the
position count on the day you ask — which is exactly why you measure it per run
(`simulate`) instead of trusting a figure written down once.

## Strategies and their parameters

Set with `--strategy <name>` and `--strategy-param key=value` (repeatable, value
is a JSON scalar). Source of truth: `src/open_allocator/core/strategies/library.py`
— the docstrings there carry the full mechanism.

| Strategy | Params | Notes |
| --- | --- | --- |
| `score_weighted` | — | Default. Reads `--score-power`, `--apy-weight` from the risk preset. |
| `equal_weight` | — | |
| `risk_parity` / `inverse_vol` | — | Aliases. Inverse APY volatility, with a vol floor. |
| `decorrelated` | `top_n`, `unknown_correlation` | `top_n` switches on greedy selection; omit it to keep every candidate and only re-weight. `unknown_correlation` defaults to `1.0` — an unmeasurable pair is charged as fully correlated. |
| `core_satellite` | `core_weight`, `core_count`, `core_selector`, `satellite_selector` | Selectors must be flat strategies; composites are rejected so dispatch stays finite. |
| `sleeves` / `ladder` | `tiers` | Aliases. `tiers` is a list of `{name, min_score, max_score, weight, strategy?}`. Omit to use the default 3-tier ladder. |

### How `sleeves` buckets

Tiers key on the **composite score** — `_tier_for_score(record.score.score, ...)`
— which is a weighted blend of nine measured factors (`core/scoring.py`,
`DEFAULT_WEIGHTS`): TVL, APY stability, reward dependence, liquidity, oracle,
fee, curator, market concentration, collateral mix. So a sleeve is a
quality band computed from data, not a hand-applied label.

Each tier declares a `weight` — a *target share* of the book. That is how you
say "half in the top band, a fifth in the bottom one". A tier that matches no
instrument raises `sleeve_empty:<name>:weight_redistributed` and its target is
spread across the populated tiers, so the allocation still sums to 1 and tells
you what it did.

Each tier may also name its own `strategy`, so a risk budget can be composed
with a different construction rule inside each band.

## Ceilings, and the one floor

Set in `policy.yaml` under `caps` (which is commented in place — read it for the
reasoning behind each default). Policy can only tighten, never loosen.

**Ceilings** — maximum weight per `instrument`, `protocol`, `curator`, `chain`,
`sector`. Plus `min_instrument_tvl_usd` and `max_reward_dependence`.

**The floor** — `min_effective_positions`. The only cap shaped this way, and the
only one computed from the instruments' own history rather than their labels. A
ceiling on a label can be satisfied by holding many names that are one position;
a floor on effective positions cannot. It **fails closed**: an allocation whose
independence cannot be measured is rejected, not passed, and instruments too new
to score are charged as fully correlated.

Why both: labels answer "am I over-exposed to a name I can point at", the floor
answers "am I actually holding more than one bet". Neither subsumes the other.

## What gets reported

`simulate --allocation <file>` returns, alongside the yield-path simulation:

- `diversification.effective_positions` — inverse-HHI over the correlation
  matrix. The independent-bet count.
- `diversification.median_tail_lift` — how much more often held pairs have a bad
  day *on the same day* than they would independently.
- `diversification.unmeasured_weight_bps` — weight held in names with no
  measurable history. Not a risk score, a coverage number: it tells you how much
  of the book the other two metrics could not see.
- `diversification.hidden_tail_pairs` — pairs that correlation calls diversified
  but that co-crash anyway.
- `sector_concentration.effective_sectors` / `unclassified_weight_bps` — the
  label view, kept for visibility.

`build-allocation` attaches `metadata.cost_estimate` (gas, bridge fee, slippage,
`net_apy_pct_year1`, `breakeven_days`, `verdict`) and `metadata.warnings`, which
name every cap that clamped, every policy exclusion, and every empty sleeve.

Advisory narrowing before construction: `screen` and the `--min-sharpe`,
`--max-drawdown`, `--max-reward-dependence`, `--min-history-days`,
`--screen-curator`, `--min-tvl-usd`, `--max-positions`, `--min-position-usd`
flags on `build-allocation`. Screens filter; caps bound; only the policy gate
blocks.

## Known limits — read before trusting a number

- **`median_tail_lift` is weight-blind.** It medians over the set of held pairs
  and ignores their weights, so two allocations over the same names return the
  same value however differently they are weighted. It discriminates between
  different *position sets*, not between weightings of one set.
- **`effective_positions` is an upper bound on independence.** Correlation of
  APY series is the optimistic error: shared collateral, depeg, principal loss
  and bridge risk are in none of these numbers. `median_tail_lift` is the
  cheap validated stand-in for composition overlap, not a substitute for it.
- **Sleeve tiers key only on the composite score.** You cannot currently bucket
  directly on correlation load, drawdown, or any single factor.
- **Sleeves have no `min_positions`.** A tier can be given a target weight but
  not a minimum name count, so a small high-risk sleeve can concentrate inside
  its own budget.
- **`--max-positions` truncates after tier allocation.** Combined with
  `sleeves`, the cut can collapse the book toward the top tier rather than
  thinning each tier against its budget. The `min_effective_positions` floor
  catches the result, so it fails closed rather than shipping quietly — but the
  interaction is a defect, not a design.
- **`max_weight_per_curator` does not bind.** `curator` arrives `Unknown` from
  discovery and each unknown gets its own bucket.
- **`max_weight_per_sector` is deliberately unbinding** while the shelf holds
  one sector; a real cap there would leave capital unplaceable rather than
  diversify anything. `effective_sectors` is reported regardless.

## Re-measuring instead of quoting

Any claim about how these knobs behave should be regenerated, not cited:

```bash
uv run open-allocator build-allocation --strategy <name> --amount <usd> > alloc.json
uv run open-allocator simulate --allocation alloc.json     # effective-N, tail lift, coverage
uv run open-allocator check-policy --allocation alloc.json  # the floor, block-only
```

Compare two strategies by building both and diffing their `simulate` output.
`metadata.cost_estimate.net_apy_pct_year1` on each allocation is the yield side
of the trade; `diversification.effective_positions` is the independence side.
