# 01-03 — Transparent risk-scoring model

**Phase:** 1 — 1Tx client + read-only
**Depends on:** 01-02, 00-03
**Status:** done

## Goal
The core intellectual product: an open, explainable, deterministic risk score per vault. No black box.

## Scope / Deliverables
- `src/open_allocator/core/scoring.py`:
  - `score_vault(vault, weights) -> VaultScore` — a weighted composite over the factors the audit
    (01-02) confirmed 1Tx populates: curator quality, TVL, APY stability (CV), reward dependence, market
    concentration, liquidity/withdrawal risk, collateral mix, LLTV, oracle risk, vault fee.
  - Each factor: normalize its raw input to [0,1], record raw+normalized+weight in `VaultScore.factors`;
    `Unknown` factors are excluded and the weight is redistributed (documented rule), never guessed.
  - Weights configurable (default set in code); `score_vault` is a pure, importable function pros can
    replace wholesale.
- No I/O in this module — it takes enriched `Vault`s and returns `VaultScore`s.

## Tests
- Determinism: same vault+weights → identical `VaultScore` (assert exact equality).
- Explainability: recomputing the composite from `factors` reproduces `score`.
- `Unknown` factor handling: score reflects redistribution, doesn't crash, doesn't invent a value.
- Monotonicity spot-checks: higher TVL / lower reward-dependence → not-worse score, all else equal.
- A replaced weight vector changes ranking as expected.

## Acceptance criteria
- [x] Every score is fully reconstructable from its `factors` (a test proves it).
- [x] Pure function, no network, no hidden state; deterministic.

## References
plan_allocator.md § Transparent Risk Model, § Core Architectural Rule
