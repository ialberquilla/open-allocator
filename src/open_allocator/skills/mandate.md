# Mandate Skill

Use this stage to turn a sentence of intent into a `mandate.yaml` and a derived
policy, with a reason attached to every knob that moved. This is the one stage
where the agent authors configuration rather than reading it — so it is also the
stage with the hardest boundary: **the agent writes files and nothing else.**
Deterministic Python validates what it wrote, and a separate stage executes.
Never sign, never broadcast, never edit `policy.yaml` itself.

The mandate is what makes the whole allocator legible in thirty seconds: a
sentence in, a governed allocation out, with a rationale per knob. The rationale
is the product, not documentation.

**Read [`mandate.yaml`](../../../mandate.yaml) at the repository root before
starting.** It is a hand-derived worked example of exactly what this skill must
produce, and its rationale entries are written to the standard yours must meet.

## The One Rule That Generates the Rest

**Do not write a number you have not measured.** Every failure this skill exists
to prevent is the same failure: a plausible value chosen without looking at the
shelf. The defaults in the library, the examples in the design spec, and the
numbers in `mandate.yaml` are all *dated observations*, not constants — the
shelf churns daily. Re-derive, every time.

## Runnable Workflow

1. **Read the mandate text.** Identify which phrase drives which knob before
   touching the CLI. "different buckets" is a strategy choice; "enough
   diversification" is a floor on measured independence; "good yield" is usually
   a constraint on *what kind* of yield, not a target number.
2. **Look at the actual shelf.** `open-allocator list-vaults --enrich`, then
   `open-allocator score-vault --instrument-id <id>` across the candidates — see
   [discover.md](discover.md) and [score.md](score.md). **The distribution that
   matters is the one the policy admits, not the raw discovery set**: run
   `open-allocator build-allocation --policy policy.yaml` once against the
   baseline and read its metadata for the candidate and `excluded` context.
   `screen` filters on explicit thresholds and does not read a policy file, so it
   answers a different question.
3. **Write down the distribution before choosing any cut.** How many candidates,
   what the scores actually span, and where the gaps are. This is the evidence
   every band decision below rests on, and it belongs in the rationale.
