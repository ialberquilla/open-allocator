# 01-00 — 1Tx REST client

**Phase:** 1 — 1Tx client + read-only
**Depends on:** 00-02, 00-03
**Status:** done

## Goal
An `httpx` client that ports the 1tx-skill `client.ts` surface 1:1 (verified endpoint map below).

## Scope / Deliverables
- `src/open_allocator/exec/client.py` — `OneTxClient(config)` with typed methods:
  - `list_instruments(**filters)` → `GET /instruments`
  - `metrics_bulk(instrument_ids, days)` → `GET /metrics/bulk`
  - `instrument_analysis(instrument_id)` → `GET /instruments/:id/analysis`
  - `analyze_portfolio(allocations)` → `POST /portfolios/analyze`
  - `compare_portfolios(before, after)` → `POST /portfolios/compare`
  - `simulate_portfolio(body)` → `POST /portfolios/simulate`
  - `build_buy(body)` → `POST /transactions/buy` → ordered `{to,data,value,chainId}[]`
  - `build_sell(body)` → `POST /transactions/sell`
  - `positions(body)` → `POST /positions`; `balances(address)` → `GET /transactions/balances/:address`
  - `account(owner_eoa)` → `GET /account?ownerEoa=`
- Auth header from `ONE_TX_API_KEY`; base `ONE_TX_API_URL`. Timeouts, bounded retries with backoff on 5xx/429.
- Responses parsed into 00-03 models where applicable; raw `{to,data,value,chainId}` preserved for exec.

## Tests
- `httpx.MockTransport` fixtures per endpoint: correct method, path, query/body, and parsing.
- `build_buy` preserves tx order and the raw calldata fields byte-for-byte.
- Retry/backoff on 429/5xx; gives up with a typed error after N attempts (no infinite loop).
- Auth header present on every request; missing creds surfaced from config (00-02), not here.
- `@pytest.mark.integration`: one live `list_instruments` smoke test, skipped without creds.

## Acceptance criteria
- [x] Every endpoint above is covered by a mocked unit test.
- [x] Transaction-builder output is usable unchanged by the executor (03-02).

## References
plan_allocator.md § Data & Execution Layer; 1tx-skill `src/client.ts`
