# 1Tx Analysis Field Audit

This audit records the fields `open_allocator.core.metrics.enrich` consumes for
01-02. It is based on mocked and currently available client model fields until
live `ONE_TX_API_URL` and `ONE_TX_API_KEY` credentials are supplied.

The live audit path is the skipped integration test
`test_live_onetx_analysis_field_audit_skips_without_creds` in
`tests/test_metrics.py`. Run it with live credentials to print and record the
current field availability report.

## Fields Consumed

| Factor | 1Tx source fields | Current mocked availability | Missing behavior |
| --- | --- | --- | --- |
| APY series | `GET /metrics/bulk` `metrics[].apy` | Present in mocked tests | Empty tuple |
| TVL series | `GET /metrics/bulk` `metrics[].tvlUsd` | Present in mocked tests | Empty tuple |
| APY stability | Derived coefficient of variation over APY series | Present when APY series exists | `Unknown` |
| TVL | `analysis.liquidity.tvlUsd`, falling back to latest metric TVL | Present in mocked tests | Existing vault TVL is retained |
| Reward dependence | `analysis.yield.rewardSharePct`, `analysis.yield.rewardShare`, or metric `apyReward / apy` | Present in mocked tests | `Unknown` |
| Liquidity | `analysis.liquidity.lowLiquidity`, falling back to liquidity TVL | Present in mocked tests | `Unknown` |
| Curator | `analysis.curator` | Unknown in mocked tests | `Unknown` |
| Oracle | `analysis.oracle` | Unknown in mocked tests | `Unknown` |
| Fee | `analysis.fee` | Unknown in mocked tests | `Unknown` |
| Market concentration | `analysis.marketConcentration` | Unknown in mocked tests | `Unknown` |
| Collateral mix | `analysis.collateralMix` | Unknown in mocked tests | `Unknown` |

## Notes

No scoring model is defined here. The 01-03 scoring factor list should be
finalized after running the integration audit against live 1Tx credentials and
updating this document with observed present versus `Unknown` counts.
