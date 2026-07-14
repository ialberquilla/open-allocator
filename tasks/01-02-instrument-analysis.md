# 01-02 — Instrument analysis & metrics + risk-factor audit

**Phase:** 1 — 1Tx client + read-only
**Depends on:** 01-00
**Status:** done

## Goal
Fetch per-instrument analysis + historical metrics, and **audit which risk fields 1Tx actually
populates today** so the scoring model (01-03) is built on real data, not assumptions.

## Scope / Deliverables
- `src/open_allocator/core/metrics.py`:
  - `enrich(client, vaults, days)`: attach `GET /metrics/bulk` history (APY/TVL series) + `GET
    /instruments/:id/analysis` fields to each `Vault`.
  - Derived features usable by scoring: APY stability (CV over `days`), TVL, reward dependence, etc. —
    each marked `Unknown` when the source field is absent.
- **Audit deliverable** (`03-04`-style spike, run once against the live API): a short doc
  `docs/onetx-analysis-fields.md` listing which analysis/metric fields are present vs `Unknown`, which
  freezes the 01-03 factor list. Marked `@pytest.mark.integration`.

## Tests
- Metrics parsed into series; APY-stability (CV) computed correctly on a known series.
- Absent analysis fields → `Unknown` on the `Vault`, never a default number.
- Enrichment is pure w.r.t. a fixed API response (deterministic).
- Integration test (skipped without creds) prints the field-availability report used by the audit doc.

## Acceptance criteria
- [x] `Vault`s carry enough (or explicit `Unknown`) to feed every 01-03 factor.
- [x] The field-availability audit exists and is referenced by 01-03.

## References
plan_allocator.md § Prerequisites to Run (risk-factor audit), § Transparent Risk Model
