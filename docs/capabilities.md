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
| `sleeves` / `ladder` | `tiers` | Aliases. `tiers` is a list of `{name, min_score, max_score, weight, strategy?, min_positions?}`. Omit to use the default 3-tier ladder. |

### How `sleeves` buckets

Tiers key on the **composite score** — `_tier_for_score(record.score.score, ...)`
— which is a weighted blend of nine measured factors (`core/scoring.py`,
`DEFAULT_WEIGHTS`): TVL, APY stability, reward dependence, liquidity, oracle,
fee, curator, market concentration, collateral mix. So a sleeve is a
quality band computed from data, not a hand-applied label.

Each tier declares a `weight` — a *target share* of the book. That is how you
say "half in the top band, a fifth in the bottom one".

Each tier may also name its own `strategy`, so a risk budget can be composed
with a different construction rule inside each band.

### The floor under a sleeve: `min_positions`

A target share says how much goes in a band; it says nothing about how many
names carry it. `min_positions` is the second half — the count a tier must
reach before it may hold its target at all.

A tier that cannot reach it is **dropped whole, not held small**. Shrinking a
sleeve does not fix a sleeve that is too thin: the failure being guarded is one
instrument going to zero, and a three-name band loses a third of itself to that
whatever share of the book it was given. So the knob is a floor on breadth, not
a discount on size, and the tier either clears it or gets nothing.

Where the released weight goes is the part worth reading twice. It moves
**upward only** — to funded tiers with a strictly higher `min_score`, in
proportion to their own targets. Spread evenly across every survivor instead, a
shortage of *safe* instruments would push weight down into the riskier bands
and quietly buy more risk than was asked for. The one case where that cannot be
avoided is the safest tier itself falling short, since nothing sits above it;
that path still runs, and says so.

What a run tells you:

| Warning | Meaning |
| --- | --- |
| `sleeve_empty:<name>:weight_redistributed` | Tier matched no instrument. |
| `sleeve_underfilled:<name>:<n>/<min>:weight_redistributed` | Tier matched `n` instruments against a floor of `min`. |
| `sleeve_no_safer_tier:<name>:weight_redistributed_down` | Nothing above the dropped tier could absorb it, so its weight went down the ladder. **This raises the book's risk.** |
| `sleeves:no_populated_tiers:using_equal_weights` | No tier cleared its floor; the run falls back to equal weights. |

The allocation still sums to 1 in every case, and the redistribution is
computed against the tiers' original targets, so the result does not depend on
the order unfillable tiers are visited.

`min_positions` defaults to `0` — unset, a tier needs one instrument, which is
the behaviour that shipped before the knob existed. Sizing it is a judgement
about how many independent failures a band should absorb, and `simulate` is
where you check what a given floor costs on the shelf you actually have. Note
this is a floor on *names*, not on independence: `min_effective_positions`
below is the measured counterpart, and a tier can clear a count floor with
instruments that are one position in disguise.

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

Gas in that block is priced from **live** chain state — the source chain's gas
price and an on-chain Chainlink ETH/USD feed — and `cost_estimate.gas_priced_live`
reports whether that succeeded. When it is `false` the estimate fell back to a
static per-chain-class constant, which is an order of magnitude only: a constant
is several times too high on an L2 at a low base fee and an order of magnitude
too low on mainnet at a normal one. `core.costs.min_economic_leg_usd()` derives a
gas-aware minimum leg size from the same pricing, for when a flat
`--min-position-usd` would be wrong on an expensive chain.

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
- **A sleeve floor can move weight the wrong way.** `min_positions` drops a tier
  that cannot fill it and hands the weight *up* the ladder — but when the tier
  that comes up short is the safest one, there is nothing above it to absorb the
  weight and it goes down instead. The run says so (`sleeve_no_safer_tier`), and
  the allocation is still valid, but a blanket floor applied to every tier can
  produce a riskier book than setting no floor at all. Size floors against the
  shelf you have, not uniformly.
- **The allocation log cannot price a buy.** A buy records the dollars it spent
  and nothing else: the build endpoint neither takes nor returns a share amount
  and the receipt carries no logs, so the shares bought are unknown until the
  position is next read. Those entries carry `basis: "unresolved"` and cannot
  contribute a per-share cost basis. Sells and withdrawals do carry a price —
  quoted where the plan knew it, derived from dollars ÷ shares otherwise — and
  say which in the same field. **There is no backfill**: an amount missing from
  the log stays missing.
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
