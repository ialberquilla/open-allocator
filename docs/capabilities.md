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

**Levered caps** — six knobs that read the leverage a synthetic levered row
hides from every weight cap above. All optional; absent = not enforced, and
`validate-mandate` compares an absent one as its permissive extreme.

| Knob | Kind | Reads |
| --- | --- | --- |
| `max_weight_levered` | ceiling | summed equity weight of levered legs; `0` admits none and narrows levered rows out before construction |
| `max_gross_leverage` | ceiling | each levered leg's leverage |
| `max_book_gross_exposure` | ceiling | `sum(weight × L)`, unlevered legs at 1 |
| `min_health_factor` | floor, must exceed 1 | each levered leg's HF at its L |
| `min_depeg_buffer_bps` | floor | `(HF − 1) × 10_000`, **cross-asset legs only** |
| `min_reward_liquidity_usd` | floor | the reward token's 24h volume |

A levered row is routed to these **instead of** `max_reward_dependence`, which
every loop fails by construction. An `AllocationLeg` carries the `leverage` it
is held at; a levered leg that names none is charged at its row's declared
`max_leverage`, never at 1. Without a published `liquidation_threshold` the HF is
computed from the LTV the ceiling implies — a lower bound, so the check errs
toward rejecting. An unreported `debt_asset` is budgeted as cross-asset, and
unmeasured reward liquidity **fails** the liquidity floor.

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

`build-allocation` attaches `metadata.cost_estimate` (gas, bridge fee, spread,
slippage, `net_apy_pct_year1`, `breakeven_days`, `verdict`) and
`metadata.warnings`, which name every cap that clamped, every policy exclusion,
and every empty sleeve.

Two of those describe the same swap and only one is charged. `spread_cost_usd`
is the **expected** cost of crossing, so it sits inside `total_expected_cost_usd`
and therefore inside `breakeven_days`, `net_apy_pct_year1` and the drift gate's
`payback_days`. `max_slippage_usd` is the **tolerance** the bundle will accept
before it reverts — reported, never counted. The first is what the trade costs;
the second is how bad it is allowed to get.

Spread is not a rounding term: measured across two trades it was $0.133 of the
$0.142 of realized execution cost. The default is 8.5 bps of traded notional and
is **one measurement, not a calibration** — pass
`CostParams(expected_spread_bps=...)` when you have a better one.

### A reward APY has to be priced before it counts

`apy_accounting` covers a reward APY only when it is both **reported** and
**priced at a traded quote**. Upstream feeds price a reward APY at the emission
schedule — what the programme says it pays — and on a thin reward token that is
not what the token sells for. The error is directional — it floats
reward-heavy rows to the top of a ranking rather than scattering them — and
leverage multiplies it.

So the two ways of missing it are reported separately:
`unknown_reward_weight_bps` is weight whose reward APY nobody reported, and
`unpriced_reward_weight_bps` is weight whose reward APY was reported on an
`emission` (or absent, or unrecognised) basis. `priced_blended_apy_pct` — base
plus the reward we are willing to count — goes `null` unless **both** coverages
are complete, exactly as `accruing_apy_pct` already does for base alone.
`advertised_blended_apy_pct` is untouched: what upstream claims is still
reported, because hiding it makes a row harder to check, not safer.

A **zero** reward APY is covered whatever basis came with it — there is nothing
to misprice. An absent basis is *not* a fourth basis: it reads as not-traded.
`list-vaults` publishes `reward_apy_basis` and `priced_reward_apy` per row, and
the second reads `Unknown` for every row on an emission basis or with no basis
reported.

### Leverage is modelled and capped, not yet executable

`core.levered` turns a snapshotted loopable pair into `f(L) = L·rs − (L−1)·rb`
plus a health factor and a depeg budget. **`f(L)` is linear in L**, so the only
number that decides whether a loop is worth doing is the per-turn gradient
`rs − rb`; leverage is a risk budget, not an edge. It is pure and offline: it
models a stored snapshot, never reads a chain and never picks L.