4. **Derive the tiers** — see [Cut Bands Where the Scores Sit](#cut-bands-where-the-scores-sit).
5. **Derive the floors** — see [Floors Rise as Score Falls](#floors-rise-as-score-falls).
   Check each one against the live shelf before emitting it.
6. **Derive the policy** as a copy of the baseline with knobs tightened — see
   [A Derived Policy May Only Narrow](#a-derived-policy-may-only-narrow).
   Copy every knob you are not changing; do not omit it.
7. **Build the allocation the mandate produces** and check it against its own
   derived policy: `build-allocation --strategy sleeves ...`, then
   `check-policy --allocation <allocation.json> --policy policy-derived.yaml`.
   **A mandate whose own output its own policy rejects is not a mandate.**
8. **Hash and bind.** Write the derived policy's `sha256:` into the mandate's
   `policy_hash`, with `policy_path` relative to the mandate.
9. **Validate:** `open-allocator validate-mandate --mandate mandate.yaml
   --baseline policy.yaml`. Fix and re-derive until all four checks pass.

## Cut Bands Where the Scores Sit

The library's default tier ladder (`safe` 0.60–1.01, `med` 0.30–0.60, `risky`
0.00–0.30) is a starting shape, not a derivation. Measured on the 2026-08-11
shelf — 49 discovered, 41 policy candidates — **the bottom band held zero
names**, because `policy.caps` already screens out everything it was meant to
hold. A mandate deriving `risky: {weight: 0.20}` on that shelf allocates nothing
to it: the "different buckets" sentence the whole product hangs on renders as
two buckets, and the allocation carries a `sleeve_empty` warning by
construction.

**So place cuts in the gaps in the observed distribution.** A cut inside a dense
cluster is one day of score movement away from reclassifying names; a cut in a
wide gap is stable. On that shelf the distribution was bimodal — 16 names in
0.31–0.52, a 0.123-wide gap, then 25 names in 0.64–0.98 — so a cut at 0.58 was a
*discovery*, and any value from 0.53 to 0.64 gave the same split.

**Say which cuts are evidence and which are judgement, and do not let one borrow
the other's confidence.** The same derivation put its core/yield boundary at
0.85, inside an upper cluster whose widest gap was 0.031. That is a judgement
about how selective "core" should be, and its rationale entry says so in its own
words rather than presenting both cuts as equally measured.

Name the tiers for what they are. `Core | Yield | Frontier` matches the vocabulary
the 1Tx API already uses and reads as the risk ladder it is.

## Floors Rise as Score Falls

`min_positions` per tier inverts the intuition, and getting it backwards
produces a **riskier** book than emitting no floor at all.

A floor exists so that one instrument going to zero cannot gut a band, and that
risk is concentrated in the *lowest* band. But when the band that fails its floor
is the **safest** one, there is nothing above it to absorb the released weight —
so the weight travels **down** the ladder. Measured on the 2026-08-11 shelf: a
core floor of 14 against core's 13 names lifted blended APY from 4.478% to
4.716%. **Yield up because risk went up, from a knob whose entire purpose is to
reduce risk.**

Therefore:

- Floors **rise as score falls** (`core 5 · yield 6 · frontier 12` on that shelf).
- Every floor must be checked against the count of names actually in its band,
  with margin for churn — a floor that only just clears today trips tomorrow.
- **Never emit a floor you have not run.** Build the allocation both with and
  without it and compare blended APY and the sleeve warnings. If the floor moves
  weight downward, it is doing the opposite of what it was asked to do.

`sleeve_no_safer_tier` reports this when it happens, but a mandate that emits
floors blindly is relying on a warning to catch a decision it should have
measured.

## A Derived Policy May Only Narrow

`validate-mandate`'s check 4 rejects a derived policy that loosens **anything**
against its baseline. This is the entire safety argument for letting a model
write policy, so write to it deliberately rather than discovering it at
validation time.

"Tighter" is five directions, not one:

| Kind | Tighter means | Knobs |
| --- | --- | --- |
| Ceilings | **lower** | every `caps.max_weight_per_*`, `caps.max_reward_dependence`, `gates.max_deploy_per_cycle_usd` |
| Floors | **higher** | `caps.min_effective_positions`, `caps.min_instrument_tvl_usd` |
| Allowlists | a **subset** | every `allowed.*` list |
| Flags | the restricting value, **named per flag** | `allowed.stablecoin_only` and `gates.new_instrument_needs_approval` tighten on `true`; `gates.autonomous_rebalance` tightens on `false` |
| Fixed | **not an axis at all** | `version`, `wallet.mode`, `wallet.signer` — changing the signer is not a tightening, it is changing the subject |

**`null` is never neutral.** Every nullable knob here is *permissive* when
absent: an unset sector cap is 1.00, an unset `min_effective_positions` is
unenforced, an unset allowlist is the whole universe. Dropping a knob the
baseline set is therefore a **loosening** and is rejected. Copy forward
everything you are not tightening.

Two floors deserve a live check rather than a confident guess:

- **`min_effective_positions` is a floor on measured independence, not a name
  count**, and the gap between the two is large. A derived book of 40 names
  behaved as 3.81 independent bets. The design spec proposed 4.0 for this
  mandate; that value **rejects the allocation the mandate produces**
  (`check-policy` actual 3.9828). Set it above the baseline with margin for the
  measurement to move, then prove it with `check-policy`.
- **A cap that barely binds should be described as barely binding.** Tightening
  `max_reward_dependence` from 0.50 to 0.40 cost one instrument and no yield on
  that shelf. That is cheap insurance against a shelf that turns reward-heavy —
  say so, rather than writing it up as though it were doing work.

## The Rationale Is the Product

One entry per knob that moved from the baseline or the library default. Each
entry is `{knob, value, because}`, and `because` has to carry the measurement,
not a restatement of the value.

- **Name the evidence.** Counts, ranges, gap widths, before/after numbers. "Cut
  at 0.58 because the distribution is bimodal with a 0.123-wide gap at 0.52–0.64"
  is a reason; "0.58 balances risk and yield" is not.
- **Separate evidence from judgement.** Split a knob into two entries when parts
  of it rest on different strength of evidence, rather than averaging them into
  one confident-sounding paragraph.
- **Record contradictions.** When a measured value contradicts what the spec, the
  defaults, or a previous mandate proposed, the entry says which value was tested
  and what it did. Three of the entries in the worked example exist because a
  proposed value failed on the live shelf.
- **State the limits of the knob.** `sleeve_drift_bps` is one absolute band
  across sleeves of very different sizes, so the same number constrains a small
  sleeve far more tightly than a large one. That is worth writing down as a
  limit, not hiding as a choice.
- **Date the observation.** The rationale describes a shelf on a day. Say which
  day, and how many candidates it had.

## Quality Bar

- `validate-mandate` returns `ok: true` with all four checks passing.
- 🔴 **Read the `ok` field, not the exit code.** All four checks *report*; the CLI
  still exits 0 on a rejection, same convention as `check-policy`.
- Every band cut, weight, floor and cap traces to a number observed on the live
  shelf during this derivation.
- The allocation the mandate produces passes `check-policy` against the mandate's
  own derived policy.
- Every non-default knob has a rationale entry, and every entry cites a
  measurement.
- The derived policy differs from the baseline only by tightening, and copies
  every knob it does not tighten.
- `policy_hash` matches the derived policy's bytes, and `policy_path` resolves
  relative to the mandate.

## Relevant CLI Commands

- `open-allocator list-vaults --enrich`
- `open-allocator score-vault --instrument-id <instrument_id>`
- `open-allocator build-allocation --amount <usd> --strategy sleeves --strategy-param tiers=<json> --policy policy-derived.yaml`
- `open-allocator simulate --allocation <allocation.json>`
- `open-allocator check-policy --allocation <allocation.json> --policy policy-derived.yaml`
- `open-allocator validate-mandate --mandate mandate.yaml --baseline policy.yaml`

## Produced Artifacts

- `mandate.yaml`, validating against
  [`mandate.schema.json`](../schemas/mandate.schema.json).
- A derived policy file, validating against `policy.schema.json` and tightening
  the baseline only.
- The `validate-mandate` result JSON, with all four checks visible.
- The score distribution the bands were cut from, with counts per band.

## Safety Gates

- **The agent writes files; it never signs anything.** The two-plane split is not
  advisory: propose in YAML, let deterministic Python validate, let a separate
  stage execute.
- Never edit the baseline `policy.yaml`. A derivation produces a *new* file; the
  baseline is what it is checked against.
- Never edit a derived policy by hand after hashing it. The mandate carries its
  bytes — an edit breaks the binding, which is the point. Regenerate instead.
- A failing check is not a reason to loosen the derived policy. Re-derive the
  knob that failed.
- Do not carry numbers forward from a previous mandate, this document, or the
  design spec. They are dated observations of a shelf that has since moved.

## Review Focus

- Band cuts placed against the observed score distribution, with the gaps named.
- Floors checked against band population, and against the weight-travels-down
  inversion.
- Evidence and judgement distinguished in the rationale rather than blended.
- Derived policy tightening in all five directions and loosening in none,
  including by omission.
- The mandate's own allocation passing its own policy.
- APY language descriptive, never predictive.