A `LeveredQuote` counts rewards only when the snapshot priced them at a traded
quote, by the rule above. `count_rewards=False` answers the separate and often
much shorter question of whether the loop carries with rewards switched off.
`kill_switch_reward_price_usd` is the reward price at which the gradient
reaches zero — a price trigger, not a judgement — and
`equity_cap_for_reward_liquidity` sizes equity against the reward token's exit
volume, because capacity on a reward-driven loop binds there and not on the
lending pool's cash.

`Vault` carries `is_levered` and `max_leverage`, and a levered row is *required*
to declare its ceiling; it may also carry `liquidation_threshold`, `debt_asset`
and `reward_liquidity_usd`. A synthetic instrument hides leverage from
`max_weight_per_instrument` — 30% of a book at L=8 is 240% of it in gross
exposure — so the declaration is what the levered caps above read. A levered
sleeve is a **mandate-level decision**: `validate-mandate` requires a rationale
entry for `caps.max_weight_levered`, at the value the derived policy ships,
whenever that policy admits any levered weight. See the limits below.

Gas in that block is priced from **live** chain state — the source chain's gas
price and an on-chain Chainlink ETH/USD feed — and `cost_estimate.gas_priced_live`
reports whether that succeeded. When it is `false` the estimate fell back to a
static per-chain-class constant, which is an order of magnitude only: a constant
is several times too high on an L2 at a low base fee and an order of magnitude
too low on mainnet at a normal one.

**Only chains whose gas token is ETH are priced live**, because that feed is the
only price source wired. A chain that pays in its own token — Monad in MON,
Polygon in POL, Avalanche in AVAX — is reported `gas_priced_live: false` and
uses the constant, rather than being converted at the price of ETH. Read the
flag before trusting a per-chain gas number, and add a price source rather than
assuming ETH: where the two tokens differ in price, so does the estimate. `core.costs.min_economic_leg_usd()` derives a
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
- **A build response cannot price a buy.** Neither 1Tx API takes or returns a
  share amount, and the receipt carries no logs, so a rebalance buy reads the
  settled position and logs the exact share delta. What happens before that
  delta is observable depends on `ONE_TX_TRANSACTION_API`:
  - `legacy` — no row is appended and the rebalance stays `in_progress`; a
    rerun retries the read without rebroadcasting.
  - `calldata` — the row is appended at once with the USDC the deposit spent
    and no shares (`basis: "unresolved"`), and the report says the cost basis
    is not yet observable. The leg is complete, so a rerun does not retry.

  Unresolved buy entries, including every calldata deposit made outside a
  rebalance, cannot contribute a per-share cost basis. Sells and withdrawals
  carry a price — quoted where the plan knew it, derived from dollars ÷ shares
  otherwise — and say which in the same field. **There is no backfill**: an amount missing from
  the log stays missing.
- **`--max-positions` truncates after tier allocation.** Combined with
  `sleeves`, the cut can collapse the book toward the top tier rather than
  thinning each tier against its budget. The `min_effective_positions` floor
  catches the result, so it fails closed rather than shipping quietly — but the
  interaction is a defect, not a design.
- **The allocator does not construct against the levered caps.** Construction
  never picks L and its caps waterfall has no levered bucket, so
  `build-allocation` can place a levered row above `max_weight_levered` or at an
  unchosen L; `check-policy` then rejects it. Only a zero ceiling and the
  liquidity floor narrow levered rows out *before* construction.
- **The health factor is modelled, not measured.** Every HF and depeg figure the
  policy reads comes from the row's declared parameters — and, with no published
  liquidation threshold, from a lower bound. The venue's simulated HF is what
  counts, and nothing compares the two yet.
- **A levered quote is rate arithmetic on a snapshot.** It does not model our
  own market impact — borrowing at size moves the rate that was just quoted —
  it has no liquidation mechanics beyond the health factor, and it has no view
  on whether anything will unwind the position when a floor breaks. On a
  cross-asset loop the health factor **is** the depeg budget, not only a
  liquidation guard, and at high leverage that budget is a few percent.
- **Unmeasured reward liquidity is not zero and is not a pass.**
  `equity_cap_for_reward_liquidity` returns `None` when the reward token's
  volume is unmeasured, and any cap reading that `None` has to fail closed.
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
